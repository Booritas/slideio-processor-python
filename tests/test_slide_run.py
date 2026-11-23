import _thread
import os
import threading
import time

import pytest
import slideio
import slideio_processor
from slideio_processor import (
    Cancellation,
    CancelledError,
    InvalidArgumentError,
    PipelineFile,
    SlideRun,
)
from support.loaded_libraries import loaded_library_paths

_EXPORTS = ["nuclei.csv", "nuclei.geojson", "nuclei-mask.ome.tif", "prob.ome.tif"]


def _run(blob_slide, tmp_path, text, **kw):
    return SlideRun(PipelineFile.from_json(text), blob_slide, tmp_path / "ws", tmp_path / "out", **kw)


def _leftovers(out):
    if not out.exists():
        return []
    return [p.name for p in out.iterdir() if p.name.endswith(".partial")]


def test_first_run_runs_everything_and_exports(blob_slide, three_stage_json, tmp_path):
    r = _run(blob_slide, tmp_path, three_stage_json).run()
    assert [s.name for s in r.stages] == ["tissue", "nuclei", "prob"]
    assert all(s.ran for s in r.stages)
    assert [s.objects_written for s in r.stages] == [9, 9, 0]
    assert [s.truncated_objects for s in r.stages] == [0, 0, 0]
    assert [e.file for e in r.exports] == _EXPORTS
    for e in r.exports:
        assert (tmp_path / "out" / e.file).stat().st_size == e.bytes > 0


def test_result_types_are_frozen(blob_slide, three_stage_json, tmp_path):
    r = _run(blob_slide, tmp_path, three_stage_json).run()
    assert isinstance(r.stages, tuple) and isinstance(r.exports, tuple)
    with pytest.raises(AttributeError):
        r.stages[0].ran = False


def test_second_run_skips_current_stages(blob_slide, three_stage_json, tmp_path):
    _run(blob_slide, tmp_path, three_stage_json).run()
    r = _run(blob_slide, tmp_path, three_stage_json).run()
    assert not any(s.ran for s in r.stages)
    assert [s.objects_written for s in r.stages] == [0, 0, 0]


def test_slide_id_default_empty_and_given(blob_slide, three_stage_json, tmp_path):
    _run(blob_slide, tmp_path, three_stage_json).run()
    for name in ("nuclei.csv", "nuclei.geojson"):
        text = (tmp_path / "out" / name).read_text(encoding="utf-8")
        assert "slide.png" not in text
        assert str(tmp_path) not in text and tmp_path.name not in text

    other = tmp_path / "given"
    other.mkdir()
    SlideRun(PipelineFile.from_json(three_stage_json), blob_slide, other / "ws", other / "out",
             slide_id="S-001").run()
    for name in ("nuclei.csv", "nuclei.geojson"):
        assert "S-001" in (other / "out" / name).read_text(encoding="utf-8")


def test_progress_reports_stages(blob_slide, three_stage_json, tmp_path):
    calls = []
    _run(blob_slide, tmp_path, three_stage_json).run(progress=lambda *a: calls.append(a))
    assert calls
    stages = {"tissue", "nuclei", "prob"}
    # The library also reports its own steps: the seam merge of a segmenting stage and
    # the export writers.
    assert {name for name, *_ in calls} <= stages | {"seam merge", "export", "raster export"}
    assert stages <= {name for name, *_ in calls}
    for _name, fraction, completed, total in calls:
        assert 0.0 <= fraction <= 1.0
        assert 0 <= completed <= total


def test_cancel_before_run(blob_slide, three_stage_json, tmp_path):
    c = Cancellation()
    assert not c.cancelled
    c.cancel()
    assert c.cancelled
    with pytest.raises(CancelledError):
        _run(blob_slide, tmp_path, three_stage_json).run(cancellation=c)
    out = tmp_path / "out"
    for pattern in ("*.csv", "*.geojson", "*.tif", "*.partial"):
        assert not list(out.glob(pattern))


def test_progress_exception_cancels_and_propagates(blob_slide, three_stage_json, tmp_path):
    calls = []

    def progress(*args):
        calls.append(args)
        if len(calls) == 1:
            1 / 0

    with pytest.raises(ZeroDivisionError) as info:
        _run(blob_slide, tmp_path, three_stage_json).run(progress=progress)
    assert isinstance(info.value.__context__, CancelledError)
    assert len(calls) == 1                  # later reports are ignored
    assert not _leftovers(tmp_path / "out")


def test_progress_exception_keeps_context_inside_except_block(blob_slide, three_stage_json, tmp_path):
    def progress(*args):
        1 / 0

    with pytest.raises(ZeroDivisionError) as info:
        try:
            raise ValueError("handled")
        except ValueError:
            _run(blob_slide, tmp_path, three_stage_json).run(progress=progress)
    assert isinstance(info.value.__context__, CancelledError)


def test_keyboard_interrupt_cancels_inside_except_block(blob_slide, three_stage_json, tmp_path):
    c = Cancellation()
    fired = []

    def progress(*args):
        if not fired:
            fired.append(True)
            _thread.interrupt_main()
            deadline = time.monotonic() + 30
            while not c.cancelled and time.monotonic() < deadline:
                time.sleep(0.01)

    with pytest.raises(KeyboardInterrupt) as info:
        try:
            raise ValueError("handled")
        except ValueError:
            _run(blob_slide, tmp_path, three_stage_json).run(progress=progress, cancellation=c)
    assert isinstance(info.value.__cause__, CancelledError)


