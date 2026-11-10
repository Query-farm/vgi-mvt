"""Turn decoded tiles into Arrow batches with one row per feature.

Every reader shares the same leading columns (layer, z, x, y, feature_id,
geometry_type, geometry, properties). When the caller picks a single
``layer``, each of that layer's attributes also becomes its own typed column,
so a tileset reads back as an ordinary table.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from typing import Any

import pyarrow as pa

from .meta import column
from .mvt import Coordinates, Layer, Projector, PropertyValue, decode_tile, feature_wkb

JSON_METADATA = {b"ARROW:extension:name": b"arrow.json"}


_CRS = {"wgs84": "OGC:CRS84", "web_mercator": "EPSG:3857"}


def _geometry_field(coordinates: str = "wgs84") -> pa.Field[Any]:
    """Build the GEOMETRY column, tagged with its CRS so DuckDB and GeoParquet writers know it."""
    crs = _CRS.get(coordinates)
    meta = {
        b"ARROW:extension:name": b"geoarrow.wkb",
        b"ARROW:extension:metadata": json.dumps({"crs": crs} if crs else {}).encode(),
        b"comment": b"Feature geometry as a DuckDB GEOMETRY, in the coordinate system chosen by `coordinates`.",
    }
    return pa.field("geometry", pa.binary(), nullable=True, metadata=meta)


def _json_field() -> pa.Field[Any]:
    meta = {**JSON_METADATA, b"comment": b"Every attribute of the feature as a JSON object."}
    return pa.field("properties", pa.string(), nullable=True, metadata=meta)


BASE_FIELDS: list[pa.Field[Any]] = [
    column("layer", pa.string(), "Name of the vector tile layer the feature belongs to.", nullable=False),
    column("z", pa.int32(), "Zoom level of the tile the feature was read from.", nullable=False),
    column("x", pa.int32(), "XYZ column of the tile (0 at the antimeridian, increasing east).", nullable=False),
    column("y", pa.int32(), "XYZ row of the tile (0 at the north edge, increasing south).", nullable=False),
    column("feature_id", pa.uint64(), "Feature id stored in the tile, or NULL when the encoder set none."),
    column(
        "geometry_type",
        pa.string(),
        "OGC type of the geometry: POINT, MULTIPOINT, LINESTRING, MULTILINESTRING, POLYGON or MULTIPOLYGON.",
        nullable=False,
    ),
    _geometry_field(),
    _json_field(),
]
BASE_NAMES = frozenset(f.name for f in BASE_FIELDS)

RESULT_COLUMNS_DOC = [
    {"name": "layer", "type": "VARCHAR", "description": "Vector tile layer name."},
    {"name": "z", "type": "INTEGER", "description": "Tile zoom level."},
    {"name": "x", "type": "INTEGER", "description": "Tile column (XYZ scheme)."},
    {"name": "y", "type": "INTEGER", "description": "Tile row (XYZ scheme, 0 at the north)."},
    {"name": "feature_id", "type": "UBIGINT", "description": "Feature id from the tile; NULL if absent."},
    {"name": "geometry_type", "type": "VARCHAR", "description": "OGC geometry type of the feature."},
    {"name": "geometry", "type": "GEOMETRY", "description": "Feature geometry (WGS84 lon/lat by default)."},
    {"name": "properties", "type": "JSON", "description": "All feature attributes as a JSON object."},
]


def dynamic_columns_md(attribute_rows: list[tuple[str, str, str]], note: str) -> str:
    """Markdown result-column tables: the base columns, then the variant with ``layer := ...``."""
    head = "| Name | Type | Description |\n|---|---|---|\n"
    base = "".join(f"| {c['name']} | {c['type']} | {c['description']} |\n" for c in RESULT_COLUMNS_DOC)
    extra = "".join(f"| {n} | {t} | {d} |\n" for n, t, d in attribute_rows)
    return (
        "**Without `layer`** (every layer):\n\n" + head + base + "\n"
        "**With `layer := '<name>'`**: the columns above, then one column per attribute of that layer. "
        + note
        + "\n\n"
        + head
        + base
        + extra
    )


# vector_layers field type -> Arrow type
_TYPE_MAP: dict[str, pa.DataType] = {
    "number": pa.float64(),
    "boolean": pa.bool_(),
    "string": pa.string(),
    "integer": pa.int64(),
}


def attribute_fields(fields: dict[str, str]) -> list[pa.Field[Any]]:
    """Typed columns for a layer's attributes; names clashing with base columns are left out."""
    out: list[pa.Field[Any]] = []
    taken = {n.lower() for n in BASE_NAMES}
    for name, kind in fields.items():
        # DuckDB column names are case-insensitive: keep the first spelling, the rest stay in `properties`.
        if name.lower() in taken:
            continue
        taken.add(name.lower())
        arrow_type = _TYPE_MAP.get(kind.lower(), pa.string())
        out.append(column(name, arrow_type, f"Layer attribute `{name}` ({kind}) from the tileset metadata."))
    return out


