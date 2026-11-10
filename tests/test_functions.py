"""End-to-end tests through the worker process. The SQL-level tests live in test/sql."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest
from conftest import FIXTURES
from vgi.arguments import Arguments
from vgi.client import Client

from mvt_worker.sources import MBTilesSource, PMTilesSource, open_source

SCHEMA = ["main"]


def _table(client: Client, name: str, *positional: Any, **named: Any) -> pa.Table:
    arguments = Arguments(
        positional=tuple(pa.scalar(v) for v in positional),
        named={k: pa.scalar(v) for k, v in named.items()},
    )
    batches = list(client.table_function(function_name=name, schema_path=SCHEMA, arguments=arguments))
    return pa.Table.from_batches(batches)


@pytest.mark.parametrize("name", ["sample.pmtiles", "sample.mbtiles"])
def test_read_tiles_containers_agree(client: Client, name: str) -> None:
    table = _table(client, "read_tiles", str(FIXTURES / name), zoom=3, layer="cities")
    assert sorted(str(n) for n in table.column("name").to_pylist()) == ["Berlin", "Milan", "Paris", "Vienna"]
    assert (table.schema.field("geometry").metadata or {})[b"ARROW:extension:name"] == b"geoarrow.wkb"


def test_read_mvt(client: Client) -> None:
    table = _table(client, "read_mvt", str(FIXTURES / "0-0-0.mvt"), 0, 0, 0)
    assert set(table.column("layer").to_pylist()) == {"cities", "regions"}


def test_tileset_layers(client: Client) -> None:
    table = _table(client, "tileset_layers", str(FIXTURES / "sample.pmtiles"))
    assert set(table.column("layer").to_pylist()) == {"cities", "regions"}


def test_xyz_template_needs_bbox(client: Client) -> None:
    with pytest.raises(Exception, match="bbox"):
        _table(client, "read_tiles", str(FIXTURES / "xyz" / "{z}" / "{x}" / "{y}.mvt"), zoom=1)


def test_xyz_template_local(client: Client) -> None:
    template = str(FIXTURES / "xyz" / "{z}" / "{x}" / "{y}.mvt")
    table = _table(client, "read_tiles", template, zoom=1, west=-180.0, south=-85.0, east=180.0, north=85.0)
    assert table.num_rows > 0
    assert set(table.column("z").to_pylist()) == {1}


def test_pmtiles_and_mbtiles_list_same_tiles() -> None:
    pm = open_source(str(FIXTURES / "sample.pmtiles"))
    mb = open_source(str(FIXTURES / "sample.mbtiles"))
    assert isinstance(pm, PMTilesSource) and isinstance(mb, MBTilesSource)
    for z in range(4):
        assert sorted(pm.tiles(z, None, 100)) == sorted(mb.tiles(z, None, 100))
        assert sorted(pm.tiles(z, (-180, -85, 180, 85), 100)) == sorted(mb.tiles(z, None, 100))
