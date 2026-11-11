"""Cut geometries into vector tiles: project, simplify, clip, snap to the tile grid.

Geometries are first projected to *unit world* coordinates: Web Mercator scaled
so the whole world is the square [0, 1] x [0, 1], y down (the XYZ scheme). At
zoom ``z`` a tile is ``extent`` units wide, so world units are ``unit * extent *
2**z`` and tile ``(x, y)`` covers ``[x*extent, (x+1)*extent)``.

Per zoom a feature is simplified once in world units (so neighbouring tiles see
the same simplified line and no seams open at tile edges), then split by a
quadtree: clip to the block of tiles it touches, split the block in four, and
recurse. That costs O(vertices * log tiles) instead of clipping the whole
geometry once per tile. Each tile's piece is snapped to the integer grid with
``shapely.set_precision``, which also repairs anything the clip or the snap made
invalid.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Any

import numpy as np
import shapely

from .edges import keep_dimension

MAX_LAT = 85.0511287798066  # Web Mercator's square: atan(sinh(pi))
_HALF = math.pi * 6378137.0


def lonlat_to_unit(coords: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    """Map lon/lat degrees to unit world coordinates (latitudes clamped to the Mercator square)."""
    lon = coords[:, 0]
    lat = np.clip(coords[:, 1], -MAX_LAT, MAX_LAT)
    x = (lon + 180.0) / 360.0
    y = 0.5 - np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)) / (2 * np.pi)
    out = np.column_stack([x, y])
    return out


def mercator_to_unit(coords: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    """Map EPSG:3857 metres to unit world coordinates."""
    x = (coords[:, 0] + _HALF) / (2 * _HALF)
    y = (_HALF - coords[:, 1]) / (2 * _HALF)
    return np.column_stack([np.clip(x, 0.0, 1.0), np.clip(y, 0.0, 1.0)])


def unit_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Inverse of :func:`lonlat_to_unit` for one point (used for bounds)."""
    lon = x * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y))))
    return lon, lat


def dimension(geom: Any) -> int:
    """0 for points, 1 for lines, 2 for polygons (the highest dimension present)."""
    return int(shapely.get_dimensions(geom))


def split_by_dimension(geom: Any) -> list[tuple[int, Any]]:
    """Split a geometry collection into one geometry per dimension; anything else is itself."""
    if shapely.get_type_id(geom) != 7:
        return [(dimension(geom), geom)]
    out = []
    for dim in (0, 1, 2):
        part = keep_dimension(geom, dim) if dim else _points(geom)
        if part is not None:
            out.append((dim, part))
    return out


def _points(geom: Any) -> Any | None:
    pts = [p for p in shapely.get_parts(shapely.get_parts(geom)) if shapely.get_type_id(p) == 0 and not p.is_empty]
    if not pts:
        return None
    return pts[0] if len(pts) == 1 else shapely.MultiPoint(pts)


