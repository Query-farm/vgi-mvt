"""Table functions that read vector tiles into rows: ``read_tiles`` and ``read_mvt``.

``read_tiles`` reads a whole tileset (PMTiles, MBTiles or an XYZ URL template)
at one zoom level, optionally inside a WGS84 bounding box. ``read_mvt`` reads a
single ``.mvt``/``.pbf`` tile whose ``z/x/y`` you supply. Both emit one row per
feature with a GEOMETRY column; naming a ``layer`` adds a typed column for each
of that layer's attributes.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Annotated, Any, cast

from vgi.arguments import Arg
from vgi.invocation import BindResponse
from vgi.metadata import FilterFunctionCapability, FunctionExample
from vgi.table_function import (
    BindParams,
    OutputCollector,
    ProcessParams,
    TableFunctionGenerator,
    init_single_worker,
)
from vgi_rpc import ArrowSerializableDataclass

from .edges import TILE_EDGE_CHOICES, EdgeAssembler, TileEdges
from .meta import FIRENZE_PMTILES, STREETS_TILE, ZCTA_PMTILES, object_tags
from .mvt import COORDINATE_CHOICES, Coordinates, decode_tile
from .pushdown import INTERSECTS_EXTENT, clamp_to_world, intersect, pushed_bbox
from .rows import RowBuilder, attribute_fields, dynamic_columns_md, infer_field_types, output_schema
from .sources import BBox, TileKey, TileSourceError, open_source, read_bytes

BATCH_ROWS = 8192
TILES_PER_FETCH = 32
DEFAULT_MAX_TILES = 10_000

_COORDS_DOC = (
    "Output coordinate system: 'wgs84' (longitude/latitude degrees, the default), 'web_mercator' "
    "(EPSG:3857 metres) or 'tile' (raw tile pixels, 0..extent from the tile's top-left corner)."
)
TILE_EDGES_DOC = (
    "How features that reach a tile edge come back: 'merge' (the default: one row per feature; points "
    "only in the tile that owns them, lines and polygons clipped to their tile and the pieces sharing a "
    "feature id joined into one geometry), 'clip' (clipped to each tile, not joined: pieces never "
    "overlap) or 'raw' (exactly as stored in the tiles, including the buffer each tile repeats from "
    "its neighbours)."
)
_LAYER_DOC = (
    "Read only this layer. Also adds one typed column per attribute of the layer (from the tileset's "
    "vector_layers metadata, or inferred from the tile). NULL reads every layer."
)


def _bbox(west: float | None, south: float | None, east: float | None, north: float | None) -> BBox | None:
    given = [v is not None for v in (west, south, east, north)]
    if not any(given):
        return None
    if not all(given):
        raise ValueError("give all four of west, south, east and north, or none of them")
    w, s, e, n = cast("tuple[float, float, float, float]", (west, south, east, north))
    if w > e or s > n:
        raise ValueError("bbox needs west <= east and south <= north")
    return (w, s, e, n)


# ---------------------------------------------------------------- read_tiles


@dataclass(slots=True, frozen=True, kw_only=True)
class ReadTilesArgs:
    """Arguments for ``read_tiles(source, ...)``."""

    source: Annotated[
        str,
        Arg(
            0,
            doc=(
                "Tileset to read: a .pmtiles file or http(s) URL, a local .mbtiles file, or a tile URL "
                "template containing {z}, {x} and {y}."
            ),
        ),
    ]
    zoom: Annotated[
        int | None,
        Arg("zoom", default=None, doc="Zoom level to read; defaults to the tileset's maxzoom.", ge=0, le=30),
    ]
    layer: Annotated[str | None, Arg("layer", default=None, doc=_LAYER_DOC)]
    west: Annotated[
        float | None,
        Arg("west", default=None, doc="West edge of the area to read, degrees longitude.", ge=-180, le=180),
    ]
    south: Annotated[
        float | None, Arg("south", default=None, doc="South edge of the area to read, degrees latitude.", ge=-90, le=90)
    ]
    east: Annotated[
        float | None,
        Arg("east", default=None, doc="East edge of the area to read, degrees longitude.", ge=-180, le=180),
    ]
    north: Annotated[
        float | None, Arg("north", default=None, doc="North edge of the area to read, degrees latitude.", ge=-90, le=90)
    ]
    coordinates: Annotated[str, Arg("coordinates", default="wgs84", doc=_COORDS_DOC, choices=list(COORDINATE_CHOICES))]
    max_tiles: Annotated[
        int,
        Arg(
            "max_tiles",
            default=DEFAULT_MAX_TILES,
            doc="Fail instead of reading more than this many tiles (a guard for huge archives).",
            ge=1,
            le=10_000_000,
        ),
    ]
    tile_edges: Annotated[str, Arg("tile_edges", default="merge", doc=TILE_EDGES_DOC, choices=list(TILE_EDGE_CHOICES))]


@dataclass(kw_only=True)
class TileCursor(ArrowSerializableDataclass):
    """Index of the next tile to read in the (deterministic) tile list, and the read it belongs to."""

    next_tile: int = 0
    run: str = ""


_ASSEMBLERS: OrderedDict[str, EdgeAssembler] = OrderedDict()
_ASSEMBLERS_LOCK = threading.Lock()


def _assembler(run: str, args: ReadTilesArgs, keys: list[TileKey]) -> EdgeAssembler:
    """Return the edge state of one read (one per query, kept while its batches are served)."""
    with _ASSEMBLERS_LOCK:
        hit = _ASSEMBLERS.get(run)
        if hit is None:
            hit = EdgeAssembler(
                open_source(args.source),
                keys,
                layer_filter=args.layer,
                coordinates=cast("Coordinates", args.coordinates),
                mode=cast("TileEdges", args.tile_edges),
            )
            _ASSEMBLERS[run] = hit
            while len(_ASSEMBLERS) > 16:
                _ASSEMBLERS.popitem(last=False)
        _ASSEMBLERS.move_to_end(run)
        return hit


# A pushed box edge can lie exactly on a tile boundary; widen it a hair so the tile beyond is read too
# and a feature crossing the box edge is not cut there (ST_Within / ST_Contains would then be wrong).
_HINT_MARGIN = 1e-9


_TILE_LISTS: OrderedDict[tuple[object, ...], list[TileKey]] = OrderedDict()
_TILE_LISTS_LOCK = threading.Lock()


def _resolve_zoom(args: ReadTilesArgs) -> int:
    if args.zoom is not None:
        return args.zoom
    info = open_source(args.source).info()
    if info.maxzoom is None:
        raise ValueError("this source has no maxzoom metadata; pass zoom := <level>")
    return int(info.maxzoom)


def _tile_list(args: ReadTilesArgs, hint: BBox | None = None) -> list[TileKey]:
    """Tiles to read: the zoom's tiles inside the argument bbox and the pushed spatial hint."""
    z = _resolve_zoom(args)
    combined = intersect(_bbox(args.west, args.south, args.east, args.north), hint)
    if combined is False:
        return []
    bbox: BBox | None = None
    if isinstance(combined, tuple):
        clamped = clamp_to_world(combined)
        if clamped is False:
            return []
        bbox = clamped if isinstance(clamped, tuple) else None
    key = (args.source, z, bbox, args.max_tiles)
    with _TILE_LISTS_LOCK:
        hit = _TILE_LISTS.get(key)
    if hit is not None:
        return hit
    keys = open_source(args.source).tiles(z, bbox, args.max_tiles)
    with _TILE_LISTS_LOCK:
        _TILE_LISTS[key] = keys
        while len(_TILE_LISTS) > 32:
            _TILE_LISTS.popitem(last=False)
    return keys


