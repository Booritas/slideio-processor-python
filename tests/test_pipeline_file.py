import pytest

from slideio_processor import ExportKind, ExportSpec, InvalidArgumentError, PipelineFile


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


def test_export_name_unsafe_message_has_no_value():
    with pytest.raises(InvalidArgumentError) as info:
        ExportSpec.csv("CON", "x")
    assert "CON" not in str(info.value)
