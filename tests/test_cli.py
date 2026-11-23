import os
import re
import subprocess
import sys

import pytest

from slideio_processor.cli import main


def _args(tmp_path, pipeline, slide, ws="ws", out="out", *extra):
    return ["run", "--pipeline", str(pipeline), "--slide", str(slide),
            "--workspace", str(tmp_path / ws), "--out", str(tmp_path / out), *extra]


@pytest.fixture
def pipeline_path(tmp_path, three_stage_json):
    p = tmp_path / "three_stage.json"
    p.write_text(three_stage_json, encoding="utf-8")
    return p


def test_run_prints_summary_and_exits_0(tmp_path, pipeline_path, blob_slide, capsys):
    assert main(_args(tmp_path, pipeline_path, blob_slide, "ws", "out", "--quiet")) == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[:3] == ["stage tissue: ran, 9 objects", "stage nuclei: ran, 9 objects",
                         "stage prob: ran, 0 objects"]
    assert len(lines) == 7
    assert all(re.fullmatch(r"export \S+: \d+ bytes", line) for line in lines[3:])
    assert captured.err == ""


def test_second_run_reports_current(tmp_path, pipeline_path, blob_slide, capsys):
    args = _args(tmp_path, pipeline_path, blob_slide, "ws", "out", "--quiet")
    assert main(args) == 0
    capsys.readouterr()
    assert main(args) == 0
    assert "stage tissue: current, not run" in capsys.readouterr().out.splitlines()


def test_progress_on_stderr(tmp_path, pipeline_path, blob_slide, capsys):
    assert main(_args(tmp_path, pipeline_path, blob_slide)) == 0
    err = capsys.readouterr().err.splitlines()
    assert any(re.fullmatch(r"(tissue|nuclei|prob): \d+%", line) for line in err)
    assert all(re.fullmatch(r".+: \d+%", line) for line in err)


def test_cli_errors_name_no_path(tmp_path, pipeline_path, blob_slide, capsys):
    other = tmp_path / "other_secret.png"
    other.write_bytes(blob_slide.read_bytes())
    assert main(_args(tmp_path, pipeline_path, blob_slide, "bound", "o1", "--quiet")) == 0
    capsys.readouterr()
    cases = [
        _args(tmp_path, tmp_path / "missing_pipeline.json", blob_slide, "w1", "o2", "--quiet"),
        _args(tmp_path, pipeline_path, tmp_path / "missing_slide.png", "w2", "o3", "--quiet"),
        _args(tmp_path, pipeline_path, other, "bound", "o4", "--quiet"),
    ]
    for argv in cases:
        assert main(argv) == 1
        captured = capsys.readouterr()
        assert captured.err.startswith("error: ")
        assert re.match(r"error: \w+Error: ", captured.err)
        for text in (captured.out, captured.err):
            assert str(tmp_path) not in text
            for arg in argv[1:]:
                if os.path.isabs(arg):
                    assert os.path.basename(arg) not in text


def test_usage_error_exits_2(capsys):
    with pytest.raises(SystemExit) as info:
        main(["run"])
    assert info.value.code == 2
    assert capsys.readouterr().err == "usage error: run 'slideio-processor run --help'\n"


def test_usage_error_names_no_path(tmp_path, pipeline_path, blob_slide, capsys):
    stray = tmp_path / "stray_secret_dir" / "patient_slide.svs"
    with pytest.raises(SystemExit) as info:
        main(_args(tmp_path, pipeline_path, blob_slide, "ws", "out", "--quiet", str(stray)))
    assert info.value.code == 2
    captured = capsys.readouterr()
    for text in (captured.out, captured.err):
        assert "stray_secret_dir" not in text and "patient_slide" not in text
        assert str(tmp_path) not in text


def test_scene_out_of_int32_names_no_path(tmp_path, pipeline_path, blob_slide, capsys):
    argv = _args(tmp_path, pipeline_path, blob_slide, "ws", "out", "--scene", "99999999999", "--quiet")
    assert main(argv) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    for text in (captured.out, captured.err):
        assert "99999999999" not in text
        assert str(tmp_path) not in text
        assert "slide.png" not in text and "three_stage.json" not in text


def test_unexpected_error_prints_class_name_only(tmp_path, pipeline_path, blob_slide, capsys, monkeypatch):
    from slideio_processor import cli

    def boom(args):
        raise RuntimeError(f"failed on {args.slide}")

    monkeypatch.setattr(cli, "_run", boom)
    assert main(_args(tmp_path, pipeline_path, blob_slide, "ws", "out", "--quiet")) == 1
    captured = capsys.readouterr()
    assert captured.err == "error: RuntimeError\n"
    assert captured.out == ""


def test_module_entry_point():
    done = subprocess.run([sys.executable, "-m", "slideio_processor", "--help"],
                          capture_output=True, text=True)
    assert done.returncode == 0
    assert "run" in done.stdout
