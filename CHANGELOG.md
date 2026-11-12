# Changelog

## 0.3.0 (2026-11-12)

- Speaks VGI protocol 3.3 (vgi-python `>=0.51.0,<0.52`). The vgi DuckDB extension matches
  the protocol's major and minor version exactly, so the current extension refuses 0.2.x
  workers and this release needs the current extension. The SQL surface is unchanged.

## 0.2.2 (2026-11-11)

- Written archives get normal file permissions (0666 minus the umask, or those of the file
  they replace) instead of the temporary file's 0600, which a web server running as another
  user couldn't read.

## 0.2.1 (2026-11-11)

- `tile_edges := 'merge'` no longer slows to a crawl on long lines spread over many tiles.
  Decoded neighbour tiles were cached by count (256), and once a read covered more tiles
  than that, each feature's walk re-decoded tiles the previous walk had evicted: 2,000
  random lines over 653 tiles took over 10 minutes instead of 3 s. The cache is now sized
  by memory (256 MiB of decoded pieces), skips tiles whose pieces are all written, and
  evicts those first. Each tile is decoded once unless a read's pieces exceed the budget.

## 0.2.0 (2026-11-11)

Write vector tilesets: `COPY (SELECT ...) TO 'out.pmtiles' (FORMAT 'mvt.pmtiles')`.

- One row per feature; a `layer` column writes several layers in one COPY. `feature_id`
  becomes the id, a `properties` column of JSON objects is expanded, other columns become
  typed attributes, so `read_tiles` output writes back.
- Clip with a buffer, per-zoom simplification, snapping to the tile grid with validity repair,
  MVT 2.1 winding; per-feature zoom ranges (`minzoom_column`, `maxzoom_column`).
- Clustered PMTiles v3 with `vector_layers` metadata, identical tiles stored once, written to a
  temporary file and renamed into place. Fails with the tile's address rather than drop
  features when a tile passes `max_tile_bytes`.
- `make conformance` also checks the writer: mapbox-vector-tile must read every written tile
  exactly like this worker's decoder, and every polygon must be valid.
- Local tilesets are no longer held open (memory-mapped) between reads, so on Windows a
  tileset the worker has read can still be replaced or deleted.

## 0.1.0 (2026-11-10)

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
