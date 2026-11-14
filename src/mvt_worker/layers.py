"""Discovery table functions: ``tileset_layers`` and ``tileset_metadata``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.metadata import FunctionExample
from vgi.table_function import (
    OutputCollector,
    ProcessParams,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)
from vgi_rpc import ArrowSerializableDataclass

from .meta import FIRENZE_PMTILES, ZCTA_PMTILES, column, object_tags
from .sources import TilesetInfo, TileSourceError, open_source

# Composed examples: each checks a tileset's declared metadata against the features read_tiles
# actually decodes from the same fixture.
ATTRIBUTE_COVERAGE_SQL = (
    "SELECT l.layer, l.field, count(*) AS features, "
    "count(*) FILTER (WHERE t.properties ->> l.field IS NOT NULL) AS with_field "
    "FROM mvt.main.tileset_layers('test/fixtures/sample.pmtiles') AS l "
    "JOIN mvt.main.read_tiles('test/fixtures/sample.pmtiles') AS t USING (layer) "
    "GROUP BY ALL ORDER BY ALL"
)
ATTRIBUTE_COVERAGE_DESCRIPTION = (
    "Check each declared attribute against the decoded features: how many of its layer's features carry it"
)
TILESTATS_CHECK_SQL = (
    "SELECT s.layer, s.count AS declared, count(t.layer) AS decoded "
    'FROM (SELECT unnest(from_json(value -> \'layers\', \'[{"layer": "VARCHAR", "count": "BIGINT"}]\'), '
    "recursive := true) FROM mvt.main.tileset_metadata('test/fixtures/sample.pmtiles') WHERE key = 'tilestats') AS s "
    "LEFT JOIN mvt.main.read_tiles('test/fixtures/sample.pmtiles') AS t USING (layer) "
    "GROUP BY ALL ORDER BY ALL"
)
TILESTATS_CHECK_DESCRIPTION = (
    "Compare each layer's feature count in the tilestats metadata with the features read_tiles decodes at max zoom"
)

_SOURCE_DOC = "Tileset: a .pmtiles file or http(s) URL, or a local .mbtiles file."


@dataclass(slots=True, frozen=True, kw_only=True)
class SourceArgs:
    """One positional argument: the tileset."""

    source: Annotated[str, Arg(0, doc=_SOURCE_DOC)]


@dataclass(kw_only=True)
class Once(ArrowSerializableDataclass):
    """Whether the single batch has been emitted."""

    done: bool = False


def _info(source: str) -> TilesetInfo:
    try:
        return open_source(source).info()
    except TileSourceError as exc:
        raise ValueError(str(exc)) from exc


LAYERS_SCHEMA = pa.schema(
    [
        column("layer", pa.string(), "Layer name, as passed to read_tiles(layer := ...).", nullable=False),
        column("field", pa.string(), "Attribute name; NULL for a layer that declares no attributes."),
        column("type", pa.string(), "Declared attribute type: Number, String, Boolean or Mixed."),
        column("description", pa.string(), "Layer description from the metadata, if any."),
        column("minzoom", pa.int32(), "Lowest zoom the layer appears at, if declared."),
        column("maxzoom", pa.int32(), "Highest zoom the layer appears at, if declared."),
    ]
)


@init_single_worker
@bind_fixed_schema
class TilesetLayers(TableFunctionGenerator[SourceArgs, Once]):
    """List a tileset's layers and their attributes."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = LAYERS_SCHEMA

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "tileset_layers"
        description = "List the layers of a vector tileset and the attributes (with types) of each"
        tags = {
            **object_tags(
                title="Tileset Layers",
                category="metadata",
                keywords="vector_layers, layers, schema, fields, attributes, tilejson, pmtiles, mbtiles",
                doc_llm=(
                    "Return one row per (layer, attribute) declared in a tileset's `vector_layers` "
                    "metadata: layer, field, type (Number/String/Boolean), description, minzoom, maxzoom. "
                    "Call this before `read_tiles` to learn the layer names and the typed columns "
                    "`read_tiles(..., layer := '<name>')` will return. `source` is a PMTiles file or "
                    "URL, or a local MBTiles file."
                ),
                doc_md=(
                    "## tileset_layers(source)\n\n"
                    "The `vector_layers` schema of a PMTiles or MBTiles tileset: one row per layer "
                    "attribute."
                ),
            ),
            "vgi.result_columns_schema": json.dumps(
                [
                    {"name": "layer", "type": "VARCHAR", "description": "Layer name."},
                    {"name": "field", "type": "VARCHAR", "description": "Attribute name."},
                    {"name": "type", "type": "VARCHAR", "description": "Declared attribute type."},
                    {"name": "description", "type": "VARCHAR", "description": "Layer description."},
                    {"name": "minzoom", "type": "INTEGER", "description": "Layer min zoom."},
                    {"name": "maxzoom", "type": "INTEGER", "description": "Layer max zoom."},
                ]
            ),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "Attributes of the fixture tileset's cities layer",
                        "sql": (
                            "SELECT field, type FROM mvt.main.tileset_layers('test/fixtures/sample.pmtiles') "
                            "WHERE layer = 'cities' ORDER BY field"
                        ),
                        "expected_result": [["capital", "Boolean"], ["name", "String"], ["population", "Number"]],
                    },
                    {
                        "description": ATTRIBUTE_COVERAGE_DESCRIPTION,
                        "sql": ATTRIBUTE_COVERAGE_SQL,
                        "expected_result": [
                            ["cities", "capital", 4, 4],
                            ["cities", "name", 4, 4],
                            ["cities", "population", 4, 4],
                            ["regions", "area_rank", 2, 2],
                            ["regions", "code", 2, 2],
                            ["regions", "label", 2, 2],
                        ],
                    },
                ]
            ),
        }
        # The first example uses a committed fixture: tools (and vgi-lint's scan probe) take sample
        # arguments from it, so it must work offline. The public-tileset examples follow.
        examples = [
            FunctionExample(
                sql=(
                    "SELECT layer, list(field ORDER BY field) AS attributes FROM mvt.main.tileset_layers("
                    "'test/fixtures/sample.mbtiles') GROUP BY layer ORDER BY layer"
                ),
                description="Attribute names of each layer of the fixture MBTiles",
            ),
            FunctionExample(sql=ATTRIBUTE_COVERAGE_SQL, description=ATTRIBUTE_COVERAGE_DESCRIPTION),
            FunctionExample(
                sql=(
                    "SELECT layer, count(field) AS attributes FROM mvt.main.tileset_layers("
                    f"'{FIRENZE_PMTILES}') "
                    "GROUP BY layer ORDER BY layer"
                ),
                description="How many attributes each layer of a remote PMTiles basemap declares",
            ),
        ]

    @classmethod
    def initial_state(cls, params: ProcessParams[SourceArgs]) -> Once:
        """Nothing emitted yet."""
        return Once()

    @classmethod
    def process(cls, params: ProcessParams[SourceArgs], state: Once, out: OutputCollector) -> None:
        """Emit the layer/attribute rows."""
        if not state.done:
            cols: dict[str, list[Any]] = {f.name: [] for f in LAYERS_SCHEMA}
            for lay in _info(params.args.source).layers:
                fields: list[tuple[str | None, str | None]] = list(lay.fields.items()) or [(None, None)]
                for name, kind in fields:
                    cols["layer"].append(lay.id)
                    cols["field"].append(name)
                    cols["type"].append(kind)
                    cols["description"].append(lay.description)
                    cols["minzoom"].append(lay.minzoom)
                    cols["maxzoom"].append(lay.maxzoom)
            state.done = True
            out.emit(pa.RecordBatch.from_pydict(cols, schema=params.output_schema))
        out.finish()


