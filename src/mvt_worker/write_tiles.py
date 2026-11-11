"""``COPY ... TO 'out.pmtiles' (FORMAT 'mvt.pmtiles')``: encode rows as a vector tileset.

Each input row is one feature: a geometry column, an optional layer name, an
optional feature id, and attributes from the remaining columns. One COPY writes
every layer: ``UNION ALL`` the tables and give each its ``layer``. Rows reach
the worker in query order (the sink is ordered), and that order is the drawing
order inside each tile, so ``ORDER BY`` controls which features draw on top.

``write`` stores each batch in the execution's storage; ``close`` (once, on one
worker) projects, tiles and encodes everything zoom by zoom and writes the
archive. See :mod:`mvt_worker.tiler` for the geometry pipeline and
:mod:`mvt_worker.archive` for the PMTiles layout.
"""

from __future__ import annotations

import datetime as dt
import decimal
import gzip
import json
import logging
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, ClassVar

import numpy as np
import pyarrow as pa
import shapely
from pmtiles.tile import tileid_to_zxy
from vgi.arguments import Arg
from vgi.copy_to_function import CopyToFunction
from vgi.metadata import FunctionExample

from .archive import ArchiveWriter
from .encode import LayerBuilder, Value, encode_pieces, encode_tile, value_kind
from .meta import object_tags
from .sources import is_url
from .tiler import ZoomCutter, lonlat_to_unit, mercator_to_unit, split_by_dimension, tile_ids, unit_to_lonlat

if TYPE_CHECKING:
    from vgi.table_buffering_function import TableBufferingParams

log = logging.getLogger(__name__)

_SHARDS = b"pmtiles_rows"
MAX_ZOOM = 24
DEFAULT_MAX_TILE_BYTES = 500_000
DEFAULT_MAX_TILES = 5_000_000
GENERATOR = "vgi-mvt"


@dataclass(slots=True, frozen=True, kw_only=True)
class WriteArgs:
    """COPY options for ``FORMAT 'mvt.pmtiles'``."""

    layer: Annotated[
        str,
        Arg(
            "layer",
            default="",
            doc="Layer name for rows without a layer column (default: the file name without .pmtiles)",
        ),
    ] = ""
    layer_column: Annotated[
        str,
        Arg("layer_column", default="", doc="Column holding each row's layer name (default: a column named layer)"),
    ] = ""
    geometry_column: Annotated[
        str,
        Arg("geometry_column", default="", doc="GEOMETRY column to encode (default: the only GEOMETRY column)"),
    ] = ""
    id_column: Annotated[
        str,
        Arg(
            "id_column",
            default="",
            doc="Column of non-negative feature ids (default: a column named feature_id)",
        ),
    ] = ""
    properties_column: Annotated[
        str,
        Arg(
            "properties_column",
            default="",
            doc="Column of JSON objects whose keys become attributes (default: a column named properties)",
        ),
    ] = ""
    minzoom: Annotated[int, Arg("minzoom", default=0, ge=0, le=MAX_ZOOM, doc="Lowest zoom level to write")] = 0
    maxzoom: Annotated[int, Arg("maxzoom", default=14, ge=0, le=MAX_ZOOM, doc="Highest zoom level to write")] = 14
    minzoom_column: Annotated[
        str,
        Arg("minzoom_column", default="", doc="Column giving the lowest zoom each feature appears at"),
    ] = ""
    maxzoom_column: Annotated[
        str,
        Arg("maxzoom_column", default="", doc="Column giving the highest zoom each feature appears at"),
    ] = ""
    extent: Annotated[
        int, Arg("extent", default=4096, ge=256, le=65536, doc="Tile coordinate grid size (units per tile edge)")
    ] = 4096
    buffer: Annotated[
        float,
        Arg("buffer", default=4.0, ge=0.0, le=128.0, doc="Margin around each tile, in pixels of a 256-pixel tile"),
    ] = 4.0
    simplify: Annotated[
        float,
        Arg(
            "simplify",
            default=1.0,
            ge=0.0,
            le=64.0,
            doc="Line and polygon simplification tolerance, in pixels of a 256-pixel tile (0 turns it off)",
        ),
    ] = 1.0
    max_tile_bytes: Annotated[
        int,
        Arg(
            "max_tile_bytes",
            default=DEFAULT_MAX_TILE_BYTES,
            ge=0,
            doc="Fail if a compressed tile is larger than this (0: no limit)",
        ),
    ] = DEFAULT_MAX_TILE_BYTES
    max_tiles: Annotated[
        int,
        Arg("max_tiles", default=DEFAULT_MAX_TILES, ge=1, doc="Fail if the tileset would have more tiles than this"),
    ] = DEFAULT_MAX_TILES
    name: Annotated[str, Arg("name", default="", doc="Tileset name in the metadata (default: the file name)")] = ""
    description: Annotated[str, Arg("description", default="", doc="Tileset description in the metadata")] = ""
    attribution: Annotated[str, Arg("attribution", default="", doc="Attribution (HTML) in the metadata")] = ""


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------


