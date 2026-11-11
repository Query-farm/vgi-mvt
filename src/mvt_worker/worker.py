"""Assemble the vector tile functions into a catalog and run the worker.

To add a function: write it, then add the class to ``FUNCTIONS`` below.

Run it:

    vgi-mvt-worker                      # stdio: what DuckDB launches by default
    vgi-mvt-worker --http --port 8000   # HTTP: for Docker / hosted deploys
    vgi-mvt-worker --help

Attach it from DuckDB:

    ATTACH 'mvt' (TYPE vgi, LOCATION '/abs/path/to/.venv/bin/vgi-mvt-worker');
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from vgi import Worker
from vgi.catalog import Catalog, Schema
from vgi.catalog.attach_option import AttachOption

from .catalog import TilesetCatalog
from .decode import MvtDecode
from .layers import TilesetLayers, TilesetMetadata
from .meta import (
    CATALOG_DOC_MD,
    CATALOG_NAME,
    CATALOG_TAGS,
    SCHEMA_CATEGORIES,
    SCHEMA_DOC_LLM,
    SCHEMA_DOC_MD,
    SOURCE_URL,
    keywords_array,
)
from .read_tiles import DEFAULT_MAX_TILES, ReadMvt, ReadTiles
from .write_tiles import WritePmtiles

FUNCTIONS: list[type[Any]] = [ReadTiles, ReadMvt, MvtDecode, TilesetLayers, TilesetMetadata, WritePmtiles]


def _stamp_example_queries(functions: list[type[Any]]) -> list[dict[str, str]]:
    """Publish each function's examples as ``vgi.example_queries`` (with descriptions).

    Overloads share one SQL name, so their examples are merged per name.
    Returns every example, for the schema-level list.
    """
    by_name: dict[str, list[dict[str, str]]] = {}
    for fn in functions:
        bucket = by_name.setdefault(fn.Meta.name, [])
        for ex in getattr(fn.Meta, "examples", []):
            entry = {"description": ex.description, "sql": ex.sql}
            if entry not in bucket:
                bucket.append(entry)
    for fn in functions:
        fn.Meta.tags = {**fn.Meta.tags, "vgi.example_queries": json.dumps(by_name[fn.Meta.name])}
    return [entry for entries in by_name.values() for entry in entries]


_ALL_EXAMPLES = _stamp_example_queries(FUNCTIONS)


class MvtCatalog(TilesetCatalog):
    """The ``mvt`` catalog: the reader functions, plus one table per layer of an attached tileset."""

    catalog = Catalog(
        name=CATALOG_NAME,
        comment=CATALOG_DOC_MD,
        source_url=SOURCE_URL,
        tags=CATALOG_TAGS,
        schemas=[
            Schema(
                path=["main"],
                comment=SCHEMA_DOC_MD,
                functions=FUNCTIONS,
                tags={
                    "vgi.title": "Vector Tile Functions",
                    "domain": "geospatial",
                    "vgi.keywords": keywords_array(
                        "vector tiles, mvt, pmtiles, mbtiles, tileset, decode, geometry, layers"
                    ),
                    "vgi.doc_llm": SCHEMA_DOC_LLM,
                    "vgi.doc_md": SCHEMA_DOC_MD,
                    "vgi.categories": json.dumps(SCHEMA_CATEGORIES),
                    "vgi.example_queries": json.dumps(_ALL_EXAMPLES),
                },
            )
        ],
    )


class MvtWorker(Worker):
    """The worker process: the ``mvt`` catalog and its ATTACH options."""

    catalog_interface = MvtCatalog
    catalog_name = CATALOG_NAME

    class AttachOptions:
        """Options for ``ATTACH 'mvt' AS name (TYPE vgi, LOCATION ..., tileset '...')``."""

        tileset: Annotated[
            str,
            AttachOption(
                desc=(
                    "PMTiles file or http(s) URL, or local MBTiles file, to expose as tables "
                    "(one per layer). Empty attaches only the reader functions."
                )
            ),
        ] = ""
        zoom: Annotated[
            int,
            AttachOption(desc="Zoom level the layer tables read; -1 (default) uses the tileset's maxzoom."),
        ] = -1
        bbox: Annotated[
            str,
            AttachOption(
                desc="Limit every layer table to the tiles covering 'west,south,east,north' (degrees); empty reads all."
            ),
        ] = ""
        coordinates: Annotated[
            str,
            AttachOption(desc="Geometry coordinates of the layer tables: 'wgs84' (default), 'web_mercator' or 'tile'."),
        ] = "wgs84"
        max_tiles: Annotated[
            int,
            AttachOption(desc="A layer table scan fails rather than read more than this many tiles."),
        ] = DEFAULT_MAX_TILES
        tile_edges: Annotated[
            str,
            AttachOption(
                desc=(
                    "Features reaching a tile edge: 'merge' (default, one row per feature), 'clip' (one "
                    "non-overlapping piece per tile) or 'raw' (as stored, tile buffers included)."
                )
            ),
        ] = "merge"


def main() -> None:
    """Console-script entry point: parse ``--http`` / ``--unix`` / logging flags and serve."""
    MvtWorker.main()


if __name__ == "__main__":
    main()
