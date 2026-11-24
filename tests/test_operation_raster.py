import _thread
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

import numpy
import pytest
import slideio
from PIL import Image
from slideio_processor import (
    Operation,
    Output,
    PipelineFile,
    SlideRun,
    StageError,
    TileContext,
)
from slideio_processor import operation as operation_module
from support.pipelines import one_stage_pipeline

_EXPORT = {"kind": "rasterTiff", "file": "grey.ome.tif", "raster": "grey"}


def _mean(tile):
    return tile.mean(axis=2, dtype=numpy.float32)


def _slide_run(slide, tmp_path, name, parameters=None, sub="run"):
    if parameters is None:
        parameters = {"version": "1"}
    text = one_stage_pipeline(name, parameters, writes_raster="grey", exports=[_EXPORT])
    (tmp_path / sub).mkdir(exist_ok=True)
    return SlideRun(PipelineFile.from_json(text), slide, tmp_path / sub / "ws", tmp_path / sub / "out")


def _stage_record(tmp_path, sub="run"):
    store = tmp_path / sub / "ws" / "workspace.store"
    connection = sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)
    try:
        (configuration,) = connection.execute("SELECT configuration FROM stages WHERE name = 'python'").fetchone()
    finally:
        connection.close()
    return json.loads(configuration)


def test_raster_matches_numpy(seam_slide, tmp_path):
    class Grey(Operation):
        name = "test.raster_matches_numpy"
        version = "1"
        output = Output.RASTER
        halo = 16

        def apply(self, tile, context):
            return _mean(tile)

    _slide_run(seam_slide, tmp_path, Grey.name).run()
    scene = slideio.open_slide(str(tmp_path / "run" / "out" / "grey.ome.tif"), "OMETIFF").get_scene(0)
    raster = scene.read_block()
    expected = _mean(numpy.asarray(Image.open(seam_slide.with_suffix(".png"))))
    assert raster.shape == expected.shape == (400, 2200)
    assert raster.dtype == numpy.float32
    assert numpy.array_equal(raster, expected)


def test_tile_is_halo_extended_copy(seam_slide, tmp_path):
    calls = []

    class Grey(Operation):
        name = "test.tile_is_halo_extended_copy"
        version = "1"
        output = Output.RASTER
        halo = 16

        def apply(self, tile, context):
            calls.append({
                "shape": tile.shape,
                "dtype": tile.dtype,
                "contiguous": tile.flags.c_contiguous,
                "tile_rect": context.tile_rect,
                "halo_rect": context.halo_rect,
                "level": context.level,
                "level_scale": context.level_scale,
                "pixel_size_um": context.pixel_size_um,
                "cancelled": context.cancelled,
                "top_rows_max": int(tile[:16].max()),
                "context": context,
            })
            return _mean(tile)

    _slide_run(seam_slide, tmp_path, Grey.name).run()
    assert len(calls) == 3
    for call in calls:
        assert call["shape"][:2] == (call["halo_rect"][3], call["halo_rect"][2])
        assert call["dtype"] == numpy.uint8
        assert call["contiguous"]
        assert call["level"] == 0
        assert call["level_scale"] == 1.0
        assert call["pixel_size_um"] >= 0
        assert call["cancelled"] is False
        # The halo rect starts above the level: those rows read as zero.
        assert call["top_rows_max"] == 0
        assert isinstance(call["context"], TileContext)
        # After apply returns, the context no longer reads the run's token.
        assert call["context"].cancelled is True
    middle = [c for c in calls if c["tile_rect"][0] == 1024]
    assert len(middle) == 1
    assert middle[0]["tile_rect"] == (1024, 0, 1024, 400)
    assert middle[0]["halo_rect"] == (1008, -16, 1056, 432)
    assert middle[0]["shape"] == (432, 1056, 3)
    with pytest.raises(AttributeError):
        calls[0]["context"].level = 1


def test_init_once_per_stage(seam_slide, tmp_path):
    class Grey(Operation):
        name = "test.init_once_per_stage"
        version = "1"
        output = Output.RASTER
        created = 0

        def __init__(self, parameters):
            super().__init__(parameters)
            type(self).created += 1

        def apply(self, tile, context):
            return _mean(tile)

    _slide_run(seam_slide, tmp_path, Grey.name).run()
    assert Grey.created == 1


