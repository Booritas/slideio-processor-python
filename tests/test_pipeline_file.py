import json
from pathlib import Path

import pytest

from slideio_processor import (
    ExportKind,
    ExportSpec,
    InvalidArgumentError,
    IoError,
    Operation,
    Output,
    PipelineFile,
    PipelineSpec,
    SlideRun,
    StageDefinition,
    Workspace,
)


def test_round_trip(three_stage_json):
    f = PipelineFile.from_json(three_stage_json)
    assert f.stage_names == ["tissue", "nuclei", "prob"]
    assert f.layer_names == ["tissue", "nuclei"]
    assert f.raster_names == ["prob"]
    assert [e.kind for e in f.exports] == [
        ExportKind.CSV, ExportKind.GEOJSON, ExportKind.LABEL_MASK, ExportKind.RASTER_TIFF]
    assert PipelineFile.from_json(f.to_json()) == f


def test_load_accepts_pathlike(tmp_path, three_stage_json):
    path = tmp_path / "p.json"
    path.write_text(three_stage_json, encoding="utf-8")
    assert PipelineFile.load(path) == PipelineFile.from_json(three_stage_json)


def test_build_and_chain():
    assert PipelineFile().require("numpy").require("numpy").requires == ["numpy"]
    with pytest.raises(InvalidArgumentError):
        PipelineFile().add_export(ExportSpec.csv("a.csv", "nuclei"))


def test_export_spec_fields():
    spec = ExportSpec.csv("n.csv", "nuclei", ["area"])
    assert spec.measurements == ["area"]
    assert spec.layer == "nuclei"
    assert spec.raster is None
    assert ExportSpec.raster_tiff("p.tif", "prob").raster == "prob"
    with pytest.raises(InvalidArgumentError):
        ExportSpec.csv("bad/name.csv", "x")


def test_export_name_unsafe_refused():
    with pytest.raises(InvalidArgumentError):
        ExportSpec.csv("CON", "x")


def test_load_undecodable_path_message_has_repr(tmp_path):
    path = str(tmp_path / "secret_dir" / "pat\udce9ient.json")
    with pytest.raises(InvalidArgumentError) as info:
        PipelineFile.load(path)
    assert repr(path) in str(info.value)


def test_nul_text_message_has_repr():
    with pytest.raises(InvalidArgumentError) as info:
        ExportSpec.csv("a\0b.csv", "x")
    assert r"text contains a NUL character: 'a\x00b.csv'" in str(info.value)


@pytest.mark.parametrize("call", [
    lambda: ExportSpec.csv("a\0b.csv", "x"),
    lambda: ExportSpec.csv("a.csv", "x\0y"),
    lambda: ExportSpec.csv("a.csv", "x", ["area\0"]),
    lambda: ExportSpec.geojson("a.geojson", "x\udce9"),
    lambda: ExportSpec.label_mask("a.ome.tif", "x\0"),
    lambda: ExportSpec.raster_tiff("a\0.tif", "prob"),
    lambda: PipelineFile.from_json("{}\0"),
    lambda: PipelineFile.from_json("\udce9"),
    lambda: PipelineFile().require("numpy\0"),
    lambda: PipelineFile.load("p\0.json"),
])
def test_nul_or_undecodable_text_is_refused(call):
    with pytest.raises(InvalidArgumentError):
        call()


def _module_dir():
    return Path(__file__).parent / "data" / "opmodule"


def test_from_workspace(nuclei_workspace):
    f = PipelineFile.from_workspace(nuclei_workspace)
    assert f.stage_names == ["nuclei"]
    assert f.layer_names == ["nuclei"]
    assert f.raster_names == []
    assert f.requires == []
    assert f.exports == []


def test_from_workspace_refuses_other_types():
    with pytest.raises(TypeError, match="^PipelineFile: "):
        PipelineFile.from_workspace(object())


def test_from_workspace_closed_refused(nuclei_workspace):
    nuclei_workspace.close()
    with pytest.raises(IoError, match="closed"):
        PipelineFile.from_workspace(nuclei_workspace)


def test_from_workspace_requires_python_operation_modules(blob_slide, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(_module_dir()))
    import sample_ops

    class Scripted(Operation):
        __module__ = "__main__"
        name = "test.from_workspace_main_module"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            return tile.mean(axis=2, dtype="float32")

    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        ws.declare_raster("grey", "float32")
        ws.declare_raster("scripted", "float32")
        ws.declare_stage(StageDefinition("python", PipelineSpec().add(sample_ops.Grey), writes_raster="grey"))
        ws.declare_stage(StageDefinition("script", PipelineSpec().add(Scripted), writes_raster="scripted"))
        assert "operationOrigins" in json.loads(ws.stage("script").record)
        f = PipelineFile.from_workspace(ws)
    assert f.stage_names == ["python", "script"]
    assert f.raster_names == ["grey", "scripted"]
    assert f.requires == ["sample_ops"]


def test_from_workspace_runs_with_slide_run(nuclei_workspace, blob_slide, tmp_path):
    f = PipelineFile.from_workspace(nuclei_workspace)
    r = SlideRun(f, blob_slide, tmp_path / "fresh_ws", tmp_path / "fresh_out").run()
    assert [s.name for s in r.stages] == ["nuclei"]
    assert r.stages[0].ran
    assert r.stages[0].objects_written == 9
