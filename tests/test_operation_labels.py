import csv
import math

import numpy
import pytest
from slideio_processor import Operation, Output, PipelineFile, SlideRun, StageError
from support.pipelines import one_stage_pipeline

# The fixture's discs have radius 40, drawn by PIL over an inclusive 81-pixel box: the
# pixel disc has diameter 81, so its area is pi * 40.5**2 (pi * 40**2 is 2.4% less).
_DISC_AREA = math.pi * 40.5**2


def _dark(tile):
    return (tile.mean(axis=2) < 128).astype(numpy.int32)


def _slide_run(slide, tmp_path, name, parameters=None):
    if parameters is None:
        parameters = {"version": "1"}
    text = one_stage_pipeline(name, parameters, writes_layer="dark",
                              exports=[{"kind": "csv", "file": "dark.csv", "layer": "dark"}])
    return SlideRun(PipelineFile.from_json(text), slide, tmp_path / "ws", tmp_path / "out")


def _rows(tmp_path):
    with open(tmp_path / "out" / "dark.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(line for line in f if not line.startswith("#")))


def _assert_three_whole_discs(result, tmp_path):
    (stage,) = result.stages
    assert stage.ran
    assert stage.objects_written == 3
    assert stage.truncated_objects == 0
    rows = _rows(tmp_path)
    assert len(rows) == 3
    for row in rows:
        assert abs(float(row["area"]) - _DISC_AREA) <= 0.02 * _DISC_AREA
    # The middle disc straddles the first seam: one whole object, not two halves.
    boxes = sorted((int(r["bbox_x"]), int(r["bbox_y"]), int(r["bbox_width"]), int(r["bbox_height"])) for r in rows)
    assert boxes == [(260, 160, 81, 81), (984, 160, 81, 81), (1660, 160, 81, 81)]


def test_labels_objects_whole(seam_slide, tmp_path):
    class Dark(Operation):
        name = "test.labels_objects_whole"
        version = "1"
        output = Output.LABELS
        halo = 96

        def apply(self, tile, context):
            return _dark(tile)

    _assert_three_whole_discs(_slide_run(seam_slide, tmp_path, Dark.name).run(), tmp_path)


def test_labels_small_halo_truncates(seam_slide, tmp_path):
    class Dark(Operation):
        name = "test.labels_small_halo_truncates"
        version = "1"
        output = Output.LABELS
        halo = 8

        def apply(self, tile, context):
            return _dark(tile)

    (stage,) = _slide_run(seam_slide, tmp_path, Dark.name).run().stages
    assert stage.truncated_objects >= 1
    assert stage.objects_written < 3


def test_labels_must_be_int32(seam_slide, tmp_path):
    class Dark(Operation):
        name = "test.labels_must_be_int32"
        version = "1"
        output = Output.LABELS
        halo = 96

        def apply(self, tile, context):
            return _dark(tile).astype(numpy.int64)

    with pytest.raises(StageError) as info:
        _slide_run(seam_slide, tmp_path, Dark.name).run()
    message = str(info.value)
    assert info.value.operation_name == Dark.name
    assert Dark.name in message
    assert "int32" in message and "int64" in message


def test_labels_wrong_shape_fails(seam_slide, tmp_path):
    class Dark(Operation):
        name = "test.labels_wrong_shape_fails"
        version = "1"
        output = Output.LABELS
        halo = 96

        def apply(self, tile, context):
            return _dark(tile)[self.halo:-self.halo, self.halo:-self.halo]

    with pytest.raises(StageError) as info:
        _slide_run(seam_slide, tmp_path, Dark.name).run()
    message = str(info.value)
    assert Dark.name in message
    assert "shape" in message and ", 1)" in message


def test_labels_accepts_trailing_plane(seam_slide, tmp_path):
    class Dark(Operation):
        name = "test.labels_accepts_trailing_plane"
        version = "1"
        output = Output.LABELS
        halo = 96

        def apply(self, tile, context):
            return _dark(tile)[:, :, numpy.newaxis]

    _assert_three_whole_discs(_slide_run(seam_slide, tmp_path, Dark.name).run(), tmp_path)


def test_target_layer_parameter(seam_slide, tmp_path):
    class Dark(Operation):
        name = "test.target_layer_parameter"
        version = "1"
        output = Output.LABELS
        halo = 96

        def apply(self, tile, context):
            return _dark(tile)

    run = _slide_run(seam_slide, tmp_path, Dark.name, {"version": "1", "targetLayer": "dark"})
    _assert_three_whole_discs(run.run(), tmp_path)


def test_labels_apply_exception_chained(seam_slide, tmp_path):
    class Broken(Operation):
        name = "test.labels_apply_exception_chained"
        version = "1"
        output = Output.LABELS

        def apply(self, tile, context):
            raise ValueError("no labels")

    with pytest.raises(StageError) as info:
        _slide_run(seam_slide, tmp_path, Broken.name).run()
    assert info.value.operation_name == Broken.name
    assert isinstance(info.value.__cause__, ValueError)
    assert "no labels" in str(info.value.__cause__)