def _ext(f: pa.Field[Any]) -> str:
    md = f.metadata or {}
    return md.get(b"ARROW:extension:name", b"").decode()


def _is_geometry(f: pa.Field[Any]) -> bool:
    return _ext(f) in ("geoarrow.wkb", "ogc.wkb")


def _is_json(f: pa.Field[Any]) -> bool:
    return _ext(f) == "arrow.json"


def _crs(f: pa.Field[Any]) -> str:
    """``lonlat`` or ``mercator`` from the column's CRS; error for anything else."""
    md = f.metadata or {}
    raw = md.get(b"ARROW:extension:metadata", b"") or b"{}"
    try:
        crs = json.loads(raw).get("crs")
    except (ValueError, AttributeError):
        crs = None
    if crs in (None, "", {}):
        return "lonlat"
    text = json.dumps(crs) if not isinstance(crs, str) else crs
    up = text.upper()
    if "3857" in up or "900913" in up or "PSEUDO-MERCATOR" in up:
        return "mercator"
    if "CRS84" in up or "4326" in up or "WGS 84" in up or "WGS84" in up:
        return "lonlat"
    raise ValueError(
        f"geometry column {f.name!r} has CRS {text[:80]}; vector tiles need WGS84 lon/lat or EPSG:3857. "
        f"Reproject first: ST_Transform({f.name}, '<its crs>', 'OGC:CRS84')"
    )


@dataclass(slots=True, frozen=True)
class Roles:
    """Which input column plays which part."""

    geometry: str
    crs: str
    layer: str | None
    fid: str | None
    properties: str | None
    properties_explicit: bool
    minzoom: str | None
    maxzoom: str | None
    attributes: tuple[str, ...]


def _pick(schema: pa.Schema, option: str, what: str, default: str) -> str | None:
    if option:
        if option not in schema.names:
            raise ValueError(
                f"{what} {option!r} is not a column of the COPY source (columns: {', '.join(schema.names)})"
            )
        return option
    return default if default in schema.names else None


