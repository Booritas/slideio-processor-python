"""Workspace queries: counts, records, NumPy batches, DataFrames and summaries."""
import gc
import math
import threading
import time

import numpy
import pytest
from slideio_processor import (
    InvalidArgumentError,
    IoError,
    LogLevel,
    MeasurementSummary,
    ObjectBatch,
    ObjectQuery,
    ObjectRecord,
    Operation,
    Output,
    PipelineSpec,
    ResultSummary,
    SpatialPredicate,
    StageDefinition,
    Workspace,
    configure_logging,
)
from support.workspaces import nuclei_definition

# The area of Pillow's 81-pixel disc (radius 40 drawn inclusively), as plan 2 ruled.
DISC = math.pi * 40.5 ** 2


def _nuclei(path, slide):
    ws = Workspace.create(path, slide)
    ws.declare_layer("nuclei")
    ws.declare_stage(nuclei_definition())
    ws.run_stage("nuclei")
    return ws


def test_count(nuclei_workspace):
    ws = nuclei_workspace
    assert ws.count(layer="nuclei") == 9
    assert ws.count(layer="nuclei", intersecting=(0, 0, 200, 200)) == 1
    assert ws.count(layer="nuclei", limit=4) == 4
    assert ws.count(ObjectQuery(layer="nuclei", stage="nuclei")) == 9
    assert ws.count() == 9


def test_objects_records(nuclei_workspace):
    ws = nuclei_workspace
    records = list(ws.objects(layer="nuclei"))
    assert len(records) == 9
    for r in records:
        assert isinstance(r, ObjectRecord)
        assert r.layer == "nuclei"
        assert r.stage == "nuclei"
        assert r.label is None
        assert abs(r.measurements["area"] - DISC) / DISC < 0.02
        assert r.bbox[2] == 81 and r.bbox[3] == 81
        points, hole = r.geometry[0]
        assert hole is False
        assert points.dtype == numpy.int64
        assert points.ndim == 2 and points.shape[1] == 2
    first = records[0]
    assert ws.object(first.id) == first
    assert len({r.id for r in records}) == 9


def test_object_not_found(nuclei_workspace):
    # A well-formed id that designates no object.
    assert nuclei_workspace.object("1:0:0") is None


def test_measurement_names(nuclei_workspace):
    names = nuclei_workspace.measurement_names
    assert isinstance(names, tuple)
    assert "area" in names and "perimeter" in names
    assert "centroid.x" in names


def test_measurement_bounds(nuclei_workspace):
    ws = nuclei_workspace
    assert ws.count(layer="nuclei", at_least={"area": DISC * 2}) == 0
    assert ws.count(layer="nuclei", at_most={"area": DISC * 2}) == 9


def test_within_polygon(nuclei_workspace):
    # Covers the disc centred at (100, 100) (bbox 60..140) and no other.
    triangle = [(numpy.array([[0, 0], [290, 0], [0, 290]]), False)]
    ws = nuclei_workspace
    assert ws.count(layer="nuclei", within=triangle, within_predicate=SpatialPredicate.CONTAINED_BY) == 1
    assert ws.count(layer="nuclei", within=triangle) >= 1


def test_batches(nuclei_workspace):
    ws = nuclei_workspace
    batches = list(ws.batches(layer="nuclei", batch_size=2))
    assert [len(b.ids) for b in batches] == [2, 2, 2, 2, 1]
    for b in batches:
        assert isinstance(b, ObjectBatch)
        n = len(b.ids)
        assert b.bbox.dtype == numpy.int64
        assert b.bbox.shape == (n, 4)
        assert b.measurements["area"].dtype == numpy.float64
        assert b.measurements["area"].shape == (n,)
        assert b.geometry is None
        assert b.layers == ("nuclei",) * n
        assert b.stages == ("nuclei",) * n
        assert b.labels == (None,) * n
        assert set(b.measurements) == set(ws.measurement_names)
    with_geometry = list(ws.batches(layer="nuclei", batch_size=4, measurements=["area"], geometry=True))
    assert [len(b.ids) for b in with_geometry] == [4, 4, 1]
    assert list(with_geometry[0].measurements) == ["area"]
    records = list(ws.objects(layer="nuclei"))
    rings = [g for b in with_geometry for g in b.geometry]
    assert len(rings) == 9
    for record, geometry in zip(records, rings):
        assert len(geometry) == len(record.geometry)
        assert numpy.array_equal(geometry[0][0], record.geometry[0][0])


