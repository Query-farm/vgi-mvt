"""Discovery metadata: the one file to edit when you rename or extend the worker.

DuckDB, coding agents and the ``vgi-lint`` metadata checker all read these
tags. Keep every object documented and ``make lint`` stays at 100.

* Catalog-level tags (title, license, author, links, long description) live in
  ``CATALOG_TAGS`` below.
* Every function and table carries ``object_tags(...)``: a title, a category
  from ``SCHEMA_CATEGORIES``, search keywords, and LLM- and human-facing docs.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa

CATALOG_NAME = "mvt"

SOURCE_URL = "https://github.com/Query-farm/vgi-mvt"

# Categories group functions in docs and agent tools. Every object's
# ``vgi.category`` must name one of these.
SCHEMA_CATEGORIES = [
    {
        "name": "readers",
        "title": "Tile Readers",
        "description": "Table functions that read vector tiles from files, URLs and tilesets into rows.",
    },
    {
        "name": "decoders",
        "title": "Tile Decoders",
        "description": "Functions that decode vector tile blobs already in DuckDB.",
    },
    {
        "name": "metadata",
        "title": "Tileset Metadata",
        "description": "Table functions that describe a tileset: layers, attributes, zoom range and bounds.",
    },
    {
        "name": "writers",
        "title": "Tileset Writers",
        "description": "COPY ... TO formats that encode query results as vector tilesets.",
    },
]


# Public sample tilesets used in the (non-executable) examples and the README.
FIRENZE_PMTILES = (
    "https://raw.githubusercontent.com/protomaps/PMTiles/main/spec/v3/protomaps(vector)ODbL_firenze.pmtiles"
)
ZCTA_PMTILES = "https://r2-public.protomaps.com/protomaps-sample-datasets/cb_2018_us_zcta510_500k.pmtiles"
STREETS_TILE = "https://raw.githubusercontent.com/mapbox/vector-tile-js/main/test/fixtures/14-8801-5371.vector.pbf"


def keywords_array(keywords: str) -> str:
    """Turn ``"a, b, c"`` into the JSON array string ``vgi.keywords`` requires."""
    return json.dumps([k.strip() for k in keywords.split(",") if k.strip()])


def object_tags(*, title: str, category: str, keywords: str, doc_llm: str, doc_md: str) -> dict[str, str]:
    """Build the standard per-object discovery tags."""
    if category not in {c["name"] for c in SCHEMA_CATEGORIES}:
        raise ValueError(f"unknown category {category!r}; add it to SCHEMA_CATEGORIES")
    return {
        "vgi.title": title,
        "vgi.category": category,
        "vgi.keywords": keywords_array(keywords),
        "vgi.doc_llm": doc_llm,
        "vgi.doc_md": doc_md,
    }


def column(name: str, type_: pa.DataType, comment: str, *, nullable: bool = True) -> pa.Field[Any]:
    """Build an Arrow field whose ``comment`` shows up in DuckDB's ``DESCRIBE`` and agent tools."""
    return pa.field(name, type_, nullable=nullable, metadata={b"comment": comment.encode()})


CATALOG_DOC_LLM = (
    "Decode Mapbox Vector Tiles (MVT, the protobuf tile format behind most web maps) back into "
    "DuckDB tables. Each feature becomes a row with its layer, tile address (z/x/y), feature id, a "
    "`GEOMETRY` in WGS84 longitude/latitude (or Web Mercator, or raw tile pixels) and its attributes "
    "as `JSON`; naming one layer also returns each attribute as a typed column. Sources: PMTiles "
    "archives (local, or remote over HTTP range requests, so planet-scale files are fine with a "
    "bbox), MBTiles SQLite files, `{z}/{x}/{y}` tile-server URL templates, single .mvt/.pbf tiles, "
    "and tile blobs already in a DuckDB query. Attaching with a `tileset` option exposes each layer "
    "of that tileset as a table with typed attribute columns, scoped by the `zoom` and `bbox` attach "
    "options. Read the tileset metadata first to learn its zoom "
    "range, layers and attributes. Geometry is quantised to the tile grid. Tilesets store a feature "
    "once per tile it touches; the tileset readers join those pieces back into one row per feature "
    "by default (`tile_edges`), while the single-tile decoders return each tile as stored. The "
    "other direction is a COPY TO format: FORMAT 'mvt.pmtiles' encodes a query's rows (one "
    "`GEOMETRY` per row, a `layer` column for several layers) as a new PMTiles tileset."
)

