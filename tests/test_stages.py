"""PipelineSpec, StageDefinition and the workspace's stage registry."""
import json
import types

import numpy
import pytest
from slideio_processor import (
    InvalidArgumentError,
    IoError,
    LayerRasterContent,
    Operation,
    Output,
    PipelineSpec,
    RunBlocker,
    StageDefinition,
    StageInfo,
    StageStatus,
    Workspace,
)
from support.workspaces import nuclei_spec


def test_spec_json_round_trip():
    s = nuclei_spec()
    assert PipelineSpec.from_json(s.to_json()) == s
    parsed = json.loads(s.to_json())
    assert len(parsed["operations"]) == 3
    assert s.operations[1] == ("pixel.threshold_fixed", {"value": 128.0, "direction": "Below"})
    assert s != PipelineSpec()


def test_spec_options():
    s = PipelineSpec(channels=[0], analysis_level=0, roi=(0, 0, 300, 300),
                     polygon=[([(0, 0), (300, 0), (300, 300)], False)])
    parsed = json.loads(s.to_json())
    assert parsed["channels"] == [0]
    assert parsed["analysisLevel"] == 0
    assert parsed["roi"] == [0, 0, 300, 300]
    assert parsed["polygon"] == [{"hole": False, "points": [[0, 0], [300, 0], [300, 300]]}]
    assert PipelineSpec.from_json(s.to_json()) == s


def test_spec_polygon_numpy_points():
    points = numpy.array([[0, 0], [10, 0], [10, 10]], dtype=numpy.int32)
    s = PipelineSpec(polygon=[(points, False)])
    assert json.loads(s.to_json())["polygon"][0]["points"] == [[0, 0], [10, 0], [10, 10]]


def test_spec_bad_parameter_type():
    with pytest.raises(TypeError, match="value"):
        PipelineSpec().add("pixel.threshold_fixed", value=object())


def test_spec_bad_roi():
    with pytest.raises(InvalidArgumentError):
        PipelineSpec(roi=(0, 0, -1, 5))


def test_spec_bad_polygon():
    with pytest.raises(InvalidArgumentError):
        PipelineSpec(polygon=[([(0, 0), (1, 1)], False)])
    with pytest.raises(InvalidArgumentError):
        PipelineSpec(polygon=[([(0, 0), (1, 1), (2, 0)], "no")])


def test_spec_bad_options():
    with pytest.raises(InvalidArgumentError):
        PipelineSpec(analysis_level=-2)
    with pytest.raises(InvalidArgumentError):
        PipelineSpec(channels=[-1])
    with pytest.raises(TypeError):
        PipelineSpec(channels="some")


def test_spec_bad_json():
    with pytest.raises(InvalidArgumentError):
        PipelineSpec.from_json("{")


def test_spec_python_operation():
    class Grey(Operation):
        name = "test.grey"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            return tile.mean(axis=2, dtype=numpy.float32)

    s = PipelineSpec().add(Grey)
    name, parameters = s.operations[0]
    assert name == "test.grey"
    assert parameters["version"] == "1"


def test_definition_is_frozen():
    definition = StageDefinition("s", nuclei_spec(), writes_layers=["nuclei"], reads_rasters={"p": "prob"})
    assert definition.writes_layers == ("nuclei",)
    assert isinstance(definition.reads_rasters, types.MappingProxyType)
    with pytest.raises(TypeError):
        definition.reads_rasters["q"] = "x"


def test_definition_copies_its_spec():
    spec = nuclei_spec()
    definition = StageDefinition("s", spec)
    assert definition.pipeline is not spec
    assert definition.pipeline == spec
    spec.add("segment.connected_components")
    assert len(definition.pipeline.operations) == 3


def _declare_nuclei(ws):
    ws.declare_layer("nuclei")
    return ws.declare_stage(StageDefinition("nuclei", nuclei_spec(), writes_layers=("nuclei",)))