def resolve_roles(schema: pa.Schema, options: WriteArgs) -> Roles:
    """Assign columns to geometry / layer / id / zoom / attributes, validating the options."""
    if options.minzoom > options.maxzoom:
        raise ValueError(f"minzoom ({options.minzoom}) is greater than maxzoom ({options.maxzoom})")
    if options.geometry_column:
        geom = _pick(schema, options.geometry_column, "geometry_column", "")
        assert geom is not None
        if not _is_geometry(schema.field(geom)):
            raise ValueError(f"geometry_column {geom!r} is not a GEOMETRY column")
    else:
        geoms = [f.name for f in schema if _is_geometry(f)]
        if not geoms:
            raise ValueError("the COPY source has no GEOMETRY column; select one (e.g. ST_Point(lon, lat) AS geometry)")
        if len(geoms) > 1:
            raise ValueError(
                f"the COPY source has several GEOMETRY columns ({', '.join(geoms)}); pick one with geometry_column"
            )
        geom = geoms[0]
    layer = _pick(schema, options.layer_column, "layer_column", "layer")
    fid = _pick(schema, options.id_column, "id_column", "feature_id")
    props = _pick(schema, options.properties_column, "properties_column", "properties")
    if props is not None and not (_is_json(schema.field(props)) or _is_text(schema.field(props).type)):
        if options.properties_column:
            raise ValueError(f"properties_column {props!r} must be JSON or VARCHAR, not {schema.field(props).type}")
        props = None  # a non-text column that happens to be called properties is an attribute
    zmin = _pick(schema, options.minzoom_column, "minzoom_column", "")
    zmax = _pick(schema, options.maxzoom_column, "maxzoom_column", "")
    for col, what in ((fid, "id column"), (zmin, "minzoom_column"), (zmax, "maxzoom_column")):
        if col is not None and not pa.types.is_integer(schema.field(col).type):
            raise ValueError(f"{what} {col!r} must be an integer column, not {schema.field(col).type}")
    if layer is not None and not _is_text(schema.field(layer).type):
        raise ValueError(f"layer column {layer!r} must be VARCHAR, not {schema.field(layer).type}")
    special = {geom, layer, fid, props, zmin, zmax}
    attributes = tuple(f.name for f in schema if f.name not in special)
    for name in attributes:
        _converter(schema.field(name))  # fail at the first batch, not after tiling
    explicit = bool(options.properties_column)
    return Roles(geom, _crs(schema.field(geom)), layer, fid, props, explicit, zmin, zmax, attributes)


def _is_text(t: pa.DataType) -> bool:
    if pa.types.is_dictionary(t):
        t = t.value_type
    return bool(pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_string_view(t))


def _int_value(v: int) -> Value:
    return v if -(1 << 63) <= v < (1 << 64) else str(v)


def _json_value(v: Any) -> Value | None:
    if v is None:
        return None
    if isinstance(v, bool | str):
        return v
    if isinstance(v, int):
        return _int_value(v)
    if isinstance(v, float):
        return v
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def _converter(f: pa.Field[Any]) -> Callable[[Any], Value | None]:
    """Return a function from a ``to_pylist`` value to an MVT value (None = omit)."""
    t = f.type
    if pa.types.is_dictionary(t):
        t = t.value_type
    if _is_json(f):
        return lambda v: None if v is None else _json_value(json.loads(v))
    if pa.types.is_boolean(t) or pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_string_view(t):
        return lambda v: v
    if pa.types.is_integer(t):
        return lambda v: None if v is None else _int_value(v)
    if pa.types.is_floating(t):
        return lambda v: None if v is None else float(v)
    if pa.types.is_decimal(t):
        return lambda v: None if v is None else float(v)
    if pa.types.is_temporal(t):
        return lambda v: None if v is None else (v.isoformat() if hasattr(v, "isoformat") else str(v))
    if _ext(f) == "arrow.uuid" or (pa.types.is_fixed_size_binary(t) and t.byte_width == 16 and "uuid" in _ext(f)):
        return lambda v: None if v is None else str(uuid.UUID(bytes=v) if isinstance(v, bytes) else v)
    if pa.types.is_nested(t):
        return lambda v: (
            None if v is None else json.dumps(v, separators=(",", ":"), default=_json_default, ensure_ascii=False)
        )
    if pa.types.is_null(t):
        return lambda v: None
    raise ValueError(
        f"column {f.name!r} has type {t}, which a vector tile attribute can't hold; "
        "cast it (e.g. to VARCHAR) or leave it out of the SELECT"
    )


def _json_default(v: Any) -> Any:
    if isinstance(v, dt.date | dt.datetime | dt.time):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, bytes):
        return v.hex()
    return str(v)


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Feature:
    """One encodable feature: unit-world geometry plus everything a tile needs."""

    layer: int
    geom: Any
    dim: int
    properties: list[tuple[str, str, Value]]
    fid: int | None
    zmin: int
    zmax: int


