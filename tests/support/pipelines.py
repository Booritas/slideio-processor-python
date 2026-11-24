"""Builds pipeline-file JSON for tests, in the form of tests/data/three_stage.json."""
import json


def one_stage_pipeline(operation, parameters, *, writes_layer=None, writes_raster=None, raster_type="Float32",
                       raster_planes=1, requires=(), exports=()):
    """Returns pipeline-file JSON with one stage, "python", that runs `operation` with
    `parameters` over all channels at the base level. It writes the layer `writes_layer`
    or the raster `writes_raster` (of `raster_type` and `raster_planes`), which the file
    declares. `exports` are export dicts as the file holds them."""
    layers = [{"name": writes_layer}] if writes_layer is not None else []
    rasters = ([{"name": writes_raster, "type": raster_type, "planes": raster_planes}]
               if writes_raster is not None else [])
    stage = {
        "stageRecordVersion": 1,
        "stage": "python",
        "readsLayers": [],
        "writesLayers": [writes_layer] if writes_layer is not None else [],
        "annotatesLayers": [],
        "restrictToLayer": None,
        "readsLayersAsRaster": [],
        "writesRaster": writes_raster,
        "readsRasters": [],
        "dependsOn": [],
        "pipeline": {
            "specVersion": 1,
            "channels": "all",
            "analysisLevel": -1,
            "operations": [{"name": operation, "parameters": parameters}],
        },
    }
    return json.dumps({
        "formatVersion": 1,
        "requires": list(requires),
        "layers": layers,
        "rasters": rasters,
        "stages": [stage],
        "exports": list(exports),
    })
