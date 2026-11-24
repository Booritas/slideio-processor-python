"""Rectangles and polygons in level-0 pixels, checked before they reach the library.

A rectangle is (x, y, width, height) of ints. A polygon is a sequence of rings, each
(points, hole): points an array-like of shape (n, 2) of ints, hole a bool; the first ring
is the outer one. The messages carry no class prefix: callers add theirs.
"""
import numbers

import numpy

from .errors import InvalidArgumentError

_INT64_MIN = -2**63
_INT64_MAX = 2**63 - 1


def _int(value, what):
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise InvalidArgumentError(f"{what} must be an int, got {value!r}")
    value = int(value)
    if not _INT64_MIN <= value <= _INT64_MAX:
        raise InvalidArgumentError(f"{what} {value} is out of the 64-bit range")
    return value


def rect(value):
    """Returns value as (x, y, width, height) of ints; width and height must be above 0."""
    try:
        items = tuple(value)
    except TypeError:
        raise InvalidArgumentError(f"a rectangle must be (x, y, width, height), got {value!r}") from None
    if len(items) != 4:
        raise InvalidArgumentError(f"a rectangle must be (x, y, width, height), got {value!r}")
    x, y, width, height = (_int(v, f"rectangle {part}") for v, part in zip(items, ("x", "y", "width", "height")))
    if width <= 0 or height <= 0:
        raise InvalidArgumentError(f"a rectangle's width and height must be above 0, got {value!r}")
    return x, y, width, height


def _points(points, index):
    try:
        array = numpy.asarray(points)
    except (TypeError, ValueError):
        raise InvalidArgumentError(f"ring {index}: the points must be an array of shape (n, 2)") from None
    if array.ndim != 2 or array.shape[1] != 2:
        raise InvalidArgumentError(f"ring {index}: the points must be an array of shape (n, 2), "
                                   f"got shape {array.shape}")
    if array.shape[0] < 3:
        raise InvalidArgumentError(f"ring {index}: a ring needs at least 3 points, got {array.shape[0]}")
    if array.dtype == numpy.bool_ or not numpy.issubdtype(array.dtype, numpy.integer):
        if array.dtype.kind == "O":
            # A list of Python ints too large for a NumPy integer type, or of other objects.
            for value in array.flat:
                _int(value, f"ring {index}: a coordinate")
            return numpy.array([[int(v) for v in row] for row in array], dtype=numpy.int64)
        raise InvalidArgumentError(f"ring {index}: the coordinates must be ints, got dtype {array.dtype}")
    if array.dtype == numpy.uint64 and array.size and array.max() > _INT64_MAX:
        raise InvalidArgumentError(f"ring {index}: a coordinate is out of the 64-bit range")
    return array.astype(numpy.int64)


def rings(polygon):
    """Returns polygon as a list of (int64 array of shape (n, 2), hole) pairs."""
    if isinstance(polygon, (str, bytes)):
        raise InvalidArgumentError(f"a polygon must be a sequence of (points, hole) rings, got {polygon!r}")
    try:
        entries = list(polygon)
    except TypeError:
        raise InvalidArgumentError(f"a polygon must be a sequence of (points, hole) rings, got {polygon!r}") from None
    if not entries:
        raise InvalidArgumentError("a polygon needs at least one ring")
    result = []
    for index, ring in enumerate(entries):
        try:
            points, hole = ring
        except (TypeError, ValueError):
            raise InvalidArgumentError(f"ring {index} must be a pair (points, hole)") from None
        if not isinstance(hole, (bool, numpy.bool_)):
            raise InvalidArgumentError(f"ring {index}: hole must be a bool, got {hole!r}")
        result.append((_points(points, index), bool(hole)))
    return result
