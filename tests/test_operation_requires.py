import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from slideio_processor import InvalidArgumentError, PipelineFile, SlideRun
from support.pipelines import one_stage_pipeline

MODULE_DIR = Path(__file__).parent / "data" / "opmodule"
_EXPORT = {"kind": "rasterTiff", "file": "grey.ome.tif", "raster": "grey"}


def _pipeline_text(module):
    return one_stage_pipeline("sample.grey", {"version": "1"}, writes_raster="grey", exports=[_EXPORT],
                              requires=[module])


def test_requires_imports_module(blob_slide, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(MODULE_DIR))
    assert "sample_ops" not in sys.modules
    pipeline = PipelineFile.from_json(_pipeline_text("sample_ops"))
    SlideRun(pipeline, blob_slide, tmp_path / "ws", tmp_path / "out").run()
    assert (tmp_path / "out" / "grey.ome.tif").exists()
    store = tmp_path / "ws" / "workspace.store"
    connection = sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)
    try:
        (configuration,) = connection.execute("SELECT configuration FROM stages WHERE name = 'python'").fetchone()
    finally:
        connection.close()
    assert json.loads(configuration)["operationOrigins"]["sample.grey"].endswith(" 0.3")


def test_requires_missing_module(blob_slide, tmp_path):
    pipeline = PipelineFile.from_json(_pipeline_text("no_such_module_xyz"))
    run = SlideRun(pipeline, blob_slide, tmp_path / "ws", tmp_path / "out")
    with pytest.raises(InvalidArgumentError, match="no_such_module_xyz") as info:
        run.run()
    assert isinstance(info.value.__cause__, ImportError)
    assert not (tmp_path / "ws").exists()


def _cli(tmp_path, blob_slide, module):
    (tmp_path / "pipeline.json").write_text(_pipeline_text(module), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(MODULE_DIR))
    return subprocess.run(
        [sys.executable, "-m", "slideio_processor", "run", "--pipeline", str(tmp_path / "pipeline.json"),
         "--slide", str(blob_slide), "--workspace", str(tmp_path / "ws"), "--out", str(tmp_path / "out"),
         "--quiet"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)


def test_cli_runs_python_operation(blob_slide, tmp_path):
    done = _cli(tmp_path, blob_slide, "sample_ops")
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "out" / "grey.ome.tif").exists()


def test_cli_missing_module_exit_1(blob_slide, tmp_path):
    done = _cli(tmp_path, blob_slide, "no_such_module_xyz")
    assert done.returncode == 1
    assert "no_such_module_xyz" in done.stderr