def test_unknown_names_refused(nuclei_workspace):
    ws = nuclei_workspace
    with pytest.raises(InvalidArgumentError, match="'missing'"):
        ws.count(layer="missing")
    with pytest.raises(InvalidArgumentError, match="'missing'"):
        ws.objects(stage="missing")
    with pytest.raises(InvalidArgumentError, match="'missing'"):
        ws.count(label="missing")
    with pytest.raises(InvalidArgumentError, match="'missing'"):
        ws.batches(measurements=["missing"])
    with pytest.raises(InvalidArgumentError, match="'not-an-id'"):
        ws.object("not-an-id")


def test_bad_filters_refused(nuclei_workspace):
    ws = nuclei_workspace
    with pytest.raises(TypeError, match="^Workspace: .*'colour'"):
        ws.count(colour="red")
    with pytest.raises(InvalidArgumentError, match="^ObjectQuery: "):
        ObjectQuery(intersecting=(0, 0, 0, 10))
    limit_range = "^ObjectQuery: limit must be from 0 to 9223372036854775807, got "
    with pytest.raises(InvalidArgumentError, match=limit_range + "-1$"):
        ObjectQuery(limit=-1)
    with pytest.raises(InvalidArgumentError, match=limit_range + "9223372036854775808$"):
        ObjectQuery(limit=2**63)
    with pytest.raises(TypeError, match="^ObjectQuery: "):
        ObjectQuery(layer=3)
    with pytest.raises(InvalidArgumentError, match="^ObjectQuery: "):
        ObjectQuery(at_least={"area": "big"})
    with pytest.raises(InvalidArgumentError, match="^Workspace: "):
        ws.batches(batch_size=0)


def test_query_and_filters_together_refused(nuclei_workspace):
    ws = nuclei_workspace
    with pytest.raises(InvalidArgumentError, match="^Workspace: "):
        ws.count(ObjectQuery(layer="nuclei"), layer="nuclei")
    with pytest.raises(InvalidArgumentError, match="^Workspace: "):
        ws.objects(ObjectQuery(), limit=1)
    with pytest.raises(TypeError, match="^Workspace: "):
        ws.count("nuclei")


def test_dataframe(nuclei_workspace):
    pytest.importorskip("pandas")
    frame = nuclei_workspace.dataframe(layer="nuclei")
    assert len(frame) == 9
    assert list(frame.columns[:8]) == ["id", "layer", "stage", "x", "y", "width", "height", "label"]
    assert "area" in frame.columns
    assert (frame["width"] == 81).all()
    assert frame["area"].dtype == numpy.float64
    empty = nuclei_workspace.dataframe(layer="nuclei", limit=0, measurements=["area"])
    assert list(empty.columns) == ["id", "layer", "stage", "x", "y", "width", "height", "label", "area"]
    assert len(empty) == 0


def test_dataframe_column_clash_refused_before_the_query(nuclei_workspace, monkeypatch):
    """A measurement named as a fixed DataFrame column is refused before a cursor opens,
    not after every batch has been streamed."""
    pytest.importorskip("pandas")
    from slideio_processor import objects

    monkeypatch.setattr(objects, "measurement_names_of", lambda handle: ("area", "x"))

    def no_cursor(*args):
        raise AssertionError("a cursor was opened")

    monkeypatch.setattr(objects, "_open_cursor", no_cursor)
    clash = "^Workspace: the measurement 'x' has the name of a DataFrame column$"
    with pytest.raises(InvalidArgumentError, match=clash):
        nuclei_workspace.dataframe(layer="nuclei", measurements=["area", "x"])

def test_summary(nuclei_workspace):
    ws = nuclei_workspace
    s = ws.summary(layer="nuclei")
    assert isinstance(s, ResultSummary)
    assert s.count == 9
    area = s.measurements["area"]
    assert isinstance(area, MeasurementSummary)
    assert area.count == 9
    assert abs(area.mean - DISC) / DISC < 0.02
    assert area.min <= area.mean <= area.max
    assert area.std >= 0
    values = numpy.concatenate([b.measurements["area"] for b in ws.batches(layer="nuclei", batch_size=2)])
    for got, want in ((area.mean, values.mean()), (area.std, values.std(ddof=0)),
                      (area.min, values.min()), (area.max, values.max())):
        assert got == pytest.approx(want, rel=1e-9)
    small = ws.summary(layer="nuclei", limit=0, measurements=["area"])
    assert small.count == 0
    assert small.measurements["area"].count == 0
    assert math.isnan(small.measurements["area"].mean)
    assert math.isnan(small.measurements["area"].std)