def infer_field_types(layers: Iterable[Layer]) -> dict[str, str]:
    """Infer ``{attribute: Number|Integer|Boolean|String}`` from decoded features."""
    seen: dict[str, set[str]] = {}
    for layer in layers:
        for feature in layer.features:
            for k, v in feature.properties.items():
                if v is None:
                    continue
                kind = (
                    "Boolean"
                    if isinstance(v, bool)
                    else "Integer"
                    if isinstance(v, int)
                    else "Number"
                    if isinstance(v, float)
                    else "String"
                )
                seen.setdefault(k, set()).add(kind)
    out: dict[str, str] = {}
    for k, kinds in seen.items():
        if len(kinds) == 1:
            out[k] = next(iter(kinds))
        elif kinds <= {"Integer", "Number"}:
            out[k] = "Number"
        else:
            out[k] = "String"
    return out


def output_schema(extra: list[pa.Field[Any]] | None = None, coordinates: str = "wgs84") -> pa.Schema:
    """Return the base schema (geometry tagged with the CRS of ``coordinates``) plus typed attribute columns."""
    fields = [_geometry_field(coordinates) if f.name == "geometry" else f for f in BASE_FIELDS]
    return pa.schema([*fields, *(extra or [])])


def _json_safe(v: PropertyValue | None) -> PropertyValue | None:
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _convert(v: PropertyValue | None, t: pa.DataType) -> Any:
    if v is None:
        return None
    if pa.types.is_boolean(t):
        return v if isinstance(v, bool) else None
    if pa.types.is_int64(t):
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v if -(1 << 63) <= v < (1 << 63) else None
        if isinstance(v, float) and v.is_integer() and abs(v) < 2**63:
            return int(v)
        return None
    if pa.types.is_floating(t):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return float(v)
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    return json.dumps(_json_safe(v))


class RowBuilder:
    """Accumulate feature rows for one output schema."""

    def __init__(self, schema: pa.Schema, coordinates: Coordinates, layer: str | None) -> None:
        """Prepare empty columns for ``schema``."""
        self.schema = schema
        self.coordinates = coordinates
        self.layer = layer
        self.extra = [f for f in schema if f.name not in BASE_NAMES]
        self.cols: dict[str, list[Any]] = {f.name: [] for f in schema}

    def __len__(self) -> int:
        """Rows accumulated so far."""
        return len(self.cols["layer"])

    def add_tile(self, z: int, x: int, y: int, data: bytes) -> None:
        """Decode one tile's bytes and append its features."""
        for layer in decode_tile(data, layer_filter=self.layer):
            self.add_layer(z, x, y, layer)

    def add_layer(self, z: int, x: int, y: int, layer: Layer) -> None:
        """Append the features of one decoded layer."""
        project = Projector(z, x, y, layer.extent, self.coordinates)
        c = self.cols
        for feature in layer.features:
            gtype, wkb = feature_wkb(feature, project, layer.version)
            c["layer"].append(layer.name)
            c["z"].append(z)
            c["x"].append(x)
            c["y"].append(y)
            c["feature_id"].append(feature.id)
            c["geometry_type"].append(gtype)
            c["geometry"].append(wkb)
            props = feature.properties
            c["properties"].append(json.dumps({k: _json_safe(v) for k, v in props.items()}, ensure_ascii=False))
            for f in self.extra:
                c[f.name].append(_convert(props.get(f.name), f.type))

    def take(self) -> pa.RecordBatch:
        """Return the accumulated rows as a batch and reset."""
        batch = pa.RecordBatch.from_pydict(self.cols, schema=self.schema)
        self.cols = {f.name: [] for f in self.schema}
        return batch
