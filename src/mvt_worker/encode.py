"""Encode features as Mapbox Vector Tile 2.1 bytes.

The geometry handed to :func:`geometry_commands` is already in integer tile
coordinates (clipped, simplified and snapped by :mod:`mvt_worker.tiler`). This
module only turns it into the command stream and writes the protobuf. It follows
the spec's MUSTs: exterior rings have positive surveyor's-formula area in tile
coordinates (y down) and holes negative; rings are not closed explicitly (the
``ClosePath`` command closes them); no ``LineTo`` repeats the previous point;
keys and values are deduplicated per layer.
"""

from __future__ import annotations

import itertools
import struct
from typing import Any

import numpy as np
import shapely

POINT, LINESTRING, POLYGON = 1, 2, 3

Value = str | bool | int | float


def _varint(n: int, out: bytearray) -> None:
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)


def _key(field: int, wire: int, out: bytearray) -> None:
    _varint((field << 3) | wire, out)


def _bytes_field(field: int, payload: bytes | bytearray, out: bytearray) -> None:
    _key(field, 2, out)
    _varint(len(payload), out)
    out += payload


def _packed(field: int, values: list[int], out: bytearray) -> None:
    body = bytearray()
    for v in values:
        _varint(v, body)
    _bytes_field(field, body, out)


def _zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def _command(cid: int, count: int) -> int:
    return (cid & 7) | (count << 3)


def _encode_value(kind: str, value: Value) -> bytes:
    out = bytearray()
    if kind == "s":
        _bytes_field(1, str(value).encode(), out)
    elif kind == "f":
        _key(2, 5, out)
        out += struct.pack("<f", value)
    elif kind == "d":
        _key(3, 1, out)
        out += struct.pack("<d", value)
    elif kind == "b":
        _key(7, 0, out)
        _varint(1 if value else 0, out)
    else:  # integer: uint when it fits, sint for negatives
        n = int(value)
        if n >= 0:
            _key(5, 0, out)
            _varint(n, out)
        else:
            _key(6, 0, out)
            _varint(_zigzag(n), out)
    return bytes(out)


def value_kind(value: Value, *, single: bool = False) -> str:
    """Return the MVT value field a Python value is written as: s, f, d, b or i."""
    if isinstance(value, bool):
        return "b"
    if isinstance(value, int):
        return "i"
    if isinstance(value, float):
        return "f" if single else "d"
    return "s"


class LayerBuilder:
    """Collect one layer's features for one tile, then encode the layer message."""

    __slots__ = ("_features", "_keys", "_values", "extent", "name")

    def __init__(self, name: str, extent: int) -> None:
        """Start an empty layer."""
        self.name = name
        self.extent = extent
        self._keys: dict[str, int] = {}
        self._values: dict[tuple[str, Any], int] = {}
        self._features: list[bytes] = []

    def __len__(self) -> int:
        """Return the number of features added so far."""
        return len(self._features)

    def add(
        self,
        gtype: int,
        geometry: bytes,
        properties: list[tuple[str, str, Value]],
        fid: int | None,
    ) -> None:
        """Add a feature.

        ``geometry`` is the packed command stream (:func:`pack` or :func:`encode_pieces`);
        ``properties`` are ``(key, kind, value)`` triples (see :func:`value_kind`).
        """
        keys, values = self._keys, self._values
        tags: list[int] = []
        for k, kind, v in properties:
            ki = keys.get(k)
            if ki is None:
                ki = keys[k] = len(keys)
            # bool and int compare equal in Python (True == 1), so the kind is part of the key.
            vk = (kind, v)
            vi = values.get(vk)
            if vi is None:
                vi = values[vk] = len(values)
            tags.append(ki)
            tags.append(vi)
        parts: list[bytes] = []
        if fid is not None:
            parts += (b"\x08", _vbytes(fid))
        if tags:
            tb = bytes(tags) if max(tags) < 0x80 else pack(tags)
            parts += (b"\x12", _vbytes(len(tb)), tb)
        parts += (_GEOM_TYPE[gtype], b"\x22", _vbytes(len(geometry)), geometry)
        self._features.append(b"".join(parts))

    def encode(self) -> bytes:
        """Encode the ``Layer`` message (version 2)."""
        parts: list[bytes] = [b"\x78\x02"]  # version = 2
        name = self.name.encode()
        parts += (b"\x0a", _vbytes(len(name)), name)
        for f in self._features:
            parts += (b"\x12", _vbytes(len(f)), f)
        for k in self._keys:
            kb = k.encode()
            parts += (b"\x1a", _vbytes(len(kb)), kb)
        for kind, v in self._values:
            vb = _encode_value(kind, v)
            parts += (b"\x22", _vbytes(len(vb)), vb)
        parts += (b"\x28", _vbytes(self.extent))
        return b"".join(parts)


