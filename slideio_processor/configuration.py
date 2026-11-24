"""Configuration and configure_logging: the library's settings and where its log goes."""
import dataclasses
import enum
import os
from typing import Optional, Tuple, Union

from ._loader import load_native
from .errors import InvalidArgumentError
from .pipeline_file import _path_string

_native = load_native()

_INT32 = (-(2**31), 2**31 - 1)
_INT64 = (-(2**63), 2**63 - 1)


class LogLevel(enum.IntEnum):
    """Severity of a library log message, as the C++ LogLevel. OFF is a threshold only."""

    TRACE = 0
    DEBUG = 1
    INFO = 2
    WARNING = 3
    ERROR = 4
    OFF = 5


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_path(value):
    return isinstance(value, (str, os.PathLike))


# field name -> (type check, what the field must be)
_CHECKS = {
    "memory_ceiling_bytes": (_is_int, "an int"),
    "worker_count": (_is_int, "an int"),
    "tile_size": (lambda v: isinstance(v, tuple) and len(v) == 2 and all(_is_int(x) for x in v),
                  "a (width, height) tuple of two ints"),
    "tile_size_from_slide": (lambda v: isinstance(v, bool), "a bool"),
    "log_file": (_is_path, "a str or os.PathLike"),
    "log_level": (_is_int, "a LogLevel"),
    "object_batch_size": (_is_int, "an int"),
    "scratch_directory": (_is_path, "a str or os.PathLike"),
}

_RANGES = {
    "memory_ceiling_bytes": _INT64,
    "worker_count": _INT32,
    "log_level": _INT32,
    "object_batch_size": _INT64,
}


@dataclasses.dataclass(frozen=True)
class Configuration:
    """The library's settings. A field left None keeps the library default.

    Types are checked here and raise TypeError naming the field. Value ranges are the
    library's: _native() (and so every call given this configuration) raises
    InvalidArgumentError naming each offending setting.

    tile_size is (width, height). log_file and log_level take effect only through
    configure_logging(); a run or workspace given them does not reconfigure logging.
    """

    memory_ceiling_bytes: Optional[int] = None
    worker_count: Optional[int] = None
    tile_size: Optional[Tuple[int, int]] = None
    tile_size_from_slide: Optional[bool] = None
    log_file: Optional[Union[str, os.PathLike]] = None
    log_level: Optional[LogLevel] = None
    object_batch_size: Optional[int] = None
    scratch_directory: Optional[Union[str, os.PathLike]] = None

    def __post_init__(self):
        for name, (check, what) in _CHECKS.items():
            value = getattr(self, name)
            if value is not None and not check(value):
                raise TypeError(f"Configuration: {name} must be {what} or None, got {value!r}")

    def _native(self):
        """The library Configuration: the defaults with every field that is not None set."""
        fields = {}
        for name in _CHECKS:
            value = getattr(self, name)
            if value is None:
                continue
            if name in _RANGES:
                low, high = _RANGES[name]
                if not low <= value <= high:
                    raise InvalidArgumentError(f"Configuration: {name} {value} is out of range")
                value = int(value)
            elif name == "tile_size":
                for side in value:
                    if not _INT32[0] <= side <= _INT32[1]:
                        raise InvalidArgumentError(f"Configuration: tile_size {value!r} is out of range")
                value = (int(value[0]), int(value[1]))
            elif name in ("log_file", "scratch_directory"):
                try:
                    value = _path_string(value)
                except (TypeError, InvalidArgumentError) as error:
                    raise type(error)(f"Configuration: {name}: {error}") from None
            fields[name] = value
        return _native._build_configuration(fields)


def configure_logging(level: LogLevel, *, file=None, python: bool = False) -> None:
    """Chooses where the library's log messages go, process-wide, and the threshold.

    With file (a str or os.PathLike), messages are appended to that file. With
    python=True, they go to the standard logging logger "slideio_processor" (TRACE as
    level 5, then DEBUG, INFO, WARNING, ERROR); that logger's own level and handlers
    apply as well. With neither, they go to stderr. file and python=True together raise
    InvalidArgumentError. An unopenable file raises IoError and leaves logging as it was.

    Messages may come from worker threads. Once the interpreter begins to exit, the
    Python route drops them.
    """
    if not _is_int(level):
        raise TypeError(f"configure_logging: the level must be a LogLevel, got {level!r}")
    try:
        level = LogLevel(level)
    except ValueError:
        raise InvalidArgumentError(f"configure_logging: {level!r} is not a LogLevel") from None
    if not isinstance(python, bool):
        raise TypeError(f"configure_logging: python must be a bool, got {python!r}")
    if file is not None and python:
        raise InvalidArgumentError("configure_logging: a log file and python=True cannot be combined")
    if python:
        _native._configure_logging_python(int(level))
        return
    if file is not None:
        if not _is_path(file):
            raise TypeError(f"configure_logging: the file must be a str or os.PathLike, got {file!r}")
        try:
            path = _path_string(file)
        except (TypeError, InvalidArgumentError) as error:
            raise type(error)(f"configure_logging: {error}") from None
        if path == "":
            raise InvalidArgumentError("configure_logging: the log file path is empty")
        _native._configure_logging_file(Configuration(log_file=path, log_level=level)._native())
    else:
        _native._configure_logging_file(Configuration(log_level=level)._native())
