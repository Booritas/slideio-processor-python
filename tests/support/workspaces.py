"""Builds pipeline specs and stage definitions for the workspace and stage tests."""
from slideio_processor import PipelineSpec, StageDefinition


def nuclei_spec():
    """Grey, a fixed threshold below 128 and connected components: the dark discs of
    blob_slide become objects."""
    return (PipelineSpec()
            .add("pixel.colour_convert", **{"from": "Rgb", "to": "Grey"})
            .add("pixel.threshold_fixed", value=128.0, direction="Below")
            .add("segment.connected_components"))


def nuclei_definition():
    """Stage "nuclei", writing layer "nuclei" with nuclei_spec()."""
    return StageDefinition("nuclei", nuclei_spec(), writes_layers=("nuclei",))