CATALOG_DOC_MD = (
    "# Read Mapbox Vector Tiles in DuckDB\n\n"
    "**Turn vector tiles back into data.** Decode Mapbox Vector Tiles into rows with a DuckDB "
    "`GEOMETRY` column and the features' attributes, from PMTiles (file or URL), MBTiles, "
    "`{z}/{x}/{y}` tile servers, single `.mvt` / `.pbf` tiles or tile blobs in a query.\n\n"
    "Coordinates come back as WGS84 longitude/latitude by default; Web Mercator metres and raw tile "
    "pixels are available too. Vector tiles are a lossy, display-oriented format: coordinates snap "
    "to a grid of usually 4096 per tile, and features are clipped at tile edges and simplified at low "
    "zoom, so read the highest zoom for the most faithful geometry."
)

SCHEMA_DOC_LLM = (
    "Vector tile readers. Every feature-reading function returns the same leading columns: layer, z, "
    "x, y, feature_id, geometry_type, geometry (`GEOMETRY`), properties (`JSON`). Tile addresses use "
    "the XYZ scheme (row 0 at the north); MBTiles stores TMS rows and is flipped for you. Whole "
    "tilesets are read one zoom at a time, limited by an optional WGS84 bbox (west, south, east, "
    "north) that selects tiles, not features: filter features afterwards with `ST_Intersects`. "
    "A `max_tiles` guard (default 10000) stops accidental planet-sized reads. Use DuckDB spatial "
    "(ST_*) on the geometry column; old (MVT v1) tilesets can contain self-intersecting polygons, "
    "which `ST_MakeValid` repairs."
)

SCHEMA_DOC_MD = (
    "## main\n\n"
    "Read vector tiles as tables: whole tilesets at one zoom, single tiles, or tile blobs already in "
    "DuckDB, plus the layer and metadata catalogues that describe a tileset.\n\n"
    "Feature rows always carry `layer`, `z`, `x`, `y`, `feature_id`, `geometry_type`, `geometry` and "
    "`properties`."
)

AGENT_TEST_TASKS = [
    {
        "name": "fixture_city_count",
        "prompt": (
            "Using the mvt worker, how many features are in the 'cities' layer of the tileset "
            "test/fixtures/sample.pmtiles at zoom 3?"
        ),
    },
    {
        "name": "largest_city",
        "prompt": (
            "Using the mvt worker, which city in the 'cities' layer of test/fixtures/sample.mbtiles has "
            "the largest population?"
        ),
    },
    {
        "name": "fixture_maxzoom",
        "prompt": "What is the maximum zoom level of the tileset test/fixtures/sample.pmtiles?",
    },
    {
        "name": "tile_polygons",
        "prompt": "How many polygon features does the single vector tile test/fixtures/0-0-0.mvt (tile 0/0/0) hold?",
    },
    {
        "name": "region_attributes",
        "prompt": "List the attribute names of the 'regions' layer in test/fixtures/sample.pmtiles, alphabetically.",
    },
    {
        "name": "decode_mbtiles_rows",
        "prompt": (
            "Read the raw rows of the 'tiles' table of test/fixtures/sample.mbtiles at zoom 2 with DuckDB's "
            "sqlite extension and decode them with the mvt worker. How many features come out?"
        ),
    },
    {
        "name": "write_cities_layer",
        "prompt": (
            "Write only the 'cities' layer of test/fixtures/sample.pmtiles into a new PMTiles vector tileset "
            "named cities-only.pmtiles in the working directory, with zoom levels 0 through 6."
        ),
    },
    {
        "name": "roads_crossing_areas",
        "prompt": (
            "Using the mvt worker, which roads in the 'roads' layer of test/fixtures/edges.pmtiles cross or touch "
            "a polygon of the 'areas' layer of the same tileset? Return one row per road and area, with VARCHAR "
            "columns road (the road's name) and area (the area's name), ordered by road then area."
        ),
    },
]

CATALOG_TAGS = {
    "vgi.title": "Mapbox Vector Tile Reader",
    "vgi.subtitle": "Decode PMTiles, MBTiles, XYZ and .mvt vector tiles into DuckDB rows with GEOMETRY columns.",
    "vgi.doc_llm": CATALOG_DOC_LLM,
    "vgi.doc_md": CATALOG_DOC_MD,
    "vgi.source_url": SOURCE_URL,
    "vgi.icon_url": "https://query.farm/media-kit/products/vgi/vgi-logo.png",
    "vgi.license": "0BSD",
    "vgi.author": "Query.Farm",
    "vgi.copyright": "Copyright 2026 Query Farm LLC - https://query.farm",
    "vgi.keywords": keywords_array(
        "vgi, duckdb, vector tiles, mvt, mapbox vector tile, pmtiles, mbtiles, xyz tiles, gis, geometry, protobuf"
    ),
    "vgi.support_contact": f"{SOURCE_URL}/issues",
    "vgi.support_policy_url": f"{SOURCE_URL}#support",
    # Public analyst tasks for `vgi-lint simulate`; private graders live in vgi-agent-tests.yaml.
    "vgi.agent_test_tasks": json.dumps(AGENT_TEST_TASKS),
}