_SMALL = [bytes([i]) for i in range(0x80)]
_GEOM_TYPE = {t: bytes([0x18, t]) for t in (0, POINT, LINESTRING, POLYGON)}


def _vbytes(n: int) -> bytes:
    """One varint as bytes."""
    if n < 0x80:
        return _SMALL[n]
    out = bytearray()
    _varint(n, out)
    return bytes(out)


def encode_tile(layers: list[LayerBuilder]) -> bytes:
    """Encode the ``Tile`` message holding every non-empty layer."""
    out = bytearray()
    for layer in layers:
        if len(layer):
            _bytes_field(3, layer.encode(), out)
    return bytes(out)


def _ints(coords: Any) -> list[tuple[int, int]]:
    """Integer points with consecutive repeats removed."""
    arr = np.rint(np.asarray(coords, dtype=float)[:, :2]).astype(np.int64)
    pts: list[tuple[int, int]] = []
    for x, y in arr.tolist():
        if not pts or pts[-1] != (x, y):
            pts.append((x, y))
    return pts


def _ring_area2(pts: list[tuple[int, int]]) -> int:
    """Twice the surveyor's-formula area of a closed ring (first point repeated at the end)."""
    s = 0
    for (x0, y0), (x1, y1) in itertools.pairwise(pts):
        s += x0 * y1 - x1 * y0
    return s


class _Cursor:
    __slots__ = ("out", "x", "y")

    def __init__(self) -> None:
        self.x = self.y = 0
        self.out: list[int] = []

    def move(self, pts: list[tuple[int, int]]) -> None:
        self.out.append(_command(1, len(pts)))
        self._deltas(pts)

    def line(self, pts: list[tuple[int, int]]) -> None:
        self.out.append(_command(2, len(pts)))
        self._deltas(pts)

    def _deltas(self, pts: list[tuple[int, int]]) -> None:
        for x, y in pts:
            self.out += (_zigzag(x - self.x), _zigzag(y - self.y))
            self.x, self.y = x, y


def _ring(pts: list[tuple[int, int]], exterior: bool) -> list[tuple[int, int]] | None:
    """Wind a ring the way MVT wants, without its closing point; None if it has no area."""
    if pts[0] != pts[-1]:
        pts = [*pts, pts[0]]
    if len(pts) < 4:
        return None
    area = _ring_area2(pts)
    if area == 0:
        return None
    if (area > 0) != exterior:
        pts = pts[::-1]
    return pts[:-1]


def geometry_commands(geom: Any) -> tuple[int, list[int]] | None:
    """MVT ``(type, commands)`` for a point, line or polygon geometry (single or multi); None if empty."""
    cur = _Cursor()
    kind = shapely.get_type_id(geom)
    if kind in (0, 4):  # Point, MultiPoint
        pts = [p for part in shapely.get_parts(geom) if not part.is_empty for p in _ints(part.coords)]
        if not pts:
            return None
        cur.move(pts)
        return POINT, cur.out
    if kind in (1, 5):  # LineString, MultiLineString
        for part in shapely.get_parts(geom):
            pts = _ints(part.coords) if not part.is_empty else []
            if len(pts) < 2:
                continue
            cur.move(pts[:1])
            cur.line(pts[1:])
        return (LINESTRING, cur.out) if cur.out else None
    if kind in (3, 6):  # Polygon, MultiPolygon
        for poly in shapely.get_parts(geom):
            if poly.is_empty:
                continue
            shell = _ring(_ints(poly.exterior.coords), exterior=True)
            if shell is None:
                continue
            rings = [shell]
            for hole in poly.interiors:
                r = _ring(_ints(hole.coords), exterior=False)
                if r is not None:
                    rings.append(r)
            for r in rings:
                cur.move(r[:1])
                cur.line(r[1:])
                cur.out.append(_command(7, 1))
        return (POLYGON, cur.out) if cur.out else None
    raise ValueError(f"cannot encode a {shapely.get_type_id(geom)} geometry as one vector tile feature")