@dataclass
class Collected:
    """Every feature of the COPY, plus per-layer bookkeeping for the metadata."""

    layers: list[str] = field(default_factory=list)
    layer_index: dict[str, int] = field(default_factory=dict)
    fields: list[dict[str, set[str]]] = field(default_factory=list)
    features: list[Feature] = field(default_factory=list)
    rows: int = 0
    bounds: list[float] = field(default_factory=lambda: [math.inf, math.inf, -math.inf, -math.inf])

    def layer_id(self, name: str) -> int:
        """Index of a layer, registering it on first sight (first appearance = layer order)."""
        i = self.layer_index.get(name)
        if i is None:
            i = self.layer_index[name] = len(self.layers)
            self.layers.append(name)
            self.fields.append({})
        return i


_KIND_TYPE = {"s": "String", "b": "Boolean", "i": "Number", "f": "Number", "d": "Number"}


def _project(geoms: np.ndarray[Any, Any], crs: str) -> np.ndarray[Any, Any]:
    if crs == "lonlat":
        b = shapely.total_bounds(geoms)
        if not np.isnan(b).any() and (b[0] < -180.5 or b[2] > 180.5 or b[1] < -90.5 or b[3] > 90.5):
            raise ValueError(
                "geometry coordinates are outside lon/lat range "
                f"({b[0]:.6g}, {b[1]:.6g}, {b[2]:.6g}, {b[3]:.6g}). If the data is projected, give the "
                "column its CRS (ST_SetCRS) or reproject with ST_Transform(geom, '<crs>', 'OGC:CRS84')"
            )
        return shapely.transform(geoms, lonlat_to_unit)  # type: ignore[no-any-return]
    return shapely.transform(geoms, mercator_to_unit)  # type: ignore[no-any-return]


def collect(batches: list[pa.RecordBatch], roles: Roles, options: WriteArgs, default_layer: str) -> Collected:
    """Turn the stored batches into features."""
    out = Collected()
    for batch in batches:
        out.rows += batch.num_rows
        gcol = batch.column(roles.geometry)
        wkb = gcol.cast(pa.binary()) if not pa.types.is_binary(gcol.type) else gcol
        geoms = shapely.from_wkb(np.asarray(wkb.to_pylist(), dtype=object))
        keep = ~(shapely.is_missing(geoms) | shapely.is_empty(geoms))
        if not keep.any():
            continue
        geoms = geoms.copy()
        geoms[keep] = _project(geoms[keep], roles.crs)
        poly = keep & (shapely.get_dimensions(geoms) == 2)
        bad = poly & ~shapely.is_valid(geoms)
        if bad.any():
            geoms[bad] = shapely.make_valid(geoms[bad])
        bx0, by0, bx1, by1 = shapely.total_bounds(geoms[keep]).tolist()
        b = out.bounds
        b[0], b[1], b[2], b[3] = min(b[0], bx0), min(b[1], by0), max(b[2], bx1), max(b[3], by1)

        names = batch.column(roles.layer).to_pylist() if roles.layer else None
        fids = batch.column(roles.fid).to_pylist() if roles.fid else None
        zmins = batch.column(roles.minzoom).to_pylist() if roles.minzoom else None
        zmaxs = batch.column(roles.maxzoom).to_pylist() if roles.maxzoom else None
        props_json = batch.column(roles.properties).to_pylist() if roles.properties else None
        attrs = [
            (n, _converter(batch.schema.field(n)), batch.column(n).to_pylist(), _single(batch.schema.field(n)))
            for n in roles.attributes
        ]
        for i in np.flatnonzero(keep).tolist():
            layer_name = (names[i] if names else None) or default_layer
            li = out.layer_id(layer_name)
            seen = out.fields[li]
            fid = fids[i] if fids else None
            if fid is not None and fid < 0:
                raise ValueError(f"feature id {fid} is negative; vector tile ids are unsigned integers")
            props: list[tuple[str, str, Value]] = []
            if props_json is not None and props_json[i] is not None:
                obj = _json_object(props_json[i])
                if obj is not None:
                    for k, v in obj.items():
                        mv = _json_value(v)
                        if mv is not None:
                            props.append((k, value_kind(mv), mv))
                elif roles.properties_explicit:
                    raise ValueError(
                        f"properties_column {roles.properties!r} holds a value that is not a JSON object: "
                        f"{str(props_json[i])[:60]!r}"
                    )
                else:  # not an object: keep it as an ordinary attribute
                    props.append((str(roles.properties), "s", str(props_json[i])))
            for n, conv, values, single in attrs:
                mv = conv(values[i])
                if mv is not None:
                    props.append((n, value_kind(mv, single=single), mv))
            for k, kind, _ in props:
                seen.setdefault(k, set()).add(_KIND_TYPE[kind])
            lo = zmins[i] if zmins else None
            hi = zmaxs[i] if zmaxs else None
            zmin = options.minzoom if lo is None else max(options.minzoom, int(lo))
            zmax = options.maxzoom if hi is None else min(options.maxzoom, int(hi))
            for dim, part in split_by_dimension(geoms[i]):
                out.features.append(Feature(li, part, dim, props, fid, zmin, zmax))
    return out