def test_keyboard_interrupt_cancels(blob_slide, three_stage_json, tmp_path):
    c = Cancellation()
    fired = []

    def progress(*args):
        if not fired:
            fired.append(True)
            _thread.interrupt_main()
            # Hold the run until the main thread has cancelled it, so the cancellation
            # lands while the run is still going.
            deadline = time.monotonic() + 30
            while not c.cancelled and time.monotonic() < deadline:
                time.sleep(0.01)

    with pytest.raises(KeyboardInterrupt) as info:
        _run(blob_slide, tmp_path, three_stage_json).run(progress=progress, cancellation=c)
    assert c.cancelled
    assert isinstance(info.value.__cause__, CancelledError)
    assert not _leftovers(tmp_path / "out")


def test_unknown_operation_is_refused_by_name(blob_slide, three_stage_json, tmp_path):
    text = three_stage_json.replace("segment.connected_components", "mylab.nope")
    with pytest.raises(InvalidArgumentError) as info:
        _run(blob_slide, tmp_path, text).run()
    assert "mylab.nope" in str(info.value)
    assert not (tmp_path / "ws").exists()


def test_workspace_of_another_slide_is_refused_without_path(blob_slide, three_stage_json, tmp_path):
    from PIL import Image

    _run(blob_slide, tmp_path, three_stage_json).run()
    Image.open(blob_slide).save(tmp_path / "other.png")
    run = SlideRun(PipelineFile.from_json(three_stage_json), tmp_path / "other.png", tmp_path / "ws",
                   tmp_path / "out")
    with pytest.raises(InvalidArgumentError) as info:
        run.run()
    message = str(info.value)
    assert str(tmp_path) not in message
    assert "slide.png" not in message and "other.png" not in message


def test_runs_in_background_thread(blob_slide, three_stage_json, tmp_path):
    run = _run(blob_slide, tmp_path, three_stage_json)
    counter = [0]
    seen = []
    results = []
    t = threading.Thread(target=lambda: results.append(run.run(progress=lambda *a: seen.append(counter[0]))))
    t.start()
    while t.is_alive():
        counter[0] += 1
    t.join()
    assert len(results) == 1 and len(results[0].stages) == 3
    # The main thread's loop advanced while the run was reporting: the GIL was released.
    assert len(seen) > 1 and seen[-1] > seen[0]


def test_negative_scene_refused(blob_slide, three_stage_json, tmp_path):
    with pytest.raises(InvalidArgumentError):
        _run(blob_slide, tmp_path, three_stage_json, scene=-1)


def test_processor_libraries_load_from_package(blob_slide, three_stage_json, tmp_path):
    _run(blob_slide, tmp_path, three_stage_json).run()
    ours = os.path.join(os.path.dirname(slideio_processor.__file__), "_libs")
    theirs = os.path.join(os.path.dirname(slideio.__file__), "core", "libs")

    def stem(path):
        return os.path.basename(path).lower().removeprefix("lib")

    loaded = loaded_library_paths()
    for name in ("slideio-processor-data", "slideio-processor-engine"):
        found = [p for p in loaded if stem(p).startswith(name + ".")]
        assert len(found) == 1, name
        assert os.path.samefile(os.path.dirname(found[0]), ours), name
    slideio_libs = [p for p in loaded
                    if stem(p).startswith("slideio") and not stem(p).startswith(("slideio-processor",
                                                                                 "slideio_processor"))]
    assert slideio_libs
    for p in slideio_libs:
        assert os.path.samefile(os.path.dirname(p), theirs), os.path.basename(p)


@pytest.mark.parametrize("scene", [2**31, 99999999999, -2**31 - 1, -99999999999, True, 1.0, "0"])
def test_scene_not_an_int32_refused_without_value(blob_slide, three_stage_json, tmp_path, scene):
    with pytest.raises(InvalidArgumentError) as info:
        _run(blob_slide, tmp_path, three_stage_json, scene=scene)
    message = str(info.value)
    assert "214748364" not in message and "99999999999" not in message
    assert "slide.png" not in message and str(tmp_path) not in message


def test_bad_path_or_slide_id_refused_without_value(blob_slide, three_stage_json, tmp_path):
    f = PipelineFile.from_json(three_stage_json)
    calls = [
        lambda: SlideRun(f, str(tmp_path / "pat\udce9ient.svs"), tmp_path / "ws", tmp_path / "out"),
        lambda: SlideRun(f, str(blob_slide) + "\0x", tmp_path / "ws", tmp_path / "out"),
        lambda: SlideRun(f, blob_slide, str(tmp_path / "w\0s"), tmp_path / "out"),
        lambda: SlideRun(f, blob_slide, tmp_path / "ws", str(tmp_path / "o\udce9ut")),
        lambda: SlideRun(f, blob_slide, tmp_path / "ws", tmp_path / "out", slide_id="S\0-001"),
    ]
    for call in calls:
        with pytest.raises(InvalidArgumentError) as info:
            call()
        message = str(info.value)
        assert str(tmp_path) not in message and "slide.png" not in message and "-001" not in message
    assert not (tmp_path / "ws").exists()


def test_overlapping_run_of_one_object_refused(blob_slide, three_stage_json, tmp_path):
    run = _run(blob_slide, tmp_path, three_stage_json)
    started = threading.Event()
    release = threading.Event()
    results = []
    errors = []

    def progress(*args):
        started.set()
        release.wait(60)

    def first():
        try:
            results.append(run.run(progress=progress))
        except BaseException as error:  # noqa: B036
            errors.append(error)

    t = threading.Thread(target=first)
    t.start()
    try:
        assert started.wait(60)
        with pytest.raises(InvalidArgumentError) as info:
            run.run()
        assert str(info.value) == "SlideRun: a run of this object is already in progress"
    finally:
        release.set()
        t.join()
    assert not errors
    assert len(results) == 1 and all(s.ran for s in results[0].stages)
    # Once the first run has finished, the object runs again.
    assert not any(s.ran for s in run.run().stages)
