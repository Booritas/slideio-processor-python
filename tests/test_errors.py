import pytest

import slideio_processor
from slideio_processor import (
    CancelledError,
    ChannelArityError,
    ErrorCode,
    InternalError,
    InvalidArgumentError,
    IoError,
    PipelineFile,
    ProcessorError,
    StageError,
    StoreFormatError,
    TileError,
    UnsupportedDataTypeError,
)
from slideio_processor.errors import _from_native

_CLASSES = {
    ErrorCode.INVALID_ARGUMENT: InvalidArgumentError,
    ErrorCode.UNSUPPORTED_DATA_TYPE: UnsupportedDataTypeError,
    ErrorCode.CHANNEL_ARITY: ChannelArityError,
    ErrorCode.STORE_FORMAT: StoreFormatError,
    ErrorCode.STAGE: StageError,
    ErrorCode.TILE: TileError,
    ErrorCode.CANCELLED: CancelledError,
    ErrorCode.IO: IoError,
    ErrorCode.INTERNAL: InternalError,
}


def test_hierarchy():
    assert len(_CLASSES) == len(ErrorCode)
    for cls in _CLASSES.values():
        assert issubclass(cls, ProcessorError)
    assert issubclass(InvalidArgumentError, ValueError)
    assert issubclass(IoError, OSError)


def test_from_native_maps_every_code():
    for code in ErrorCode:
        e = _from_native(int(code), "m", "", "", "", False, 0, 0)
        assert isinstance(e, _CLASSES[code])
        assert e.code == code
        assert str(e) == "m"
        assert e.stage_name is None
        assert e.operation_name is None
        assert e.cause is None
        assert e.tile is None

        e = _from_native(int(code), "m", "s", "o", "c", True, 3, 4)
        assert isinstance(e, _CLASSES[code])
        assert (e.stage_name, e.operation_name, e.cause, e.tile) == ("s", "o", "c", (3, 4))


def test_invalid_json_raises_invalid_argument():
    with pytest.raises(InvalidArgumentError) as info:
        PipelineFile.from_json("{")
    assert info.value.code == ErrorCode.INVALID_ARGUMENT


def test_missing_file_raises_io_error_without_path(tmp_path):
    with pytest.raises(IoError) as info:
        PipelineFile.load(tmp_path / "missing.json")
    assert str(tmp_path) not in str(info.value)
    assert "missing.json" not in str(info.value)


def test_slideio_exceptions_are_not_translated(tmp_path):
    # The translator is local to this module: after import slideio_processor, SlideIO's
    # own errors still reach the caller as RuntimeError.
    import slideio

    with pytest.raises(RuntimeError) as info:
        slideio.open_slide(str(tmp_path / "missing.svs"), "SVS")
    assert not isinstance(info.value, ProcessorError)


def test_unknown_native_exception_is_internal_without_text():
    native = slideio_processor._native
    if not hasattr(native, "_raise_std_exception_for_testing"):
        pytest.skip("the native test helpers are not built in")
    with pytest.raises(InternalError) as info:
        native._raise_std_exception_for_testing("secret/path")
    assert str(info.value) == "an unexpected native error occurred"
