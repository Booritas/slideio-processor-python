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
