"""Attach a tileset as a database: one table per vector tile layer.

::

    ATTACH 'mvt' AS florence (TYPE vgi, LOCATION '/path/to/vgi-mvt-worker',
                              tileset 'https://example.com/florence.pmtiles');
    SELECT name, geometry FROM florence.pois WHERE kind = 'cafe';

Without a ``tileset`` option the catalog holds only the reader functions. With
one, ``main`` also lists a table per layer of the tileset's ``vector_layers``
metadata. Each table's columns are the feature columns plus one typed column
per layer attribute, and every scan is redirected to ``read_tiles`` with the
attach's zoom, bbox, coordinate system and tile limit.

Nothing per attach is kept on ``self``: the ATTACH options travel inside
``attach_opaque_data`` (the framework carries them) and every hook re-reads
them, so pooled and HTTP workers behave the same.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa
from vgi.catalog import (
    AttachOpaqueData,
    CatalogAttachResult,
    ReadOnlyCatalogInterface,
    ScanFunctionResult,
    SchemaInfo,
    SchemaObjectType,
    SerializedSchema,
    TableInfo,
    TransactionOpaqueData,
)

from .edges import TILE_EDGE_CHOICES
from .mvt import COORDINATE_CHOICES
from .read_tiles import DEFAULT_MAX_TILES
from .rows import attribute_fields, output_schema
from .sources import BBox, LayerInfo, TileSourceError, open_source, source_format

if TYPE_CHECKING:
    from vgi import SchemaPath
    from vgi.catalog.catalog_interface import FunctionInfo, IndexInfo, MacroInfo, ViewInfo
    from vgi_rpc.rpc import CallContext

MAIN = ["main"]
_ATTACH_PREFIX = b"vgi-mvt-tileset:"


@dataclass(frozen=True, slots=True)
class TilesetAttach:
    """The tileset settings of one ATTACH."""

    source: str
    zoom: int | None
    coordinates: str
    bbox: BBox | None
    max_tiles: int
    tile_edges: str = "merge"


def parse_bbox(raw: str) -> BBox | None:
    """Parse ``'west,south,east,north'`` (degrees); empty means the whole tileset."""
    raw = raw.strip()
    if not raw:
        return None
    parts = raw.replace(" ", ",").split(",")
    try:
        values = [float(p) for p in parts if p]
    except ValueError as exc:
        raise ValueError(f"bbox must be 'west,south,east,north' in degrees, got {raw!r}") from exc
    if len(values) != 4 or not all(math.isfinite(v) for v in values):
        raise ValueError(f"bbox must be 'west,south,east,north' in degrees, got {raw!r}")
    w, s, e, n = values
    if not (-180 <= w <= e <= 180 and -90 <= s <= n <= 90):
        raise ValueError(f"bbox needs -180 <= west <= east <= 180 and -90 <= south <= north <= 90, got {raw!r}")
    return (w, s, e, n)


def tileset_attach(options: Mapping[str, Any]) -> TilesetAttach | None:
    """Read and validate the tileset options; None when no ``tileset`` was given."""
    source = str(options.get("tileset") or "").strip()
    if not source:
        return None
    zoom_raw = options.get("zoom")
    zoom = None if zoom_raw is None or int(zoom_raw) < 0 else int(zoom_raw)
    if zoom is not None and zoom > 30:
        raise ValueError(f"zoom must be between 0 and 30, got {zoom}")
    coordinates = str(options.get("coordinates") or "wgs84")
    if coordinates not in COORDINATE_CHOICES:
        raise ValueError(f"coordinates must be one of {', '.join(COORDINATE_CHOICES)}, got {coordinates!r}")
    max_tiles = int(options.get("max_tiles") or DEFAULT_MAX_TILES)
    if max_tiles < 1:
        raise ValueError(f"max_tiles must be at least 1, got {max_tiles}")
    tile_edges = str(options.get("tile_edges") or "merge")
    if tile_edges not in TILE_EDGE_CHOICES:
        raise ValueError(f"tile_edges must be one of {', '.join(TILE_EDGE_CHOICES)}, got {tile_edges!r}")
    return TilesetAttach(
        source=source,
        zoom=zoom,
        coordinates=coordinates,
        bbox=parse_bbox(str(options.get("bbox") or "")),
        max_tiles=max_tiles,
        tile_edges=tile_edges,
    )


def _layers(attach: TilesetAttach) -> list[LayerInfo]:
    try:
        return open_source(attach.source).info().layers
    except TileSourceError as exc:
        raise ValueError(str(exc)) from exc


def _layer_comment(layer: LayerInfo, attach: TilesetAttach, zoom: int | None) -> str:
    parts = [layer.description.strip()] if layer.description and layer.description.strip() else []
    parts.append(f"Features of the `{layer.id}` layer of {attach.source}, read at zoom {zoom}.")
    if layer.minzoom is not None and zoom is not None and zoom < layer.minzoom:
        parts.append(f"The layer starts at zoom {layer.minzoom}, so it is empty at this zoom.")
    return " ".join(parts)


def _table_info(layer: LayerInfo, attach: TilesetAttach, zoom: int | None) -> TableInfo:
    schema = output_schema(attribute_fields(layer.fields), attach.coordinates)
    return TableInfo(
        name=layer.id,
        schema_path=list(MAIN),
        columns=SerializedSchema(schema.serialize().to_pybytes()),
        constraints=[],
        comment=_layer_comment(layer, attach, zoom),
        tags={
            "vgi.title": f"Layer {layer.id}",
            "mvt.tileset": attach.source,
            "mvt.layer": layer.id,
            "mvt.zoom": "" if zoom is None else str(zoom),
            "mvt.attributes": json.dumps(layer.fields),
        },
    )


class TilesetCatalog(ReadOnlyCatalogInterface):
    """The ``mvt`` catalog: reader functions, plus layer tables when a tileset is attached."""

    # Tables depend on the ATTACH options, so the worker must not share one
    # catalog_contents snapshot between attaches.
    catalog_contents_attach_independent = False

    def loggable_attach_options(self, options: Mapping[str, Any]) -> Mapping[str, Any]:
        """Log the tileset settings (no credentials are accepted at attach)."""
        keys = ("tileset", "zoom", "coordinates", "bbox", "max_tiles", "tile_edges")
        return {k: options[k] for k in keys if k in options}

    def catalog_attach(
        self,
        *,
        name: str,
        options: dict[str, Any],
        data_version_spec: str | None,
        implementation_version: str | None,
        client_capabilities: Any = None,
        ctx: CallContext | None = None,
    ) -> CatalogAttachResult:
        """Attach, failing early when the tileset cannot be opened or described."""
        attach = tileset_attach(options)
        if attach is not None:
            if source_format(attach.source) == "xyz":
                raise ValueError(
                    "a {z}/{x}/{y} URL template has no layer metadata, so it cannot be attached as a "
                    "database; query it with read_tiles(...) instead"
                )
            if not _layers(attach):
                raise ValueError(
                    f"{attach.source} declares no vector_layers metadata, so there are no tables to list; "
                    "query it with read_tiles(...) instead"
                )
        return super().catalog_attach(
            name=name,
            options=options,
            data_version_spec=data_version_spec,
            implementation_version=implementation_version,
            client_capabilities=client_capabilities,
            ctx=ctx,
        )

    def attach_opaque_data_for(self, *, name: str, options: dict[str, Any]) -> AttachOpaqueData:
        """Carry the tileset settings to every hook of this attach.

        Catalog hooks see only these bytes, not the ATTACH options. None of the
        tileset options is a credential, so they can travel in plain text.
        """
        attach = tileset_attach(options)
        if attach is None:
            return super().attach_opaque_data_for(name=name, options=options)
        payload = {
            "v": 1,
            "source": attach.source,
            "zoom": attach.zoom,
            "coordinates": attach.coordinates,
            "bbox": list(attach.bbox) if attach.bbox else None,
            "max_tiles": attach.max_tiles,
            "tile_edges": attach.tile_edges,
        }
        return AttachOpaqueData(_ATTACH_PREFIX + json.dumps(payload, separators=(",", ":")).encode())

    @staticmethod
    def _attach(attach_opaque_data: AttachOpaqueData) -> TilesetAttach | None:
        raw = bytes(attach_opaque_data)
        if not raw.startswith(_ATTACH_PREFIX):
            return None
        d = json.loads(raw[len(_ATTACH_PREFIX) :])
        bbox = d.get("bbox")
        return TilesetAttach(
            source=str(d["source"]),
            zoom=d.get("zoom"),
            coordinates=str(d.get("coordinates") or "wgs84"),
            bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])) if bbox else None,
            max_tiles=int(d.get("max_tiles") or DEFAULT_MAX_TILES),
            tile_edges=str(d.get("tile_edges") or "merge"),
        )

    @staticmethod
    def _zoom(attach: TilesetAttach) -> int | None:
        if attach.zoom is not None:
            return attach.zoom
        try:
            return open_source(attach.source).info().maxzoom
        except TileSourceError as exc:
            raise ValueError(str(exc)) from exc

    def _layer_tables(self, attach_opaque_data: AttachOpaqueData) -> list[TableInfo]:
        attach = self._attach(attach_opaque_data)
        if attach is None:
            return []
        zoom = self._zoom(attach)
        return [_table_info(layer, attach, zoom) for layer in _layers(attach)]

    def _with_table_count(self, info: SchemaInfo, attach_opaque_data: AttachOpaqueData) -> SchemaInfo:
        """Correct ``main``'s advertised table count: the declared schema has none, the tileset adds them.

        The client (and ``catalog_contents``) skip an object kind whose estimated
        count is exactly 0, so leaving the declared count would hide every layer.
        """
        if [p.lower() for p in info.path] != MAIN:
            return info
        counts = dict(info.estimated_object_count or {})
        counts["table"] = len(self._layer_tables(attach_opaque_data))
        return dataclasses.replace(info, estimated_object_count=counts)

    def schemas(
        self, *, attach_opaque_data: AttachOpaqueData, transaction_opaque_data: TransactionOpaqueData | None
    ) -> list[SchemaInfo]:
        """List the schemas, with ``main`` counting the layer tables."""
        return [
            self._with_table_count(info, attach_opaque_data)
            for info in super().schemas(
                attach_opaque_data=attach_opaque_data, transaction_opaque_data=transaction_opaque_data
            )
        ]

    def schema_get(
        self,
        *,
        attach_opaque_data: AttachOpaqueData,
        transaction_opaque_data: TransactionOpaqueData | None,
        path: SchemaPath,
    ) -> SchemaInfo | None:
        """Look up a schema, with ``main`` counting the layer tables."""
        info = super().schema_get(
            attach_opaque_data=attach_opaque_data, transaction_opaque_data=transaction_opaque_data, path=path
        )
        return None if info is None else self._with_table_count(info, attach_opaque_data)

    def schema_contents(  # type: ignore[override]
        self,
        *,
        attach_opaque_data: AttachOpaqueData,
        transaction_opaque_data: TransactionOpaqueData | None,
        path: SchemaPath,
        type: SchemaObjectType,  # noqa: A002 - SDK keyword
    ) -> Sequence[TableInfo | ViewInfo | FunctionInfo | MacroInfo | IndexInfo]:
        """List the reader functions, plus one table per layer in ``main``."""
        base = list(
            super().schema_contents(  # type: ignore[call-overload]
                attach_opaque_data=attach_opaque_data,
                transaction_opaque_data=transaction_opaque_data,
                path=path,
                type=type,
            )
        )
        kind = type if isinstance(type, SchemaObjectType) else SchemaObjectType(type)
        if kind == SchemaObjectType.TABLE and [p.lower() for p in path] == MAIN:
            base.extend(self._layer_tables(attach_opaque_data))
        return base

    def table_get(
        self,
        *,
        attach_opaque_data: AttachOpaqueData,
        transaction_opaque_data: TransactionOpaqueData | None,
        schema_path: SchemaPath,
        name: str,
        at_unit: str | None = None,
        at_value: str | None = None,
    ) -> TableInfo | None:
        """Look up a layer table by name (case-insensitive)."""
        if at_unit:
            raise ValueError("vector tile layers do not support time travel (AT ...)")
        if [p.lower() for p in schema_path] != MAIN:
            return None
        for info in self._layer_tables(attach_opaque_data):
            if info.name.lower() == name.lower():
                return info
        return None

    def table_scan_function_get(
        self,
        *,
        attach_opaque_data: AttachOpaqueData,
        transaction_opaque_data: TransactionOpaqueData | None,
        schema_path: SchemaPath,
        name: str,
        at_unit: str | None,
        at_value: str | None,
    ) -> ScanFunctionResult:
        """Redirect a layer table scan to ``read_tiles`` with the attach's settings."""
        attach = self._attach(attach_opaque_data)
        info = self.table_get(
            attach_opaque_data=attach_opaque_data,
            transaction_opaque_data=transaction_opaque_data,
            schema_path=schema_path,
            name=name,
            at_unit=at_unit,
            at_value=at_value,
        )
        if attach is None or info is None:
            raise ValueError(f"no layer table named {name!r} in this tileset")
        named: dict[str, pa.Scalar[Any]] = {
            "layer": pa.scalar(info.tags["mvt.layer"]),
            "coordinates": pa.scalar(attach.coordinates),
            "max_tiles": pa.scalar(attach.max_tiles, pa.int64()),
            "tile_edges": pa.scalar(attach.tile_edges),
        }
        if attach.zoom is not None:
            named["zoom"] = pa.scalar(attach.zoom, pa.int64())
        if attach.bbox is not None:
            for key, value in zip(("west", "south", "east", "north"), attach.bbox, strict=True):
                named[key] = pa.scalar(value, pa.float64())
        return ScanFunctionResult(
            function_name="read_tiles",
            positional_arguments=[pa.scalar(attach.source)],
            named_arguments=named,
            required_extensions=[],
            schema_path=list(MAIN),
        )
