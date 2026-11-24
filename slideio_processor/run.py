"""SlideRun: runs a pipeline file against one slide scene and writes its exports."""
import dataclasses
import importlib
import threading
from typing import Callable, Optional, Tuple

from ._loader import load_native
from ._runner import operation_causes, run_interruptibly
from .configuration import Configuration
from .errors import InvalidArgumentError
from .pipeline_file import _path_string, _text

_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1

_native = load_native()


class Cancellation:
    """A cancellation flag for a run. cancel() may be called from any thread; it is
    permanent and idempotent."""

    __slots__ = ("_source",)

    def __init__(self):
        self._source = _native._CancellationSource()

    def cancel(self):
        """Requests cancellation of every run given this object."""
        self._source.cancel()

    @property
    def cancelled(self):
        return self._source.is_cancelled()

    def __repr__(self):
        return f"Cancellation(cancelled={self.cancelled})"


@dataclasses.dataclass(frozen=True)
class StageResult:
    """One stage of a run: whether it ran (False when it was complete and current) and
    what it wrote. truncated_objects counts dropped tile views, not distinct objects."""

    name: str
    ran: bool
    objects_written: int
    truncated_objects: int


@dataclasses.dataclass(frozen=True)
class ExportResult:
    """One export of a run: its plain file name and the bytes written."""

    file: str
    bytes: int


@dataclasses.dataclass(frozen=True)
class SlideRunResult:
    """What a run did, per stage and per export, in pipeline-file order."""

    stages: Tuple[StageResult, ...]
    exports: Tuple[ExportResult, ...]


class SlideRun:
    """Runs a pipeline file against one slide scene.

    The workspace directory is created on the first run and reused by later ones, which
    run only the stages that are not current. The exports go to the out directory. The
    paths are str or os.PathLike. slide_id is the identifier the CSV and GeoJSON exports
    carry; the default "" means they carry none. configuration is a Configuration; None
    means the library defaults.
    """

    __slots__ = ("_run", "_lock", "_requires")

    def __init__(self, pipeline, slide, workspace, out, *, scene=0, slide_id="", configuration=None):
        # Outside int32, pybind11's conversion would fail with a TypeError that
        # repeats every argument; refuse here with a clean message.
        # Negative scenes inside int32 are refused by the library, with its own message.
        if isinstance(scene, bool) or not isinstance(scene, int):
            raise InvalidArgumentError(f"SlideRun: the scene must be an int, got {scene!r}")
        if not _INT32_MIN <= scene <= _INT32_MAX:
            raise InvalidArgumentError(f"SlideRun: the scene {scene} is out of range")
        if configuration is None:
            configuration = Configuration()
        elif not isinstance(configuration, Configuration):
            raise TypeError(f"SlideRun: the configuration must be a Configuration, got {configuration!r}")
        self._run = _native._SlideRun(pipeline._file, _path_string(slide), scene, _path_string(workspace),
                                      _path_string(out), _text(slide_id, "the slide id"), configuration._native())
        self._lock = threading.Lock()
        self._requires = tuple(pipeline.requires)

    def run(self, *, progress: Optional[Callable[[str, float, int, int], None]] = None,
            cancellation: Optional[Cancellation] = None) -> SlideRunResult:
        """Runs the file: validates, opens or creates the workspace, runs what is not
        current and writes the exports.

        progress(name, fraction, completed, total) is called from worker threads. name is
        a stage name or the name of a library step: "export", "raster export" or
        "seam merge". When progress raises, the run is cancelled and its exception is
        raised, with the CancelledError as its __context__. Ctrl-C cancels the run and
        raises KeyboardInterrupt once the run has stopped. A cancelled run raises
        CancelledError and leaves no partial file.

        When a Python operation's __init__ or apply raises, the run raises a StageError
        naming the operation, with that exception as its __cause__.

        The pipeline file's `requires` modules are imported first, on the calling thread. A
        module whose import raises (ImportError or anything else) gives an
        InvalidArgumentError naming it, with the original exception as its __cause__,
        before the workspace is touched.

        One object runs once at a time: run() while another run() of the same object is
        in progress raises InvalidArgumentError.
        """
        if not self._lock.acquire(blocking=False):
            raise InvalidArgumentError("SlideRun: a run of this object is already in progress")
        try:
            for module in self._requires:
                try:
                    importlib.import_module(module)
                except Exception as error:
                    raise InvalidArgumentError(
                        f"SlideRun: the required module {module!r} cannot be imported: "
                        f"{type(error).__name__}: {error}") from error
            if cancellation is None:
                cancellation = Cancellation()
            native = self._run
            with operation_causes():
                stages, exports = run_interruptibly(lambda: native.run(progress, cancellation._source), cancellation)
            return SlideRunResult(tuple(StageResult(*s) for s in stages), tuple(ExportResult(*e) for e in exports))
        finally:
            self._lock.release()
