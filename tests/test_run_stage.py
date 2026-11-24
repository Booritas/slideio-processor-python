"""Workspace.run_stage: results, progress, cancellation, Ctrl-C, causes and the long-call lock."""
import _thread
import gc
import threading
import time

import numpy
import pytest
from slideio_processor import (
    Cancellation,
    CancelledError,
    InvalidArgumentError,
    LogLevel,
    Operation,
    Output,
    PipelineSpec,
    StageDefinition,
    StageError,
    StageRunResult,
    StageStatus,
    Workspace,
    configure_logging,
)
from support.workspaces import nuclei_definition


def _nuclei(path, slide):
    ws = Workspace.create(path, slide)
    ws.declare_layer("nuclei")
    ws.declare_stage(nuclei_definition())
    return ws


def _raster_stage(ws, operation, name="python"):
    """Declares a raster "grey" and a stage `name` running `operation` (an Operation subclass)."""
    ws.declare_raster("grey", "float32")
    ws.declare_stage(StageDefinition(name, PipelineSpec().add(operation), writes_raster="grey"))


def _mean(tile):
    return tile.mean(axis=2, dtype=numpy.float32)


def test_run_stage_writes_objects(blob_slide, tmp_path):
    with _nuclei(tmp_path / "ws", blob_slide) as ws:
        r = ws.run_stage("nuclei")
        assert isinstance(r, StageRunResult)
        assert r.name == "nuclei"
        assert r.objects_written == 9
        assert r.rerun is False
        assert r.truncated_objects == 0      # the PNG is one tile
        assert r.edges_asserted == 0
        info = ws.stage("nuclei")
        assert info.status is StageStatus.COMPLETE
        assert info.completed_at is not None
        with pytest.raises(AttributeError):
            r.rerun = True


def test_rerun_reports_rerun(nuclei_workspace):
    r = nuclei_workspace.run_stage("nuclei")
    assert r.rerun is True
    assert r.objects_written == 9


def test_unknown_stage_named(blob_slide, tmp_path):
    with _nuclei(tmp_path / "ws", blob_slide) as ws:
        with pytest.raises(InvalidArgumentError, match="'nope'"):
            ws.run_stage("nope")
        with pytest.raises(TypeError, match="^Workspace: "):
            ws.run_stage(3)


def test_run_stage_progress(blob_slide, tmp_path):
    calls = []
    with _nuclei(tmp_path / "ws", blob_slide) as ws:
        ws.run_stage("nuclei", progress=lambda *a: calls.append(a))
    assert "nuclei" in {name for name, *_ in calls}
    nuclei = [c for c in calls if c[0] == "nuclei"]
    assert nuclei[-1][1] == 1.0
    for _name, fraction, completed, total in calls:
        assert 0.0 <= fraction <= 1.0
        assert 0 <= completed <= total


def test_run_stage_progress_raises(blob_slide, tmp_path):
    def progress(*args):
        1 / 0

    with _nuclei(tmp_path / "ws", blob_slide) as ws:
        with pytest.raises(ZeroDivisionError) as info:
            ws.run_stage("nuclei", progress=progress)
        assert isinstance(info.value.__context__, CancelledError)
        assert ws.stage("nuclei").status is not StageStatus.COMPLETE


def test_run_stage_cancelled(blob_slide, tmp_path):
    c = Cancellation()
    c.cancel()
    with _nuclei(tmp_path / "ws", blob_slide) as ws:
        with pytest.raises(CancelledError):
            ws.run_stage("nuclei", cancellation=c)
        assert ws.stage("nuclei").status is not StageStatus.COMPLETE


def test_bad_cancellation_refused(blob_slide, tmp_path):
    with _nuclei(tmp_path / "ws", blob_slide) as ws:
        with pytest.raises(TypeError, match="^Workspace: "):
            ws.run_stage("nuclei", cancellation=object())


