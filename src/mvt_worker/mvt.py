"""Decode Mapbox Vector Tiles (MVT 2.1) into features with WKB geometry.

Dependency-free: a minimal protobuf reader for the four MVT messages
(``Tile``, ``Layer``, ``Feature``, ``Value``), the geometry command decoder,
polygon ring classification, and a WKB writer.

Coordinates in a tile are integers in ``[0, extent)`` (usually 4096) measured
from the tile's top-left corner, plus a small buffer outside it. They can be
mapped back to WGS84 longitude/latitude or Web Mercator metres exactly, given
the tile's ``z/x/y``; the only loss is the quantisation the encoder applied.
"""

from __future__ import annotations

import math
import struct
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal

Coordinates = Literal["wgs84", "web_mercator", "tile"]
COORDINATE_CHOICES: tuple[Coordinates, ...] = ("wgs84", "web_mercator", "tile")

PropertyValue = str | float | int | bool

GEOM_TYPES = {0: "UNKNOWN", 1: "POINT", 2: "LINESTRING", 3: "POLYGON"}

_EARTH_HALF_CIRCUMFERENCE = math.pi * 6378137.0

# A decompressed tile larger than this is refused. Real tiles are well under 10 MiB
# (Mapbox caps them at 500 KiB compressed); the cap stops a few kilobytes of gzip
# from expanding into gigabytes of memory.
MAX_TILE_BYTES = 64 << 20

_U64 = (1 << 64) - 1


class MvtDecodeError(ValueError):
    """The bytes are not a valid vector tile."""


@contextmanager
def tile_context(z: int, x: int, y: int) -> Iterator[None]:
    """Name the tile in any :class:`MvtDecodeError` raised inside the block."""
    try:
        yield
    except MvtDecodeError as exc:
        if str(exc).startswith("tile "):
            raise
        raise MvtDecodeError(f"tile {z}/{x}/{y} is not a valid vector tile: {exc}") from exc


# ---------------------------------------------------------------- protobuf


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Read a protobuf varint at ``pos``; return ``(value, next_pos)``."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise MvtDecodeError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result & _U64, pos  # protobuf truncates varints to 64 bits
        shift += 7
        if shift > 70:
            raise MvtDecodeError("varint too long")


