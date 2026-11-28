# slideio-processor-python

Python bindings (pybind11) for the C++ library slideio-processor, which is the submodule
`extern/slideio-processor`. Package `slideio-processor`, import name `slideio_processor`,
native module `slideio_processor_native` in `slideio_processor/_libs/`.

## Build and test (Windows, conda env `slideio`, Visual Studio 17 2022 x64)

```
$env:SLIDEIO_INSTALL_DIR='D:/Projects/slideio/slideio-processor/build/slideio-install/release'
python build_deps.py
python -m pip wheel . --no-deps -w dist
python -m pip install --no-deps --force-reinstall dist/slideio_processor-0.1.0-*.whl
cd <an empty directory outside the checkout>; python -m pytest <repo>/tests -v
```

- `./build.sh` (Git Bash, Linux, macOS) runs `build_deps.py` once and builds one wheel per
  Python: every 3.9–3.15 it finds, or only `--python 3.12` (repeatable; a version or an
  interpreter path). `--test-helpers`, `--skip-deps`, `-o <dir>`; see `./build.sh --help`.
  `setup.py` keeps one CMake tree per interpreter (`build/cmake-cpython-312`, ...).
- `build_deps.py` prepares SlideIO and the Conan dependencies and writes `build/deps.json`.
  Without `SLIDEIO_INSTALL_DIR` it builds SlideIO from the nested submodule into
  `build/slideio-install` (slow). `setup.py` fails until `build/deps.json` exists.
- The `slideio` wheel the package pins (2.11.1) must be installed first; it is not on PyPI.
  Get it from `Booritas/slideio-dist`, branch `2.11.1`, folder `2.11.1/<platform>/`.
- **Tests run against the installed wheel, from a directory outside the checkout**, so
  `import slideio_processor` never resolves to the source tree. Avoid `%TEMP%` itself if it
  holds stray `.py` files that shadow the standard library.

## Pins and CI

- `python scripts/check_pins.py` checks that the `slideio==X.Y.Z` pin in `pyproject.toml`
  equals `SLIDEIO_VERSION` of the nested SlideIO (`extern/slideio-processor/extern/slideio`);
  `--online` also checks that `Booritas/slideio-dist` has branch `X.Y.Z` and that its last
  commit is no older than the nested SlideIO submodule commit (the wheels are republished
  under the same version when SlideIO master moves; the `vX.Y.Z` tag may lag behind).