def test_parameters_reach_init(seam_slide, tmp_path):
    received = []

    class Grey(Operation):
        name = "test.parameters_reach_init"
        version = "1"
        output = Output.RASTER
        parameters = {"scale": 1.0}

        def __init__(self, parameters):
            super().__init__(parameters)
            received.append(parameters)

        def apply(self, tile, context):
            return _mean(tile)

    _slide_run(seam_slide, tmp_path, Grey.name, Grey.stage_parameters(scale=2.0)).run()
    assert received == [{"scale": 2.0}]


def test_max_concurrency_one(seam_slide, tmp_path):
    lock = threading.Lock()
    state = {"now": 0, "most": 0}

    class Grey(Operation):
        name = "test.max_concurrency_one"
        version = "1"
        output = Output.RASTER
        max_concurrency = 1

        def apply(self, tile, context):
            with lock:
                state["now"] += 1
                state["most"] = max(state["most"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return _mean(tile)

    _slide_run(seam_slide, tmp_path, Grey.name).run()
    assert state["most"] == 1


def test_max_concurrency_unlimited_runs_tiles_together(seam_slide, tmp_path):
    # The positive control for test_max_concurrency_one: without a limit, two applies overlap.
    if (os.cpu_count() or 1) < 2:
        pytest.skip("needs two CPUs")
    lock = threading.Lock()
    overlap = threading.Event()
    state = {"now": 0, "most": 0}
    deadline = time.monotonic() + 30

    class Grey(Operation):
        name = "test.max_concurrency_unlimited_runs_tiles_together"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            with lock:
                state["now"] += 1
                state["most"] = max(state["most"], state["now"])
                if state["now"] >= 2:
                    overlap.set()
            # The first apply waits, without the GIL, for a second one to arrive.
            overlap.wait(max(0, deadline - time.monotonic()))
            with lock:
                state["now"] -= 1
            return _mean(tile)

    _slide_run(seam_slide, tmp_path, Grey.name).run()
    assert state["most"] > 1


def _no_records_left():
    # Read-only: a check that cleared the records would hide what a run left behind.
    return operation_module._peek_errors() == []


def test_apply_exception_chained(seam_slide, tmp_path):
    name = "test.apply_exception_chained"

    class Broken(Operation):
        name = "test.apply_exception_chained"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            raise ValueError("boom")

    run = _slide_run(seam_slide, tmp_path, name)
    with pytest.raises(StageError) as info:
        run.run()
    assert info.value.operation_name == name
    assert isinstance(info.value.__cause__, ValueError)
    assert "boom" in str(info.value.__cause__)
    assert "ValueError: boom" in str(info.value)
    assert _no_records_left()

    class OtherBroken(Operation):
        name = "test.apply_exception_chained"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            raise KeyError("second")

    # A stale record from before the run is not the cause either: the run's mark filters it.
    operation_module._record_error(name, ValueError("stale"))
    with pytest.raises(StageError) as info:
        run.run()
    assert isinstance(info.value.__cause__, KeyError)
    assert "second" in str(info.value.__cause__)
    # Only the stale record is left, and it is older than the run's mark.
    assert [n for n, _ in operation_module._peek_errors()] == [name]
    operation_module._take_errors(name, 0)
    assert _no_records_left()

    class Fixed(Operation):
        name = "test.apply_exception_chained"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            return _mean(tile)

    result = run.run()
    assert result.stages[0].ran
    assert _no_records_left()


def test_init_exception_chained(seam_slide, tmp_path):
    class Broken(Operation):
        name = "test.init_exception_chained"
        version = "1"
        output = Output.RASTER
        attempts = 0

        def __init__(self, parameters):
            super().__init__(parameters)
            type(self).attempts += 1
            raise RuntimeError("no model")

        def apply(self, tile, context):
            return _mean(tile)

    with pytest.raises(StageError) as info:
        _slide_run(seam_slide, tmp_path, Broken.name).run()
    assert info.value.operation_name == Broken.name
    assert isinstance(info.value.__cause__, RuntimeError)
    assert "no model" in str(info.value.__cause__)
    # A failed construction is not retried for the stage's other tiles.
    assert Broken.attempts == 1
    assert _no_records_left()


def test_unprintable_exception_is_reported(seam_slide, tmp_path):
    class Unprintable(Exception):
        def __str__(self):
            raise RuntimeError("no text")

    class Broken(Operation):
        name = "test.unprintable_exception_is_reported"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            raise Unprintable()

    with pytest.raises(StageError) as info:
        _slide_run(seam_slide, tmp_path, Broken.name).run()
    assert "Unprintable" in str(info.value)
    assert isinstance(info.value.__cause__, Unprintable)


_WRONG_RESULTS = {
    "tile_sized": lambda tile, h: numpy.zeros((tile.shape[0] - 2 * h, tile.shape[1] - 2 * h), numpy.float32),
    "float64": lambda tile, h: tile.mean(axis=2),
    "list": lambda tile, h: [[0.0]],
}


@pytest.mark.parametrize("case", sorted(_WRONG_RESULTS))
def test_raster_wrong_shape_or_dtype_fails(seam_slide, tmp_path, case):
    shapes = []
    make = _WRONG_RESULTS[case]

    class Wrong(Operation):
        name = f"test.raster_wrong_shape_or_dtype_fails.{case}"
        version = "1"
        output = Output.RASTER
        halo = 16

        def apply(self, tile, context):
            shapes.append(tile.shape[:2])
            return make(tile, self.halo)

    with pytest.raises(StageError) as info:
        _slide_run(seam_slide, tmp_path, Wrong.name).run()
    message = str(info.value)
    assert Wrong.name in message
    if case == "tile_sized":
        assert any(f"({h}, {w})" in message and f"({h - 32}, {w - 32})" in message for h, w in shapes)
    elif case == "float64":
        assert "float32" in message and "float64" in message
    else:
        assert "numpy.ndarray" in message and "list" in message


def test_nondeterministic_flag_recorded(seam_slide, tmp_path):
    class Grey(Operation):
        name = "test.nondeterministic_flag_recorded"
        version = "1"
        output = Output.RASTER
        deterministic = False

        def apply(self, tile, context):
            return _mean(tile)

    _slide_run(seam_slide, tmp_path, Grey.name).run()
    assert _stage_record(tmp_path)["nondeterministicOperations"] == [Grey.name]


def test_origin_recorded(seam_slide, tmp_path):
    class Grey(Operation):
        name = "test.origin_recorded"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            return _mean(tile)

    _slide_run(seam_slide, tmp_path, Grey.name).run()
    origins = _stage_record(tmp_path)["operationOrigins"]
    assert origins == {Grey.name: f"{__name__}:test_origin_recorded.<locals>.Grey"}


def test_keyboard_interrupt_during_python_apply(seam_slide, tmp_path):
    fired = []

    class Slow(Operation):
        name = "test.keyboard_interrupt_during_python_apply"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            if not fired:
                fired.append(True)
                _thread.interrupt_main()
                time.sleep(0.2)
            return _mean(tile)

    start = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        _slide_run(seam_slide, tmp_path, Slow.name).run()
    assert time.monotonic() - start < 10
    out = tmp_path / "run" / "out"
    assert not out.exists() or not [p for p in out.iterdir() if p.name.endswith(".partial")]


def test_exit_after_run_is_clean(blob_slide, tmp_path):
    text = one_stage_pipeline("test.exit_after_run_is_clean", {"version": "1"}, writes_raster="grey",
                              exports=[_EXPORT])
    (tmp_path / "pipeline.json").write_text(text, encoding="utf-8")
    code = (
        "import sys\n"
        "import slideio_processor as sp\n"
        "class Grey(sp.Operation):\n"
        "    name = 'test.exit_after_run_is_clean'\n"
        "    version = '1'\n"
        "    output = sp.Output.RASTER\n"
        "    def apply(self, tile, context):\n"
        "        return tile.mean(axis=2, dtype='float32')\n"
        "pipeline = sp.PipelineFile.from_json(open(sys.argv[1], encoding='utf-8').read())\n"
        "sp.SlideRun(pipeline, sys.argv[2], sys.argv[3], sys.argv[4]).run()\n"
    )
    args = [str(tmp_path / "pipeline.json"), str(blob_slide), str(tmp_path / "ws"), str(tmp_path / "out")]
    done = subprocess.run([sys.executable, "-c", code, *args], cwd=tmp_path, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert (tmp_path / "out" / "grey.ome.tif").exists()


def test_tile_context_cannot_be_constructed():
    with pytest.raises(TypeError):
        TileContext()
