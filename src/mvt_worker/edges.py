"""One row per feature: drop tile buffers and join the pieces of features that cross tile edges.

A tileset stores each feature once per tile it touches, and encoders add a
buffer around every tile, so a raw read returns a feature near or across a tile
edge several times. ``tile_edges`` picks how ``read_tiles`` deals with that:

``merge`` (default)
    Points are kept only in the tile that owns them (``0 <= x, y < extent``).
    Lines and polygons that reach a tile edge are clipped to their tile, and the
    clipped pieces that share a layer, feature id, geometry kind and properties
    are joined across neighbouring tiles into one geometry (GEOS union / line
    merge). The joined row is reported at the first tile, in read order, holding
    a piece. Features without an id are clipped but not joined, so they never
    overlap but may come back as several pieces.
``clip``
    The same, without joining: one non-overlapping piece per tile.
``raw``
    Exactly what the tiles store, buffers included.

Work happens in a global pixel grid (``REF`` units per tile) so pieces from
neighbouring tiles share exact coordinates; geometry is projected to the output
coordinate system only when a row is written. Pieces outside the tiles being
read are not fetched: a feature is cut at the edge of the area read.
"""

from __future__ import annotations

import json
import math
import threading
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import shapely
from shapely.errors import GEOSException

from .mvt import Coordinates, Projector, PropertyValue, Ring, decode_geometry, decode_tile, parts_wkb
from .rows import RowBuilder
from .sources import TileKey, TileSource

TileEdges = Literal["merge", "clip", "raw"]
TILE_EDGE_CHOICES: tuple[TileEdges, ...] = ("merge", "clip", "raw")

REF = 1 << 16  # global grid units per tile: exact for every power-of-two extent up to 65536
_HALF_CIRCUMFERENCE = math.pi * 6378137.0
_NEIGHBOUR_CACHE = 256
EDGE_MARGIN = 16  # a feature within extent/16 of an edge (256 px of 4096) may continue in the next tile

# (layer, feature id, geometry dimension, properties as JSON)
EdgeKey = tuple[str, int | None, int, str]
Node = tuple[TileKey, int]  # (tile, index of the piece in that tile)
TOUCH = REF / 1024  # pieces without an id join when they come this close (4 px of 4096) at the edge


class _GlobalPixels:
    """Map a tile's pixels to the global grid (``REF`` units per tile)."""

    def __init__(self, x: int, y: int, extent: int) -> None:
        self.ox = float(x) * REF
        self.oy = float(y) * REF
        self.k = REF / float(extent)

    def __call__(self, ring: Ring) -> list[float]:
        ox, oy, k = self.ox, self.oy, self.k
        out: list[float] = []
        for px, py in ring:
            out.append(ox + px * k)
            out.append(oy + py * k)
        return out


@dataclass(slots=True)
class Piece:
    """The part of one feature that lies inside one tile, in global grid units."""

    layer: str
    fid: int | None
    dim: int  # 1 lines, 2 polygons
    extent: int
    props: dict[str, PropertyValue | None]
    geom: Any  # shapely geometry
    reach: tuple[bool, bool, bool, bool]  # the unclipped feature reaches (or nears) west, north, east, south

    def key(self) -> EdgeKey:
        """Pieces of one feature share a key (and, without an id, must also meet at the tile edge)."""
        return (self.layer, self.fid, self.dim, json.dumps(self.props, sort_keys=True, default=str))


Row = tuple[str, int | None, str, bytes | None, dict[str, PropertyValue | None]]


def _flatten(geom: Any) -> Iterator[Any]:
    for part in shapely.get_parts(geom):
        if part.geom_type.startswith("Multi") or part.geom_type == "GeometryCollection":
            yield from _flatten(part)
        elif not part.is_empty:
            yield part


def keep_dimension(geom: Any, dim: int) -> Any | None:
    """Keep the parts of ``geom`` of dimension ``dim`` (clipping can leave stray points/lines); None if none."""
    parts = [p for p in _flatten(geom) if shapely.get_dimensions(p) == dim]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return shapely.MultiPolygon(parts) if dim == 2 else shapely.MultiLineString(parts)


