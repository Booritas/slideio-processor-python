import logging
import subprocess
import sys

import pytest
from slideio_processor import InvalidArgumentError, LogLevel, PipelineFile, SlideRun, configure_logging


@pytest.fixture(autouse=True)
def _restore_logging():
    yield
    configure_logging(LogLevel.WARNING)


def _run_twice(blob_slide, tmp_path, three_stage_json):
    pipeline = PipelineFile.from_json(three_stage_json)
    for _ in range(2):
        # The second run reopens the workspace, which logs its scratch and raster sweep.
        SlideRun(pipeline, blob_slide, tmp_path / "ws", tmp_path / "out").run()


def test_log_level_values():
    assert [int(v) for v in LogLevel] == [0, 1, 2, 3, 4, 5]
    assert [v.name for v in LogLevel] == ["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "OFF"]


def test_python_logging_receives_records(caplog, blob_slide, tmp_path, three_stage_json):
    caplog.set_level(5, logger="slideio_processor")
    configure_logging(LogLevel.DEBUG, python=True)
    _run_twice(blob_slide, tmp_path, three_stage_json)
    records = [r for r in caplog.records if r.name == "slideio_processor"]
    assert records
    assert any(r.levelno == logging.INFO and "reclaimed" in r.getMessage() for r in records)


def test_python_logging_respects_threshold(caplog, blob_slide, tmp_path, three_stage_json):
    caplog.set_level(5, logger="slideio_processor")
    configure_logging(LogLevel.ERROR, python=True)
    _run_twice(blob_slide, tmp_path, three_stage_json)
    assert not [r for r in caplog.records if r.name == "slideio_processor"]


def test_file_logging_writes(tmp_path, blob_slide, three_stage_json):
    log = tmp_path / "log.txt"
    configure_logging(LogLevel.DEBUG, file=log)
    _run_twice(blob_slide, tmp_path, three_stage_json)
    configure_logging(LogLevel.WARNING)  # closes the file before it is read
    assert log.stat().st_size > 0
    assert "INFO" in log.read_text(encoding="utf-8")


def test_file_and_python_refused(tmp_path):
    with pytest.raises(InvalidArgumentError, match="^configure_logging: "):
        configure_logging(LogLevel.DEBUG, file=tmp_path / "log.txt", python=True)


def test_bad_level_refused():
    with pytest.raises(TypeError, match="^configure_logging: "):
        configure_logging("DEBUG")
    with pytest.raises(TypeError, match="^configure_logging: "):
        configure_logging(True)
    with pytest.raises(InvalidArgumentError, match="^configure_logging: "):
        configure_logging(9)


def test_unopenable_log_file_raises(tmp_path):
    from slideio_processor import IoError

    with pytest.raises(IoError):
        configure_logging(LogLevel.DEBUG, file=tmp_path / "no" / "such" / "dir" / "log.txt")


def test_exit_with_python_logging_is_clean(blob_slide, tmp_path, three_stage_json):
    (tmp_path / "pipeline.json").write_text(three_stage_json, encoding="utf-8")
    code = (
        "import logging, sys\n"
        "import slideio_processor as sp\n"
        "logging.basicConfig(level=5, stream=sys.stdout)\n"
        "sp.configure_logging(sp.LogLevel.DEBUG, python=True)\n"
        "pipeline = sp.PipelineFile.from_json(open(sys.argv[1], encoding='utf-8').read())\n"
        "sp.SlideRun(pipeline, sys.argv[2], sys.argv[3], sys.argv[4]).run()\n"
        "sp.SlideRun(pipeline, sys.argv[2], sys.argv[3], sys.argv[4]).run()\n"
    )
    args = [str(tmp_path / "pipeline.json"), str(blob_slide), str(tmp_path / "ws"), str(tmp_path / "out")]
    done = subprocess.run([sys.executable, "-c", code, *args], cwd=tmp_path, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert "slideio_processor" in done.stdout