def pack(commands: list[int]) -> bytes:
    """Varint-pack a command stream (the payload of a feature's geometry field)."""
    out = bytearray()
    for v in commands:
        _varint(v, out)
    return bytes(out)


# ---------------------------------------------------------------------------
# Vectorised encoding: a whole zoom's pieces of one dimension at once.
# geometry_commands above is the reference; tests check both agree byte for byte.
# ---------------------------------------------------------------------------

_I64 = np.int64


def varints(values: np.ndarray[Any, Any]) -> tuple[bytes, np.ndarray[Any, Any]]:
    """Varint-encode non-negative integers; return the bytes and each value's start offset (plus the end)."""
    v = values.astype(np.uint64)
    nbytes = np.ones(len(v), dtype=_I64)
    rest = v >> np.uint64(7)
    while rest.any():
        nbytes += rest > 0
        rest >>= np.uint64(7)
    starts = np.zeros(len(v) + 1, dtype=_I64)
    np.cumsum(nbytes, out=starts[1:])
    buf = np.empty(int(starts[-1]), dtype=np.uint8)
    cur = v.copy()
    for k in range(int(nbytes.max()) if len(v) else 0):
        m = nbytes > k
        byte = (cur[m] & np.uint64(0x7F)).astype(np.uint8)
        byte |= (nbytes[m] > k + 1).astype(np.uint8) << 7
        buf[starts[:-1][m] + k] = byte
        cur[m] >>= np.uint64(7)
    return buf.tobytes(), starts


