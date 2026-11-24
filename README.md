# slideio-processor

Python bindings for the C++ library slideio-processor: run a pipeline file against a slide
scene, from Python or from the command line.

## Install

```
pip install slideio-processor
```

The package pins `slideio==2.11.1` and uses that copy of SlideIO; install the `slideio`
version this package pins. Only one SlideIO is loaded per process.

## Python

```python
from slideio_processor import PipelineFile, SlideRun

pipeline = PipelineFile.load("pipeline.json")
result = SlideRun(pipeline, "slide.svs", "workspace", "out", slide_id="case-1").run(
    progress=lambda name, fraction, done, total: None)
for stage in result.stages:
    print(stage.name, stage.ran, stage.objects_written)
```

## Command line

```
slideio-processor run --pipeline <file> --slide <path> [--scene <index>] \
    --workspace <dir> --out <dir> [--slide-id <id>] [--quiet]
```

`python -m slideio_processor` is equivalent. stdout gets one line per stage and per export;
stderr gets progress unless `--quiet`. Exit codes: 0 success, 1 a processing error
(`error: <ClassName>: <message>`), 2 usage error, 130 interrupted.

The slide id is the identifier the CSV and GeoJSON exports carry; the default is empty, which
means they carry none. Pass `--slide-id` to identify the slide there. Choose an id that is not
patient-identifying.

## Python operations

Subclass `Operation` to run your own code tile by tile inside a pipeline file. Defining the
class registers it, by `name`, for every `SlideRun` in the process.

```python
import numpy
from skimage.measure import label
from slideio_processor import Operation, Output


class NucleiSegmenter(Operation):
    name = "myLab.nucleiSegmenter"
    version = "1.0"
    output = Output.LABELS          # or Output.RASTER
    halo = 32                       # level pixels of context on all four sides
    max_concurrency = 1             # e.g. one model on one GPU
    parameters = {"threshold": 0.5}

    def __init__(self, parameters):  # once per stage, on the first tile
        super().__init__(parameters)
        self.threshold = parameters["threshold"]

    def apply(self, tile, context):
        # tile: a C-contiguous copy, shape (height, width, planes), over the halo rect.
        # Return int32 labels of shape (height, width) over the same rect.
        mask = tile.mean(axis=2) > self.threshold * 255
        return label(mask).astype(numpy.int32)
```

In the pipeline file, a stage's parameters carry the class's `version`, so that changing the
version changes what the file declares. `NucleiSegmenter.stage_parameters(threshold=0.4)` builds
that dict: `{"version": "1.0", "threshold": 0.4}`. A run refuses a stage whose `version` is
missing or differs from the class's, before it creates the workspace.

The module that defines the class goes in the pipeline file's `requires` list, so that
`SlideRun.run` imports it (and so registers the class) before the run starts:

```python
pipeline = PipelineFile.from_json(text)   # or build one with PipelineFile()
pipeline.require("mylab.models")          # the module that defines NucleiSegmenter
```

- `Output.LABELS`: `apply` returns `int32` labels, `(H, W)` or `(H, W, 1)`; the library turns
  them into objects, joining those that cross tile borders. `Output.RASTER`: `apply` returns an
  array of `output_dtype` (default `float32`) and shape `(H, W, output_planes)`, or `(H, W)`
  for one plane. `H` and `W` are the halo rect's size. Arrays are checked, never cast.
- `context` is a `TileContext`: `tile_rect`, `halo_rect`, `level`, `level_scale`,
  `pixel_size_um` and `cancelled`.
- Put the modules that define operations in the file's `requires` list (`PipelineFile.require`).
  `SlideRun.run` imports them first, on the calling thread; a module whose import raises
  (`ImportError` or any other exception) gives an `InvalidArgumentError` naming it, chained to
  the original exception, before the workspace is touched. The command line adds nothing to
  `sys.path`: the modules must be installed or on `PYTHONPATH`.
- **Labels and halo.** With `Output.LABELS`, an object that touches a halo edge inside the level
  is dropped and counted in `truncated_objects`. Set `halo` at least as large as the largest
  object.
- **Concurrency.** `max_concurrency` limits concurrent `apply` calls; `__init__` is not covered
  by it. Each `apply` call may run on a fresh Python thread state, so do not rely on
  `threading.local` across tiles.
- **Ctrl-C.** The run stops after the current `apply` or `__init__` returns; `context.cancelled`
  lets a long `apply` stop early.
- **Staleness.** A stage is stale when what the file declares differs from its record.
  Changing an operation's code without bumping `version` does not make the stage stale.