def clip_to_tile(geom: Any, x: int, y: int, dim: int) -> Any | None:
    """Intersect a global-grid geometry with tile ``x, y``; repair invalid input first; None if nothing is left."""
    box = shapely.box(x * REF, y * REF, (x + 1) * REF, (y + 1) * REF)
    try:
        out = shapely.intersection(geom, box, grid_size=1.0)
    except GEOSException:
        try:
            out = shapely.intersection(shapely.make_valid(geom), box, grid_size=1.0)
        except GEOSException:
            out = shapely.clip_by_rect(geom, *box.bounds)
    return keep_dimension(out, dim)


def _snap_line_ends(geoms: list[Any]) -> list[Any]:
    """Move line ends that lie within ``TOUCH`` of another end onto it.

    Each tile simplified its copy of a line on its own, so the two ends that
    meet at a tile edge can be a fraction of a pixel apart and would not merge.
    """
    lines = [list(part.coords) for g in geoms for part in _flatten(g)]

    def on_edge(v: float) -> bool:
        r = v % REF
        return min(r, REF - r) <= TOUCH

    # Only ends on a tile edge can be cut points; the rest are the feature's own ends.
    ends = [(i, j) for i in range(len(lines)) for j in (0, -1) if on_edge(lines[i][j][0]) or on_edge(lines[i][j][1])]
    if len(ends) > 2000:  # quadratic below; very long features keep their tiny gaps
        ends = []
    for a, (ia, ja) in enumerate(ends):
        ax, ay = lines[ia][ja][:2]
        for ib, jb in ends[a + 1 :]:
            bx, by = lines[ib][jb][:2]
            if (bx, by) != (ax, ay) and abs(bx - ax) <= TOUCH and abs(by - ay) <= TOUCH:
                lines[ib][jb] = (ax, ay)
    return [shapely.LineString(c) for c in lines if len(c) >= 2]


def join_pieces(geoms: list[Any], dim: int) -> Any | None:
    """Union a feature's pieces; lines also get their ends joined where they meet at tile edges."""
    if len(geoms) == 1:
        return geoms[0]
    if dim == 1:
        geoms = _snap_line_ends(geoms)
    try:
        out = shapely.union_all(geoms, grid_size=1.0)
    except GEOSException:
        cls = shapely.MultiPolygon if dim == 2 else shapely.MultiLineString
        out = cls([p for g in geoms for p in _flatten(g)])
    if dim == 1:
        out = shapely.line_merge(out)
    return keep_dimension(out, dim)


def project_global(geom: Any, z: int, x: int, y: int, extent: int, coordinates: Coordinates) -> tuple[str, bytes]:
    """Project a global-grid geometry to the output coordinates; return ``(geometry_type, wkb)``."""
    size = float(REF) * (1 << z)

    def fn(c: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
        gx, gy = c[:, 0], c[:, 1]
        if coordinates == "tile":  # pixels of the reporting tile
            k = extent / REF
            return np.column_stack(((gx - x * REF) * k, (gy - y * REF) * k))
        if coordinates == "web_mercator":
            h = _HALF_CIRCUMFERENCE
            return np.column_stack((gx / size * 2 * h - h, h - gy / size * 2 * h))
        lon = gx / size * 360.0 - 180.0
        lat = np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * gy / size))))
        return np.column_stack((lon, lat))

    out = shapely.transform(geom, fn)
    return out.geom_type.upper(), shapely.to_wkb(out, byte_order=1, flavor="iso", output_dimension=2)


