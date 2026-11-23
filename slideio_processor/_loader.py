"""Loads the native module against the SlideIO libraries of the installed slideio package.

D2: this package ships no SlideIO library. The native module and the processor's two
libraries in _libs/ use the SlideIO shared libraries in slideio/core/libs, so the
process holds exactly one SlideIO. On Linux and macOS the libraries' run path finds them;
on Windows the directory is added to the DLL search path here.
"""
import os
import sys

_dll_directory = None  # Kept for the life of the process: closing it drops the directory.


def _check_slideio_version(built, loaded):
    """Raises ImportError when the installed SlideIO is not the one the package was built against."""
    if built != loaded:
        raise ImportError(
            f"slideio_processor was built against SlideIO {built}, but the installed slideio "
            f"package provides SlideIO {loaded}; install slideio=={built}")


def load_native():
    """Imports slideio, checks its version and returns the native module."""
    global _dll_directory
    import slideio

    from . import _build_info

    _check_slideio_version(_build_info.SLIDEIO_VERSION, slideio.get_version())
    if sys.platform == "win32" and _dll_directory is None:
        libs = os.path.join(os.path.dirname(os.path.abspath(slideio.__file__)), "core", "libs")
        _dll_directory = os.add_dll_directory(libs)

    from ._libs import slideio_processor_native

    return slideio_processor_native