class ZoomCutter:
    """Cut unit-world geometries into the tiles of one zoom level."""

    def __init__(self, z: int, extent: int, buffer: float, tolerance: float) -> None:
        """``buffer`` and ``tolerance`` are in tile units (``extent`` per tile)."""
        self.z = z
        self.extent = extent
        self.n = 1 << z
        self.scale = float(extent) * self.n
        self.buffer = buffer
        self.tolerance = tolerance

    def cut_many(
        self, geoms: np.ndarray[Any, Any], dim: int
    ) -> tuple[np.ndarray[Any, Any], np.ndarray[Any, Any], np.ndarray[Any, Any], np.ndarray[Any, Any]]:
        """Cut an array of same-dimension geometries: ``(x, y, index into geoms, piece)`` arrays.

        Pieces are in integer tile coordinates. Geometries that fit inside one
        tile (most of them, at most zooms) are handled as whole arrays; the rest
        go through the per-geometry quadtree.
        """
        e, b, n = float(self.extent), self.buffer, self.n
        g = shapely.transform(geoms, lambda c: c * self.scale)
        if dim and self.tolerance > 0:
            g = shapely.simplify(g, self.tolerance, preserve_topology=True)
        live = ~shapely.is_empty(g)
        bounds = shapely.bounds(g)
        with np.errstate(invalid="ignore"):
            x0 = np.floor((bounds[:, 0] - b) / e)
            y0 = np.floor((bounds[:, 1] - b) / e)
            x1 = np.floor((bounds[:, 2] + b) / e)
            y1 = np.floor((bounds[:, 3] + b) / e)
        live &= (x1 >= 0) & (y1 >= 0) & (x0 <= n - 1) & (y0 <= n - 1)
        # inside one tile, buffer included: no clipping needed
        single = live & (x0 == x1) & (y0 == y1)
        xs: list[Any] = []
        ys: list[Any] = []
        idx: list[Any] = []
        pieces: list[Any] = []
        si = np.flatnonzero(single)
        if len(si):
            sx = x0[si].astype(np.int64)
            sy = y0[si].astype(np.int64)
            sg = g[si].copy()
            coords, owner = shapely.get_coordinates(sg, return_index=True)
            coords -= np.column_stack([sx, sy])[owner] * e
            sg = shapely.set_coordinates(sg, coords)
            xs.append(sx)
            ys.append(sy)
            idx.append(si)
            pieces.append(sg)
        mx: list[int] = []
        my: list[int] = []
        mi: list[int] = []
        mp: list[Any] = []
        for i in np.flatnonzero(live & ~single).tolist():
            lo_x, lo_y = max(0, int(x0[i])), max(0, int(y0[i]))
            hi_x, hi_y = min(n - 1, int(x1[i])), min(n - 1, int(y1[i]))
            for tx, ty, piece in self._split(g[i], dim, lo_x, lo_y, hi_x, hi_y):
                mx.append(tx)
                my.append(ty)
                mi.append(i)
                mp.append(piece)
        if mi:
            xs.append(np.array(mx, dtype=np.int64))
            ys.append(np.array(my, dtype=np.int64))
            idx.append(np.array(mi, dtype=np.int64))
            pieces.append(np.array(mp, dtype=object))
        if not idx:
            empty = np.zeros(0, dtype=np.int64)
            return empty, empty, empty, np.zeros(0, dtype=object)
        ax, ay, ai, ap = (np.concatenate(v) for v in (xs, ys, idx, pieces))
        ap = _snap(ap, dim)
        keep = ~shapely.is_missing(ap)
        return ax[keep], ay[keep], ai[keep], ap[keep]

    def cut(self, geom: Any, dim: int) -> Iterator[tuple[int, int, Any]]:
        """Yield ``(x, y, piece)`` for one geometry (see :meth:`cut_many`)."""
        x, y, _, p = self.cut_many(np.array([geom], dtype=object), dim)
        yield from zip(x.tolist(), y.tolist(), p, strict=True)

    def _split(self, g: Any, dim: int, x0: int, y0: int, x1: int, y1: int) -> Iterator[tuple[int, int, Any]]:
        e, b = self.extent, self.buffer
        if x0 == x1 and y0 == y1:
            piece = _clip(g, dim, (x0 * e - b, y0 * e - b, (x0 + 1) * e + b, (y0 + 1) * e + b))
            if piece is not None:
                yield x0, y0, shapely.transform(piece, lambda c: c - (x0 * e, y0 * e))
            return
        if x1 - x0 >= y1 - y0:
            mid = (x0 + x1) // 2
            halves = ((x0, y0, mid, y1), (mid + 1, y0, x1, y1))
        else:
            mid = (y0 + y1) // 2
            halves = ((x0, y0, x1, mid), (x0, mid + 1, x1, y1))
        for hx0, hy0, hx1, hy1 in halves:
            box = (hx0 * e - b, hy0 * e - b, (hx1 + 1) * e + b, (hy1 + 1) * e + b)
            part = _clip(g, dim, box)
            if part is not None:
                yield from self._split(part, dim, hx0, hy0, hx1, hy1)


_TYPES = {0: (0, 4), 1: (1, 5), 2: (3, 6)}


def _snap(pieces: np.ndarray[Any, Any], dim: int) -> np.ndarray[Any, Any]:
    """Snap to the integer grid (repairing validity); None where nothing of ``dim`` is left."""
    try:
        out = shapely.set_precision(pieces, 1.0)
    except shapely.errors.GEOSException:
        out = np.array([_snap_one(p) for p in pieces], dtype=object)
    out = np.asarray(out, dtype=object)
    ok = np.isin(shapely.get_type_id(out), _TYPES[dim]) & ~shapely.is_empty(out)
    for i in np.flatnonzero(~ok).tolist():
        p = out[i]
        out[i] = None if p is None or p.is_empty or dim == 0 else keep_dimension(p, dim)
    return out


def _snap_one(p: Any) -> Any:
    try:
        return shapely.set_precision(p, 1.0)
    except shapely.errors.GEOSException:
        return shapely.set_precision(shapely.make_valid(p), 1.0)


def _clip(g: Any, dim: int, box: tuple[float, float, float, float]) -> Any | None:
    """Clip to a rectangle; None when nothing of the right dimension is left."""
    minx, miny, maxx, maxy = shapely.bounds(g).tolist()
    if minx >= box[0] and miny >= box[1] and maxx <= box[2] and maxy <= box[3]:
        return g  # wholly inside
    if maxx < box[0] or maxy < box[1] or minx > box[2] or miny > box[3]:
        return None
    if dim == 0:
        out = shapely.intersection(g, shapely.box(*box))
        return None if out.is_empty else out
    out = shapely.clip_by_rect(g, *box)
    if out.is_empty:
        return None
    return keep_dimension(out, dim)


def tile_ids(z: int, x: np.ndarray[Any, Any], y: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    """PMTiles tile ids (Hilbert order within a zoom) for arrays of tile columns and rows.

    The same arithmetic as ``pmtiles.tile.zxy_to_tileid``, on whole arrays.
    """
    x = x.astype(np.int64).copy()
    y = y.astype(np.int64).copy()
    acc = np.full(len(x), ((1 << (2 * z)) - 1) // 3, dtype=np.int64)
    for a in range(z - 1, -1, -1):
        s = 1 << a
        rx = x & s
        ry = y & s
        acc += ((3 * rx) ^ ry) << a
        # rotate the quadrant
        flip = (ry == 0) & (rx != 0)
        x = np.where(flip, s - 1 - x, x)
        y = np.where(flip, s - 1 - y, y)
        swap = ry == 0
        x, y = np.where(swap, y, x), np.where(swap, x, y)
    return acc
