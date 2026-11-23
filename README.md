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
