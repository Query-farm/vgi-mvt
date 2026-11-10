# Changelog

## 0.1.0 (unreleased)

First release: decode Mapbox Vector Tiles from PMTiles (local or HTTP range), MBTiles,
XYZ templates and single `.mvt` files into DuckDB rows with `GEOMETRY` and JSON columns.

- `read_tiles`, `read_mvt`, `mvt_decode`, `tileset_layers`, `tileset_metadata`.
- `ATTACH ... (TYPE vgi, tileset '...')`: one table per layer, with typed attribute columns.
- Spatial filters (`ST_Intersects` and friends) choose which tiles are read.
- `tile_edges` (`merge` by default): one row per feature across tile edges.
- Decoder conformance: Mapbox mvt-fixtures, mapbox-vector-tile feature-by-feature on
  real tilesets, GDAL, and fuzzing (`make conformance`).
- Hardening: 64 MiB decompressed-tile cap, 128 MiB HTTP response cap, refusal of servers
  that ignore `Range`, decode errors that name the tile, a bounded PMTiles directory cache.
- Remote PMTiles: neighbouring tiles share one range request, and connections are kept
  alive (about 5x faster than one request per tile).

Started from the VGI starter template.

Targets: vgi-python 0.50 · VGI protocol 3.2 · Haybarn 2.0.0rc11 · vgi-lint-check 2.x (unpinned)
