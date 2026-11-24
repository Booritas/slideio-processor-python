"""Python bindings for slideio-processor."""
from . import _build_info
from ._loader import load_native

_native = load_native()

from .configuration import Configuration, LogLevel, configure_logging  # noqa: E402
from .errors import (  # noqa: E402  (after the native module is loaded)
    CancelledError,
    ChannelArityError,
    ErrorCode,
    InternalError,
    InvalidArgumentError,
    IoError,
    ProcessorError,
    StageError,
    StoreFormatError,
    TileError,
    UnsupportedDataTypeError,
)
from .operation import Operation, Output, TileContext, registered_operations  # noqa: E402
from .pipeline_file import ExportKind, ExportSpec, PipelineFile  # noqa: E402
from .run import Cancellation, ExportResult, SlideRun, SlideRunResult, StageResult  # noqa: E402
from .workspace import Layer, LayerKind, Raster, Workspace  # noqa: E402

__version__ = _build_info.VERSION
SLIDEIO_VERSION = _build_info.SLIDEIO_VERSION


def library_version():
    """Returns the version of the slideio-processor C++ library, as "major.minor.patch"."""
    return _native.library_version()


__all__ = [
    "__version__", "SLIDEIO_VERSION", "library_version",
    "ErrorCode", "ProcessorError", "InvalidArgumentError", "UnsupportedDataTypeError",
    "ChannelArityError", "StoreFormatError", "StageError", "TileError", "CancelledError",
    "IoError", "InternalError",
    "ExportKind", "ExportSpec", "PipelineFile",
    "Cancellation", "ExportResult", "SlideRun", "SlideRunResult", "StageResult",
    "Operation", "Output", "TileContext", "registered_operations",
    "Configuration", "LogLevel", "configure_logging",
    "Workspace", "Layer", "LayerKind", "Raster",
]
