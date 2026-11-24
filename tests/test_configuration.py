import pytest
from slideio_processor import Configuration, InvalidArgumentError, LogLevel, PipelineFile, SlideRun


def test_defaults_build():
    Configuration()._native()


def test_fields_reach_the_library():
    c = Configuration(worker_count=2, tile_size=(256, 512), object_batch_size=100)
    n = c._native()
    assert (n.worker_count(), n.tile_width(), n.tile_height(), n.object_batch_size()) == (2, 256, 512, 100)


def test_every_field_is_accepted(tmp_path):
    c = Configuration(memory_ceiling_bytes=2**33, worker_count=1, tile_size=(64, 32), tile_size_from_slide=False,
                      log_file=tmp_path / "log.txt", log_level=LogLevel.DEBUG, object_batch_size=7,
                      scratch_directory=str(tmp_path))
    n = c._native()
    assert n.memory_ceiling_bytes() == 2**33
    assert (n.tile_width(), n.tile_height()) == (64, 32)


def test_wrong_type_refused():
    with pytest.raises(TypeError, match="worker_count"):
        Configuration(worker_count=True)
    with pytest.raises(TypeError, match="tile_size"):
        Configuration(tile_size=(1, 2, 3))
    with pytest.raises(TypeError, match="tile_size"):
        Configuration(tile_size=(1, True))
    with pytest.raises(TypeError, match="tile_size_from_slide"):
        Configuration(tile_size_from_slide=1)
    with pytest.raises(TypeError, match="log_file"):
        Configuration(log_file=3)
    with pytest.raises(TypeError, match="log_level"):
        Configuration(log_level="DEBUG")
    with pytest.raises(TypeError, match="memory_ceiling_bytes"):
        Configuration(memory_ceiling_bytes=1.5)


def test_message_starts_with_class_name():
    with pytest.raises(TypeError, match="^Configuration: "):
        Configuration(object_batch_size="1")


def test_out_of_int_range_refused_before_native():
    with pytest.raises(InvalidArgumentError, match="^Configuration: .*worker_count"):
        Configuration(worker_count=2**40)._native()


def test_library_range_check():
    with pytest.raises(InvalidArgumentError, match="workerCount"):
        Configuration(worker_count=0)._native()


def test_frozen():
    c = Configuration()
    with pytest.raises(Exception):
        c.worker_count = 3


def test_slide_run_takes_configuration(blob_slide, tmp_path, three_stage_json):
    pipeline = PipelineFile.from_json(three_stage_json)
    default = SlideRun(pipeline, blob_slide, tmp_path / "ws1", tmp_path / "out1").run()
    configured = SlideRun(pipeline, blob_slide, tmp_path / "ws2", tmp_path / "out2",
                          configuration=Configuration(worker_count=1)).run()
    assert configured.stages == default.stages


def test_slide_run_refuses_a_non_configuration(blob_slide, tmp_path, three_stage_json):
    with pytest.raises(TypeError, match="configuration"):
        SlideRun(PipelineFile.from_json(three_stage_json), blob_slide, tmp_path / "ws", tmp_path / "out",
                 configuration={"worker_count": 1})