def test_declare_stage(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        info = _declare_nuclei(ws)
        assert isinstance(info, StageInfo)
        assert info.name == "nuclei"
        assert info.status is StageStatus.PENDING
        assert info.stale is False
        assert info.runnable is True
        assert info.blocker is RunBlocker.NONE
        assert info.blocker_name is None
        assert info.completed_at is None
        assert info.dependencies == ()
        assert info.unresolved_dependencies == ()
        assert json.loads(info.record)["stage"] == "nuclei"
        assert tuple(s.name for s in ws.stages) == ("nuclei",)
        assert ws.stage("nuclei") == info


def test_redeclare_identical_is_accepted(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        first = _declare_nuclei(ws)
        second = ws.declare_stage(StageDefinition("nuclei", nuclei_spec(), writes_layers=("nuclei",)))
        assert first == second


def test_unknown_layer_refused(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        with pytest.raises(InvalidArgumentError, match="missing"):
            ws.declare_stage(StageDefinition("s", nuclei_spec(), writes_layers=("missing",)))
        assert ws.stages == ()


def test_unknown_raster_refused(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        with pytest.raises(InvalidArgumentError, match="nothere"):
            ws.declare_stage(StageDefinition("s", nuclei_spec(), writes_raster="nothere"))


def test_unresolved_dependency_blocks(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        ws.declare_layer("nuclei")
        info = ws.declare_stage(StageDefinition("nuclei", nuclei_spec(), writes_layers=("nuclei",),
                                                depends_on=("later",)))
        assert info.unresolved_dependencies == ("later",)
        assert info.runnable is False
        assert info.blocker is RunBlocker.UNRESOLVED_DEPENDENCY
        assert info.blocker_name == "later"


def test_dependency_and_mark_stale(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        _declare_nuclei(ws)
        ws.declare_layer("cells")
        reader = ws.declare_stage(StageDefinition("cells", nuclei_spec(), reads_layers=("nuclei",),
                                                  writes_layers=("cells",)))
        assert reader.dependencies == ("nuclei",)
        assert reader.runnable is False
        assert reader.blocker is RunBlocker.DEPENDENCY_INCOMPLETE
        assert reader.blocker_name == "nuclei"
        ws.mark_stale("nuclei", downstream=True)
        assert ws.stage("nuclei").stale is False
        assert ws.stage("cells").stale is True
        ws.mark_stale("nuclei")
        assert ws.stage("nuclei").stale is True


def test_layer_as_raster_and_restriction(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        _declare_nuclei(ws)
        ws.declare_raster("prob", "float32")
        info = ws.declare_stage(StageDefinition(
            "prob", PipelineSpec().add("pixel.colour_convert", **{"from": "Rgb", "to": "Grey"}),
            writes_raster="prob", restrict_to_layer="nuclei",
            reads_layers_as_raster={"mask": ("nuclei", LayerRasterContent.MEMBERSHIP)}))
        assert info.dependencies == ("nuclei",)
        assert json.loads(info.record)["writesRaster"] == "prob"


def test_raster_stage(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        ws.declare_raster("prob", numpy.float32)
        spec = (PipelineSpec()
                .add("pixel.colour_convert", **{"from": "Rgb", "to": "Grey"})
                .add("pixel.gaussian_blur"))
        info = ws.declare_stage(StageDefinition("prob", spec, writes_raster="prob"))
        assert info.status is StageStatus.PENDING


def test_unknown_stage_refused(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        with pytest.raises(InvalidArgumentError, match="'x'"):
            ws.stage("x")
        with pytest.raises(InvalidArgumentError, match="'x'"):
            ws.mark_stale("x")


def test_stages_reopen_and_read_only(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        info = _declare_nuclei(ws)
    with Workspace.open_read_only(tmp_path / "ws") as reader:
        assert reader.stages == (info,)
        with pytest.raises(IoError):
            reader.declare_stage(StageDefinition("nuclei", nuclei_spec(), writes_layers=("nuclei",)))
        with pytest.raises(IoError):
            reader.mark_stale("nuclei")


def test_bad_definition_types(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        with pytest.raises(TypeError):
            ws.declare_stage("nuclei")
    with pytest.raises(TypeError):
        StageDefinition("s", "not a spec")
