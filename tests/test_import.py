import os

import pytest
import slideio
import slideio_processor
from slideio_processor import _loader
from slideio_processor._libs import slideio_processor_native as native
from support.loaded_libraries import loaded_library_paths


def _slideio_libs():
    return os.path.join(os.path.dirname(slideio.__file__), "core", "libs")


def test_versions():
    assert slideio_processor.SLIDEIO_VERSION == "2.11.1"
    assert slideio_processor.SLIDEIO_VERSION == slideio.get_version()
    assert native.slideio_build_version() == slideio_processor.SLIDEIO_VERSION
    assert slideio_processor.library_version() == "0.1.0"
    assert slideio_processor.__version__.startswith("0.1.")


def test_version_mismatch_raises_import_error_naming_both():
    with pytest.raises(ImportError) as e:
        _loader._check_slideio_version("2.11.1", "2.11.0")
    assert "2.11.1" in str(e.value) and "2.11.0" in str(e.value) and "slideio==2.11.1" in str(e.value)
    _loader._check_slideio_version("2.11.1", "2.11.1")  # no error


def test_ships_no_slideio_library():
    ours = set(os.listdir(os.path.dirname(native.__file__))) - {"__init__.py", "__pycache__"}
    assert not ours & set(os.listdir(_slideio_libs()))
    assert len(ours) == 3                       # module + data + engine
    assert not [f for f in ours if f.lower().startswith(("msvcp", "vcruntime", "concrt", "vccorlib"))]


def test_one_slideio_copy():
    # SlideIO's own libraries only: slideio/core/libs also ships the MSVC runtime, which
    # python.exe has already loaded from its own directory before slideio is imported.
    # Strip "lib" first: on Linux and macOS they are libslideio-*.so / .dylib.
    theirs = {f.lower() for f in os.listdir(_slideio_libs())
              if f.lower().removeprefix("lib").startswith("slideio")}
    loaded = [p for p in loaded_library_paths() if os.path.basename(p).lower() in theirs]
    assert loaded                               # SlideIO is loaded at all
    for p in loaded:
        assert os.path.samefile(os.path.dirname(p), _slideio_libs()), os.path.basename(p)
