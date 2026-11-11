"""The PMTiles writer: encoder, tiler, archive, and round trips through the reader."""

from __future__ import annotations

import itertools
import json
import math
import random
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
import shapely
from pmtiles.tile import zxy_to_tileid

from mvt_worker.encode import _varint, encode_pieces, geometry_commands, pack, varints
from mvt_worker.mvt import classify_rings, decode_geometry, decode_tile, maybe_decompress
from mvt_worker.sources import open_source
from mvt_worker.tiler import tile_ids
from mvt_worker.write_tiles import WriteArgs, collect, resolve_roles, write_archive

GEO = {b"ARROW:extension:name": b"geoarrow.wkb", b"ARROW:extension:metadata": b'{"crs":"OGC:CRS84"}'}
MERC = {b"ARROW:extension:name": b"geoarrow.wkb", b"ARROW:extension:metadata": b'{"crs":"EPSG:3857"}'}
JSON = {b"ARROW:extension:name": b"arrow.json"}


def batch(geoms: list[Any], *, meta: dict[bytes, bytes] = GEO, **cols: Any) -> pa.RecordBatch:
    """A COPY-source batch: the given columns plus a WKB ``geometry`` column."""
    arrays = []
    fields = []
    for name, values in cols.items():
        arr = values if isinstance(values, pa.Array) else pa.array(values)
        fields.append(pa.field(name, arr.type, metadata=JSON if name == "properties_json" else None))
        arrays.append(arr)
    wkb = [None if g is None else shapely.to_wkb(g) for g in geoms]
    fields.append(pa.field("geometry", pa.binary(), metadata=meta))
    arrays.append(pa.array(wkb, pa.binary()))
    return pa.RecordBatch.from_arrays(arrays, schema=pa.schema(fields))


def write(tmp_path: Path, batches: list[pa.RecordBatch], name: str = "out.pmtiles", **opts: Any) -> Path:
    """Run the writer's close step on batches, as the COPY does."""
    options = WriteArgs(**opts)
    out = tmp_path / name
    roles = resolve_roles(batches[0].schema, options)
    data = collect(batches, roles, options, options.layer or out.stem)
    write_archive(str(out), data, options, out.stem)
    return out


def tiles(path: Path, z: int) -> dict[tuple[int, int, int], list[Any]]:
    """Decoded layers of every tile at zoom ``z``."""
    src = open_source(str(path))
    keys = src.tiles(z, None, 1_000_000)
    return {key: decode_tile(maybe_decompress(data)) for key, data in src.fetch(keys)}


# --- encoder ---------------------------------------------------------------


def _rand_geom(rng: random.Random, dim: int) -> Any:
    def pt() -> tuple[int, int]:
        return rng.randint(-50, 4150), rng.randint(-50, 4150)

    def ring(n: int, r: int) -> list[tuple[int, int]]:
        cx, cy = pt()
        pts = [
            (round(cx + r * math.cos(a)), round(cy + r * math.sin(a)))
            for a in sorted(rng.random() * 6.283 for _ in range(n))
        ]
        if rng.random() < 0.5:
            pts = pts[::-1]
        if rng.random() < 0.2:
            pts.insert(1, pts[1])
        return pts + [pts[0]] * rng.randint(1, 3)  # repeated closing points

    if dim == 0:
        return (
            shapely.Point(pt()) if rng.random() < 0.5 else shapely.MultiPoint([pt() for _ in range(rng.randint(1, 6))])
        )
    if dim == 1:
        lines = [shapely.LineString([pt() for _ in range(rng.randint(2, 6))]) for _ in range(rng.randint(1, 3))]
        if rng.random() < 0.1:
            lines.append(shapely.LineString([(5, 5), (5, 5)]))
        return lines[0] if len(lines) == 1 else shapely.MultiLineString(lines)
    polys = [
        shapely.Polygon(
            ring(rng.randint(3, 9), rng.randint(0, 800)),
            [ring(rng.randint(3, 6), rng.randint(0, 40)) for _ in range(rng.randint(0, 2))],
        )
        for _ in range(rng.randint(1, 3))
    ]
    return polys[0] if len(polys) == 1 else shapely.MultiPolygon(polys)


@pytest.mark.parametrize("dim", [0, 1, 2])
def test_vectorised_encoder_matches_reference(dim: int) -> None:
    rng = random.Random(dim)
    for _ in range(200):
        geoms = np.array([_rand_geom(rng, dim) for _ in range(rng.randint(1, 20))], dtype=object)
        _, payloads = encode_pieces(geoms)
        for g, p in zip(geoms, payloads, strict=True):
            ref = geometry_commands(g)
            assert p == (None if ref is None else pack(ref[1])), g.wkt