def split_tile(
    key: TileKey,
    data: bytes,
    *,
    layer_filter: str | None,
    coordinates: Coordinates,
) -> tuple[list[Row], list[Piece]]:
    """Split a tile into finished rows (owned points, features clear of every edge) and edge pieces."""
    z, x, y = key
    rows: list[Row] = []
    pieces: list[Piece] = []
    for layer in decode_tile(data, layer_filter=layer_filter):
        ext = layer.extent
        project = Projector(z, x, y, ext, coordinates)
        grid: _GlobalPixels | None = None
        for f in layer.features:
            parts = decode_geometry(f.geometry)
            if f.geom_type not in (2, 3) or not parts:
                if f.geom_type == 1:
                    points = [p for part in parts for p in part]
                    own = [p for p in points if 0 <= p[0] < ext and 0 <= p[1] < ext]
                    if points and not own:
                        continue  # another tile owns it
                    parts = [own] if own else []
                gtype, wkb = parts_wkb(f.geom_type, parts, project, layer.version)
                rows.append((layer.name, f.id, gtype, wkb, f.properties))
                continue
            min_x = min(px for part in parts for px, _ in part)
            max_x = max(px for part in parts for px, _ in part)
            min_y = min(py for part in parts for _, py in part)
            max_y = max(py for part in parts for _, py in part)
            crosses = min_x < 0 or min_y < 0 or max_x > ext or max_y > ext
            # Encoders simplify each tile on its own, so a neighbour's copy of a feature that crosses the
            # edge can stop a few pixels short of it. Features with an id that come near an edge are
            # therefore treated as pieces too, so they can be joined.
            m = ext // EDGE_MARGIN if f.id is not None else 0
            reach = (min_x <= m, min_y <= m, max_x >= ext - m, max_y >= ext - m)
            if not crosses and not any(reach):
                gtype, wkb = parts_wkb(f.geom_type, parts, project, layer.version)
                rows.append((layer.name, f.id, gtype, wkb, f.properties))
                continue
            grid = grid or _GlobalPixels(x, y, ext)
            _gtype, wkb = parts_wkb(f.geom_type, parts, grid, layer.version)
            if wkb is None:
                continue
            dim = f.geom_type - 1
            geom = shapely.from_wkb(wkb)
            clipped = clip_to_tile(geom, x, y, dim) if crosses else keep_dimension(geom, dim)
            if clipped is not None:
                pieces.append(Piece(layer.name, f.id, dim, ext, f.properties, clipped, reach))
    return rows, pieces


@dataclass(slots=True)
class _TilePieces:
    pieces: list[Piece]
    by_key: dict[EdgeKey, list[int]]
    rows: list[Row] | None  # finished rows, kept until the tile's own turn so it is decoded once


def _index(pieces: list[Piece], rows: list[Row] | None) -> _TilePieces:
    by_key: dict[EdgeKey, list[int]] = {}
    for i, p in enumerate(pieces):
        by_key.setdefault(p.key(), []).append(i)
    return _TilePieces(pieces, by_key, rows)


def _faces(p: Piece, dx: int, dy: int) -> bool:
    """Whether piece ``p`` reaches the side of its tile shared with the neighbour at ``dx, dy``."""
    w, n, e, s = p.reach
    return (dx == 0 or (w if dx < 0 else e)) and (dy == 0 or (n if dy < 0 else s))


def _touch(a: Piece, b: Piece) -> bool:
    ax0, ay0, ax1, ay1 = a.geom.bounds
    bx0, by0, bx1, by1 = b.geom.bounds
    if ax0 > bx1 + TOUCH or bx0 > ax1 + TOUCH or ay0 > by1 + TOUCH or by0 > ay1 + TOUCH:
        return False
    return bool(shapely.distance(a.geom, b.geom) <= TOUCH)