@init_single_worker
class ReadTiles(TableFunctionGenerator[ReadTilesArgs, TileCursor]):
    """Read every feature of a tileset at one zoom level."""

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "read_tiles"
        # Accept DuckDB's filters so the VGI extension can push the bounding box of
        # WHERE ST_Intersects(geometry, <constant>) (an optional `&&` hint) into the
        # scan; the SDK applies every pushed filter to the rows we emit.
        filter_pushdown = True
        auto_apply_filters = True
        additional_filter_functions = [FilterFunctionCapability(*INTERSECTS_EXTENT)]
        description = "Read the features of a PMTiles, MBTiles or XYZ vector tileset as rows with a GEOMETRY column"
        tags = {
            **object_tags(
                title="Read Vector Tileset",
                category="readers",
                keywords=(
                    "vector tiles, mvt, mapbox vector tile, pmtiles, mbtiles, xyz tiles, tileset, protobuf, "
                    "geometry, gis, map tiles, decode tiles"
                ),
                doc_llm=(
                    "Decode a Mapbox Vector Tile tileset back into rows, one per feature. `source` is a "
                    "PMTiles archive (local path or http(s) URL, read with range requests), a local "
                    "MBTiles file, or a tile URL template with `{z}/{x}/{y}`. Named arguments: `zoom` "
                    "(defaults to the tileset maxzoom), `layer` (read one layer and add a typed column per "
                    "attribute), `west`/`south`/`east`/`north` (WGS84 bbox; all four or none; required for "
                    "URL templates), `coordinates` ('wgs84' default, 'web_mercator' or 'tile') and "
                    "`max_tiles` (default 10000; the call fails rather than read more). A constant "
                    "`WHERE ST_Intersects(geometry, <geometry>)` (or ST_Within/ST_Contains/...) also narrows "
                    "the tiles read, so it can replace the bbox arguments. Columns: layer, "
                    "z, x, y, feature_id, geometry_type, geometry (GEOMETRY), properties (JSON) and, with "
                    "`layer`, one typed column per layer attribute. `tile_edges` ('merge' default, 'clip', "
                    "'raw') controls features that cross tile edges: 'merge' returns one row per feature, "
                    "joining the per-tile pieces that share a feature_id (features without an id may come "
                    "back as several non-overlapping pieces); 'raw' returns each tile's copy, buffer "
                    "included. A feature extending past the tiles read is cut at their edge. Use "
                    "`tileset_layers` first to discover layer and attribute names."
                ),
                doc_md=(
                    "## read_tiles(source, zoom := maxzoom, layer := NULL, west/south/east/north := NULL, "
                    "coordinates := 'wgs84', max_tiles := 10000, tile_edges := 'merge')\n\n"
                    "One row per vector-tile feature, with a `GEOMETRY` column and a JSON `properties` "
                    "column. Sources: PMTiles (file or URL), MBTiles (file) or an `{z}/{x}/{y}` URL "
                    "template.\n\n"
                    "Pass `layer` to also get each attribute of that layer as its own typed column. "
                    "Features crossing tile edges come back once (`tile_edges := 'merge'`); use "
                    "`'raw'` for each tile's stored copy."
                ),
            ),
            "vgi.result_dynamic_columns_md": dynamic_columns_md(
                [
                    ("name", "VARCHAR", "Example: a String attribute."),
                    ("population", "DOUBLE", "Example: a Number attribute."),
                    ("capital", "BOOLEAN", "Example: a Boolean attribute."),
                ],
                "Number becomes `DOUBLE`, Boolean `BOOLEAN`, String and anything else `VARCHAR`; names that "
                "clash case-insensitively with another column are skipped (they stay in `properties`).",
            ),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "Count the fixture tileset's features per layer at zoom 2",
                        "sql": (
                            "SELECT layer, count(*) AS features FROM mvt.main.read_tiles("
                            "'test/fixtures/sample.pmtiles', zoom := 2) GROUP BY layer ORDER BY layer"
                        ),
                        "expected_result": [["cities", 4], ["regions", 2]],
                    },
                ]
            ),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT properties->>'pmap:kind' AS kind, count(*) AS n FROM mvt.main.read_tiles("
                    f"'{FIRENZE_PMTILES}', "
                    "zoom := 14) WHERE layer = 'roads' GROUP BY ALL ORDER BY n DESC"
                ),
                description="Road kinds in the Protomaps Florence sample, read over HTTP range requests",
            ),
            FunctionExample(
                sql=(
                    "SELECT DISTINCT ZCTA5CE10 AS zip FROM mvt.main.read_tiles("
                    f"'{ZCTA_PMTILES}', zoom := 7, layer := 'zcta', "
                    "west := -74.02, south := 40.70, east := -73.93, north := 40.80) "
                    "WHERE ST_Intersects(geometry, ST_MakeEnvelope(-74.02, 40.70, -73.93, 40.80)) ORDER BY zip"
                ),
                description="US ZIP code areas touching a Manhattan bbox, from a remote PMTiles archive",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[ReadTilesArgs]) -> BindResponse:
        """Open the source, check the arguments and add typed columns for ``layer``."""
        args = params.args
        _bbox(args.west, args.south, args.east, args.north)
        try:
            info = open_source(args.source).info()
        except TileSourceError as exc:
            raise ValueError(str(exc)) from exc
        if args.zoom is None and info.maxzoom is None:
            raise ValueError("this source has no maxzoom metadata; pass zoom := <level>")
        extra = []
        if args.layer is not None and info.layers:
            match = [lay for lay in info.layers if lay.id == args.layer]
            if not match:
                names = ", ".join(sorted(lay.id for lay in info.layers))
                raise ValueError(f"layer {args.layer!r} is not in this tileset; layers: {names}")
            extra = attribute_fields(match[0].fields)
        return BindResponse(output_schema=output_schema(extra, args.coordinates))

    @classmethod
    def _pushed_filters(cls, params: ProcessParams[ReadTilesArgs]) -> Any:
        init = params.init_call
        if init is None or init.pushdown_filters is None:
            return None
        return cls.pushdown_filters(init.pushdown_filters, join_keys=init.join_keys, output_schema=init.output_schema)

    @classmethod
    def initial_state(cls, params: ProcessParams[ReadTilesArgs]) -> TileCursor:
        """Start at the first tile."""
        return TileCursor(run=uuid.uuid4().hex)

    @classmethod
    def process(cls, params: ProcessParams[ReadTilesArgs], state: TileCursor, out: OutputCollector) -> None:
        """Decode tiles until a batch is full, then emit it."""
        args = params.args
        hint = pushed_bbox(cls._pushed_filters(params), cast("Coordinates", args.coordinates))
        if hint is False:  # the spatial filter lies outside the world: nothing can match
            out.finish()
            return
        try:
            box = None
            if isinstance(hint, tuple):
                m = _HINT_MARGIN
                box = (hint[0] - m, hint[1] - m, hint[2] + m, hint[3] + m)
            keys = _tile_list(args, box)
            source = open_source(args.source)
            rows = RowBuilder(params.output_schema, cast("Coordinates", args.coordinates), args.layer)
            if not state.run:
                state.run = uuid.uuid4().hex
            edges = _assembler(state.run, args, keys)
            while state.next_tile < len(keys) and len(rows) < BATCH_ROWS:
                chunk = keys[state.next_tile : state.next_tile + TILES_PER_FETCH]
                state.next_tile += len(chunk)
                with edges.lock:
                    for key, data in source.fetch(chunk):
                        edges.add_tile(rows, key, data)
        except TileSourceError as exc:
            raise ValueError(str(exc)) from exc
        if len(rows):
            out.emit(rows.take())
        if state.next_tile >= len(keys):
            out.finish()