def test_varints_match_scalar() -> None:
    values = np.array([0, 1, 127, 128, 300, 2**32, 2**63 - 1, 2**64 - 1], dtype=np.uint64)
    buf, starts = varints(values)
    for i, v in enumerate(values.tolist()):
        ref = bytearray()
        _varint(v, ref)
        assert buf[starts[i] : starts[i + 1]] == bytes(ref)


def test_tile_ids_match_pmtiles() -> None:
    rng = random.Random(1)
    for z in (0, 1, 5, 14, 24):
        xs = np.array([rng.randrange(1 << z) for _ in range(100)])
        ys = np.array([rng.randrange(1 << z) for _ in range(100)])
        got = tile_ids(z, xs, ys).tolist()
        assert got == [zxy_to_tileid(z, int(x), int(y)) for x, y in zip(xs, ys, strict=True)]


def test_polygon_winding_follows_the_spec(tmp_path: Path) -> None:
    shell = [(10, 40), (14, 40), (14, 44), (10, 44), (10, 40)]
    hole = [(11, 41), (12, 41), (12, 42), (11, 42), (11, 41)]
    for s, h in ((shell, hole), (shell[::-1], hole[::-1])):
        out = write(tmp_path, [batch([shapely.Polygon(s, [h])])], maxzoom=4)
        for layers in tiles(out, 4).values():
            for f in layers[0].features:
                rings = decode_geometry(f.geometry)
                areas = [sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in itertools.pairwise(r)) for r in rings]
                assert areas[0] > 0  # MVT 2.x: exterior positive in tile coordinates (y down)
                assert all(a < 0 for a in areas[1:])
                assert len(classify_rings(rings)) == 1


# --- round trips -----------------------------------------------------------


def test_layers_attributes_and_ids_round_trip(tmp_path: Path) -> None:
    b = batch(
        [shapely.Point(12.5, 41.9), shapely.LineString([(2, 48), (12, 42)]), shapely.box(2, 41, 13, 49)],
        layer=["cities", "roads", "areas"],
        feature_id=pa.array([7, None, 2**63 + 5], pa.uint64()),
        name=["Rome", "A1", None],
        pop=pa.array([2_800_000, -3, None], pa.int64()),
        capital=[True, False, None],
        ratio=pa.array([0.25, None, 1.5], pa.float32()),
        score=[1.0e300, None, -0.5],
        tags=pa.array([["a", "b"], None, []]),
    )
    out = write(tmp_path, [b], maxzoom=3)
    info = open_source(str(out)).info()
    layers = {lay.id: lay for lay in info.layers}
    assert list(layers) == ["cities", "roads", "areas"]  # first appearance
    assert layers["cities"].fields == {
        "name": "String",
        "pop": "Number",
        "capital": "Boolean",
        "ratio": "Number",
        "score": "Number",
        "tags": "String",
    }
    assert layers["areas"].fields == {"ratio": "Number", "score": "Number", "tags": "String"}
    decoded = {lay.name: lay.features for t in tiles(out, 0).values() for lay in t}
    city = decoded["cities"][0]
    assert city.id == 7
    assert city.properties == {
        "name": "Rome",
        "pop": 2_800_000,
        "capital": True,
        "ratio": 0.25,
        "score": 1.0e300,
        "tags": '["a","b"]',
    }
    assert type(city.properties["capital"]) is bool
    road = decoded["roads"][0]
    assert road.id is None
    assert road.properties == {"name": "A1", "pop": -3, "capital": False}
    assert decoded["areas"][0].id == 2**63 + 5


def test_properties_json_is_expanded(tmp_path: Path) -> None:
    obj = json.dumps({"kind": "park", "area": 3.5, "nested": {"a": 1}, "gone": None})
    fields = [pa.field("properties", pa.string(), metadata=JSON)]
    b = batch([shapely.Point(1, 1)])
    b = pa.RecordBatch.from_arrays(
        [pa.array([obj]), b.column("geometry")], schema=pa.schema([*fields, b.schema.field("geometry")])
    )
    out = write(tmp_path, [b], maxzoom=0)
    (layers,) = tiles(out, 0).values()
    assert layers[0].name == "out"
    assert layers[0].features[0].properties == {"kind": "park", "area": 3.5, "nested": '{"a":1}'}


