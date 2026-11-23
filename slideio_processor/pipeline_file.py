"""PipelineFile and ExportSpec: the JSON file that describes a whole slide run."""
import enum
import os

from ._loader import load_native
from .errors import InvalidArgumentError

_native = load_native()


class ExportKind(enum.Enum):
    """What an export writes. The values are the JSON spellings."""

    CSV = "csv"
    GEOJSON = "geojson"
    LABEL_MASK = "labelMask"
    RASTER_TIFF = "rasterTiff"


# In the order of the C++ ExportKind values.
_KINDS = (ExportKind.CSV, ExportKind.GEOJSON, ExportKind.LABEL_MASK, ExportKind.RASTER_TIFF)


# Every str reaches the native module through _text or _path_string. A str pybind11
# cannot convert would raise a TypeError that repeats every argument, and a NUL would
# truncate the C string; both are refused here with fixed text that echoes no value.
def _text(value, what="a name"):
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a str")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidArgumentError(f"{what} is not valid Unicode text") from None
    if "\0" in value:
        raise InvalidArgumentError(f"{what} contains a NUL character")
    return value


def _path_string(path):
    path = os.fspath(path)
    if not isinstance(path, str):
        raise TypeError("the path must be a str or an os.PathLike giving a str")
    return _text(path, "a path")


class ExportSpec:
    """One export of a pipeline file: a kind, a plain output file name, and the layer or
    raster it reads. Build it with csv(), geojson(), label_mask() or raster_tiff()."""

    __slots__ = ("_spec",)

    def __init__(self, native):
        self._spec = native

    @staticmethod
    def csv(file, layer, measurements=()):
        """A CSV export of a layer's objects; no measurements means every measurement."""
        spec = _native._ExportSpec.csv(_text(file), _text(layer))
        for name in measurements:
            spec.measurement(_text(name))
        return ExportSpec(spec)

    @staticmethod
    def geojson(file, layer):
        """A GeoJSON export of a layer's objects."""
        return ExportSpec(_native._ExportSpec.geo_json(_text(file), _text(layer)))

    @staticmethod
    def label_mask(file, layer):
        """A label-mask export of a layer's objects, an OME-TIFF."""
        return ExportSpec(_native._ExportSpec.label_mask(_text(file), _text(layer)))

    @staticmethod
    def raster_tiff(file, raster):
        """A TIFF export of a raster."""
        return ExportSpec(_native._ExportSpec.raster_tiff(_text(file), _text(raster)))

    @property
    def kind(self):
        return _KINDS[self._spec.kind()]

    @property
    def file(self):
        return self._spec.file()

    @property
    def layer(self):
        """The layer an object export reads; None for a raster export."""
        return self._spec.layer()

    @property
    def raster(self):
        """The raster a raster export reads; None for an object export."""
        return self._spec.raster()

    @property
    def measurements(self):
        """The listed measurement names; empty means every measurement."""
        return [self._spec.measurement_at(i) for i in range(self._spec.measurement_count())]

    def __eq__(self, other):
        if not isinstance(other, ExportSpec):
            return NotImplemented
        return self._spec.equals(other._spec)

    __hash__ = None

    def __repr__(self):
        return f"ExportSpec({self.kind.name})"


class PipelineFile:
    """What a slide run does: the Python modules it requires, the layers and rasters it
    declares, its stages, and its exports."""

    __slots__ = ("_file",)

    def __init__(self):
        self._file = _native._PipelineFile()

    @classmethod
    def _wrap(cls, native):
        f = cls.__new__(cls)
        f._file = native
        return f

    @classmethod
    def from_json(cls, text):
        """Reads a file from its JSON text."""
        return cls._wrap(_native._PipelineFile.from_json(_text(text, "the JSON text")))

    @classmethod
    def load(cls, path):
        """Reads a file from a JSON file on disk; path is a str or os.PathLike."""
        return cls._wrap(_native._PipelineFile.load(_path_string(path)))

    def to_json(self):
        """The JSON form; from_json() of it gives an equal file."""
        return self._file.to_json()

    def require(self, module):
        """Adds a required Python module; a name already listed is ignored. Returns self."""
        self._file.require(_text(module))
        return self

    def add_export(self, spec):
        """Adds an export. Returns self."""
        self._file.add_export(spec._spec)
        return self

    @property
    def requires(self):
        return [self._file.require_at(i) for i in range(self._file.require_count())]

    @property
    def layer_names(self):
        """The declared layers, the pixel layer not included."""
        return [self._file.layer_name(i) for i in range(self._file.layer_count())]

    @property
    def raster_names(self):
        return [self._file.raster_name(i) for i in range(self._file.raster_count())]

    @property
    def stage_names(self):
        return [self._file.stage_name(i) for i in range(self._file.stage_count())]

    @property
    def exports(self):
        return [ExportSpec(self._file.export_at(i)) for i in range(self._file.export_count())]

    def __eq__(self, other):
        if not isinstance(other, PipelineFile):
            return NotImplemented
        return self._file.equals(other._file)

    __hash__ = None

    def __repr__(self):
        f = self._file
        return (f"PipelineFile(requires={f.require_count()}, layers={f.layer_count()}, "
                f"rasters={f.raster_count()}, stages={f.stage_count()}, exports={f.export_count()})")