def _json_object(text: Any) -> dict[str, Any] | None:
    """Return the JSON object a properties value holds; None if it is not one."""
    if not isinstance(text, str) or not text.lstrip().startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _single(f: pa.Field[Any]) -> bool:
    t = f.type.value_type if pa.types.is_dictionary(f.type) else f.type
    return pa.types.is_float32(t) or pa.types.is_float16(t)


# ---------------------------------------------------------------------------
# Tiling
# ---------------------------------------------------------------------------


@dataclass
class TilingStats:
    """What was written, for the metadata and the log."""

    tiles: int = 0
    layer_zooms: dict[int, list[int]] = field(default_factory=dict)


def write_archive(destination: str, data: Collected, options: WriteArgs, name: str) -> TilingStats:
    """Tile every feature zoom by zoom and write the PMTiles archive.

    Per zoom and geometry dimension, the active features are cut and encoded
    as whole arrays; the pieces are then sorted by (tile id, input order) and
    assembled into tiles in archive order.
    """
    px = options.extent / 256.0
    stats = TilingStats()
    feats = data.features
    geoms = np.array([f.geom for f in feats], dtype=object)
    dims = np.array([f.dim for f in feats], dtype=np.int8)
    zmin = np.array([f.zmin for f in feats], dtype=np.int16)
    zmax = np.array([f.zmax for f in feats], dtype=np.int16)
    writer = ArchiveWriter(destination)
    try:
        for z in range(options.minzoom, options.maxzoom + 1):
            cutter = ZoomCutter(z, options.extent, options.buffer * px, options.simplify * px)
            active = (zmin <= z) & (zmax >= z)
            tids: list[Any] = []
            owners: list[Any] = []
            kinds: list[int] = []
            payloads: list[Any] = []
            for dim in (0, 1, 2):
                sel = np.flatnonzero(active & (dims == dim))
                if not len(sel):
                    continue
                x, y, which, pieces = cutter.cut_many(geoms[sel], dim)
                kind, pay = encode_pieces(pieces)
                tids.append(tile_ids(z, x, y))
                owners.append(sel[which])
                kinds += [kind] * len(pay)
                payloads += pay
            if not payloads:
                continue
            tid = np.concatenate(tids)
            owner = np.concatenate(owners)
            order = np.lexsort((owner, tid))
            distinct = int(np.count_nonzero(np.diff(tid[order]))) + 1
            if stats.tiles + distinct > options.max_tiles:
                raise ValueError(
                    f"the tileset would have more than {options.max_tiles:,} tiles (reached at zoom {z}); "
                    "lower maxzoom, or raise max_tiles"
                )
            current = -1
            layers: dict[int, LayerBuilder] = {}
            for i in order.tolist():
                payload = payloads[i]
                if payload is None:
                    continue
                t = int(tid[i])
                if t != current:
                    if layers:
                        _emit(writer, current, layers, options, stats)
                    current, layers = t, {}
                feat = feats[int(owner[i])]
                lb = layers.get(feat.layer)
                if lb is None:
                    lb = layers[feat.layer] = LayerBuilder(data.layers[feat.layer], options.extent)
                    zr = stats.layer_zooms.setdefault(feat.layer, [z, z])
                    zr[1] = max(zr[1], z)
                lb.add(kinds[i], payload, feat.properties, feat.fid)
            if layers:
                _emit(writer, current, layers, options, stats)
            log.info("pmtiles: zoom %d done, %d tiles so far", z, stats.tiles)
        writer.finish(_header(data, options), _metadata(data, stats, options, name))
    except BaseException:
        writer.abort()
        raise
    return stats


