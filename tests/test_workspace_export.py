"""Workspace.export: the four export kinds, slide ids, read-only workspaces, refusals and cancellation."""
import json
import os

import numpy
import pytest
import slideio
from slideio_processor import (
    Cancellation,
    CancelledError,
    ExportResult,
    ExportSpec,
    InvalidArgumentError,
    PipelineFile,
    PipelineSpec,
    SlideRun,
    StageDefinition,
    Workspace,
)


def _data_lines(path):
    """The CSV's lines that are not provenance comments ("#..."), the header included."""
    with open(path, encoding="utf-8") as f:
        return [line for line in f.read().splitlines() if not line.startswith("#")]


def test_csv_export(nuclei_workspace, tmp_path):
    results = nuclei_workspace.export(ExportSpec.csv("n.csv", "nuclei"), tmp_path / "out")
    assert len(results) == 1
    assert isinstance(results[0], ExportResult)
    assert results[0].file == "n.csv"
    assert results[0].bytes > 0
    path = tmp_path / "out" / "n.csv"
    assert os.path.getsize(path) == results[0].bytes
    assert len(_data_lines(path)) - 1 == 9


def test_matches_slide_run(nuclei_workspace, blob_slide, tmp_path):
    spec = ExportSpec.csv("n.csv", "nuclei")
    pipeline = PipelineFile.from_workspace(nuclei_workspace).add_export(spec)
    SlideRun(pipeline, blob_slide, tmp_path / "run_ws", tmp_path / "run_out").run()
    nuclei_workspace.export(spec, tmp_path / "ws_out")
    run_lines = _data_lines(tmp_path / "run_out" / "n.csv")
    assert len(run_lines) == 10
    assert _data_lines(tmp_path / "ws_out" / "n.csv") == run_lines


def test_each_kind(nuclei_workspace, tmp_path):
    ws = nuclei_workspace
    ws.declare_raster("blur", "float32")
    ws.declare_stage(StageDefinition(
        "blur",
        PipelineSpec().add("pixel.colour_convert", **{"from": "Rgb", "to": "Grey"}).add("pixel.gaussian_blur"),
        writes_raster="blur"))
    ws.run_stage("blur")
    out = tmp_path / "out"
    specs = [ExportSpec.geojson("n.geojson", "nuclei"),
             ExportSpec.label_mask("n.ome.tif", "nuclei"),
             ExportSpec.raster_tiff("blur.tif", "blur")]
    results = ws.export(specs, out)
    assert [r.file for r in results] == ["n.geojson", "n.ome.tif", "blur.tif"]
    for spec, result in zip(specs, results):
        path = out / spec.file
        assert path.is_file()
        assert os.path.getsize(path) == result.bytes
        assert not (out / (spec.file + ".partial")).exists()
    with open(out / "n.geojson", encoding="utf-8") as f:
        assert len(json.load(f)["features"]) == 9
    scene = slideio.open_slide(str(out / "n.ome.tif"), "OMETIFF").get_scene(0)
    assert tuple(scene.size) == (600, 600)
    assert scene.num_channels == 1
    pixels = numpy.asarray(scene.read_block())
    assert pixels.dtype == numpy.int32
    # A label mask holds each object's label value (rasteriseLabels). The built-in
    # operations attach no label to the objects they write, so every pixel is 0 here, in
    # a SlideRun's mask as in this one.
    assert not pixels.any()


def test_slide_id(nuclei_workspace, blob_slide, tmp_path):
    spec = ExportSpec.csv("n.csv", "nuclei")
    nuclei_workspace.export(spec, tmp_path / "with_id", slide_id="S-1")
    assert "S-1" in (tmp_path / "with_id" / "n.csv").read_text(encoding="utf-8")
    nuclei_workspace.export(spec, tmp_path / "default")
    assert str(blob_slide) not in (tmp_path / "default" / "n.csv").read_text(encoding="utf-8")


def test_read_only_export(nuclei_workspace, tmp_path):
    with Workspace.open_read_only(nuclei_workspace.path) as ro:
        (result,) = ro.export(ExportSpec.csv("n.csv", "nuclei"), str(tmp_path / "ro"))
    assert result.bytes > 0
    assert len(_data_lines(tmp_path / "ro" / "n.csv")) - 1 == 9


def test_unknown_layer_refused(nuclei_workspace, tmp_path):
    with pytest.raises(InvalidArgumentError, match="ghost"):
        nuclei_workspace.export(ExportSpec.csv("g.csv", "ghost"), tmp_path / "out")
    assert not (tmp_path / "out" / "g.csv").exists()


def test_export_cancelled_leaves_no_file(nuclei_workspace, tmp_path):
    c = Cancellation()
    c.cancel()
    out = tmp_path / "out"
    with pytest.raises(CancelledError, match="^Workspace: the csv export was cancelled$"):
        nuclei_workspace.export(ExportSpec.csv("n.csv", "nuclei"), out, cancellation=c)
    assert not (out / "n.csv").exists()
    assert not (out / "n.csv.partial").exists()


def test_export_progress_raises(nuclei_workspace, tmp_path):
    calls = []

    def progress(name, fraction, completed, total):
        calls.append(name)
        raise RuntimeError("stop")

    out = tmp_path / "out"
    with pytest.raises(RuntimeError, match="stop"):
        nuclei_workspace.export(ExportSpec.csv("n.csv", "nuclei"), out, progress=progress)
    assert calls
    assert not (out / "n.csv.partial").exists()


def test_bad_arguments_refused(nuclei_workspace, tmp_path):
    with pytest.raises(TypeError, match="^Workspace: "):
        nuclei_workspace.export("n.csv", tmp_path)
    with pytest.raises(TypeError, match="^Workspace: "):
        nuclei_workspace.export([ExportSpec.csv("n.csv", "nuclei"), 1], tmp_path)
    with pytest.raises(TypeError, match="^Workspace: "):
        nuclei_workspace.export(ExportSpec.csv("n.csv", "nuclei"), tmp_path, cancellation=object())