def _fields(buf: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    """Yield ``(field_number, wire_type, value)``; length-delimited values are bytes."""
    pos = 0
    end = len(buf)
    while pos < end:
        key, pos = read_varint(buf, pos)
        num, wire = key >> 3, key & 7
        if wire == 0:
            val, pos = read_varint(buf, pos)
            yield num, wire, val
        elif wire == 2:
            length, pos = read_varint(buf, pos)
            if pos + length > end:
                raise MvtDecodeError("truncated length-delimited field")
            yield num, wire, buf[pos : pos + length]
            pos += length
        elif wire == 1:
            if pos + 8 > end:
                raise MvtDecodeError("truncated fixed64")
            yield num, wire, buf[pos : pos + 8]
            pos += 8
        elif wire == 5:
            if pos + 4 > end:
                raise MvtDecodeError("truncated fixed32")
            yield num, wire, buf[pos : pos + 4]
            pos += 4
        else:
            raise MvtDecodeError(f"unsupported protobuf wire type {wire}")


def _packed(value: int | bytes) -> list[int]:
    if isinstance(value, int):  # a non-packed single element
        return [value]
    out: list[int] = []
    pos = 0
    n = len(value)
    while pos < n:
        v, pos = read_varint(value, pos)
        out.append(v)
    return out


def _zigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def _decode_value(buf: bytes) -> PropertyValue | None:
    result: PropertyValue | None = None
    for num, wire, val in _fields(buf):
        # Check the wire type, not just the Python type: a corrupt tile can put a
        # length-delimited field where a fixed32/fixed64 float belongs.
        if num == 1 and wire == 2 and isinstance(val, bytes):
            result = val.decode("utf-8", errors="replace")
        elif num == 2 and wire == 5 and isinstance(val, bytes):
            result = struct.unpack("<f", val)[0]
        elif num == 3 and wire == 1 and isinstance(val, bytes):
            result = struct.unpack("<d", val)[0]
        elif num == 4 and isinstance(val, int):
            result = val - (1 << 64) if val >= 1 << 63 else val
        elif num == 5 and isinstance(val, int):
            result = val
        elif num == 6 and isinstance(val, int):
            result = _zigzag(val)
        elif num == 7 and isinstance(val, int):
            result = bool(val)
    return result


# ---------------------------------------------------------------- model


@dataclass(slots=True)
class Feature:
    """One feature: id, geometry type, raw command stream and decoded properties."""

    id: int | None
    geom_type: int
    geometry: list[int]
    properties: dict[str, PropertyValue | None]


@dataclass(slots=True)
class Layer:
    """One named layer of a tile."""

    name: str
    extent: int = 4096
    version: int = 1
    features: list[Feature] = field(default_factory=list)


def maybe_decompress(data: bytes, limit: int = MAX_TILE_BYTES) -> bytes:
    """Undo gzip/zlib compression, which most tile stores apply to MVT bytes.

    Output is capped at ``limit`` bytes (:data:`MAX_TILE_BYTES`); corrupt or
    oversized input raises :class:`MvtDecodeError`. Raw MVT never starts with
    the gzip or zlib magic, so sniffing is safe.
    """
    if data[:2] == b"\x1f\x8b":
        return _inflate(data, 16 + zlib.MAX_WBITS, limit, "gzip")
    if len(data) >= 2 and data[0] == 0x78 and (data[0] * 256 + data[1]) % 31 == 0:
        try:
            return _inflate(data, zlib.MAX_WBITS, limit, "zlib")
        except _NotCompressedError:
            return data  # zlib-looking bytes that are not zlib
    return data


class _NotCompressedError(MvtDecodeError):
    pass


def _inflate(data: bytes, wbits: int, limit: int, kind: str) -> bytes:
    out: list[bytes] = []
    size = 0
    rest = data
    while rest:  # gzip allows several concatenated members
        d = zlib.decompressobj(wbits)
        try:
            chunk = d.decompress(rest, limit - size + 1)
        except zlib.error as exc:
            err = _NotCompressedError if kind == "zlib" and not out else MvtDecodeError
            raise err(f"corrupt {kind} data: {exc}") from exc
        size += len(chunk)
        out.append(chunk)
        if size > limit or d.unconsumed_tail:
            raise MvtDecodeError(f"tile expands past {limit >> 20} MiB when decompressed")
        if not d.eof:
            raise MvtDecodeError(f"truncated {kind} data")
        rest = d.unused_data if kind == "gzip" else b""
    return b"".join(out)


def decode_tile(data: bytes, *, layer_filter: str | None = None) -> list[Layer]:
    """Parse MVT bytes (optionally gzip'd) into layers; skip layers not matching ``layer_filter``."""
    data = maybe_decompress(bytes(data))
    layers: list[Layer] = []
    for num, _wire, val in _fields(data):
        if num != 3 or not isinstance(val, bytes):
            continue
        layer = _decode_layer(val, layer_filter)
        if layer is not None:
            layers.append(layer)
    return layers


def _decode_layer(buf: bytes, layer_filter: str | None) -> Layer | None:
    name = ""
    extent = 4096
    version = 1
    keys: list[str] = []
    values: list[PropertyValue | None] = []
    raw_features: list[bytes] = []
    for num, _wire, val in _fields(buf):
        if num == 1 and isinstance(val, bytes):
            name = val.decode("utf-8", errors="replace")
            if layer_filter is not None and name != layer_filter:
                return None
        elif num == 2 and isinstance(val, bytes):
            raw_features.append(val)
        elif num == 3 and isinstance(val, bytes):
            keys.append(val.decode("utf-8", errors="replace"))
        elif num == 4 and isinstance(val, bytes):
            values.append(_decode_value(val))
        elif num == 5 and isinstance(val, int):
            extent = val
        elif num == 15 and isinstance(val, int):
            version = val
    if layer_filter is not None and name != layer_filter:
        return None
    layer = Layer(name=name, extent=extent or 4096, version=version)
    nkeys, nvals = len(keys), len(values)
    for raw in raw_features:
        fid: int | None = None
        gtype = 0
        tags: list[int] = []
        geometry: list[int] = []
        for num, _wire, val in _fields(raw):
            if num == 1 and isinstance(val, int):
                fid = val
            elif num == 2:
                tags.extend(_packed(val))
            elif num == 3 and isinstance(val, int):
                gtype = val
            elif num == 4:
                geometry.extend(_packed(val))
        props: dict[str, PropertyValue | None] = {}
        for i in range(0, len(tags) - 1, 2):
            k, v = tags[i], tags[i + 1]
            if k < nkeys and v < nvals:
                props[keys[k]] = values[v]
        layer.features.append(Feature(id=fid, geom_type=gtype, geometry=geometry, properties=props))
    return layer


# ---------------------------------------------------------------- geometry

Ring = list[tuple[int, int]]


def decode_geometry(commands: list[int]) -> list[Ring]:
    """Turn the MVT command stream into parts (point groups, lines or rings) in tile pixels."""
    parts: list[Ring] = []
    current: Ring = []
    x = y = 0
    i = 0
    n = len(commands)
    while i < n:
        cmd = commands[i]
        i += 1
        cid, count = cmd & 7, cmd >> 3
        if cid == 1:  # MoveTo
            for _ in range(count):
                if i + 1 >= n:
                    break
                x += _zigzag(commands[i])
                y += _zigzag(commands[i + 1])
                i += 2
                if current:
                    parts.append(current)
                current = [(x, y)]
        elif cid == 2:  # LineTo
            for _ in range(count):
                if i + 1 >= n:
                    break
                x += _zigzag(commands[i])
                y += _zigzag(commands[i + 1])
                i += 2
                current.append((x, y))
        elif cid == 7:  # ClosePath
            if current and current[0] != current[-1]:
                current.append(current[0])
        else:
            raise MvtDecodeError(f"unknown geometry command {cid}")
    if current:
        parts.append(current)
    return parts


def _signed_area(ring: Ring) -> float:
    total = 0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True):
        total += x1 * y2 - x2 * y1
    return total / 2.0


