"""The slideio-processor command line: `slideio-processor run`."""
import argparse
import sys
import threading

from .errors import ProcessorError
from .pipeline_file import PipelineFile
from .run import SlideRun


class _Progress:
    """Prints "<name>: <p>%" to stderr each time a name crosses a multiple of 10%."""

    def __init__(self, stream):
        self._stream = stream
        self._lock = threading.Lock()
        self._last = {}

    def __call__(self, name, fraction, completed, total):
        decile = min(int(fraction * 10 + 1e-9), 10)
        with self._lock:
            if decile <= self._last.get(name, 0):
                return
            self._last[name] = decile
            print(f"{name}: {decile * 10}%", file=self._stream, flush=True)


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser whose usage errors print fixed text: argparse's own messages
    repeat the offending argument, which may be a path. Subparsers inherit the class."""

    def error(self, message):
        print("usage error: run 'slideio-processor run --help'", file=sys.stderr)
        sys.exit(2)


def _build_parser():
    parser = _Parser(prog="slideio-processor")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run a pipeline file against one slide scene")
    run.add_argument("--pipeline", required=True, help="pipeline JSON file")
    run.add_argument("--slide", required=True, help="slide file")
    run.add_argument("--scene", type=int, default=0, help="scene index (default 0)")
    run.add_argument("--workspace", required=True, help="workspace directory")
    run.add_argument("--out", required=True, help="directory the exports are written to")
    run.add_argument("--slide-id", default="", help="identifier carried by the CSV and GeoJSON exports")
    run.add_argument("--quiet", action="store_true", help="print no progress")
    return parser


def _run(args):
    pipeline = PipelineFile.load(args.pipeline)
    slide_run = SlideRun(pipeline, args.slide, args.workspace, args.out,
                         scene=args.scene, slide_id=args.slide_id)
    result = slide_run.run(progress=None if args.quiet else _Progress(sys.stderr))
    for stage in result.stages:
        if stage.ran:
            print(f"stage {stage.name}: ran, {stage.objects_written} objects")
        else:
            print(f"stage {stage.name}: current, not run")
    for export in result.exports:
        print(f"export {export.file}: {export.bytes} bytes")


def main(argv=None):
    """Runs the command line; returns the exit code (argparse exits 2 on a usage error)."""
    args = _build_parser().parse_args(argv)
    try:
        _run(args)
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    except ProcessorError as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001  (its text may carry a path: print the class only)
        print(f"error: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0
