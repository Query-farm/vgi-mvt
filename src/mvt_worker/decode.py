"""Table in-out function ``mvt_decode``: explode tile BLOBs from any query into features.

Use it when the tiles already sit in DuckDB: an MBTiles file read through the
sqlite extension, a Parquet table of tiles, or blobs fetched by another function.
The input needs a ``BLOB`` column ``tile`` (or ``tile_data``) and whole-number ``z``, ``x``
and ``y`` columns in the XYZ scheme.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, cast

import pyarrow as pa
from vgi.arguments import Arg, TableInput
from vgi.invocation import BindResponse
from vgi.metadata import FunctionExample
from vgi.table_function import BindParams, ProcessParams
from vgi.table_in_out_function import OutputCollector, TableInOutGenerator

from .meta import object_tags
from .mvt import COORDINATE_CHOICES, Coordinates
from .rows import RESULT_COLUMNS_DOC, RowBuilder, output_schema

TILE_COLUMNS = ("tile", "tile_data")

# A 48-byte tile: layer "points", one POINT at (2048, 2048) of 4096, id 7, name='origin'.
_POINT_TILE_HEX = "1a2e78020a06706f696e7473120f0807120200001801220509802080201a046e616d6522080a066f726967696e288020"


@dataclass(slots=True, frozen=True, kw_only=True)
class MvtDecodeArgs:
    """Arguments for ``mvt_decode((SELECT tile, z, x, y FROM ...), ...)``."""

    data: Annotated[
        TableInput,
        Arg(0, doc="Rows holding one vector tile each in `tile` (or `tile_data`) with its XYZ address in z, x, y."),
    ]
    layer: Annotated[str | None, Arg("layer", default=None, doc="Decode only this layer; NULL decodes every layer.")]
    coordinates: Annotated[
        str,
        Arg(
            "coordinates",
            default="wgs84",
            doc="Output coordinates: 'wgs84' (default), 'web_mercator' or 'tile' (raw tile pixels).",
            choices=list(COORDINATE_CHOICES),
        ),
    ]


def _tile_column(schema: pa.Schema) -> str:
    for name in TILE_COLUMNS:
        if name in schema.names:
            return name
    raise ValueError("mvt_decode needs a BLOB input column named 'tile' or 'tile_data'")


class MvtDecode(TableInOutGenerator[MvtDecodeArgs]):
    """Decode a query's tile blobs into one row per feature."""

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "mvt_decode"
        description = "Decode vector tile BLOBs from a query (tile, z, x, y) into one row per feature"
        tags = {
            **object_tags(
                title="Decode Tile Blobs",
                category="decoders",
                keywords="vector tile, mvt, decode, blob, protobuf, mbtiles sqlite, table in-out, geometry",
                doc_llm=(
                    "Table in-out function: pass a subquery whose rows hold vector tiles and get one row "
                    "per feature back. The input needs a `BLOB` column `tile` (or `tile_data`; gzip is "
                    "detected) and whole-number columns `z`, `x`, `y` in the XYZ scheme (row 0 at the north). "
                    "MBTiles rows are TMS: convert with `(1 << zoom_level) - 1 - tile_row AS y`. Named "
                    "arguments `layer` and `coordinates` work as in `read_tiles`. Output columns: layer, "
                    "z, x, y, feature_id, geometry_type, geometry (GEOMETRY), properties (JSON). Use "
                    "`read_tiles` instead when the tileset is a file or URL."
                ),
                doc_md=(
                    "## mvt_decode(data, layer := NULL, coordinates := 'wgs84')\n\n"
                    "Explode vector-tile blobs that are already in DuckDB into features. Input rows need "
                    "`tile` (`BLOB`) and XYZ `z`, `x`, `y`. MBTiles rows are TMS: flip them with "
                    "`(1 << zoom_level) - 1 - tile_row`."
                ),
            ),
            "vgi.result_columns_schema": json.dumps(RESULT_COLUMNS_DOC),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "Decode a hand-made tile holding one point at the centre of tile 0/0/0",
                        "sql": (
                            "SELECT layer, feature_id, ST_AsText(geometry) AS wkt FROM mvt.main.mvt_decode(("
                            f"SELECT from_hex('{_POINT_TILE_HEX}') AS tile, 0 AS z, 0 AS x, 0 AS y)) ORDER BY layer"
                        ),
                        "expected_result": [["points", 7, "POINT (0 0)"]],
                    },
                ]
            ),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT layer, count(*) AS n FROM mvt.main.mvt_decode((SELECT tile_data AS tile, "
                    "zoom_level AS z, tile_column AS x, (1 << zoom_level) - 1 - tile_row AS y "
                    "FROM sqlite_scan('test/fixtures/sample.mbtiles', 'tiles') WHERE zoom_level = 2)) "
                    "GROUP BY layer ORDER BY n DESC"
                ),
                description="Decode MBTiles rows read with DuckDB's sqlite extension",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[MvtDecodeArgs]) -> BindResponse:
        """Check the input has the tile and address columns."""
        schema = params.bind_call.input_schema
        if schema is None:
            raise ValueError("mvt_decode needs an input query")
        _tile_column(schema)
        missing = [c for c in ("z", "x", "y") if c not in schema.names]
        if missing:
            raise ValueError(f"mvt_decode input is missing column(s): {', '.join(missing)}")
        return BindResponse(output_schema=output_schema(coordinates=params.args.coordinates))

    @classmethod
    def process(
        cls, params: ProcessParams[MvtDecodeArgs], state: None, batch: pa.RecordBatch, out: OutputCollector
    ) -> None:
        """Decode every non-NULL tile in the batch."""
        rows = RowBuilder(params.output_schema, cast("Coordinates", params.args.coordinates), params.args.layer)
        tiles = batch.column(_tile_column(batch.schema)).to_pylist()
        zs, xs, ys = (batch.column(c).to_pylist() for c in ("z", "x", "y"))
        for tile, z, x, y in zip(tiles, zs, xs, ys, strict=True):
            if tile is None or z is None or x is None or y is None:
                continue
            rows.add_tile(int(z), int(x), int(y), tile)  # errors name the tile
        out.emit(rows.take())