def _emit(
    writer: ArchiveWriter, tid: int, layers: dict[int, LayerBuilder], options: WriteArgs, stats: TilingStats
) -> None:
    """Encode, compress, size-check and store one tile."""
    raw = encode_tile([layers[k] for k in sorted(layers)])
    blob = gzip.compress(raw, compresslevel=6, mtime=0)
    if options.max_tile_bytes and len(blob) > options.max_tile_bytes:
        tz, tx, ty = tileid_to_zxy(tid)
        raise ValueError(
            f"tile {tz}/{tx}/{ty} is {len(blob):,} bytes compressed, over max_tile_bytes "
            f"({options.max_tile_bytes:,}). Show fewer features at low zooms (raise minzoom, or "
            "set a per-feature minzoom_column), simplify more, or set max_tile_bytes 0"
        )
    writer.add(tid, blob)
    stats.tiles += 1


def _header(data: Collected, options: WriteArgs) -> dict[str, Any]:
    if data.features:
        x0, y0, x1, y1 = data.bounds
        lon0, lat1 = unit_to_lonlat(x0, y0)
        lon1, lat0 = unit_to_lonlat(x1, y1)
    else:
        lon0, lat0, lon1, lat1 = -180.0, -85.0511287798066, 180.0, 85.0511287798066
    e7 = 10_000_000
    return {
        "min_zoom": options.minzoom,
        "max_zoom": options.maxzoom,
        "min_lon_e7": round(lon0 * e7),
        "min_lat_e7": round(lat0 * e7),
        "max_lon_e7": round(lon1 * e7),
        "max_lat_e7": round(lat1 * e7),
        "center_zoom": options.minzoom,
        "center_lon_e7": round((lon0 + lon1) / 2 * e7),
        "center_lat_e7": round((lat0 + lat1) / 2 * e7),
    }


def _metadata(data: Collected, stats: TilingStats, options: WriteArgs, name: str) -> dict[str, Any]:
    layers = []
    for i, lname in enumerate(data.layers):
        zr = stats.layer_zooms.get(i, [options.minzoom, options.maxzoom])
        fields = {k: (next(iter(v)) if len(v) == 1 else "Mixed") for k, v in data.fields[i].items()}
        layers.append({"id": lname, "fields": fields, "minzoom": zr[0], "maxzoom": zr[1], "description": ""})
    meta: dict[str, Any] = {
        "name": options.name or name,
        "format": "pbf",
        "type": "overlay",
        "generator": GENERATOR,
        "vector_layers": layers,
    }
    if options.description:
        meta["description"] = options.description
    if options.attribution:
        meta["attribution"] = options.attribution
    return meta


# ---------------------------------------------------------------------------
# The COPY function
# ---------------------------------------------------------------------------


def _ipc(batch: pa.RecordBatch) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, batch.schema) as w:
        w.write_batch(batch)
    return bytes(sink.getvalue().to_pybytes())