def test_run_stage_keyboard_interrupt(blob_slide, tmp_path):
    # The library checks the token before each tile and before each operation of a tile,
    # so the first of two operations holds its tile until Ctrl-C has cancelled the run:
    # the check before the second one stops it. (A cancellation that lands in the last
    # operation of the last tile is not observed; the stage then completes.)
    started = threading.Event()

    class Slow(Operation):
        name = "test.run_stage_keyboard_interrupt"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            started.set()
            deadline = time.monotonic() + 30
            while not context.cancelled and time.monotonic() < deadline:
                time.sleep(0.01)
            return _mean(tile)

    class Then(Operation):
        name = "test.run_stage_keyboard_interrupt_then"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            return numpy.ascontiguousarray(tile.reshape(tile.shape[:2]), dtype=numpy.float32)

    def interrupt():
        if started.wait(30):
            _thread.interrupt_main()

    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        ws.declare_raster("grey", "float32")
        ws.declare_stage(StageDefinition("python", PipelineSpec().add(Slow).add(Then), writes_raster="grey"))
        timer = threading.Thread(target=interrupt)
        timer.start()
        start = time.monotonic()
        try:
            with pytest.raises(KeyboardInterrupt) as info:
                ws.run_stage("python")
        finally:
            timer.join()
        assert time.monotonic() - start < 20
        assert isinstance(info.value.__cause__, CancelledError)
        assert ws.stage("python").status is StageStatus.FAILED


def test_python_operation_failure_is_the_cause(blob_slide, tmp_path):
    class Broken(Operation):
        name = "test.run_stage_failure_is_the_cause"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            raise ValueError("boom")

    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        _raster_stage(ws, Broken)
        with pytest.raises(StageError) as info:
            ws.run_stage("python")
        assert info.value.operation_name == Broken.name
        assert isinstance(info.value.__cause__, ValueError)
        assert str(info.value.__cause__) == "boom"
        assert ws.stage("python").status is StageStatus.FAILED


def test_read_only_refuses_run(nuclei_workspace):
    with Workspace.open_read_only(nuclei_workspace.path) as reader:
        with pytest.raises(InvalidArgumentError, match="^Workspace: .*read-only"):
            reader.run_stage("nuclei")


def test_closed_workspace_refuses_run(blob_slide, tmp_path):
    from slideio_processor import IoError

    ws = _nuclei(tmp_path / "ws", blob_slide)
    ws.close()
    with pytest.raises(IoError, match="closed"):
        ws.run_stage("nuclei")


def test_concurrent_long_call_refused(blob_slide, tmp_path):
    started = threading.Event()
    release = threading.Event()

    class Blocking(Operation):
        name = "test.run_stage_concurrent_long_call"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            started.set()
            release.wait(60)
            return _mean(tile)

    results = []
    errors = []
    ws = Workspace.create(tmp_path / "ws", blob_slide)
    try:
        _raster_stage(ws, Blocking)

        def first():
            try:
                results.append(ws.run_stage("python"))
            except BaseException as error:  # noqa: B036
                errors.append(error)

        t = threading.Thread(target=first)
        t.start()
        try:
            assert started.wait(60)
            with pytest.raises(InvalidArgumentError, match="^Workspace: .*in progress"):
                ws.run_stage("python")
            with pytest.raises(InvalidArgumentError, match="^Workspace: .*in progress"):
                ws.close()
            assert not ws.closed
            # Queries run alongside.
            assert ws.stage("python").status is StageStatus.RUNNING
        finally:
            release.set()
            t.join(60)
        assert not t.is_alive()
        assert not errors
        assert len(results) == 1 and results[0].name == "python"
        # Once the first run has finished, the object runs again.
        assert ws.run_stage("python").rerun is True
    finally:
        release.set()
        ws.close()
    assert ws.closed


def test_drop_unclosed_workspace_while_stage_logs(blob_slide, tmp_path):
    """Dropping an unclosed workspace closes its store with the GIL released, so a worker
    thread logging through the Python sink is never blocked behind it."""
    started = threading.Event()
    dropped = threading.Event()

    class Waiting(Operation):
        name = "test.drop_unclosed_workspace_while_stage_logs"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            started.set()
            dropped.wait(30)
            return _mean(tile)

    configure_logging(LogLevel.DEBUG, python=True)
    try:
        other = Workspace.create(tmp_path / "other", blob_slide)
        other.declare_layer("tissue")
        with Workspace.create(tmp_path / "ws", blob_slide) as ws:
            _raster_stage(ws, Waiting)
            errors = []

            def run():
                try:
                    ws.run_stage("python")
                except BaseException as error:  # noqa: B036
                    errors.append(error)

            t = threading.Thread(target=run)
            t.start()
            try:
                assert started.wait(30)
                del other
                gc.collect()
            finally:
                dropped.set()
                t.join(60)
            assert not t.is_alive()
            assert not errors
        # The dropped workspace's lock is free again.
        Workspace.open(tmp_path / "other").close()
    finally:
        configure_logging(LogLevel.WARNING)