def classify_rings(rings: list[Ring]) -> list[list[Ring]]:
    """Group rings into polygons: a ring with the exterior's winding starts a new polygon.

    MVT 2.x defines exterior rings as positive surveyor's-formula area in tile
    coordinates. Some 1.x encoders wound the other way, so the first ring's sign
    decides (the same rule mapbox/vector-tile-js applies). Zero-area rings drop.
    """
    polygons: list[list[Ring]] = []
    exterior_sign = 0.0
    for ring in rings:
        area = _signed_area(ring)
        if area == 0:
            continue
        if exterior_sign == 0:
            exterior_sign = math.copysign(1.0, area)
        if math.copysign(1.0, area) == exterior_sign:
            polygons.append([ring])
        elif polygons:
            polygons[-1].append(ring)
    return polygons


def _point_in_ring(px: float, py: float, ring: Ring) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _bbox_contains(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


# Above this many rings in one v1 feature, group by winding instead of nesting.
_MAX_NESTING_RINGS = 5000


def classify_rings_by_nesting(rings: list[Ring]) -> list[list[Ring]]:
    """Group rings by containment, ignoring winding: even nesting depth = exterior, odd = hole.

    MVT 1.x had no winding rule, and encoders of that era (TileMill, early
    Mapnik) wrote rings in either direction. Nesting is the only reliable
    signal there. A ring's depth is the number of larger rings containing a
    sample point of it.
    """
    if len(rings) > _MAX_NESTING_RINGS:
        return classify_rings(rings)  # nesting is quadratic; fall back to winding
    items = []
    for ring in rings:
        area = abs(_signed_area(ring))
        if area == 0:
            continue
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        items.append((area, (min(xs), min(ys), max(xs), max(ys)), ring))
    items.sort(key=lambda t: -t[0])
    polygons: list[list[Ring]] = []
    placed: list[tuple[tuple[int, int, int, int], Ring, int, int]] = []  # bbox, ring, depth, polygon index
    for _area, bbox, ring in items:
        # Sample the midpoint of the first edge: vertices often touch the parent ring.
        (x0, y0), (x1, y1) = ring[0], ring[1]
        sx, sy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        parent: tuple[int, int] | None = None
        for pb, pring, pdepth, pidx in reversed(placed):  # smallest containing ring wins
            if _bbox_contains(pb, bbox) and _point_in_ring(sx, sy, pring):
                parent = (pdepth, pidx)
                break
        depth = 0 if parent is None else parent[0] + 1
        if depth % 2 == 0:
            polygons.append([ring])
            placed.append((bbox, ring, depth, len(polygons) - 1))
        else:
            assert parent is not None
            polygons[parent[1]].append(ring)
            placed.append((bbox, ring, depth, parent[1]))
    return polygons


class Projector:
    """Map tile pixel coordinates to the requested coordinate system."""

    def __init__(self, z: int, x: int, y: int, extent: int, coordinates: Coordinates) -> None:
        """Bind the projector to a tile."""
        self.coordinates = coordinates
        self.size = float(extent) * (1 << z)
        self.x0 = float(x) * extent
        self.y0 = float(y) * extent

    def __call__(self, ring: Ring) -> list[float]:
        """Return a flat ``[x0, y0, x1, y1, ...]`` list of projected coordinates."""
        out: list[float] = []
        if self.coordinates == "tile":
            for px, py in ring:
                out.append(float(px))
                out.append(float(py))
            return out
        size, x0, y0 = self.size, self.x0, self.y0
        if self.coordinates == "web_mercator":
            c = _EARTH_HALF_CIRCUMFERENCE
            for px, py in ring:
                out.append((x0 + px) / size * 2 * c - c)
                out.append(c - (y0 + py) / size * 2 * c)
            return out
        for px, py in ring:
            out.append((x0 + px) / size * 360.0 - 180.0)
            t = math.pi * (1.0 - 2.0 * (y0 + py) / size)
            # |t| > 40 is already ±90° in double precision; clamping keeps sinh from overflowing.
            out.append(math.degrees(math.atan(math.sinh(max(-40.0, min(40.0, t))))))
        return out


def _wkb_coords(flat: list[float]) -> bytes:
    return struct.pack(f"<I{len(flat)}d", len(flat) // 2, *flat)


def _wkb_polygon(rings: list[list[float]]) -> bytes:
    return struct.pack("<BII", 1, 3, len(rings)) + b"".join(_wkb_coords(r) for r in rings)


def feature_wkb(feature: Feature, project: Callable[[Ring], list[float]], version: int = 2) -> tuple[str, bytes | None]:
    """Return ``(geometry_type, wkb)`` for a feature; wkb is None for empty/unknown geometry.

    The type is the OGC type actually written: POINT, MULTIPOINT, LINESTRING,
    MULTILINESTRING, POLYGON or MULTIPOLYGON. ``version`` is the layer's MVT
    version: 2 classifies polygon rings by winding (the spec), 1 by nesting.
    """
    return parts_wkb(feature.geom_type, decode_geometry(feature.geometry), project, version)


def parts_wkb(
    gtype: int, parts: list[Ring], project: Callable[[Ring], list[float]], version: int = 2
) -> tuple[str, bytes | None]:
    """Like :func:`feature_wkb`, from already decoded parts (see :func:`decode_geometry`)."""
    if gtype == 1:
        points = [pt for part in parts for pt in part]
        if not points:
            return "POINT", None
        if len(points) == 1:
            return "POINT", struct.pack("<BI2d", 1, 1, *project(points))
        flat = project(points)
        body = b"".join(struct.pack("<BI2d", 1, 1, flat[i], flat[i + 1]) for i in range(0, len(flat), 2))
        return "MULTIPOINT", struct.pack("<BII", 1, 4, len(points)) + body
    if gtype == 2:
        lines = [p for p in parts if len(p) >= 2]
        if not lines:
            return "LINESTRING", None
        if len(lines) == 1:
            return "LINESTRING", struct.pack("<BI", 1, 2) + _wkb_coords(project(lines[0]))
        body = b"".join(struct.pack("<BI", 1, 2) + _wkb_coords(project(line)) for line in lines)
        return "MULTILINESTRING", struct.pack("<BII", 1, 5, len(lines)) + body
    if gtype == 3:
        closed: list[Ring] = []
        for ring in parts:
            if len(ring) >= 3:
                closed.append(ring if ring[0] == ring[-1] else [*ring, ring[0]])
        usable = [r for r in closed if len(r) >= 4]
        polygons = classify_rings(usable) if version >= 2 else classify_rings_by_nesting(usable)
        if not polygons:
            return "POLYGON", None
        if len(polygons) == 1:
            return "POLYGON", _wkb_polygon([project(r) for r in polygons[0]])
        body = b"".join(_wkb_polygon([project(r) for r in poly]) for poly in polygons)
        return "MULTIPOLYGON", struct.pack("<BII", 1, 6, len(polygons)) + body
    return "UNKNOWN", None


# ---------------------------------------------------------------- tile math


def _tile_xy_float(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = 1 << z
    lat = max(min(lat, 85.0511287798066), -85.0511287798066)
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    """Return the XYZ tile containing a WGS84 point at zoom ``z`` (clamped to the world)."""
    n = 1 << z
    x, y = _tile_xy_float(lon, lat, z)
    return min(max(int(x), 0), n - 1), min(max(int(y), 0), n - 1)


def tile_range(z: int, west: float, south: float, east: float, north: float) -> tuple[int, int, int, int]:
    """Return inclusive ``(min_x, min_y, max_x, max_y)`` of the tiles covering a bbox.

    An east or south edge that lies exactly on a tile boundary does not pull in
    the next tile.
    """
    n = 1 << z
    x0, y0 = lonlat_to_tile(west, north, z)
    fx1, fy1 = _tile_xy_float(east, south, z)
    x1 = min(max(math.ceil(fx1) - 1, x0), n - 1)
    y1 = min(max(math.ceil(fy1) - 1, y0), n - 1)
    return x0, y0, x1, y1
