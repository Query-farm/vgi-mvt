"""tile_edges: features crossing tile edges come back once (tests/mvt_encode.py builds the tiles)."""

from __future__ import annotations

from collections.abc import Iterable

import pytest
import shapely
from conftest import FIXTURES
from mvt_encode import F, geometry, layer, points, tile

from mvt_worker.edges import EdgeAssembler, TileEdges
from mvt_worker.mvt import Coordinates
from mvt_worker.rows import RowBuilder, output_schema
from mvt_worker.sources import TileKey, open_source

# Two neighbouring tiles at zoom 1, extent 4096, written with a 64 px buffer as encoders do.
WEST: TileKey = (1, 0, 0)
EAST: TileKey = (1, 1, 0)


def _ring(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def _tiles() -> dict[TileKey, bytes]:
    west = [
        F(3, geometry([_ring(3000, 1000, 4160, 2000)], close=True), {"kind": "park"}, id=7),
        F(2, geometry([[(3500, 600), (4160, 600)]], close=False), {"name": "a"}, id=9),
        F(2, geometry([[(3600, 800), (4160, 800)]], close=False), {"name": "b"}),  # no id: joins by touch
        F(2, geometry([[(3600, 1500), (4160, 1500)]], close=False), {"name": "c"}),  # no id, no partner
        F(1, points([(4100, 100)]), {"name": "p"}),  # in the buffer: the east tile owns it
        F(3, geometry([_ring(100, 100, 200, 200)], close=True), {"kind": "pond"}, id=11),  # interior
    ]
    east = [
        F(3, geometry([_ring(-64, 1000, 1000, 2000)], close=True), {"kind": "park"}, id=7),
        F(2, geometry([[(-64, 600), (800, 600)]], close=False), {"name": "a"}, id=9),
        F(2, geometry([[(-64, 800), (700, 800)]], close=False), {"name": "b"}),
        F(2, geometry([[(-64, 3000), (500, 3000)]], close=False), {"name": "c"}),
        F(1, points([(4, 100)]), {"name": "p"}),
    ]
    return {WEST: tile(layer("l", west)), EAST: tile(layer("l", east))}


class FakeSource:
    """A TileSource over in-memory tiles."""

    def __init__(self, tiles: dict[TileKey, bytes]) -> None:
        self.data = tiles

    def fetch(self, keys: Iterable[TileKey]) -> list[tuple[TileKey, bytes]]:
        return [(k, self.data[k]) for k in keys if k in self.data]


def _read(
    mode: TileEdges, keys: list[TileKey], *, only: list[TileKey] | None = None, coordinates: Coordinates = "tile"
) -> list[dict[str, object]]:
    src = FakeSource(_tiles())
    edges = EdgeAssembler(src, keys, layer_filter=None, coordinates=coordinates, mode=mode)  # type: ignore[arg-type]
    rows = RowBuilder(output_schema(None, coordinates), coordinates, None)
    for k, data in src.fetch(only or keys):
        edges.add_tile(rows, k, data)
    return rows.take().to_pylist()


def _by_name(rows: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    out: dict[str, list[dict[str, object]]] = {}
    for r in rows:
        props = str(r["properties"])
        out.setdefault(props, []).append(r)
    return out


def test_raw_keeps_every_copy() -> None:
    rows = _read("raw", [WEST, EAST])
    assert len(rows) == 11


def test_merge_one_row_per_feature() -> None:
    rows = _read("merge", [WEST, EAST])
    got = _by_name(rows)
    assert sorted(len(v) for v in got.values()) == [1, 1, 1, 1, 1, 2]  # only the unmatched 'c' stays split
    park = shapely.from_wkb(got['{"kind": "park"}'][0]["geometry"])
    assert park.geom_type == "Polygon"
    assert park.bounds == (3000.0, 1000.0, 5096.0, 2000.0)  # tile pixels of the west (first) tile
    assert park.area == pytest.approx(2096 * 1000)
    road = shapely.from_wkb(got['{"name": "a"}'][0]["geometry"])
    assert road.geom_type == "LineString"
    assert road.length == pytest.approx(4896 - 3500)
    untagged = shapely.from_wkb(got['{"name": "b"}'][0]["geometry"])
    assert untagged.geom_type == "LineString"
    assert untagged.length == pytest.approx(4796 - 3600)
    point = got['{"name": "p"}']
    assert [(r["x"], r["y"]) for r in point] == [(1, 0)]


def test_clip_drops_buffers_without_joining() -> None:
    rows = _read("clip", [WEST, EAST])
    areas = [shapely.from_wkb(r["geometry"]).area for r in rows if r["feature_id"] == 7]
    assert areas == [pytest.approx(1096 * 1000), pytest.approx(1000 * 1000)]  # no overlap
    assert sum(1 for r in rows if r["properties"] == '{"name": "p"}') == 1


def test_merge_in_wgs84_matches_the_tiles() -> None:
    rows = _read("merge", [WEST, EAST], coordinates="wgs84")
    park = next(r for r in rows if r["feature_id"] == 7)
    w, _s, e, _n = shapely.from_wkb(park["geometry"]).bounds
    assert w == pytest.approx(3000 / 4096 * 180 - 180)
    assert e == pytest.approx(1000 / 4096 * 180)


def test_a_process_that_starts_mid_read_writes_nothing_twice() -> None:
    """Over HTTP each batch can be served by a fresh process; the halves must add up to the whole."""
    keys = [WEST, EAST]
    whole = _read("merge", keys)
    halves = _read("merge", keys, only=[WEST]) + _read("merge", keys, only=[EAST])

    def key(r: dict[str, object]) -> tuple[object, ...]:
        return (r["properties"], r["x"], r["y"], r["geometry"])

    assert sorted(map(key, halves), key=repr) == sorted(map(key, whole), key=repr)


def test_merge_on_the_gdal_fixture_is_the_same_at_every_zoom() -> None:
    src = open_source(str(FIXTURES / "edges.pmtiles"))
    for z in (0, 1, 2, 3):
        keys = src.tiles(z, None, 100)
        edges = EdgeAssembler(src, keys, layer_filter=None, coordinates="wgs84", mode="merge")
        rows = RowBuilder(output_schema(None), "wgs84", None)
        for k, data in src.fetch(keys):
            edges.add_tile(rows, k, data)
        out = rows.take().to_pylist()
        assert sorted(r["geometry_type"] for r in out) == ["LINESTRING", "LINESTRING", "POLYGON", "POLYGON"], z
        big = max(
            (shapely.from_wkb(r["geometry"]) for r in out if r["geometry_type"] == "POLYGON"), key=lambda g: g.area
        )
        assert big.area == pytest.approx(20 * 12, rel=0.01), z