class EdgeAssembler:
    """Per-query state for ``tile_edges``: which pieces were already written, plus neighbour tiles.

    Pieces are nodes of a graph. Two pieces in neighbouring tiles (or, with an
    id, in the same tile) are linked when they share a key and either has an id
    or they meet at the tile edge. Each connected group is written once, by its
    first piece in read order. Tiles are processed in that order, so in one
    process the first piece is met first and marks the whole group ``done``. A
    process that has not seen earlier tiles (HTTP transport) recomputes the
    group, finds an earlier piece and leaves it to whoever owns it, so a group
    is never written twice. Links are tested from both sides, so every process
    finds the same group.
    """

    def __init__(
        self,
        source: TileSource,
        keys: list[TileKey],
        *,
        layer_filter: str | None,
        coordinates: Coordinates,
        mode: TileEdges,
    ) -> None:
        """Bind to the tile list being read."""
        self.source = source
        self.order = {k: i for i, k in enumerate(keys)}
        self.layer_filter = layer_filter
        self.coordinates = coordinates
        self.mode = mode
        self.done: set[Node] = set()
        self._tiles: OrderedDict[TileKey, _TilePieces] = OrderedDict()
        self.lock = threading.Lock()

    def add_tile(self, rows: RowBuilder, key: TileKey, data: bytes) -> None:
        """Append the rows that tile ``key`` contributes."""
        if self.mode == "raw":
            rows.add_tile(*key, data)
            return
        z, x, y = key
        cached = self._tiles.get(key) if self.mode == "merge" else None
        if cached is not None and cached.rows is not None:
            finished, pieces = cached.rows, cached.pieces
            cached.rows = None
        else:
            finished, pieces = split_tile(key, data, layer_filter=self.layer_filter, coordinates=self.coordinates)
        for name, fid, gtype, wkb, props in finished:
            rows.append(name, z, x, y, fid, gtype, wkb, props)
        if self.mode == "clip":
            for p in pieces:
                self._write(rows, key, p, p.geom)
            return
        self._remember(key, _index(pieces, None))
        for i, p in enumerate(pieces):
            if (key, i) in self.done:
                continue
            group = self._group((key, i))
            if group is None:
                continue
            self.done.update(group)
            joined = join_pieces([self._tile(t).pieces[j].geom for t, j in group], p.dim)
            if joined is not None:
                self._write(rows, key, p, joined)

    def _write(self, rows: RowBuilder, key: TileKey, piece: Piece, geom: Any) -> None:
        z, x, y = key
        gtype, wkb = project_global(geom, z, x, y, piece.extent, self.coordinates)
        rows.append(piece.layer, z, x, y, piece.fid, gtype, wkb, piece.props)

    def _rank(self, node: Node) -> tuple[int, int]:
        return (self.order[node[0]], node[1])

    def _group(self, start: Node) -> list[Node] | None:
        """Return the pieces linked to ``start``; None when one of them comes before it in read order."""
        first = self._rank(start)
        seen = {start}
        stack = [start]
        found: list[Node] = []
        while stack:
            node = stack.pop()
            if self._rank(node) < first:
                return None
            found.append(node)
            tile, i = node
            p = self._tile(tile).pieces[i]
            k = p.key()
            links: list[Node] = []
            if p.fid is not None:
                links += [(tile, j) for j in self._tile(tile).by_key.get(k, ())]
            z, x, y = tile
            self._load([(z, x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (z, x + dx, y + dy)
                    if (dx == dy == 0) or n not in self.order:
                        continue
                    other = self._tile(n)
                    for j in other.by_key.get(k, ()):
                        q = other.pieces[j]
                        if not (_faces(p, dx, dy) or _faces(q, -dx, -dy)):
                            continue
                        if p.fid is not None or _touch(p, q):
                            links.append((n, j))
            for link in links:
                if link not in seen:
                    seen.add(link)
                    stack.append(link)
        return found

    def _remember(self, key: TileKey, pieces: _TilePieces) -> None:
        self._tiles[key] = pieces
        self._tiles.move_to_end(key)
        while len(self._tiles) > _NEIGHBOUR_CACHE:
            self._tiles.popitem(last=False)

    def _load(self, keys: list[TileKey]) -> None:
        """Fetch and split the tiles of ``keys`` (in the read list, not cached yet) in one round trip."""
        missing = [k for k in keys if k in self.order and k not in self._tiles]
        if not missing:
            return
        got = dict(self.source.fetch(missing))
        for k in missing:
            data = got.get(k)
            if data is None:
                self._remember(k, _index([], []))
                continue
            rows, pieces = split_tile(k, data, layer_filter=self.layer_filter, coordinates=self.coordinates)
            self._remember(k, _index(pieces, rows))

    def _tile(self, key: TileKey) -> _TilePieces:
        hit = self._tiles.get(key)
        if hit is None:
            self._load([key])
            hit = self._tiles.get(key) or _index([], None)
        if key in self._tiles:
            self._tiles.move_to_end(key)
        return hit