def test_cursor_after_close_raises(blob_slide, tmp_path):
    ws = _nuclei(tmp_path / "ws", blob_slide)
    records = ws.objects(layer="nuclei")
    next(records)
    raw = ws.batches(layer="nuclei", batch_size=1)
    next(raw)
    ws.close()
    with pytest.raises(IoError):
        next(records)
    with pytest.raises(IoError):
        next(raw)


def test_native_cursor_after_close_raises(blob_slide, tmp_path):
    """The library's own IoError, from a native cursor whose store was closed between two
    fetches (the iterators above refuse before reaching it)."""
    from slideio_processor import objects

    ws = _nuclei(tmp_path / "ws", blob_slide)
    cursor = objects._open_cursor(ws, ObjectQuery(layer="nuclei"))
    assert cursor.fetch(1, [], False) is not None
    ws.close()
    with pytest.raises(IoError):
        cursor.fetch(1, [], False)
    cursor.close()
    del cursor
    gc.collect()


def test_cursor_keeps_workspace_alive(blob_slide, tmp_path):
    ws = _nuclei(tmp_path / "ws", blob_slide)
    it = ws.batches(layer="nuclei", batch_size=2)
    del ws
    gc.collect()
    assert sum(len(b.ids) for b in it) == 9


def test_query_during_stage_run_with_python_logging(blob_slide, tmp_path):
    """Queries on the main thread while a stage with a slow Python operation runs on
    another, with the library logging to Python at DEBUG.

    Limitation (see the task 5 report): a liveness check, not a proof of freedom from
    deadlock. The library sends no log records to Python from a normal stage run, even at
    TRACE, so nothing here makes a worker want the GIL while it holds a lock a query waits
    on; the test shows only that the queries and the run both finish."""
    started = threading.Event()

    class Slow(Operation):
        name = "test.query_during_stage_run"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            started.set()
            time.sleep(0.5)
            return tile.mean(axis=2, dtype=numpy.float32)

    configure_logging(LogLevel.DEBUG, python=True)
    try:
        ws = _nuclei(tmp_path / "ws", blob_slide)
        try:
            ws.declare_raster("grey", "float32")
            ws.declare_stage(StageDefinition("slow", PipelineSpec().add(Slow), writes_raster="grey"))
            errors = []

            def run():
                try:
                    ws.run_stage("slow")
                except BaseException as error:  # noqa: B036
                    errors.append(error)

            t = threading.Thread(target=run)
            t.start()
            try:
                assert started.wait(60)
                counts = [ws.count(layer="nuclei") for _ in range(20)]
            finally:
                t.join(60)
            assert not t.is_alive()
            assert not errors
            assert counts == [9] * 20
        finally:
            ws.close()
    finally:
        configure_logging(LogLevel.WARNING)


def test_records_are_frozen(nuclei_workspace):
    r = next(nuclei_workspace.objects(layer="nuclei"))
    with pytest.raises(AttributeError):
        r.id = "x"
    with pytest.raises(TypeError):
        r.measurements["area"] = 1.0
    with pytest.raises(ValueError):
        r.geometry[0][0][0, 0] = 1


def test_closed_workspace_refuses_queries(blob_slide, tmp_path):
    ws = _nuclei(tmp_path / "ws", blob_slide)
    ws.close()
    for call in (lambda: ws.count(), lambda: ws.objects(), lambda: ws.object("1:0:0"),
                 lambda: ws.batches(), lambda: ws.summary(), lambda: ws.measurement_names):
        with pytest.raises(IoError, match="closed"):
            call()


def test_dataframe_without_pandas(nuclei_workspace, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "pandas", None)
    with pytest.raises(ImportError, match=r"^Workspace.dataframe needs pandas: pip install slideio-processor\[pandas\]$"):
        nuclei_workspace.dataframe()
