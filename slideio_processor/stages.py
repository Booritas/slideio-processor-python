"""Pipeline specs, stage definitions and what the stage registry reports about a stage."""
import copy
import dataclasses
import datetime
import enum
import json
import math
import numbers
import types
from typing import List, Mapping, Optional, Tuple

from . import _geometry
from ._loader import load_native
from .errors import InvalidArgumentError
from .operation import Operation
from .pipeline_file import _text

_native = load_native()

_INT32_MAX = 2**31 - 1
_INT64_MIN = -2**63
_INT64_MAX = 2**63 - 1


def _spec_error(error):
    return type(error)(f"PipelineSpec: {error}")


def _spec_text(value, what):
    try:
        return _text(value, what)
    except (TypeError, InvalidArgumentError) as error:
        raise _spec_error(error) from None


def _channels(channels):
    if isinstance(channels, str):
        if channels != "all":
            raise TypeError(f"PipelineSpec: channels must be \"all\" or a sequence of ints, got {channels!r}")
        return "all"
    try:
        items = list(channels)
    except TypeError:
        raise TypeError(f"PipelineSpec: channels must be \"all\" or a sequence of ints, got {channels!r}") from None
    result = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, numbers.Integral):
            raise TypeError(f"PipelineSpec: a channel must be an int, got {item!r}")
        if not 0 <= int(item) <= _INT32_MAX:
            raise InvalidArgumentError(f"PipelineSpec: channel {int(item)} is out of range")
        result.append(int(item))
    return result


def _level(level):
    if isinstance(level, bool) or not isinstance(level, numbers.Integral):
        raise TypeError(f"PipelineSpec: analysis_level must be an int, got {level!r}")
    if not -1 <= int(level) <= _INT32_MAX:
        raise InvalidArgumentError(f"PipelineSpec: analysis_level must be -1 or a level index, got {int(level)}")
    return int(level)


def _parameter(key, value):
    """value as a JSON value PipelineSpec::fromJson accepts as a parameter."""
    if isinstance(value, (bool, str)):
        if isinstance(value, str):
            _spec_text(value, f"parameter {key!r}")
        return value
    if isinstance(value, numbers.Integral):
        if not _INT64_MIN <= int(value) <= _INT64_MAX:
            raise InvalidArgumentError(f"PipelineSpec: parameter {key!r} holds an int out of the 64-bit range")
        return int(value)
    if isinstance(value, numbers.Real):
        value = float(value)
        if not math.isfinite(value):
            raise InvalidArgumentError(f"PipelineSpec: parameter {key!r} holds a non-finite number")
        return value
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, numbers.Real):
                raise TypeError(f"PipelineSpec: parameter {key!r} holds a list with a non-number {item!r}")
            item = float(item)
            if not math.isfinite(item):
                raise InvalidArgumentError(f"PipelineSpec: parameter {key!r} holds a non-finite number")
            result.append(item)
        return result
    raise TypeError(f"PipelineSpec: parameter {key!r} must be a bool, int, float, str or a list of numbers, "
                    f"got {value!r}")