# ---------------------------------------------------------------- read_mvt


@dataclass(slots=True, frozen=True, kw_only=True)
class ReadMvtArgs:
    """Arguments for ``read_mvt(source, z, x, y, ...)``."""

    source: Annotated[str, Arg(0, doc="One vector tile: a .mvt/.pbf file path or http(s) URL (gzip is detected).")]
    z: Annotated[int, Arg(1, doc="Zoom level of the tile.", ge=0, le=30)]
    x: Annotated[int, Arg(2, doc="XYZ column of the tile.", ge=0)]
    y: Annotated[int, Arg(3, doc="XYZ row of the tile (0 at the north edge).", ge=0)]
    layer: Annotated[str | None, Arg("layer", default=None, doc=_LAYER_DOC)]
    coordinates: Annotated[str, Arg("coordinates", default="wgs84", doc=_COORDS_DOC, choices=list(COORDINATE_CHOICES))]


@dataclass(kw_only=True)
class DoneState(ArrowSerializableDataclass):
    """Whether the single tile has been emitted."""

    done: bool = False


_TILE_CACHE: OrderedDict[str, bytes] = OrderedDict()


def _tile_bytes(source: str) -> bytes:
    hit = _TILE_CACHE.get(source)
    if hit is None:
        try:
            hit = read_bytes(source)
        except TileSourceError as exc:
            raise ValueError(str(exc)) from exc
        _TILE_CACHE[source] = hit
        while len(_TILE_CACHE) > 8:
            _TILE_CACHE.popitem(last=False)
    return hit