METADATA_SCHEMA = pa.schema(
    [
        column(
            "key",
            pa.string(),
            "Metadata key, e.g. format, minzoom, maxzoom, bounds, name, attribution.",
            nullable=False,
        ),
        column("value", pa.string(), "Metadata value as text; JSON for structured values."),
    ]
)


def metadata_rows(info: TilesetInfo) -> list[tuple[str, str | None]]:
    """Flatten tileset info into sorted ``(key, value)`` pairs."""
    rows: dict[str, str | None] = {
        "format": info.format,
        "minzoom": None if info.minzoom is None else str(info.minzoom),
        "maxzoom": None if info.maxzoom is None else str(info.maxzoom),
        "bounds": None if info.bounds is None else ",".join(f"{v:.7g}" for v in info.bounds),
    }
    for k, v in info.metadata.items():
        if k in rows:
            continue
        rows[k] = v if isinstance(v, str) or v is None else json.dumps(v, ensure_ascii=False)
    return sorted(rows.items())


@init_single_worker
@bind_fixed_schema
class TilesetMetadata(TableFunctionGenerator[SourceArgs, Once]):
    """Show a tileset's header and metadata as key/value rows."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = METADATA_SCHEMA

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "tileset_metadata"
        description = (
            "Show a vector tileset's metadata (format, zoom range, bounds, attribution, ...) as key/value rows"
        )
        tags = {
            **object_tags(
                title="Tileset Metadata",
                category="metadata",
                keywords="tileset metadata, tilejson, pmtiles header, mbtiles metadata, bounds, zoom range",
                doc_llm=(
                    "Return the metadata of a PMTiles (file or URL) or MBTiles (file) tileset as "
                    "(key, value) text rows, sorted by key. Always includes format, minzoom, maxzoom and "
                    "bounds ('west,south,east,north'); also any name, attribution, description, "
                    "vector_layers (JSON) and generator-specific keys. Use it to find the zoom range "
                    "before calling `read_tiles`."
                ),
                doc_md=(
                    "## tileset_metadata(source)\n\n"
                    "Header and metadata of a PMTiles or MBTiles tileset as key/value rows."
                ),
            ),
            "vgi.result_columns_schema": json.dumps(
                [
                    {"name": "key", "type": "VARCHAR", "description": "Metadata key."},
                    {"name": "value", "type": "VARCHAR", "description": "Metadata value."},
                ]
            ),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "Zoom range of the fixture MBTiles",
                        "sql": (
                            "SELECT key, value FROM mvt.main.tileset_metadata('test/fixtures/sample.mbtiles') "
                            "WHERE key IN ('format', 'minzoom', 'maxzoom') ORDER BY key"
                        ),
                        "expected_result": [["format", "mbtiles"], ["maxzoom", "3"], ["minzoom", "0"]],
                    },
                    {
                        "description": TILESTATS_CHECK_DESCRIPTION,
                        "sql": TILESTATS_CHECK_SQL,
                        "expected_result": [["cities", 4, 4], ["regions", 2, 2]],
                    },
                ]
            ),
        }
        # The first example uses a committed fixture: tools (and vgi-lint's scan probe) take sample
        # arguments from it, so it must work offline. The public-tileset examples follow.
        examples = [
            FunctionExample(
                sql=(
                    "SELECT key, value FROM mvt.main.tileset_metadata('test/fixtures/sample.pmtiles') "
                    "WHERE key IN ('name', 'bounds') ORDER BY key"
                ),
                description="Name and bounds of the fixture PMTiles archive",
            ),
            FunctionExample(sql=TILESTATS_CHECK_SQL, description=TILESTATS_CHECK_DESCRIPTION),
            FunctionExample(
                sql=(f"SELECT value FROM mvt.main.tileset_metadata('{ZCTA_PMTILES}') WHERE key = 'maxzoom'"),
                description="Max zoom of a remote PMTiles archive (reads only its header)",
            ),
        ]

    @classmethod
    def initial_state(cls, params: ProcessParams[SourceArgs]) -> Once:
        """Nothing emitted yet."""
        return Once()

    @classmethod
    def process(cls, params: ProcessParams[SourceArgs], state: Once, out: OutputCollector) -> None:
        """Emit the key/value rows."""
        if not state.done:
            pairs = metadata_rows(_info(params.args.source))
            state.done = True
            out.emit(
                pa.RecordBatch.from_pydict(
                    {"key": [k for k, _ in pairs], "value": [v for _, v in pairs]}, schema=params.output_schema
                )
            )
        out.finish()
