"""Python operations: a subclass of Operation that sets `name` registers in the library's
operation catalog, and a pipeline file that names it runs it tile by tile."""
import enum
import sys
import threading

import numpy

from ._loader import load_native
from .errors import InvalidArgumentError

_native = load_native()

_INT32_MAX = 2**31 - 1
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

# Keys a stage's parameters give meaning to; a class may not declare them.
_RESERVED = ("version", "targetLayer")

# NumPy dtype -> library DataType name.
_DATA_TYPES = {
    numpy.dtype(numpy.uint8): "Byte",
    numpy.dtype(numpy.int8): "Int8",
    numpy.dtype(numpy.uint16): "UInt16",
    numpy.dtype(numpy.int16): "Int16",
    numpy.dtype(numpy.uint32): "UInt32",
    numpy.dtype(numpy.int32): "Int32",
    numpy.dtype(numpy.uint64): "UInt64",
    numpy.dtype(numpy.int64): "Int64",
    numpy.dtype(numpy.float16): "Float16",
    numpy.dtype(numpy.float32): "Float32",
    numpy.dtype(numpy.float64): "Float64",
}


TileContext = _native.TileContext
TileContext.__module__ = __name__


class Output(enum.Enum):
    """What an operation's apply returns: a label image the library turns into objects,
    or a raster."""

    LABELS = "labels"
    RASTER = "raster"


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parameter_kind(value):
    """The library kind of a default value, or None when it has none. bool before int."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int" if _INT64_MIN <= value <= _INT64_MAX else None
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string" if "\0" not in value else None
    if isinstance(value, (list, tuple)) and all(_is_number(v) for v in value):
        return "doubleArray"
    return None


def _origin(cls):
    """"<module>:<qualname>", plus " <version>" when the module, or failing that its
    top-level package, has a __version__."""
    origin = f"{cls.__module__}:{cls.__qualname__}"
    module = sys.modules.get(cls.__module__)
    version = getattr(module, "__version__", None)
    if version is None:
        package = sys.modules.get(cls.__module__.partition(".")[0])
        version = getattr(package, "__version__", None)
    if version is not None:
        origin += f" {version}"
    return origin


def _snapshot(cls):
    """Validates the declarations of `cls` and returns what the native factory keeps."""
    name = cls.__dict__["name"]
    label = f"operation {name!r}: class {cls.__qualname__}"

    def refuse(text):
        raise InvalidArgumentError(f"{label}: {text}")

    def text(attribute):
        value = getattr(cls, attribute, None)
        if not isinstance(value, str) or not value or "\0" in value:
            refuse(f"{attribute} must be a non-empty str without NUL, got {value!r}")
        return value

    def integer(attribute, minimum, maximum):
        value = getattr(cls, attribute)
        if not _is_int(value) or not minimum <= value <= maximum:
            refuse(f"{attribute} must be an int in [{minimum}, {maximum}], got {value!r}")
        return value

    text("name")
    version = text("version")
    output = getattr(cls, "output", None)
    if not isinstance(output, Output):
        refuse(f"output must be an Output (Output.LABELS or Output.RASTER), got {output!r}")
    halo = integer("halo", 0, _INT32_MAX)
    if not isinstance(cls.deterministic, bool):
        refuse(f"deterministic must be a bool, got {cls.deterministic!r}")
    max_concurrency = cls.max_concurrency
    if max_concurrency is not None:
        max_concurrency = integer("max_concurrency", 1, _INT32_MAX)
    working_set = integer("working_set_bytes_per_pixel", 0, _INT64_MAX)

    parameters = cls.parameters
    if not isinstance(parameters, dict):
        refuse(f"parameters must be a dict, got {type(parameters).__name__}")
    kinds = {}
    for key, value in parameters.items():
        if not isinstance(key, str) or not key or "\0" in key:
            refuse(f"parameters: the key {key!r} must be a non-empty str without NUL")
        if key in _RESERVED:
            refuse(f"parameters: the key {key!r} is reserved")
        kind = _parameter_kind(value)
        if kind is None:
            refuse(f"parameters: the default of {key!r} must be a bool, int, float, str or a list of numbers, "
                   f"got {type(value).__name__}")
        kinds[key] = kind

    output_type, output_planes = "Int32", 1
    if output is Output.RASTER:
        dtype = None
        if cls.output_dtype is not None:  # numpy.dtype(None) is float64
            try:
                dtype = numpy.dtype(cls.output_dtype)
            except (TypeError, ValueError):
                pass
        if dtype not in _DATA_TYPES:
            refuse(f"output_dtype {cls.output_dtype!r} is not one of "
                   + ", ".join(str(d) for d in _DATA_TYPES))
        output_type = _DATA_TYPES[dtype]
        output_planes = integer("output_planes", 1, _INT32_MAX)

    if not callable(getattr(cls, "apply", None)) or cls.apply is Operation.apply:
        refuse("apply is not defined")

    return {
        "name": name,
        "version": version,
        "output": output.value,
        "halo": halo,
        "deterministic": cls.deterministic,
        "max_concurrency": max_concurrency or 0,
        "parameter_kinds": kinds,
        "output_type": output_type,
        "output_planes": output_planes,
        "working_set_bytes_per_pixel": working_set,
        "origin": _origin(cls),
    }


class Operation:
    """Base class of Python operations.

    A subclass whose own body sets `name` is registered in the library's operation
    catalog when it is defined; one without is an abstract intermediate. Defining a class
    again under the same name replaces the earlier one. Class attributes:

    name, version (non-empty str), output (Output): required.
    halo (int >= 0, 0): level pixels added on each side of the tile.
    deterministic (bool, True).
    max_concurrency (None or int >= 1, None): concurrent apply calls across the process.
    parameters (dict, {}): each key's default, a bool, int, float, str or list of numbers.
    output_dtype ("float32") and output_planes (1): the raster apply returns (RASTER only).
    working_set_bytes_per_pixel (int >= 0, 32): working memory per level pixel.

    A stage that uses the operation carries "version" equal to the class's version among
    its parameters; stage_parameters() builds them.
    """

    halo = 0
    deterministic = True
    max_concurrency = None
    parameters = {}
    output_dtype = "float32"
    output_planes = 1
    working_set_bytes_per_pixel = 32

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "name" not in cls.__dict__:
            return
        _native._register_python_operation(cls, _snapshot(cls))

    def __init__(self, parameters):
        """Receives the stage's parameters, with defaults for the missing keys and without
        "version". The base does nothing."""

    def apply(self, tile, context):
        """Returns the result for one tile. `tile` is a C-contiguous copy of the input,
        shaped (height, width, planes), over the halo rect; `context` is a TileContext,
        valid only during the call. A RASTER operation returns an array of output_dtype
        shaped (height, width, output_planes), or (height, width) for one plane.
        Subclasses must override it."""
        raise NotImplementedError

    @classmethod
    def stage_parameters(cls, **values):
        """The parameters to put in a stage record: the declared defaults updated with
        `values`, plus "version"."""
        allowed = set(cls.parameters)
        if getattr(cls, "output", None) is Output.LABELS:
            allowed.add("targetLayer")
        for key in values:
            if key not in allowed:
                raise InvalidArgumentError(f"operation {getattr(cls, 'name', None)!r}: unknown parameter {key!r}")
        version = getattr(cls, "version", None)
        if not isinstance(version, str) or not version:
            raise InvalidArgumentError(f"operation {getattr(cls, 'name', None)!r}: class {cls.__qualname__} "
                                       f"has no version")
        result = dict(cls.parameters)
        result.update(values)
        result["version"] = version
        return result


def registered_operations():
    """The names in the library's operation catalog, built-in and Python, in its order."""
    return list(_native._catalog_names())


# Exceptions __init__ or apply raised, as (sequence number, operation name, exception),
# for SlideRun.run to chain as the cause of the TileError (decision 7).
_errors_lock = threading.Lock()
_errors = []
_next_error = 0


def _record_error(name, error):
    global _next_error
    with _errors_lock:
        _errors.append((_next_error, name, error))
        _next_error += 1


def _error_mark():
    """The sequence number the next recorded error gets."""
    with _errors_lock:
        return _next_error


def _take_errors(name, since):
    """Removes every error recorded at or after `since` and returns those of `name`."""
    with _errors_lock:
        taken = [error for number, n, error in _errors if number >= since and n == name]
        _errors[:] = [entry for entry in _errors if entry[0] < since]
        return taken


def _instantiate(cls, values):
    """Builds the instance a stand-in uses: the declared defaults updated with the
    stage's values (without "version")."""
    parameters = dict(cls.parameters)
    parameters.update(values)
    return cls(parameters)
