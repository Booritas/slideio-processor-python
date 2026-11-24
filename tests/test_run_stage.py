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
    IoError,
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


def test_drop_unclosed_workspaces_while_stage_runs(blob_slide, seam_slide, tmp_path):
    """Unclosed workspaces are dropped and collected again and again on the main thread
    while a three-tile stage runs on worker threads, with the library logging to Python at
    TRACE. Each drop closes a store from the native deleter, which releases the GIL; the
    run and the drops must all finish.

    Limitation (see the task 5 fix report): this is a liveness check, not a proof. With the
    deleter's GIL release reverted, the test still passes: closing one workspace's store
    waits on no lock that another workspace's run holds while it waits for the GIL, and
    the library logs nothing from the tile workers during such a run. A deadlock needs a
    thread that holds a lock the closing store waits on and wants the GIL, which this
    library build does not offer to a test."""
    tiles = []

    class Steady(Operation):
        name = "test.drop_unclosed_workspaces_while_stage_runs"
        version = "1"
        output = Output.RASTER
        max_concurrency = 1

        def apply(self, tile, context):
            tiles.append(True)
            time.sleep(0.2)
            return _mean(tile)

    configure_logging(LogLevel.TRACE, python=True)
    try:
        with Workspace.create(tmp_path / "ws", seam_slide) as ws:
            _raster_stage(ws, Steady)
            errors = []

            def run():
                try:
                    ws.run_stage("python")
                except BaseException as error:  # noqa: B036
                    errors.append(error)

            t = threading.Thread(target=run)
            t.start()
            drops = 0
            try:
                deadline = time.monotonic() + 60
                while (t.is_alive() or drops < 3) and time.monotonic() < deadline:
                    other = Workspace.create(tmp_path / f"other{drops}", blob_slide)
                    other.declare_layer("tissue")
                    del other
                    gc.collect()
                    drops += 1
            finally:
                t.join(60)
            assert not t.is_alive()
            assert not errors
            assert len(tiles) == 3
            assert drops >= 3
        # Every dropped workspace's lock is free again.
        for i in range(drops):
            Workspace.open(tmp_path / f"other{i}").close()
    finally:
        configure_logging(LogLevel.WARNING)


def test_close_between_checks_refuses_run(blob_slide, tmp_path):
    """A close() that lands after run_stage's first closed check, while the arguments are
    validated, still gives IoError: the check is repeated atomically with close()."""
    ws = _nuclei(tmp_path / "ws", blob_slide)

    class ClosingName(str):
        def encode(self, *args, **kwargs):
            ws.close()  # runs inside run_stage's argument validation
            return str.encode(self, *args, **kwargs)

    try:
        with pytest.raises(IoError, match="^Workspace: the workspace is closed$"):
            ws.run_stage(ClosingName("nuclei"))
        assert ws.closed
    finally:
        ws.close()


def test_concurrent_second_close_is_a_no_op(blob_slide, tmp_path):
    """While one close() is inside the native close, a second close() returns at once and
    run_stage gives IoError, not 'in progress'."""
    ws = _nuclei(tmp_path / "ws", blob_slide)
    native = ws._native
    entered = threading.Event()
    release = threading.Event()

    class SlowClose:
        def close(self):
            entered.set()
            release.wait(30)
            native.close()

    ws._native = SlowClose()
    errors = []

    def first():
        try:
            ws.close()
        except BaseException as error:  # noqa: B036
            errors.append(error)

    t = threading.Thread(target=first)
    t.start()
    try:
        assert entered.wait(30)
        assert ws.close() is None
        with pytest.raises(IoError, match="closed"):
            ws.run_stage("nuclei")
    finally:
        release.set()
        t.join(30)
    assert not t.is_alive()
    assert not errors
    assert ws.closed
    # The native close ran once: the lock is free again.
    Workspace.open(tmp_path / "ws").close()