def _zz(d: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    return ((d << 1) ^ (d >> 63)).astype(np.uint64)


def _normalise(
    gtype: int, offsets: tuple[np.ndarray[Any, Any], ...], ncoords: int, ngeoms: int
) -> tuple[Any, Any, Any]:
    """``(part_coord_offsets, poly_part_offsets, geom_poly_offsets)`` for any ragged layout.

    Lines have one "polygon" per part; points one part per geometry.
    """
    ar = np.arange
    if gtype == 0:  # Point
        return ar(ncoords + 1), ar(ngeoms + 1), ar(ngeoms + 1)
    if gtype in (1, 4):  # LineString, MultiPoint: one part per geometry
        return offsets[0], ar(ngeoms + 1), ar(ngeoms + 1)
    if gtype == 5:  # MultiLineString
        parts, geoms = offsets
        return parts, ar(len(parts)), geoms
    if gtype == 3:  # Polygon
        rings, geoms = offsets
        return rings, geoms, ar(ngeoms + 1)
    if gtype == 6:  # MultiPolygon
        return offsets[0], offsets[1], offsets[2]
    raise ValueError(f"unexpected geometry type {gtype}")


def encode_pieces(geoms: np.ndarray[Any, Any]) -> tuple[int, list[bytes | None]]:
    """Encode same-dimension tile-coordinate geometries; ``(mvt_type, payload or None per geometry)``."""
    n = len(geoms)
    if n == 0:
        return 0, []
    gtype, coords, offsets = shapely.to_ragged_array(geoms, include_z=False)
    gtype = int(gtype)
    c = np.rint(coords).astype(_I64)
    part_off, poly_off, geom_off = (np.asarray(o, dtype=_I64) for o in _normalise(gtype, offsets, len(c), n))
    nparts = len(part_off) - 1
    part_len = np.diff(part_off)
    part_of = np.repeat(np.arange(nparts), part_len)
    # which polygon / geometry each part belongs to
    poly_of_part = np.repeat(np.arange(len(poly_off) - 1), np.diff(poly_off))
    geom_of_poly = np.repeat(np.arange(n), np.diff(geom_off))
    geom_of_part = geom_of_poly[poly_of_part]

    if gtype in (0, 4):  # points: a geometry is one MoveTo with every point
        keep_c = np.ones(len(c), dtype=bool)
        kind = POINT
    else:
        first = np.zeros(len(c), dtype=bool)
        first[part_off[:-1][part_len > 0]] = True
        same = np.zeros(len(c), dtype=bool)
        same[1:] = (c[1:] == c[:-1]).all(axis=1)
        keep_c = first | ~same
        polygon = gtype in (3, 6)
        kind = POLYGON if polygon else LINESTRING
    c = c[keep_c]
    part_of = part_of[keep_c]
    if kind == POLYGON and len(c):
        # drop each ring's closing point (the ClosePath command closes the ring), after deduplication
        cnt = np.bincount(part_of, minlength=nparts)
        st = np.zeros(nparts + 1, dtype=_I64)
        np.cumsum(cnt, out=st[1:])
        ok = cnt > 1
        last = st[1:][ok] - 1
        closing = np.zeros(len(c), dtype=bool)
        closing[last] = (c[last] == c[st[:-1][ok]]).all(axis=1)
        c = c[~closing]
        part_of = part_of[~closing]
    plen = np.bincount(part_of, minlength=nparts).astype(_I64)
    pstart = np.zeros(nparts + 1, dtype=_I64)
    np.cumsum(plen, out=pstart[1:])

    if kind == POINT:
        keep_part = plen > 0
    elif kind == LINESTRING:
        keep_part = plen >= 2
    else:
        # twice the signed area of each ring (cyclic shoelace)
        nxt = np.arange(len(c)) + 1
        ends = pstart[1:][plen > 0] - 1
        nxt[ends] = pstart[:-1][plen > 0]
        cross = c[:, 0] * c[nxt, 1] - c[nxt, 0] * c[:, 1] if len(c) else np.zeros(0, dtype=_I64)
        area = np.bincount(part_of, weights=cross, minlength=nparts) if len(c) else np.zeros(nparts)
        keep_part = (plen >= 3) & (area != 0)
        exterior = np.zeros(nparts, dtype=bool)
        exterior[poly_off[:-1][np.diff(poly_off) > 0]] = True
        # a polygon whose shell has no area loses its holes too
        shell_ok = np.zeros(len(poly_off) - 1, dtype=bool)
        shell_ok[poly_of_part[exterior]] = keep_part[exterior]
        keep_part &= shell_ok[poly_of_part]
        flip = keep_part & ((area > 0) != exterior)
        if flip.any():
            idx = np.arange(len(c))
            fc = flip[part_of]
            s0 = pstart[:-1][part_of]
            e0 = pstart[1:][part_of] - 1
            # keep the first point, reverse the rest (what reversing the closed ring gives)
            fc &= idx != s0
            idx[fc] = (e0 + 1 + s0 - idx)[fc]
            c = c[idx]

    # sequence of kept coordinates
    kc = keep_part[part_of]
    seq = c[kc]
    seq_part = part_of[kc]
    kept = np.flatnonzero(keep_part)
    klen = plen[kept]
    kgeom = geom_of_part[kept]
    if kind == POINT:
        per_part = 2 * klen + 1
    elif kind == LINESTRING:
        per_part = 2 * klen + 2
    else:
        per_part = 2 * klen + 3
    out_start = np.zeros(len(kept) + 1, dtype=_I64)
    np.cumsum(per_part, out=out_start[1:])
    ints = np.zeros(int(out_start[-1]), dtype=np.uint64)

    # deltas within a geometry (the cursor carries across its parts, resets per geometry)
    prev = np.zeros_like(seq)
    if len(seq):
        prev[1:] = seq[:-1]
        seq_geom = kgeom[np.searchsorted(kept, seq_part)]
        gfirst = np.ones(len(seq), dtype=bool)
        gfirst[1:] = seq_geom[1:] != seq_geom[:-1]
        prev[gfirst] = 0
    d = _zz(seq - prev)

    # output position of each coordinate pair
    rank = np.searchsorted(kept, seq_part)
    within = np.arange(len(seq)) - np.repeat(np.cumsum(klen) - klen, klen)
    pos = out_start[:-1][rank] + 1 + 2 * within
    if kind != POINT:
        pos += within >= 1  # the LineTo command sits after the first point
    ints[pos] = d[:, 0]
    ints[pos + 1] = d[:, 1]
    s = out_start[:-1]
    if kind == POINT:
        ints[s] = (1 | (klen << 3)).astype(np.uint64)
    else:
        ints[s] = 1 | (1 << 3)
        ints[s + 3] = (2 | ((klen - 1) << 3)).astype(np.uint64)
        if kind == POLYGON:
            ints[out_start[1:] - 1] = 7 | (1 << 3)

    buf, bstart = varints(ints)
    payloads: list[bytes | None] = [None] * n
    if len(kept):
        gs = np.flatnonzero(np.r_[True, kgeom[1:] != kgeom[:-1]])
        ge = np.r_[gs[1:], len(kept)]
        lo = bstart[out_start[gs]]
        hi = bstart[out_start[ge]]
        for g, a, b in zip(kgeom[gs].tolist(), lo.tolist(), hi.tolist(), strict=True):
            payloads[g] = buf[a:b]
    return kind, payloads