- `.github/workflows/build.yml` builds the wheel with `SLIDEIO_PROCESSOR_PYTHON_TEST_HELPERS=1`
  on Windows (`windows-2022`), manylinux_2_28 x86_64 and macOS arm64 (`macos-15`, as the
  processor's own CI), runs the tests from outside the checkout and then
  `check_pins.py --online`. On Windows and macOS the Conan cache and `build/slideio-install`
  are cached, keyed on the submodule commits. The manylinux job runs everything in one
  `docker run` of `booritas/slideio-manylinux_2_28_x86_64:2.10.0`, as the processor's CI
  does: the bare pypa image lacks the -devel packages SlideIO's Conan graph needs. That image
  puts cp311 first on PATH, so the job puts cp312 first (the `slideio` wheel is cp312).
- `slideio` comes from `Booritas/slideio-dist` until the pinned version is on PyPI; then
  switch the workflow to `pip install slideio==<pin>`.
- A wheel rebuilt under the same version (the `slideio` wheel, or this package's) needs
  `--force-reinstall --no-cache-dir`, or pip keeps the copy it already has.

## D2: one SlideIO per process

- The wheel ships **no SlideIO library** and no MSVC runtime: `_libs/` holds exactly the
  native module, `slideio-processor-data` and `slideio-processor-engine`. SlideIO comes from
  the installed `slideio` package, `slideio/core/libs`.
- `slideio==2.11.1` is an exact pin. `_loader.load_native()` imports `slideio` first,
  compares `slideio.get_version()` with the version the wheel was built against and raises
  `ImportError` on a mismatch, then (Windows) calls `os.add_dll_directory(<slideio>/core/libs)`
  and keeps the handle for the life of the process.
- Linux run path `$ORIGIN:$ORIGIN/../../slideio/core/libs`, macOS
  `@loader_path;@loader_path/../../slideio/core/libs`, used in the build tree too.
- Nothing in CMake or `setup.py` may copy a SlideIO library into the build output or wheel.
  `tests/test_import.py::test_one_slideio_copy` checks every loaded SlideIO image.
- Built with `SLIDEIO_PROCESSOR_TEST_SEAMS=OFF`.

## Messages

Messages may carry paths and argument values (decision 2026-11-23: the software runs in a
secure environment). Argument validation still runs before native calls, so a bad argument gives
an InvalidArgumentError naming the value (repr) instead of pybind11's overload noise.

## Python operations

Code: `slideio_processor/operation.py` (`Operation`, registration, declarations, error
records), `src/python_operation.cpp` (factory and stand-in operations), `src/python_runtime.*`
(shared helpers), `slideio_processor/run.py` (`requires` import, cause chaining). Tests:
`tests/test_operation_*.py`; `tests/data/opmodule/sample_ops.py` is the `requires` fixture.

- GIL rules: no `OperationFactory` method touches Python (the factory answers from a C++
  snapshot of the class's declarations). A stand-in takes the GIL only around `__init__`,
  `apply`, array conversions and recording an error; it waits on the construction mutex and on
  the `max_concurrency` semaphore with the GIL released (mutex before GIL, never the reverse).
- Decision 4: one Python instance per stand-in, built on its first tile.
- Decision 7: the cause is chained through a Python-side record list keyed by operation name
  (`_record_error`, `_take_errors`); `SlideRun.run` attaches the first record since its mark as
  `__cause__`. Concurrent runs of one operation in a process can cross-attach; documented.
- Decision 9: the factory keeps its class as a strong reference never released. Exit gate:
  an atexit handler, `closePython` (`src/python_runtime.cpp`), closes entry to Python; once it
  has run, stand-ins no longer enter Python and leak their instances. It waits, GIL released,
  for threads inside Python to leave.

## Workspaces and results

Code: `slideio_processor/workspace.py` (`Workspace`: create/open over a path or a
`slideio.Scene`, layers, rasters, stages, `run_stage`, queries, `export`),
`slideio_processor/configuration.py` (`Configuration`, `LogLevel`, `configure_logging`),
`slideio_processor/stages.py` (`PipelineSpec`, `StageDefinition`, `StageInfo`),
`slideio_processor/objects.py` (`ObjectQuery`, records, batches, DataFrames, summaries),
`slideio_processor/_runner.py` (`run_interruptibly`, `operation_causes`, shared with
`SlideRun`), `slideio_processor/_geometry.py`. Native: `src/configuration.cpp`,
`src/workspace.cpp`, `src/stages.cpp`, `src/objects.cpp`, `src/exports.cpp` (`_export`, through
the library's `writeExport`, the function `SlideRun`'s exports use), `src/progress.hpp/.cpp`
(`PythonProgress`, `raiseKept`, shared by runs, stage runs and exports). Tests:
`tests/test_configuration.py`, `test_logging.py`, `test_workspace.py`, `test_stages.py`,
`test_run_stage.py`, `test_objects.py`, `test_scene_handle.py`, `test_workspace_export.py`;
`tests/support/workspaces.py` and the `nuclei_workspace` fixture.

- Names, not ids: every public call takes and returns layer, stage and raster names; the
  native layer resolves them and an unknown name raises `InvalidArgumentError` naming it.
- GIL rule: every native call that reaches the store, the workspace, a cursor, a stage run or an
  export runs with the GIL released (`py::gil_scoped_release` or
  `py::call_guard<py::gil_scoped_release>`); Python objects are converted before and after,
  with the GIL held. Waiting on a library lock with the GIL held can deadlock against a worker
  thread that logs through the Python sink.
- Long-call lock (decision 8): `run_stage` and `export` are the long calls, one per `Workspace`
  object at a time. `Workspace._state` guards `_closed` and `_running`; `_begin_long_call`
  re-checks closed under it, so `close()` and the start of a long call decide atomically, and
  `close()` during a long call raises `InvalidArgumentError`. Every declare, drop and
  `mark_stale` runs its native call inside `_mutation()`, which holds `_state` and refuses
  with `InvalidArgumentError` while a long call runs (validate arguments before entering it:
  `_state` is not reentrant). Long calls run through `run_interruptibly` on a helper thread,
  so Ctrl-C cancels them; after the first Ctrl-C it waits for the call to finish, swallowing
  further Ctrl-Cs, so `_end_long_call` never runs under a live native call. Queries run
  alongside.
- A cursor keeps its native workspace alive; after `close()` its next fetch raises `IoError`.
- Log sink (decision 6): `configure_logging(python=True)` installs `PythonLogSink`
  (`src/configuration.cpp`), which enters Python only through the exit gate
  (`enterPython`/`leavePython`, `src/python_runtime.cpp`) and holds no Python object: once
  `closePython` has run at exit, it drops messages instead of taking the GIL.
- Exports: `Workspace.export` makes the out directory, writes each spec to
  `os.path.join(out, spec.file)` and raises `CancelledError("Workspace: the <kind> export was
  cancelled")` for a cancelled one, on the helper thread so a Ctrl-C chains from it (the library
  leaves no file of that export; earlier specs' files stay). `PythonProgress` enters Python
  through the exit gate, as the log sink does. `slide_id` defaults to `""`.
