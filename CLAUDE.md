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
  `--online` also checks that tag `vX.Y.Z` of `Booritas/slideio` is the nested submodule commit.
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

No exception message, CLI output or log line carries a path, a slide name or a value from
the slide. The Python layer adds only fixed text and stage, layer, export and operation names.