- **Failures.** When `__init__` or `apply` raises, `SlideRun.run` raises a `StageError` naming
  the operation (`operation_name`), with your exception as its `__cause__`. The cause is
  attached by operation name, so two runs of the same operation at once in one process may
  attach each other's exception, and a concurrent run's records may be cleared. When another
  tile of the same operation raised too, its exception may be attached instead. Run one slide
  per process.
- Redefining a class under a registered name replaces it, as when a notebook cell is re-run;
  a running stage keeps its class, and later stages of the same run use the class registered
  when they start. The replaced class stays alive: it is never released.

## Workspaces

A `Workspace` is a directory that binds one slide scene to a store. Build a pipeline in it
step by step, look at the results, export them, and save what you built as a pipeline file
for `SlideRun`:

```python
import slideio
from slideio_processor import (ExportSpec, PipelineFile, PipelineSpec, StageDefinition,
                               Workspace)

slide = slideio.open_slide("slide.png", "GDAL")   # the driver for the slide's format
with Workspace.create("workspace", slide.get_scene(0), scene=0) as ws:
    ws.declare_layer("nuclei")
    ws.declare_stage(StageDefinition(
        "nuclei",
        PipelineSpec()
        .add("pixel.colour_convert", **{"from": "Rgb", "to": "Grey"})
        .add("pixel.threshold_fixed", value=128.0, direction="Below")
        .add("segment.connected_components"),
        writes_layers=("nuclei",)))
    ws.run_stage("nuclei")

    print(ws.count(layer="nuclei"))
    table = ws.dataframe(layer="nuclei")             # needs pandas
    print(ws.summary(layer="nuclei", measurements=["area"]).measurements["area"].mean)

    ws.export([ExportSpec.csv("nuclei.csv", "nuclei"),
               ExportSpec.geojson("nuclei.geojson", "nuclei")], "out", slide_id="case-1")

    with open("pipeline.json", "w", encoding="utf-8") as f:
        f.write(PipelineFile.from_workspace(ws)
                .add_export(ExportSpec.csv("nuclei.csv", "nuclei")).to_json())
```

- **Slides.** `Workspace.create(path, slide)` takes a slide path (`scene=` defaults to 0) or an
  open `slideio.Scene`, with `scene=` then required: a `Scene` cannot tell its own index.
  SlideIO has no automatic driver; name the one for the format ("GDAL" for PNG and JPEG).
- **Transformed scenes.** A scene from `slideio.transform_scene` reports its origin's file
  path, so a workspace accepts it; reopening that workspace by path binds the untransformed
  slide.
- **Declarations.** Layers and rasters are named by `str`. Declaring one again with the same
  definition is accepted and changes nothing; another definition raises
  `InvalidArgumentError`. `Workspace.open_read_only` takes no writer lock; it refuses every
  declare and drop.
- **Closing.** A closed workspace still answers `path`, `slide_path`, `scene_index`,
  `read_only` and `closed`; every other member raises `IoError`, and so does the next fetch of
  a result iterator that outlived it.
- **One long call at a time.** `run_stage` and `export` are the long calls: one runs per
  `Workspace` object at a time, and a second one, or `close()`, raises `InvalidArgumentError`
  meanwhile. Queries may run alongside from other threads. Ctrl-C cancels a long call and raises
  `KeyboardInterrupt` once it has stopped; a `Cancellation` raises `CancelledError`. A
  cancellation that arrives during a stage's last operation may let the stage complete (a
  library limitation); a cancelled export leaves no file.
- **Queries.** `count`, `objects` (records), `batches` (NumPy columns), `dataframe` and
  `summary` take an `ObjectQuery` or its fields as keywords. `at_least` and `at_most` name
  measurements without checking them: an unknown name matches nothing.
- **Measurement values only.** Records, batches and DataFrames carry measurement values as
  `float`, without units (the CSV export carries the units). A measurement an object lacks is
  `NaN` in batches and DataFrames and absent from a record.
- **No raster read-back.** Rasters are declared, written by stages and exported as TIFF;
  reading one into NumPy is not offered yet.
- **Exports.** `export` writes the same files a `SlideRun` with the same `ExportSpec` writes,
  and works on a read-only workspace. A label mask holds each object's label value; the
  built-in operations attach no labels, so their objects rasterise as 0.
- **Pipeline files.** `PipelineFile.from_workspace(ws)` takes the layers, rasters and stages
  (not the exports) and requires the module of each Python operation the stages use. An
  operation defined in `__main__` (a notebook or a script) is not required: move it to a module
  before running the file elsewhere.
- **Slide paths in logs and exports.** Log messages may carry slide and workspace paths, and
  exports carry whatever `slide_id` the caller passes (a path, if one is passed). A log file
  (`configure_logging(file=...)`), the Python log records and the export files therefore fall
  under the deployment's HIPAA Security Rule and GDPR controls for their storage and access.
  Choose a `slide_id` that is not patient-identifying.