class PipelineSpec:
    """What a stage computes: the channels, the analysis level, an optional rectangle or
    polygon (level-0 pixels) and an ordered list of operations with their parameters.

    channels is "all" or a sequence of channel indices; analysis_level -1 lets the library
    choose. roi is (x, y, width, height); polygon is a sequence of (points, hole) rings,
    the first the outer one. add() appends an operation by catalog name or as an Operation
    subclass and returns the spec, so calls chain. Two specs are equal when the library's
    PipelineSpec::equals says so (key order and float formatting do not matter).
    """

    __slots__ = ("_data",)
    __hash__ = None

    def __init__(self, *, channels="all", analysis_level=-1, roi=None, polygon=None):
        data = {"specVersion": 1, "channels": _channels(channels), "analysisLevel": _level(analysis_level)}
        if roi is not None:
            try:
                data["roi"] = list(_geometry.rect(roi))
            except InvalidArgumentError as error:
                raise _spec_error(error) from None
        if polygon is not None:
            try:
                data["polygon"] = [{"hole": hole, "points": points.tolist()}
                                   for points, hole in _geometry.rings(polygon)]
            except InvalidArgumentError as error:
                raise _spec_error(error) from None
        data["operations"] = []
        self._data = data

    def add(self, operation, **parameters) -> "PipelineSpec":
        """Appends an operation: a catalog name with its parameters, or an Operation
        subclass, whose stage_parameters(**parameters) (its defaults, the values given and
        "version") become the parameters. Parameter values are bool, int, float, str or a
        list or tuple of numbers. Nothing is resolved here; an unknown name fails when the
        stage runs."""
        if isinstance(operation, type) and issubclass(operation, Operation):
            name = _spec_text(getattr(operation, "name", None), "the operation's name")
            parameters = operation.stage_parameters(**parameters)
        elif isinstance(operation, str):
            name = _spec_text(operation, "the operation name")
            if not name:
                raise InvalidArgumentError("PipelineSpec: the operation name is empty")
        else:
            raise TypeError(f"PipelineSpec: the operation must be a name or an Operation subclass, got {operation!r}")
        converted = {}
        for key, value in parameters.items():
            _spec_text(key, "a parameter name")
            converted[key] = _parameter(key, value)
        self._data["operations"].append({"name": name, "parameters": converted})
        return self

    @property
    def operations(self) -> List[Tuple[str, dict]]:
        """The operations in order, as (name, parameters); copies."""
        return [(op["name"], copy.deepcopy(op["parameters"])) for op in self._data["operations"]]

    def _native(self):
        return _native._PipelineSpec.from_json(json.dumps(self._data))

    def to_json(self) -> str:
        """The library's canonical JSON for this spec (specVersion 1)."""
        return self._native().to_json()

    @classmethod
    def from_json(cls, text) -> "PipelineSpec":
        """Reads a spec from its JSON form, as to_json() writes it or a stage record holds
        it; InvalidArgumentError names what is wrong."""
        text = _spec_text(text, "the JSON text")
        spec = cls.__new__(cls)
        spec._data = json.loads(_native._PipelineSpec.from_json(text).to_json())
        spec._data.setdefault("operations", [])
        return spec

    def __eq__(self, other):
        if not isinstance(other, PipelineSpec):
            return NotImplemented
        return self._native().equals(other._native())

    def __repr__(self):
        return f"PipelineSpec.from_json({json.dumps(self._data)!r})"


class LayerRasterContent(enum.IntEnum):
    """What a layer read as a raster holds per pixel: the covering object's label, 1 inside
    any object, or the covering object's packed anchor (-1 where none)."""

    LABEL = 0
    MEMBERSHIP = 1
    OBJECT_ANCHOR = 2


class StageStatus(enum.IntEnum):
    PENDING = 0
    RUNNING = 1
    COMPLETE = 2
    FAILED = 3


class RunBlocker(enum.IntEnum):
    """The first reason a stage cannot run, in the order a stage run refuses them."""

    NONE = 0
    UNREADABLE = 1
    UNRESOLVED_DEPENDENCY = 2
    DEPENDENCY_INCOMPLETE = 3
    DEPENDENCY_STALE = 4


def _definition_name(value, what):
    try:
        return _text(value, what)
    except (TypeError, InvalidArgumentError) as error:
        raise type(error)(f"StageDefinition: {error}") from None


def _names(value, what):
    if isinstance(value, str):
        raise TypeError(f"StageDefinition: {what} must be a sequence of names, not a str ({value!r})")
    try:
        items = tuple(value)
    except TypeError:
        raise TypeError(f"StageDefinition: {what} must be a sequence of names, got {value!r}") from None
    return tuple(_definition_name(item, f"a name in {what}") for item in items)


def _mapping(value, what):
    if not isinstance(value, Mapping):
        raise TypeError(f"StageDefinition: {what} must be a mapping, got {value!r}")
    return dict(value)