def _check_xy(z: int, x: int, y: int) -> None:
    if x >= 1 << z or y >= 1 << z:
        raise ValueError(f"tile {z}/{x}/{y} does not exist: x and y must be below {1 << z} at zoom {z}")


@init_single_worker
class ReadMvt(TableFunctionGenerator[ReadMvtArgs, DoneState]):
    """Read the features of one vector tile."""

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "read_mvt"
        description = "Read the features of one Mapbox Vector Tile (.mvt/.pbf) at a given z/x/y"
        tags = {
            **object_tags(
                title="Read One Vector Tile",
                category="readers",
                keywords="vector tile, mvt, pbf, mapbox vector tile, protobuf, decode tile, geometry, z/x/y",
                doc_llm=(
                    "Decode a single Mapbox Vector Tile file or URL into rows, one per feature. A tile "
                    "stores coordinates relative to its own corner, so you must pass its zoom `z`, column "
                    "`x` and row `y` (XYZ scheme, row 0 at the north) to get real coordinates back. "
                    "Named arguments: `layer` (one layer, plus a typed column per attribute inferred from "
                    "the tile) and `coordinates` ('wgs84', 'web_mercator' or 'tile'). Columns match "
                    "`read_tiles`: layer, z, x, y, feature_id, geometry_type, geometry, properties."
                ),
                doc_md=(
                    "## read_mvt(source, z, x, y, layer := NULL, coordinates := 'wgs84')\n\n"
                    "Decode one `.mvt` / `.pbf` tile (file or URL, gzip detected). The tile address is "
                    "needed to turn tile pixels back into longitude/latitude."
                ),
            ),
            "vgi.result_dynamic_columns_md": dynamic_columns_md(
                [
                    ("name", "VARCHAR", "Example: an attribute holding strings."),
                    ("population", "BIGINT", "Example: an attribute holding whole numbers."),
                    ("capital", "BOOLEAN", "Example: an attribute holding booleans."),
                ],
                "Types are inferred from the tile's values: whole numbers `BIGINT`, other numbers `DOUBLE`, "
                "booleans `BOOLEAN`, strings or mixed values `VARCHAR`.",
            ),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "Features per geometry type in the fixture tile",
                        "sql": (
                            "SELECT geometry_type, count(*) AS n FROM mvt.main.read_mvt("
                            "'test/fixtures/0-0-0.mvt', 0, 0, 0) GROUP BY geometry_type ORDER BY geometry_type"
                        ),
                        "expected_result": [["POINT", 4], ["POLYGON", 2]],
                    },
                ]
            ),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT layer, count(*) AS features FROM mvt.main.read_mvt("
                    f"'{STREETS_TILE}', "
                    "14, 8801, 5371) GROUP BY layer ORDER BY features DESC"
                ),
                description="Feature counts per layer of a Mapbox Streets sample tile",
            ),
            FunctionExample(
                sql=(
                    "SELECT name, geometry FROM mvt.main.read_mvt('test/fixtures/0-0-0.mvt', 0, 0, 0, "
                    "layer := 'cities') ORDER BY population DESC"
                ),
                description="Cities from one tile with their attributes as typed columns",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[ReadMvtArgs]) -> BindResponse:
        """Infer typed attribute columns from the tile when ``layer`` is given."""
        args = params.args
        _check_xy(args.z, args.x, args.y)
        extra = []
        if args.layer is not None:
            layers = decode_tile(_tile_bytes(args.source), layer_filter=args.layer)
            extra = attribute_fields(infer_field_types(layers))
        return BindResponse(output_schema=output_schema(extra, args.coordinates))

    @classmethod
    def initial_state(cls, params: ProcessParams[ReadMvtArgs]) -> DoneState:
        """Nothing emitted yet."""
        return DoneState()

    @classmethod
    def process(cls, params: ProcessParams[ReadMvtArgs], state: DoneState, out: OutputCollector) -> None:
        """Decode the tile in one batch."""
        args = params.args
        if not state.done:
            rows = RowBuilder(params.output_schema, cast("Coordinates", args.coordinates), args.layer)
            rows.add_tile(args.z, args.x, args.y, _tile_bytes(args.source))
            state.done = True
            out.emit(rows.take())
        out.finish()