def test_area_survives_tiling(tmp_path: Path) -> None:
    poly = shapely.Polygon([(0, 0), (20, 2), (18, 25), (3, 20), (0, 0)], [[(5, 5), (10, 6), (8, 12), (5, 5)]])
    out = write(tmp_path, [batch([poly])], minzoom=7, maxzoom=7, simplify=0)
    total = 0.0
    for (_, _, _), layers in tiles(out, 7).items():
        for f in layers[0].features:
            for rings in classify_rings(decode_geometry(f.geometry)):
                piece = shapely.Polygon(rings[0], rings[1:]).intersection(shapely.box(0, 0, 4096, 4096))
                total += piece.area
    # the source area in the same units (Web Mercator scaled to 4096 * 2**7 per world)
    from mvt_worker.tiler import lonlat_to_unit

    world = shapely.transform(poly, lambda c: lonlat_to_unit(c) * 4096 * 2**7)
    assert total == pytest.approx(world.area, rel=1e-4)


def test_identical_tiles_are_stored_once(tmp_path: Path) -> None:
    out = write(tmp_path, [batch([shapely.box(-60, -40, 60, 40)])], minzoom=6, maxzoom=6)
    src = open_source(str(out))
    header = src.header  # type: ignore[attr-defined]
    assert header["addressed_tiles_count"] > 300
    assert header["tile_contents_count"] < 100  # the interior tiles are all the same full square


def test_empty_input_writes_a_valid_archive(tmp_path: Path) -> None:
    out = write(tmp_path, [batch([None])], maxzoom=2)
    src = open_source(str(out))
    assert src.tiles(2, None, 100) == []
    assert src.info().layers == []


def test_zoom_columns(tmp_path: Path) -> None:
    b = batch(
        [shapely.Point(10, 10), shapely.Point(20, 20)],
        name=["early", "late"],
        minz=pa.array([None, 3], pa.int32()),
        maxz=pa.array([1, None], pa.int32()),
    )
    out = write(tmp_path, [b], maxzoom=4, minzoom_column="minz", maxzoom_column="maxz")
    seen = {
        z: sorted(f.properties["name"] for t in tiles(out, z).values() for lay in t for f in lay.features)
        for z in range(5)
    }
    assert seen == {0: ["early"], 1: ["early"], 2: [], 3: ["late"], 4: ["late"]}


def test_mercator_input_matches_lonlat(tmp_path: Path) -> None:
    lonlat = shapely.Point(12.5, 41.9)
    x = lonlat.x * math.pi * 6378137.0 / 180.0
    y = math.log(math.tan(math.pi / 4 + math.radians(lonlat.y) / 2)) * 6378137.0
    a = write(tmp_path, [batch([lonlat])], "a.pmtiles", maxzoom=10, minzoom=10)
    b = write(tmp_path, [batch([shapely.Point(x, y)], meta=MERC)], "b.pmtiles", maxzoom=10, minzoom=10)
    assert {k: v[0].features[0].geometry for k, v in tiles(a, 10).items()} == {
        k: v[0].features[0].geometry for k, v in tiles(b, 10).items()
    }


def test_drawing_order_is_input_order(tmp_path: Path) -> None:
    names = [f"f{i}" for i in range(50)]
    geoms = [shapely.Point(1 + i * 0.01, 1) for i in range(50)]
    out = write(tmp_path, [batch(geoms[:25], name=names[:25]), batch(geoms[25:], name=names[25:])], maxzoom=0)
    (layers,) = tiles(out, 0).values()
    assert [f.properties["name"] for f in layers[0].features] == names


# --- refusals --------------------------------------------------------------


def _fails(tmp_path: Path, b: pa.RecordBatch, match: str, **opts: Any) -> None:
    with pytest.raises(ValueError, match=match):
        write(tmp_path, [b], **opts)
    assert not list(tmp_path.iterdir()), "a failed write must leave nothing behind"


def test_refusals(tmp_path: Path) -> None:
    pt = shapely.Point(1, 1)
    no_geom = pa.record_batch([pa.array([1])], names=["a"])
    with pytest.raises(ValueError, match="no GEOMETRY column"):
        resolve_roles(no_geom.schema, WriteArgs())
    _fails(tmp_path, batch([pt]), "minzoom .* greater", minzoom=5, maxzoom=2)
    _fails(tmp_path, batch([pt], blob=pa.array([b"x"], pa.binary())), "can't hold")
    _fails(tmp_path, batch([pt], feature_id=pa.array([-1], pa.int64())), "negative")
    _fails(tmp_path, batch([shapely.Point(500000, 4000000)]), "outside lon/lat")
    _fails(tmp_path, batch([pt], layer=pa.array([1])), "must be VARCHAR")
    _fails(tmp_path, batch([pt]), "is not a column", layer_column="nope")
    bad_crs = {b"ARROW:extension:name": b"geoarrow.wkb", b"ARROW:extension:metadata": b'{"crs":"EPSG:32633"}'}
    _fails(tmp_path, batch([pt], meta=bad_crs), "Reproject")
    _fails(tmp_path, batch([shapely.box(-60, -40, 60, 40)]), "more than 10 tiles", maxzoom=5, max_tiles=10)
    many = batch([shapely.Point(1 + i * 1e-4, 1) for i in range(20000)], name=[f"n{i}" for i in range(20000)])
    _fails(tmp_path, many, "over max_tile_bytes", maxzoom=0, max_tile_bytes=10_000)


