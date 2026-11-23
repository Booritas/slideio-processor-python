"""The exceptions slideio_processor raises: one class per C++ ErrorCode.

The native translator builds them with _from_native. A message comes from the library or
is text this package writes; it may carry a path or an argument value.
"""
import enum


class ErrorCode(enum.IntEnum):
    """The category of a ProcessorError; the values are the C++ ErrorCode values."""

    INVALID_ARGUMENT = 0
    UNSUPPORTED_DATA_TYPE = 1
    CHANNEL_ARITY = 2
    STORE_FORMAT = 3
    STAGE = 4
    TILE = 5
    CANCELLED = 6
    IO = 7
    INTERNAL = 8


class ProcessorError(Exception):
    """Base class of every error raised by slideio_processor.

    Attributes:
        code: the ErrorCode.
        stage_name: the failing stage, or None.
        operation_name: the failing operation, or None.
        cause: the text of the underlying failure, or None.
        tile: the failing tile as (x, y), or None.
    """

    code = ErrorCode.INTERNAL

    # Only the message reaches the constructor: OSError.__new__ reads extra positional
    # arguments as errno and strerror.
    def __init__(self, message=""):
        super().__init__(message)
        self.stage_name = None
        self.operation_name = None
        self.cause = None
        self.tile = None


class InvalidArgumentError(ProcessorError, ValueError):
    """A caller-supplied argument is invalid."""

    code = ErrorCode.INVALID_ARGUMENT


class UnsupportedDataTypeError(ProcessorError):
    """A pixel data type is not supported by an operation."""

    code = ErrorCode.UNSUPPORTED_DATA_TYPE


class ChannelArityError(ProcessorError):
    """A channel count does not match what an operation needs."""

    code = ErrorCode.CHANNEL_ARITY


class StoreFormatError(ProcessorError):
    """A store has an unreadable or incompatible format."""

    code = ErrorCode.STORE_FORMAT


class StageError(ProcessorError):
    """A pipeline stage failed."""

    code = ErrorCode.STAGE


class TileError(ProcessorError):
    """Processing of a tile failed."""

    code = ErrorCode.TILE


class CancelledError(ProcessorError):
    """The operation was cancelled."""

    code = ErrorCode.CANCELLED


class IoError(ProcessorError, OSError):
    """An input/output operation failed."""

    code = ErrorCode.IO


class InternalError(ProcessorError):
    """A broken invariant or an unclassified native failure."""

    code = ErrorCode.INTERNAL


_CLASSES = {cls.code: cls for cls in (
    InvalidArgumentError, UnsupportedDataTypeError, ChannelArityError, StoreFormatError,
    StageError, TileError, CancelledError, IoError, InternalError)}


def _from_native(code, message, stage, operation, cause, has_tile, tile_x, tile_y):
    """Builds the exception for a native error; empty strings read as None."""
    error = _CLASSES[ErrorCode(code)](message)
    error.stage_name = stage or None
    error.operation_name = operation or None
    error.cause = cause or None
    error.tile = (tile_x, tile_y) if has_tile else None
    return error