@dataclasses.dataclass(frozen=True)
class StageDefinition:
    """A stage by names: what it computes (pipeline) and the layers and rasters it reads
    and writes. reads_rasters maps a binding name to a raster; reads_layers_as_raster maps a
    binding name to (layer, LayerRasterContent). The workspace resolves every name when the
    stage is declared."""

    name: str
    pipeline: PipelineSpec
    reads_layers: Tuple[str, ...] = ()
    writes_layers: Tuple[str, ...] = ()
    annotates_layers: Tuple[str, ...] = ()
    depends_on: Tuple[str, ...] = ()
    writes_raster: Optional[str] = None
    reads_rasters: Mapping[str, str] = dataclasses.field(default_factory=dict)
    reads_layers_as_raster: Mapping[str, Tuple[str, LayerRasterContent]] = dataclasses.field(default_factory=dict)
    restrict_to_layer: Optional[str] = None

    def __post_init__(self):
        put = object.__setattr__
        put(self, "name", _definition_name(self.name, "the stage name"))
        if not self.name:
            raise InvalidArgumentError("StageDefinition: the stage name is empty")
        if not isinstance(self.pipeline, PipelineSpec):
            raise TypeError(f"StageDefinition: pipeline must be a PipelineSpec, got {self.pipeline!r}")
        for field in ("reads_layers", "writes_layers", "annotates_layers", "depends_on"):
            put(self, field, _names(getattr(self, field), field))
        for field in ("writes_raster", "restrict_to_layer"):
            value = getattr(self, field)
            if value is not None:
                put(self, field, _definition_name(value, field))
        rasters = {}
        for binding, raster in _mapping(self.reads_rasters, "reads_rasters").items():
            rasters[_definition_name(binding, "a binding in reads_rasters")] = _definition_name(
                raster, "a raster in reads_rasters")
        put(self, "reads_rasters", types.MappingProxyType(rasters))
        layers = {}
        for binding, entry in _mapping(self.reads_layers_as_raster, "reads_layers_as_raster").items():
            binding = _definition_name(binding, "a binding in reads_layers_as_raster")
            try:
                layer, content = entry
            except (TypeError, ValueError):
                raise TypeError(f"StageDefinition: reads_layers_as_raster[{binding!r}] must be "
                                f"(layer, LayerRasterContent), got {entry!r}") from None
            try:
                content = LayerRasterContent(content)
            except ValueError:
                raise InvalidArgumentError(f"StageDefinition: reads_layers_as_raster[{binding!r}]: "
                                           f"{content!r} is not a LayerRasterContent") from None
            layers[binding] = (_definition_name(layer, "a layer in reads_layers_as_raster"), content)
        put(self, "reads_layers_as_raster", types.MappingProxyType(layers))


@dataclasses.dataclass(frozen=True)
class StageInfo:
    """A declared stage as the registry reports it. completed_at is when it last completed
    (UTC), None if never; dependencies are the stages it depends on, unresolved_dependencies
    the depends_on names no stage carries yet; blocker is why it cannot run (blocker_name the
    stage concerned); record is its stored configuration JSON."""

    name: str
    status: StageStatus
    stale: bool
    deterministic: bool
    completed_at: Optional[datetime.datetime]
    dependencies: Tuple[str, ...]
    unresolved_dependencies: Tuple[str, ...]
    runnable: bool
    blocker: RunBlocker
    blocker_name: Optional[str]
    record: str


def _info(row):
    (name, status, stale, deterministic, completed_ms, dependencies, unresolved, runnable, blocker,
     blocker_name, record) = row
    completed_at = None
    if completed_ms != 0:
        completed_at = datetime.datetime.fromtimestamp(completed_ms / 1000, tz=datetime.timezone.utc)
    return StageInfo(name, StageStatus(status), stale, deterministic, completed_at, tuple(dependencies),
                     tuple(unresolved), runnable, RunBlocker(blocker), blocker_name, record)


def _declare(handle, definition):
    """Declares definition in the native workspace handle and returns the stage's name."""
    if not isinstance(definition, StageDefinition):
        raise TypeError(f"Workspace: the stage must be a StageDefinition, got {definition!r}")
    spec = definition.pipeline._native()
    return _native._declare_stage(
        handle, definition.name, spec,
        list(definition.reads_layers), list(definition.writes_layers), list(definition.annotates_layers),
        list(definition.depends_on), definition.writes_raster,
        list(definition.reads_rasters.items()),
        [(binding, layer, int(content)) for binding, (layer, content) in definition.reads_layers_as_raster.items()],
        definition.restrict_to_layer)
