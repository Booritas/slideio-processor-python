"""Object queries: the filters, and the results as records, NumPy batches and summaries.

Workspace.count, objects, object, batches, dataframe and summary are built on what is here.
Names (layers, stages, labels, measurements) are str; an unknown one raises
InvalidArgumentError naming it. Coordinates are level-0 pixels.
"""
import dataclasses
import enum
import math
import numbers
import types
from typing import Mapping, Optional, Sequence, Tuple

import numpy

from . import _geometry
from ._loader import load_native
from .errors import InvalidArgumentError

_native = load_native()

_RECORD_FETCH = 1024
_INT64_MAX = 2**63 - 1


class SpatialPredicate(enum.IntEnum):
    """How an ObjectQuery's polygon relates to an object's geometry: INTERSECTS admits an
    object whose closed region meets the polygon's, touching included; CONTAINED_BY one whose
    closed region lies inside or on it."""

    INTERSECTS = 0
    CONTAINED_BY = 1


def _optional_name(value, what):
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"ObjectQuery: {what} must be a str or None, got {value!r}")
    if "\0" in value:
        raise InvalidArgumentError(f"ObjectQuery: {what} contains a NUL character: {value!r}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidArgumentError(f"ObjectQuery: {what} is not valid Unicode: {value!r}") from None
    return value


def _bounds(value, what):
    if not isinstance(value, Mapping):
        raise TypeError(f"ObjectQuery: {what} must be a mapping of measurement names to numbers, got {value!r}")
    result = {}
    for name, bound in value.items():
        if not isinstance(name, str) or not name or "\0" in name:
            raise InvalidArgumentError(f"ObjectQuery: {what}: {name!r} is not a measurement name")
        if isinstance(bound, bool) or not isinstance(bound, numbers.Real):
            raise InvalidArgumentError(f"ObjectQuery: {what}[{name!r}] must be a number, got {bound!r}")
        bound = float(bound)
        if math.isnan(bound):
            raise InvalidArgumentError(f"ObjectQuery: {what}[{name!r}] is NaN")
        result[name] = bound
    return types.MappingProxyType(result)


def _polygon(value):
    """The polygon as a tuple of (tuple of (x, y) int pairs, hole), comparable and hashable."""
    try:
        rings = _geometry.rings(value)
    except InvalidArgumentError as error:
        raise InvalidArgumentError(f"ObjectQuery: within: {error}") from None
    return tuple((tuple((int(x), int(y)) for x, y in points.tolist()), hole) for points, hole in rings)


@dataclasses.dataclass(frozen=True)
class ObjectQuery:
    """The objects a query selects. Every filter given must hold; None (or an empty
    mapping) leaves it out.

    layer, stage and label are names. intersecting is a rectangle (x, y, width, height):
    an object whose bounding box shares a pixel with it is selected. within is a polygon
    (a sequence of (points, hole) rings, the first the outer one), related to each object's
    exact geometry by within_predicate. label matches an object any stage labelled so.
    at_least and at_most bound measurements by name: an object without the measurement fails
    the bound. By default only objects of completed stages are seen; include_in_flight also
    shows those of a stage still running. limit caps the number of objects, after every other
    filter. Objects come in ascending id order, by layer and then by anchor.
    """

    layer: Optional[str] = None
    stage: Optional[str] = None
    intersecting: Optional[Tuple[int, int, int, int]] = None
    within: Optional[Sequence] = None
    within_predicate: SpatialPredicate = SpatialPredicate.INTERSECTS
    label: Optional[str] = None
    at_least: Mapping[str, float] = dataclasses.field(default_factory=dict)
    at_most: Mapping[str, float] = dataclasses.field(default_factory=dict)
    include_in_flight: bool = False
    limit: Optional[int] = None

    def __post_init__(self):
        put = object.__setattr__
        put(self, "layer", _optional_name(self.layer, "layer"))
        put(self, "stage", _optional_name(self.stage, "stage"))
        put(self, "label", _optional_name(self.label, "label"))
        if self.intersecting is not None:
            try:
                put(self, "intersecting", _geometry.rect(self.intersecting))
            except InvalidArgumentError as error:
                raise InvalidArgumentError(f"ObjectQuery: intersecting: {error}") from None
        if self.within is not None:
            put(self, "within", _polygon(self.within))
        try:
            put(self, "within_predicate", SpatialPredicate(self.within_predicate))
        except (ValueError, TypeError):
            raise InvalidArgumentError(
                f"ObjectQuery: within_predicate must be a SpatialPredicate, got {self.within_predicate!r}") from None
        put(self, "at_least", _bounds(self.at_least, "at_least"))
        put(self, "at_most", _bounds(self.at_most, "at_most"))
        if not isinstance(self.include_in_flight, bool):
            raise TypeError(f"ObjectQuery: include_in_flight must be a bool, got {self.include_in_flight!r}")
        if self.limit is not None:
            if isinstance(self.limit, bool) or not isinstance(self.limit, numbers.Integral):
                raise TypeError(f"ObjectQuery: limit must be an int or None, got {self.limit!r}")
            if not 0 <= int(self.limit) <= _INT64_MAX:
                raise InvalidArgumentError(f"ObjectQuery: limit must be at least 0, got {self.limit!r}")
            put(self, "limit", int(self.limit))

    def __hash__(self):
        return hash((self.layer, self.stage, self.intersecting, self.within, self.within_predicate, self.label,
                     tuple(sorted(self.at_least.items())), tuple(sorted(self.at_most.items())),
                     self.include_in_flight, self.limit))

    def _native(self):
        within = None
        if self.within is not None:
            within = [(numpy.array(points, dtype=numpy.int64), hole) for points, hole in self.within]
        return (self.layer, self.stage, self.intersecting, within, int(self.within_predicate), self.label,
                list(self.at_least.items()), list(self.at_most.items()), self.include_in_flight, self.limit)


_FILTERS = frozenset(field.name for field in dataclasses.fields(ObjectQuery))


@dataclasses.dataclass(frozen=True, eq=False)
class ObjectRecord:
    """One stored object. id is its ObjectId text ("<layer>:<x>:<y>"); bbox is
    (x, y, width, height); label is the producing stage's own label, or None; measurements
    maps each measurement the object has to its value; geometry is its rings, each
    (int64 array of shape (n, 2), hole), the first the outer one. Read-only throughout.
    Two records are equal when every field is."""

    id: str
    layer: str
    stage: str
    bbox: Tuple[int, int, int, int]
    label: Optional[str]
    measurements: Mapping[str, float]
    geometry: Tuple[Tuple[numpy.ndarray, bool], ...]

    def __eq__(self, other):
        if not isinstance(other, ObjectRecord):
            return NotImplemented
        return ((self.id, self.layer, self.stage, self.bbox, self.label, dict(self.measurements))
                == (other.id, other.layer, other.stage, other.bbox, other.label, dict(other.measurements))
                and len(self.geometry) == len(other.geometry)
                and all(hole == other_hole and numpy.array_equal(points, other_points)
                        for (points, hole), (other_points, other_hole) in zip(self.geometry, other.geometry)))

    def __hash__(self):
        return hash((self.id, self.stage))


@dataclasses.dataclass(frozen=True, eq=False)
class ObjectBatch:
    """A run of query results as columns: bbox is an (n, 4) int64 array of
    (x, y, width, height) rows; measurements maps each requested name to an (n,) float64
    array, NaN where an object lacks it; geometry holds each object's rings when asked for,
    else None."""

    ids: Tuple[str, ...]
    layers: Tuple[str, ...]
    stages: Tuple[str, ...]
    bbox: numpy.ndarray
    labels: Tuple[Optional[str], ...]
    measurements: Mapping[str, numpy.ndarray]
    geometry: Optional[Tuple[Tuple[Tuple[numpy.ndarray, bool], ...], ...]]

    def __len__(self):
        return len(self.ids)


@dataclasses.dataclass(frozen=True)
class MeasurementSummary:
    """Statistics of one measurement over a query's objects, NaN values skipped. std is the
    population standard deviation. With no values, count is 0 and the rest NaN."""

    count: int
    mean: float
    std: float
    min: float
    max: float


@dataclasses.dataclass(frozen=True)
class ResultSummary:
    """count objects matched the query; measurements summarises each requested measurement."""

    count: int
    measurements: Mapping[str, MeasurementSummary]


def query_of(query, filters):
    """The ObjectQuery that query=None, **filters stand for."""
    unknown = sorted(set(filters) - _FILTERS)
    if unknown:
        raise TypeError(f"Workspace: unknown query filter {unknown[0]!r}; the filters are {sorted(_FILTERS)}")
    if query is None:
        return ObjectQuery(**filters)
    if not isinstance(query, ObjectQuery):
        raise TypeError(f"Workspace: query must be an ObjectQuery or None, got {query!r}")
    if filters:
        raise InvalidArgumentError(
            f"Workspace: give an ObjectQuery or filters, not both (filters: {sorted(filters)})")
    return query


def measurement_names_of(handle):
    return tuple(_native._measurement_names(handle))


def requested_names(handle, measurements):
    """The measurement names to fetch: every one the store holds when measurements is None."""
    known = measurement_names_of(handle)
    if measurements is None:
        return list(known)
    if isinstance(measurements, str):
        raise TypeError(f"Workspace: measurements must be a sequence of names or None, got {measurements!r}")
    try:
        names = list(measurements)
    except TypeError:
        raise TypeError(f"Workspace: measurements must be a sequence of names or None, got {measurements!r}") from None
    known_set = set(known)
    for name in names:
        if not isinstance(name, str):
            raise TypeError(f"Workspace: a measurement name must be a str, got {name!r}")
        if name not in known_set:
            raise InvalidArgumentError(f"Workspace: no measurement named {name!r}")
    if len(set(names)) != len(names):
        raise InvalidArgumentError(f"Workspace: a measurement is named twice in {names!r}")
    return names


def batch_size_of(batch_size):
    if isinstance(batch_size, bool) or not isinstance(batch_size, numbers.Integral):
        raise TypeError(f"Workspace: batch_size must be an int, got {batch_size!r}")
    if not 1 <= batch_size <= 2**31 - 1:
        raise InvalidArgumentError(f"Workspace: batch_size must be from 1 to {2**31 - 1}, got {batch_size!r}")
    return int(batch_size)


def _open_cursor(ws, query):
    """A native cursor over query's result in ws; it keeps ws's native workspace alive."""
    return _native._query(ws._handle(), query._native())


def count(handle, query):
    return _native._count(handle, query._native())


def _frozen_rings(rings):
    out = []
    for points, hole in rings:
        points.flags.writeable = False
        out.append((points, bool(hole)))
    return tuple(out)


def _records_of(batch):
    ids, layers, stages, bbox, labels, measurements, geometry = batch
    boxes = bbox.tolist()
    for i, object_id in enumerate(ids):
        yield ObjectRecord(object_id, layers[i], stages[i], tuple(boxes[i]), labels[i],
                           types.MappingProxyType(measurements[i]), _frozen_rings(geometry[i]))


def record(handle, object_id):
    if not isinstance(object_id, str):
        raise TypeError(f"Workspace: an object id must be a str, got {object_id!r}")
    batch = _native._get(handle, object_id)
    return None if batch is None else next(_records_of(batch))


def records(ws, cursor):
    """Yields the cursor's records, fetched _RECORD_FETCH at a time. Holds ws (decision 7);
    raises IoError once ws is closed, even with records already fetched."""
    try:
        while True:
            batch = cursor.fetch(_RECORD_FETCH, None, True)
            if batch is None:
                return
            for item in _records_of(batch):
                ws._handle()
                yield item
            ws._handle()
    finally:
        cursor.close()


def batches(ws, cursor, batch_size, names, geometry):
    """Yields ObjectBatch objects of up to batch_size records. Holds ws (decision 7)."""
    try:
        while True:
            ws._handle()
            batch = cursor.fetch(batch_size, names, geometry)
            if batch is None:
                return
            ids, layers, stages, bbox, labels, measurements, shapes = batch
            if shapes is not None:
                shapes = tuple(tuple((points, bool(hole)) for points, hole in rings) for rings in shapes)
            yield ObjectBatch(tuple(ids), tuple(layers), tuple(stages), bbox, tuple(labels), measurements, shapes)
    finally:
        cursor.close()


_FRAME_COLUMNS = ("id", "layer", "stage", "x", "y", "width", "height", "label")


def dataframe(stream, names):
    try:
        import pandas
    except ImportError:
        raise ImportError("Workspace.dataframe needs pandas: pip install slideio-processor[pandas]") from None
    ids, layers, stages, boxes, labels = [], [], [], [], []
    values = {name: [] for name in names}
    for batch in stream:
        ids.extend(batch.ids)
        layers.extend(batch.layers)
        stages.extend(batch.stages)
        boxes.append(batch.bbox)
        labels.extend(batch.labels)
        for name in names:
            values[name].append(batch.measurements[name])
    bbox = numpy.concatenate(boxes) if boxes else numpy.empty((0, 4), dtype=numpy.int64)
    columns = {
        "id": pandas.Series(ids, dtype=object),
        "layer": pandas.Series(layers, dtype=object),
        "stage": pandas.Series(stages, dtype=object),
        "x": bbox[:, 0], "y": bbox[:, 1], "width": bbox[:, 2], "height": bbox[:, 3],
        "label": pandas.Series(labels, dtype=object),
    }
    for name in names:
        if name in columns:
            raise InvalidArgumentError(f"Workspace: the measurement {name!r} has the name of a DataFrame column")
        columns[name] = numpy.concatenate(values[name]) if values[name] else numpy.empty(0, dtype=numpy.float64)
    return pandas.DataFrame(columns)


class _Running:
    """Streaming count, mean, M2, min and max (Chan et al.'s merge of per-batch moments)."""

    __slots__ = ("n", "mean", "m2", "min", "max")

    def __init__(self):
        self.n = 0
        self.mean = 0.0
        self.m2 = 0.0
        self.min = math.inf
        self.max = -math.inf

    def add(self, column):
        values = column[~numpy.isnan(column)]
        nb = values.size
        if nb == 0:
            return
        mb = float(values.mean())
        m2b = float(((values - mb) ** 2).sum())
        total = self.n + nb
        delta = mb - self.mean
        self.mean += delta * nb / total
        self.m2 += m2b + delta * delta * self.n * nb / total
        self.n = total
        self.min = min(self.min, float(values.min()))
        self.max = max(self.max, float(values.max()))

    def summary(self):
        if self.n == 0:
            nan = math.nan
            return MeasurementSummary(0, nan, nan, nan, nan)
        return MeasurementSummary(self.n, self.mean, math.sqrt(self.m2 / self.n), self.min, self.max)


def summary(stream, names):
    total = 0
    running = {name: _Running() for name in names}
    for batch in stream:
        total += len(batch.ids)
        for name in names:
            running[name].add(batch.measurements[name])
    return ResultSummary(total, types.MappingProxyType({name: r.summary() for name, r in running.items()}))
