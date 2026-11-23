"""SlideRun: runs a pipeline file against one slide scene and writes its exports."""
import dataclasses
import threading
from typing import Callable, Optional, Tuple

from ._loader import load_native
from .pipeline_file import _path_string

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
    carry; the default "" means they carry none.
    """

    __slots__ = ("_run",)

    def __init__(self, pipeline, slide, workspace, out, *, scene=0, slide_id=""):
        self._run = _native._SlideRun(pipeline._file, _path_string(slide), scene, _path_string(workspace),
                                      _path_string(out), slide_id)

    def run(self, *, progress: Optional[Callable[[str, float, int, int], None]] = None,
            cancellation: Optional[Cancellation] = None) -> SlideRunResult:
        """Runs the file: validates, opens or creates the workspace, runs what is not
        current and writes the exports.

        progress(name, fraction, completed, total) is called from worker threads. When it
        raises, the run is cancelled and its exception is raised, with the CancelledError
        as its __context__. Ctrl-C cancels the run and raises KeyboardInterrupt once the
        run has stopped. A cancelled run raises CancelledError and leaves no partial file.
        """
        if cancellation is None:
            cancellation = Cancellation()
        outcome = {}
        done = threading.Lock()
        done.acquire()

        def target():
            try:
                outcome["result"] = self._run.run(progress, cancellation._source)
            except BaseException as error:  # noqa: B036  (handed to the calling thread)
                outcome["error"] = error
            finally:
                done.release()

        # The run happens on its own thread so that this one stays in an interruptible
        # wait: Ctrl-C reaches Python here and cancels the run. The wait is on a plain
        # lock, not Thread.join(timeout): when Ctrl-C interrupts that join, CPython before
        # 3.13 marks the still-running thread as stopped, and a later join() returns at once.
        t = threading.Thread(target=target, name="slideio-processor-run", daemon=True)
        t.start()
        try:
            while not done.acquire(timeout=0.1):
                pass
        except KeyboardInterrupt:
            cancellation.cancel()
            t.join()
            raise KeyboardInterrupt from outcome.get("error")
        t.join()
        if "error" in outcome:
            error = outcome.pop("error")
            # Raising here chains implicitly to any exception the caller is handling and
            # would replace the context the run set (the CancelledError); put it back.
            context = error.__context__
            try:
                raise error
            finally:
                error.__context__ = context
        stages, exports = outcome["result"]
        return SlideRunResult(tuple(StageResult(*s) for s in stages), tuple(ExportResult(*e) for e in exports))
