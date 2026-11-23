"""Python bindings for slideio-processor."""
from . import _build_info
from ._loader import load_native

_native = load_native()

__version__ = _build_info.VERSION
SLIDEIO_VERSION = _build_info.SLIDEIO_VERSION


def library_version():
    """Returns the version of the slideio-processor C++ library, as "major.minor.patch"."""
    return _native.library_version()


__all__ = ["__version__", "SLIDEIO_VERSION", "library_version"]
