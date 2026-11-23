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