class WritePmtiles(CopyToFunction[WriteArgs]):
    """Encode the COPY source as a PMTiles vector tileset."""

    COPY_TO_FORMAT: ClassVar[str] = "pmtiles"
    COPY_TO_COMMENT: ClassVar[str | None] = "Vector tiles (MVT) in a PMTiles v3 archive"

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "write_pmtiles"
        description = "COPY ... TO format that encodes rows with a GEOMETRY column as a PMTiles vector tileset"
        sink_order_dependent = True  # input order is drawing order inside a tile
        tags = {
            **object_tags(
                title="Write PMTiles",
                category="writers",
                keywords="copy to, export, write, tippecanoe, tileset, pmtiles, mvt, vector tiles, basemap",
                doc_llm=(
                    "A COPY TO format, not a table function: use it as FORMAT 'mvt.pmtiles' in a COPY ... TO "
                    "statement whose destination is a local .pmtiles path. Each row is a feature. The `GEOMETRY` "
                    "column must be WGS84 lon/lat (or EPSG:3857). A `VARCHAR` column named `layer` (or "
                    "`layer_column`) names each row's layer, so one COPY can write several layers via UNION ALL; "
                    "without it every row goes to `layer` (default: the file name). A column named `feature_id` "
                    "(or `id_column`) becomes the feature id, and a column named `properties` holding JSON "
                    "objects is expanded into attributes, so read_tiles output can be written back. Every other "
                    "column becomes an attribute (`VARCHAR`, numbers, `BOOLEAN`; dates as ISO text; lists and "
                    "structs as JSON text); NULLs are omitted. Options: minzoom/maxzoom (default 0/14); "
                    "minzoom_column/maxzoom_column for per-feature zoom ranges; simplify and buffer (pixels of "
                    "a 256-pixel tile); extent (4096); max_tile_bytes (500000: fails rather than dropping "
                    "features); max_tiles; name, description, attribution. ORDER BY sets drawing order. "
                    "COPY reports the number of rows written."
                ),
                doc_md=(
                    "## COPY ... TO (FORMAT 'mvt.pmtiles')\n\n"
                    "Encode a query's rows as Mapbox Vector Tiles in a PMTiles archive. One row is one feature; "
                    "a `layer` column puts rows in different layers of the same tiles, so a single COPY of a "
                    "`UNION ALL` writes a whole multi-layer basemap."
                ),
            ),
            "vgi.result_columns_schema": json.dumps(
                [{"name": "Count", "type": "BIGINT", "description": "Rows written (reported by COPY)."}]
            ),
        }
        examples = [
            FunctionExample(
                sql=(
                    "COPY (SELECT 'cities' AS layer, name, population, ST_Point(lon, lat) AS geometry "
                    "FROM (VALUES ('Rome', 2800000, 12.5, 41.9), ('Paris', 2100000, 2.35, 48.86)) "
                    "t(name, population, lon, lat)) TO 'cities.pmtiles' (FORMAT 'mvt.pmtiles', maxzoom 8)"
                ),
                description="Write two cities as a one-layer tileset (zoom 0 to 8)",
            ),
            FunctionExample(
                sql=(
                    "COPY (SELECT layer, feature_id, properties, geometry FROM mvt.main.read_tiles("
                    "'test/fixtures/sample.pmtiles')) TO 'sample-copy.pmtiles' (FORMAT 'mvt.pmtiles', maxzoom 4, "
                    "name 'sample copy')"
                ),
                description="Re-tile every layer of an existing tileset into a new archive",
            ),
        ]

    @classmethod
    def write(
        cls,
        *,
        batch: pa.RecordBatch,
        options: WriteArgs,
        file_path: str,
        params: TableBufferingParams[WriteArgs],
    ) -> None:
        """Validate the columns (first batch fails fast) and store the batch."""
        if is_url(file_path):
            raise ValueError(f"cannot write {file_path}: the PMTiles writer writes local files only")
        resolve_roles(batch.schema, options)
        if batch.num_rows:
            params.storage.state_append(_SHARDS, b"", _ipc(batch))

    @classmethod
    def close(
        cls,
        *,
        options: WriteArgs,
        file_path: str,
        params: TableBufferingParams[WriteArgs],
    ) -> int:
        """Tile everything and write the archive."""
        if is_url(file_path):
            raise ValueError(f"cannot write {file_path}: the PMTiles writer writes local files only")
        assert params.init_call is not None
        batches = [
            pa.ipc.open_stream(blob).read_next_batch()
            for _id, blob in params.storage.state_log_scan(_SHARDS, b"", after_id=-1)
        ]
        schema = batches[0].schema if batches else params.init_call.bind_call.input_schema
        stem = Path(file_path).name.removesuffix(".pmtiles") or "layer"
        if schema is None or not batches:
            data = Collected()
            if schema is not None:
                resolve_roles(schema, options)
        else:
            roles = resolve_roles(schema, options)
            data = collect(batches, roles, options, options.layer or stem)
        del batches
        stats = write_archive(file_path, data, options, stem)
        log.info("pmtiles: wrote %s: %d rows, %d tiles", file_path, data.rows, stats.tiles)
        return data.rows
