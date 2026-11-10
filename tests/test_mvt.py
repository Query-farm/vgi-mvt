"""Decoder unit tests against hand-encoded tiles (tests/mvt_encode.py)."""

from __future__ import annotations

import gzip
import json
import math
import struct

import pytest
from mvt_encode import F, geometry, layer, points, tile

from mvt_worker.mvt import (
    MvtDecodeError,
    Projector,
    decode_tile,
    feature_wkb,
    tile_range,
)
from mvt_worker.rows import RowBuilder, attribute_fields, infer_field_types, output_schema

SQUARE = [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)]  # positive area in tile coords (y down)
HOLE = [(25, 25), (25, 75), (75, 75), (75, 25), (25, 25)]  # negative area
SQUARE2 = [(200, 200), (300, 200), (300, 300), (200, 300), (200, 200)]


def _wkb_type(wkb: bytes) -> int:
    return int(struct.unpack_from("<I", wkb, 1)[0])


def _decode_one(gtype: int, commands: list[int], version: int = 2) -> tuple[str, bytes | None]:
    lay = decode_tile(tile(layer("l", [F(gtype, commands)], version=version)))[0]
    return feature_wkb(lay.features[0], Projector(0, 0, 0, 4096, "tile"), lay.version)


def test_point_and_multipoint() -> None:
    assert _decode_one(1, points([(10, 20)]))[0] == "POINT"
    kind, wkb = _decode_one(1, points([(10, 20), (30, 40)]))
    assert kind == "MULTIPOINT"
    assert wkb is not None and _wkb_type(wkb) == 4


def test_line_and_multiline() -> None:
    kind, wkb = _decode_one(2, geometry([[(0, 0), (10, 0), (10, 10)]], close=False))
    assert kind == "LINESTRING" and wkb is not None
    assert struct.unpack_from("<I", wkb, 5)[0] == 3  # three vertices
    kind, _ = _decode_one(2, geometry([[(0, 0), (10, 0)], [(20, 20), (30, 30)]], close=False))
    assert kind == "MULTILINESTRING"


def test_polygon_with_hole_and_multipolygon() -> None:
    kind, wkb = _decode_one(3, geometry([SQUARE, HOLE], close=True))
    assert kind == "POLYGON" and wkb is not None
    assert struct.unpack_from("<I", wkb, 5)[0] == 2  # exterior + hole
    kind, wkb = _decode_one(3, geometry([SQUARE, HOLE, SQUARE2], close=True))
    assert kind == "MULTIPOLYGON" and wkb is not None
    assert struct.unpack_from("<I", wkb, 5)[0] == 2


def test_v1_reversed_winding_uses_nesting() -> None:
    rev = list(reversed(SQUARE))
    rev_hole = list(reversed(HOLE))
    kind, wkb = _decode_one(3, geometry([rev, rev_hole, list(reversed(SQUARE2))], close=True), version=1)
    assert kind == "MULTIPOLYGON" and wkb is not None
    # first polygon: exterior + hole
    assert struct.unpack_from("<I", wkb, 9 + 5)[0] == 2


def test_degenerate_geometry_is_null() -> None:
    assert _decode_one(3, geometry([[(0, 0), (10, 0), (0, 0)]], close=True))[1] is None
    assert _decode_one(2, geometry([[(5, 5)]], close=False))[1] is None


def test_property_value_types_and_ids() -> None:
    props = {"s": "x", "d": 1.5, "u": 42, "neg": -7, "b": True}
    lay = decode_tile(tile(layer("l", [F(1, points([(1, 1)]), props, id=99)])))[0]
    f = lay.features[0]
    assert f.id == 99
    assert f.properties == props
    assert infer_field_types([lay]) == {"s": "String", "d": "Number", "u": "Integer", "neg": "Integer", "b": "Boolean"}


def test_gzip_and_layer_filter() -> None:
    raw = tile(layer("a", [F(1, points([(1, 1)]))]), layer("b", [F(1, points([(2, 2)]))]))
    assert [lay.name for lay in decode_tile(gzip.compress(raw))] == ["a", "b"]
    assert [lay.name for lay in decode_tile(raw, layer_filter="b")] == ["b"]


def test_garbage_raises() -> None:
    with pytest.raises(MvtDecodeError):
        decode_tile(b"\x1a\xff\xff\xff")


def test_projection_corners() -> None:
    wgs = Projector(0, 0, 0, 4096, "wgs84")
    lon, lat = wgs([(0, 0)])
    assert lon == -180.0 and math.isclose(lat, 85.0511287798066)
    assert wgs([(2048, 2048)]) == [0.0, 0.0]
    merc = Projector(1, 1, 1, 4096, "web_mercator")
    x, y = merc([(4096, 4096)])
    assert math.isclose(x, 20037508.342789244) and math.isclose(y, -20037508.342789244)


def test_tile_range() -> None:
    assert tile_range(0, -180, -85, 180, 85) == (0, 0, 0, 0)
    assert tile_range(2, 0, 0, 90, 66) == (2, 1, 2, 1)


def test_row_builder_typed_columns() -> None:
    lay = layer("c", [F(1, points([(1, 1)]), {"name": "A", "n": 3, "flag": False})])
    extra = attribute_fields({"name": "String", "n": "Number", "flag": "Boolean", "layer": "String", "NAME": "String"})
    assert [f.name for f in extra] == ["name", "n", "flag"]  # base-name and case clashes dropped
    rows = RowBuilder(output_schema(extra), "wgs84", "c")
    rows.add_tile(3, 1, 2, tile(lay))
    out = rows.take().to_pylist()[0]
    assert (out["name"], out["n"], out["flag"]) == ("A", 3.0, False)
    assert (out["z"], out["x"], out["y"]) == (3, 1, 2)
    assert json.loads(out["properties"]) == {"name": "A", "n": 3, "flag": False}


def test_nan_property_is_null_in_json() -> None:
    lay = layer("c", [F(1, points([(1, 1)]), {"v": float("nan")})])
    rows = RowBuilder(output_schema(), "wgs84", None)
    rows.add_tile(0, 0, 0, tile(lay))
    assert json.loads(rows.take().to_pylist()[0]["properties"]) == {"v": None}
