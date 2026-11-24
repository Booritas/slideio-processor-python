"""The project-2 consumer test (decision 12): a workspace over a slideio.Scene reads its
pixels through the shared handle after every Python slide object is gone."""
import gc

import slideio
from slideio_processor import PipelineSpec, StageDefinition, Workspace
from support.workspaces import nuclei_definition


def _measured_nuclei():
    """Stage "nuclei" over all three channels: each is thresholded below 128, plane 0 (red)
    is segmented and planes 1 and 2 are measured. connected_components measures the samples
    of the channels its planes 1 to 3 carry, as the scene holds them, not the thresholded
    planes; the segmented channel 0 is not measured. (nuclei_definition() converts to grey
    first, leaving no channel to measure, so its objects carry no intensity.)"""
    spec = (PipelineSpec()
            .add("pixel.threshold_fixed", value=128.0, direction="Below")
            .add("segment.connected_components"))
    return StageDefinition("nuclei", spec, writes_layers=("nuclei",))


def test_scene_handle_outlives_python_objects(blob_slide, tmp_path):
    slide = slideio.open_slide(str(blob_slide), "GDAL")
    scene = slide.get_scene(0)
    ws = Workspace.create(tmp_path / "ws", scene, scene=0)
    try:
        del scene, slide
        gc.collect()
        ws.declare_layer("nuclei")
        ws.declare_stage(_measured_nuclei())
        assert ws.run_stage("nuclei").objects_written == 9
        records = list(ws.objects(layer="nuclei"))
        assert len(records) == 9
        for record in records:
            # The discs' fill is (20, 20, 20); the thresholded planes would read 255.
            for channel in (1, 2):
                assert abs(record.measurements[f"intensity.{channel}.mean"] - 20) <= 0.5
            assert "intensity.0.mean" not in record.measurements
    finally:
        ws.close()


def test_transformed_scene_is_accepted(blob_slide, tmp_path):
    slide = slideio.open_slide(str(blob_slide), "GDAL")
    scene = slide.get_scene(0)
    blur = slideio.GaussianBlurFilter()
    blur.kernel_size_x = 3
    blur.kernel_size_y = 3
    transformed = slideio.transform_scene(scene, [blur])
    ws = Workspace.create(tmp_path / "ws", transformed, scene=0)
    try:
        del transformed, scene, slide, blur
        gc.collect()
        ws.declare_layer("nuclei")
        ws.declare_stage(nuclei_definition())
        assert ws.run_stage("nuclei").objects_written == 9
        assert ws.count(layer="nuclei") == 9
    finally:
        ws.close()