def test_text_properties_column(tmp_path: Path) -> None:
    # DuckDB sends JSON columns as plain text, so a text `properties` column is expanded per value;
    # a value that isn't a JSON object stays an ordinary attribute.
    b = batch([shapely.Point(10, 10), shapely.Point(20, 20)], properties=['{"kind": "park"}', "just text"])
    out = write(tmp_path, [b], maxzoom=0)
    (layers,) = tiles(out, 0).values()
    assert [f.properties for f in layers[0].features] == [{"kind": "park"}, {"properties": "just text"}]
    with pytest.raises(ValueError, match="not a JSON object"):
        write(tmp_path, [b], "strict.pmtiles", maxzoom=0, properties_column="properties")


def test_rewrite_a_tileset_that_was_read(tmp_path: Path) -> None:
    # The reader must not keep the file open (Windows can't replace an open file), and must
    # see the new contents afterwards rather than a cached copy.
    first = write(tmp_path, [batch([shapely.Point(10, 10)], name=["old"])], maxzoom=0)
    assert [f.properties["name"] for t in tiles(first, 0).values() for f in t[0].features] == ["old"]
    import os
    import time

    time.sleep(0.01)
    again = write(tmp_path, [batch([shapely.Point(10, 10)], name=["new"])], maxzoom=0)
    os.utime(again, ns=(time.time_ns(), time.time_ns() + 1_000_000))  # distinct mtime on coarse clocks
    assert [f.properties["name"] for t in tiles(again, 0).values() for f in t[0].features] == ["new"]


def test_merging_long_lines_decodes_each_tile_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Long lines heading in all directions over more tiles than a count-limited cache held
    # made every walk re-decode tiles it had just evicted (minutes instead of seconds).
    import mvt_worker.edges as edges_mod
    from mvt_worker.rows import RowBuilder, output_schema

    rng = random.Random(5)
    lines = [shapely.LineString([(rng.uniform(-10, 20), rng.uniform(35, 60)) for _ in range(3)]) for _ in range(150)]
    real = edges_mod.split_tile
    budget = edges_mod.CACHE_BYTES

    def archive(n: int) -> tuple[Any, list[Any]]:
        b = batch(lines[:n], feature_id=pa.array(range(n), pa.uint64()))
        src = open_source(str(write(tmp_path, [b], f"l{n}.pmtiles", minzoom=8, maxzoom=8)))
        return src, src.tiles(8, None, 100_000)

    def merged(src: Any, keys: list[Any], cache_bytes: int) -> tuple[int, list[tuple[Any, ...]]]:
        monkeypatch.setattr(edges_mod, "CACHE_BYTES", cache_bytes)
        decodes = 0

        def counting(*a: Any, **k: Any) -> Any:
            nonlocal decodes
            decodes += 1
            return real(*a, **k)

        monkeypatch.setattr(edges_mod, "split_tile", counting)
        asm = edges_mod.EdgeAssembler(src, keys, layer_filter=None, coordinates="wgs84", mode="merge")
        rows = RowBuilder(output_schema(None, "wgs84"), "wgs84", None)
        for k, data in src.fetch(keys):
            asm.add_tile(rows, k, data)
        t = rows.take()
        return decodes, sorted(zip(t["feature_id"].to_pylist(), t["geometry"].to_pylist(), strict=True))

    src, keys = archive(150)
    assert len(keys) > 256
    decodes, rows = merged(src, keys, budget)
    assert decodes == len(keys)
    assert [fid for fid, _ in rows] == list(range(150))
    # Evicting under a tiny budget re-decodes tiles but must give exactly the same rows.
    src, keys = archive(20)
    _, full = merged(src, keys, budget)
    decodes, small = merged(src, keys, 64 << 10)
    assert decodes > len(keys)
    assert small == full
