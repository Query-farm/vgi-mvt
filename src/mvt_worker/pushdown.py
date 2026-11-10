"""Turn the spatial filter DuckDB pushes into ``read_tiles`` into a bounding box.

For ``WHERE ST_Intersects(geometry, <constant>)`` (and ST_Contains, ST_Within,
ST_Covers, ...) the VGI extension adds an *optional* ``geometry && <constant>``
filter to the scan of any function that declares
``duckdb.spatial/intersects_extent@1``. DuckDB still applies the exact
predicate, so the worker may use the constant's bounding box only to read fewer
tiles, never to change the answer. Every helper here errs towards "no box"
(read everything) when it is unsure.
"""

from __future__ import annotations

import math
import struct
from typing import Any

from .mvt import Coordinates
from .sources import BBox

INTERSECTS_EXTENT = ("duckdb.spatial", "intersects_extent", 1)
GEOMETRY_COLUMN = "geometry"

_R = 6378137.0
_MAX_LAT = 85.0511287798066


class _WkbError(ValueError):
    pass


def wkb_envelope(wkb: bytes) -> tuple[float, float, float, float] | None:
    """Return ``(min_x, min_y, max_x, max_y)`` of a WKB/EWKB geometry; None if empty, invalid or non-finite."""
    box = [math.inf, math.inf, -math.inf, -math.inf]
    try:
        end = _read_geometry(memoryview(wkb), 0, box, depth=0)
    except (_WkbError, struct.error, IndexError):
        return None
    if end != len(wkb) or not all(math.isfinite(v) for v in box):
        return None
    return (box[0], box[1], box[2], box[3])


def _read_geometry(buf: memoryview, pos: int, box: list[float], *, depth: int) -> int:
    if depth > 32:
        raise _WkbError("geometry nested too deeply")
    order = "<" if buf[pos] == 1 else ">" if buf[pos] == 0 else None
    if order is None:
        raise _WkbError("bad byte order")
    (code,) = struct.unpack_from(order + "I", buf, pos + 1)
    pos += 5
    has_z = bool(code & 0x80000000)
    has_m = bool(code & 0x40000000)
    if code & 0x20000000:  # EWKB SRID
        pos += 4
    code &= 0x0FFFFFFF
    base = code % 1000
    iso = code // 1000
    if iso in (1, 3):
        has_z = True
    if iso in (2, 3):
        has_m = True
    dims = 2 + int(has_z) + int(has_m)
    if base == 1:
        return _read_points(buf, pos, order, dims, 1, box)
    if base == 2:
        (n,) = struct.unpack_from(order + "I", buf, pos)
        return _read_points(buf, pos + 4, order, dims, n, box)
    if base == 3:
        (rings,) = struct.unpack_from(order + "I", buf, pos)
        pos += 4
        for _ in range(rings):
            (n,) = struct.unpack_from(order + "I", buf, pos)
            pos = _read_points(buf, pos + 4, order, dims, n, box)
        return pos
    if base in (4, 5, 6, 7):
        (parts,) = struct.unpack_from(order + "I", buf, pos)
        pos += 4
        for _ in range(parts):
            pos = _read_geometry(buf, pos, box, depth=depth + 1)
        return pos
    raise _WkbError(f"unsupported WKB type {code}")


def _read_points(buf: memoryview, pos: int, order: str, dims: int, n: int, box: list[float]) -> int:
    if n > (len(buf) - pos) // (8 * dims):
        raise _WkbError("point count exceeds the buffer")
    values = struct.unpack_from(f"{order}{n * dims}d", buf, pos)
    for i in range(0, n * dims, dims):
        x, y = values[i], values[i + 1]
        if math.isnan(x) and math.isnan(y):
            continue  # an empty point is written as NaN NaN
        box[0] = min(box[0], x)
        box[1] = min(box[1], y)
        box[2] = max(box[2], x)
        box[3] = max(box[3], y)
    return pos + n * dims * 8


def mercator_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Inverse spherical Web Mercator (EPSG:3857 metres to degrees)."""
    lon = math.degrees(x / _R)
    lat = math.degrees(2.0 * math.atan(math.exp(y / _R)) - math.pi / 2.0)
    return lon, lat


def to_lonlat_box(envelope: tuple[float, float, float, float], coordinates: Coordinates) -> BBox | None:
    """Express an envelope in the output coordinates as a WGS84 bbox; None when it cannot be (raw tile pixels)."""
    if coordinates == "tile":
        return None
    w, s, e, n = envelope
    if coordinates == "web_mercator":
        w, s = mercator_to_lonlat(w, s)
        e, n = mercator_to_lonlat(e, n)
    return (w, s, e, n)


def intersect(a: BBox | None, b: BBox | None) -> BBox | bool | None:
    """Intersect two optional boxes; ``False`` when they are disjoint (nothing can match)."""
    if a is None:
        return b
    if b is None:
        return a
    w, s = max(a[0], b[0]), max(a[1], b[1])
    e, n = min(a[2], b[2]), min(a[3], b[3])
    if w > e or s > n:
        return False
    return (w, s, e, n)


def clamp_to_world(box: BBox) -> BBox | bool:
    """Clip a bbox to the area tiles cover; ``False`` when it lies entirely outside it."""
    w, s, e, n = box
    if e < -180 or w > 180 or n < -_MAX_LAT or s > _MAX_LAT:
        return False
    return (max(w, -180.0), max(s, -_MAX_LAT), min(e, 180.0), min(n, _MAX_LAT))


def pushed_bbox(filters: Any, coordinates: Coordinates) -> BBox | bool | None:
    """WGS84 box implied by the pushed ``geometry && <constant>`` filters.

    ``filters`` is the decoded ``PushdownFilters`` (top-level entries are ANDed).
    Returns None when there is no usable spatial filter, ``False`` when the
    filters prove that no feature can match.
    """
    if filters is None:
        return None
    from vgi.filter_v2 import Call, ColumnRef, Literal
    from vgi.table_filter_pushdown import V2ExpressionFilter

    current: BBox | None = None
    for f in getattr(filters, "filters", []):
        if not isinstance(f, V2ExpressionFilter):
            continue
        call = f.expression
        if not isinstance(call, Call):
            continue
        ident = call.function
        key = (getattr(ident, "namespace", None), getattr(ident, "name", None), getattr(ident, "version", None))
        if key != INTERSECTS_EXTENT or len(call.arguments) != 2:
            continue
        column = next((a for a in call.arguments if isinstance(a, ColumnRef)), None)
        literal = next((a for a in call.arguments if isinstance(a, Literal)), None)
        if column is None or literal is None or column.column_name != GEOMETRY_COLUMN:
            continue
        value = literal.value.as_py() if literal.value is not None and literal.value.is_valid else None
        if not isinstance(value, bytes):
            continue
        envelope = wkb_envelope(value)
        if envelope is None:
            continue
        box = to_lonlat_box(envelope, coordinates)
        if box is None:
            continue
        combined = intersect(current, box)
        if combined is False:
            return False
        current = combined if isinstance(combined, tuple) else None
    return current
