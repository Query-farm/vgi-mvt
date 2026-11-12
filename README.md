<p align="center">
  <img src="docs/vgi-logo.png" alt="Vector Gateway Interface (VGI)" width="320">
</p>

<p align="center"><em>A Query.Farm VGI worker for DuckDB.</em></p>

# Read & Write Mapbox Vector Tiles, PMTiles & MBTiles in DuckDB

> **vgi-mvt** · a [Query.Farm](https://query.farm) VGI worker · written in Python

[![CI](https://github.com/Query-farm/vgi-mvt/actions/workflows/ci.yml/badge.svg)](https://github.com/Query-farm/vgi-mvt/actions/workflows/ci.yml)
[![vgi worker: query.farm](https://query.farm/media-kit/shields/vgi-worker.svg)](https://query.farm)

Vector tiles are how most web maps ship data: features cut into `z/x/y` squares and
encoded as [Mapbox Vector Tile](https://github.com/mapbox/vector-tile-spec) protobufs.
This worker decodes them **back into rows**. Each row holds one feature with a DuckDB
`GEOMETRY('OGC:CRS84')` column (WGS84 longitude/latitude), plus its attributes, both as
`JSON` and, when you pick a layer, as typed columns. The CRS tag carries through to
GeoParquet when you `COPY` the result out.

**Attach a tileset as a database.** Each layer becomes a table, and each layer attribute
becomes a typed column:

```sql
ATTACH 'mvt' AS florence (TYPE vgi, LOCATION '/abs/path/to/vgi-mvt-worker',
  tileset 'https://raw.githubusercontent.com/protomaps/PMTiles/main/spec/v3/protomaps(vector)ODbL_firenze.pmtiles');

SHOW ALL TABLES;                                   -- boundaries, buildings, earth, ..., roads, water
SELECT "pmap:kind" AS kind, count(*) FROM florence.roads GROUP BY kind;
SELECT name, geometry FROM florence.pois WHERE "pmap:kind" = 'cafe';
```

| ATTACH option | Default | Meaning |
|---|---|---|
| `tileset` | (none) | PMTiles file or `https://` URL, or a local MBTiles file. Without it you get only the functions below. |
| `zoom` | the tileset's max zoom | Zoom level every layer table reads. |
| `bbox` | whole tileset | `'west,south,east,north'` in degrees: every table reads only the tiles covering it. Needed for planet-scale archives. |
| `coordinates` | `'wgs84'` | `'web_mercator'` or `'tile'` instead. |
| `max_tiles` | 10000 | A table scan fails rather than read more tiles than this. |
| `tile_edges` | `'merge'` | `'merge'`: one row per feature. `'clip'`: one non-overlapping piece per tile. `'raw'`: each tile's copy as stored, buffer included. |

**Spatial filters choose the tiles.** For `WHERE ST_Intersects(geometry, <constant>)`, and
likewise `ST_Contains`, `ST_Within`, `ST_Covers`, `ST_CoveredBy`, `ST_Overlaps`, `ST_Touches`,
`ST_Crosses` and `ST_Equals`, the VGI extension pushes the constant's bounding box into the
scan, and only the tiles under it are read. DuckDB still applies the exact predicate. So a
worldwide archive needs no `bbox` option:

```sql
-- 4.6 GB of Overture places worldwide; this reads only the tiles over central Florence (about 1.4 s).
ATTACH 'mvt' AS world (TYPE vgi, LOCATION '/abs/path/to/vgi-mvt-worker',
  tileset 'https://r2-public.protomaps.com/protomaps-sample-datasets/overture-pois.pmtiles');

SELECT category_main, count(*) AS n
FROM world.pois
WHERE ST_Intersects(geometry, ST_MakeEnvelope(11.24, 43.76, 11.27, 43.78))
GROUP BY ALL ORDER BY n DESC LIMIT 10;
```

The box has to come from a constant geometry, and it is not derived through an `OR` or a
negation (`NOT ST_Intersects`, `ST_Disjoint`), or from a join against another table. Those
queries read the whole zoom level and stop at `max_tiles`; use the `bbox` option for them. The
same pushdown works on `read_tiles(...)` directly, and it lets a `{z}/{x}/{y}` template run
without an explicit bbox. A bad path, a `{z}/{x}/{y}`
template (which has no layer list) or a malformed `bbox` fails at `ATTACH` time.

**Or call the reader functions directly:**

```sql
SELECT name, population, capital, geometry
FROM mvt.main.read_tiles('cities.pmtiles', layer := 'cities')
ORDER BY population DESC;
```

| Function | Reads |
|---|---|
| `read_tiles(source, zoom :=, layer :=, west/south/east/north :=, coordinates :=, max_tiles :=, tile_edges :=)` | a whole tileset at one zoom: **PMTiles** (file or `https://` URL, via range requests), **MBTiles** (file), or a `{z}/{x}/{y}` **tile-server URL template** |
| `read_mvt(source, z, x, y, layer :=, coordinates :=)` | one `.mvt` / `.pbf` tile (file or URL, gzip detected) |
| `mvt_decode((SELECT tile, z, x, y FROM …), layer :=, coordinates :=)` | tile blobs already in DuckDB, for example MBTiles rows read with `sqlite_scan` |
| `tileset_layers(source)` | the layers and typed attributes a tileset declares (`vector_layers`) |
| `tileset_metadata(source)` | format, zoom range, bounds, attribution and the rest of the metadata |

Every feature-reading function returns `layer, z, x, y, feature_id, geometry_type,
geometry, properties`. With `layer := 'name'`, one typed column per attribute of that
layer follows.

## Write a tileset

The other direction is a `COPY` format. One row is one feature, and a `layer` column puts
rows into different layers of the same tiles, so one `COPY` of a `UNION ALL` writes a whole
multi-layer tileset:

```sql
COPY (
    SELECT 'places' AS layer, id AS feature_id, name, population, ST_Point(lon, lat) AS geometry FROM places
    UNION ALL BY NAME
    SELECT 'roads' AS layer, id AS feature_id, name, kind, geom AS geometry FROM roads
    UNION ALL BY NAME
    SELECT 'parks' AS layer, id AS feature_id, name, geom AS geometry FROM parks
    ORDER BY layer, population   -- drawing order inside each tile
) TO 'city.pmtiles' (FORMAT 'mvt.pmtiles', maxzoom 14, name 'City basemap', attribution '© OpenStreetMap');
```

The result is an ordinary PMTiles v3 archive (clustered, gzip tiles, `vector_layers` metadata).
The reference `pmtiles` Python reader, GDAL and this worker's own readers have been checked
against it.

| Column | Becomes |
|---|---|
| the `GEOMETRY` column | the feature's geometry. WGS84 lon/lat (or `EPSG:3857`); other CRSs are refused, so `ST_Transform` first. |
| `layer` (`VARCHAR`) | the layer name. Without it every row goes to the `layer` option (default: the file name). |
| `feature_id` (non-negative integer) | the feature id. |
| `properties` (JSON objects) | expanded into attributes, so `read_tiles` output writes straight back. |
| anything else | an attribute: text, numbers and booleans as themselves, dates as ISO text, lists and structs as JSON text. NULLs are left out. |

Re-tiling an existing tileset: `COPY (SELECT * EXCLUDE (z, x, y, geometry_type) FROM
mvt.main.read_tiles('in.pmtiles')) TO 'out.pmtiles' (FORMAT 'mvt.pmtiles', maxzoom 10)`.
Without the `EXCLUDE`, the tile address columns would become attributes too.

| Option | Default | Meaning |
|---|---|---|
| `minzoom`, `maxzoom` | 0, 14 | Zoom range written. |
| `minzoom_column`, `maxzoom_column` | (none) | Integer columns giving each feature its own zoom range, e.g. small towns only from z8. |
| `simplify` | 1 | Line and polygon simplification tolerance, in pixels of a 256-pixel tile. 0 turns it off. |
| `buffer` | 4 | Margin each tile carries around its edge (pixels of a 256-pixel tile), so lines and labels don't clip at tile edges. |
| `extent` | 4096 | Grid units per tile edge. |
| `max_tile_bytes` | 500000 | The COPY fails if a compressed tile is larger. 0 removes the limit. |
| `max_tiles` | 5000000 | The COPY fails rather than write more tiles than this. |
| `layer_column`, `geometry_column`, `id_column`, `properties_column` | (by name) | Use other columns for those roles. |
| `name`, `description`, `attribution` | file name, empty, empty | Tileset metadata. |

How it tiles:
- **Features are clipped to each tile plus the buffer**, snapped to the tile grid and
  repaired, so every tile holds valid geometry. Polygons follow the MVT 2.1 winding rule.
- **Lines and polygons are simplified per zoom** before they're cut, so neighbouring tiles
  agree at their shared edge. A polygon smaller than a grid cell at some zoom disappears there,
  as in other tilers.
- **Nothing is dropped to fit a size limit.** The writer keeps every feature at every zoom in
  range; when a tile grows past `max_tile_bytes` it stops with the tile's address and the
  options that help (a later `minzoom`, a per-feature `minzoom_column`, more `simplify`).
  tippecanoe and planetiler thin features automatically instead. Use them for planet-scale
  basemaps.
- **Identical tiles are stored once**, such as the all-water tiles inside a large polygon.
- **The archive is written to a temporary file and renamed into place**, so a failed COPY
  leaves no partial file. Destinations are local paths.

Every written tile is checked by `make conformance`: mapbox-vector-tile reads 2,580 tiles of
random points, lines and polygons with holes exactly as this worker's decoder does, and every
polygon is valid. GDAL reads the archives too, with the same polygon areas.

## What survives the round trip

Vector tiles are a display format, so the decoding is exact but the data it recovers has
already lost some detail:

- **Coordinates are quantised.** A tile stores integers on a grid, usually 4096×4096 per
  tile. Decoding inverts the projection exactly, so the error is the grid cell: about 1 m at
  z14 and about 0.6 km at z4. Read the highest zoom (the default) for the most faithful
  geometry.
- **Tiles split features, and the tileset readers join them back.** A tileset stores a
  feature once per tile it touches, plus a buffer copy in each neighbour. `read_tiles` and the
  attached tables undo that by default (`tile_edges := 'merge'`):
  - A point is kept only in the tile that owns it.
  - Lines and polygons are clipped to their tile, and the pieces that belong together are
    joined into one geometry (with GEOS, via shapely).
  - Pieces belong together when they share a layer, a `feature_id` and their properties. When
    the tileset has no ids, they must also meet at the shared tile edge.

  `count(*)`, `sum(ST_Area(...))` and `sum(ST_Length(...))` are then right without extra
  work. On the Protomaps Florence sample, 4,092 road rows became 3,557 rows for 3,557 ids. On
  Overture POIs, 8,029 rows became 7,211. A feature can still come back as more than one row
  when it has no id and its pieces don't meet (an island of a multipolygon in another tile).
  A feature is cut at the edge of the tiles read, so its geometry covers the area you read,
  not necessarily all of it. `'clip'` skips the joining (non-overlapping pieces, faster), and
  `'raw'` returns exactly what the tiles store. `read_mvt` and `mvt_decode` decode one tile at
  a time and always return it as stored.
- **Low zooms are simplified and filtered.** Generators such as tippecanoe and planetiler
  drop small features and vertices at low zoom.
- **Attributes are exact.** Strings, numbers and booleans decode to what the encoder wrote.
- **A bbox selects tiles, not features.** `west/south/east/north` decide which tiles are
  read. Filter the features with `ST_Intersects(geometry, ST_MakeEnvelope(…))`.
- **Old (MVT v1) tilesets** have no ring-winding rule. This worker groups their polygon rings
  by nesting instead, but encoders of that era often wrote self-intersecting polygons:
  `ST_MakeValid` repairs them.

### How the decoding was checked

`make conformance` repeats these checks:

- **Mapbox's [mvt-fixtures](https://github.com/mapbox/mvt-fixtures) suite.** All 74 spec
  edge cases decode with no unexpected error. Every valid fixture matches
  [mapbox-vector-tile](https://github.com/tilezen/mapbox-vector-tile), an independent decoder,
  feature by feature: id, every property value and its type, and the exact geometry. The
  suite's 211 real-world Mapbox Streets tiles (385,919 features) match the same way.
- **Real tilesets from different encoders**, all identical feature by feature to
  mapbox-vector-tile:
  - Protomaps/planetiler Florence basemap, z0–15: about 105k features.
  - Overture POIs: 112k features.
  - Census ZIP code areas: 34.5k polygons.
  - A Mapbox Streets v8 tile.
- **GDAL's MVT reader.** It agrees on the Florence roads and landuse layers: feature count,
  vertex count, total length and total area.
- **Broken input.** A mutation fuzz of 30,000 corrupted tiles (plus corrupted gzip) raises
  only clear decode errors, never a crash or a hang, and each tile takes under 10 ms. Errors
  name the tile (`tile 14/8801/5371 is not a valid vector tile: ...`).

For MVT v1 tiles, where the spec defines no ring winding, the worker groups rings by nesting.
On the TileMill-era Natural Earth `countries.mbtiles` this gives better polygons than GDAL:
GDAL makes the tiles' zero-area junk rings into outer rings and real islands into their holes.

### Limits and safety

- A tile that decompresses past 64 MiB is refused, so a small gzip bomb can't exhaust memory.
  Real tiles are far smaller; Mapbox caps tiles at 500 KiB compressed.
- One HTTP response is capped at 128 MiB. Reading a PMTiles URL needs a server that supports
  `Range` requests (S3, R2, GCS, CloudFront and nginx all do). A server that ignores `Range`
  gets a clear error instead of a full-archive download.
- `source` can be any local path or URL the worker process can reach, so the worker reads
  with that process's permissions. Run it locally (DuckDB launches it on your machine) or in
  a sandbox. Don't expose `--http` to untrusted users without restricting network and file
  access.

### Performance

Decoding is pure Python and takes about 25 ms for a dense 2,759-feature Mapbox Streets tile.
Remote PMTiles reads are network-bound. Neighbouring tiles are adjacent in the archive, so
they're fetched in one range request over a kept-alive connection.

Reading 6,776 Overture POI tiles (about 590k features) over the internet takes about 45 s
in-process and 70 s through DuckDB, with about 200 MiB of worker memory. The default `merge`
mode costs about the same as `raw` on points. On the full US ZIP code archive at z7
(320 tiles of complex polygons), `merge` takes 12 s against 5 s for `raw`. Each tile is decoded once
while the read's decoded pieces fit in 256 MiB (lines over 9,538 tiles took about 50 MiB).

Writing is single-process Python with the geometry work done on whole arrays per zoom. Through
DuckDB, 1M points take 70 s to z10 (146 MB archive) and 85 s to z12 (198 MB); 104k mixed
points, lines and polygons take 24 s to z10. That suits city- and country-scale data; for
planet-scale basemaps use tippecanoe or planetiler.

## Try it on public tilesets

All of these read over HTTP. PMTiles fetches only the header, the directory pages it needs
and the tiles inside your bbox, so a multi-gigabyte archive is fine.

| Tileset | Source | Size | Notes |
|---|---|---|---|
| Protomaps basemap, Florence (OSM, ODbL) | `https://raw.githubusercontent.com/protomaps/PMTiles/main/spec/v3/protomaps(vector)ODbL_firenze.pmtiles` | 6.6 MB | 13 layers (roads, buildings, pois, …), z0–15 |
| US ZIP code areas (Census ZCTA 2018) | `https://r2-public.protomaps.com/protomaps-sample-datasets/cb_2018_us_zcta510_500k.pmtiles` | 21 MB | polygons, attribute `ZCTA5CE10` |
| Overture Maps places | `https://r2-public.protomaps.com/protomaps-sample-datasets/overture-pois.pmtiles` | 4.6 GB | worldwide POIs, z0–14; always pass a bbox |
| Mapbox Streets v6 sample tile | `https://raw.githubusercontent.com/mapbox/vector-tile-js/main/test/fixtures/14-8801-5371.vector.pbf` | 180 KB | one tile, `14/8801/5371`, 20 layers |
| Natural Earth countries (MBTiles, MVT v1) | `https://github.com/klokantech/vector-tiles-sample/releases/download/v1.0/countries.mbtiles` | 9 MB | download first: MBTiles must be local |

```sql
LOAD spatial;

-- What is in the Florence basemap?
SELECT layer, count(field) AS attributes
FROM mvt.main.tileset_layers('https://raw.githubusercontent.com/protomaps/PMTiles/main/spec/v3/protomaps(vector)ODbL_firenze.pmtiles')
GROUP BY layer ORDER BY layer;

-- Road kinds at z14, from the JSON properties.
SELECT properties->>'pmap:kind' AS kind, count(*) AS n
FROM mvt.main.read_tiles('https://raw.githubusercontent.com/protomaps/PMTiles/main/spec/v3/protomaps(vector)ODbL_firenze.pmtiles', zoom := 14)
WHERE layer = 'roads' GROUP BY ALL ORDER BY n DESC;

-- 10,758 Overture places in central Florence, from a 4.6 GB archive, in about 2 s.
SELECT properties->>'category_main' AS category, count(*) AS n
FROM mvt.main.read_tiles('https://r2-public.protomaps.com/protomaps-sample-datasets/overture-pois.pmtiles',
       zoom := 14, west := 11.24, south := 43.76, east := 11.27, north := 43.78)
GROUP BY ALL ORDER BY n DESC LIMIT 10;

-- ZIP code areas touching a Manhattan bbox, with typed attribute columns.
SELECT DISTINCT ZCTA5CE10 AS zip
FROM mvt.main.read_tiles('https://r2-public.protomaps.com/protomaps-sample-datasets/cb_2018_us_zcta510_500k.pmtiles',
       zoom := 7, layer := 'zcta', west := -74.02, south := 40.70, east := -73.93, north := 40.80)
WHERE ST_Intersects(geometry, ST_MakeEnvelope(-74.02, 40.70, -73.93, 40.80))
ORDER BY zip;

-- One tile from a URL.
SELECT layer, count(*) AS features
FROM mvt.main.read_mvt('https://raw.githubusercontent.com/mapbox/vector-tile-js/main/test/fixtures/14-8801-5371.vector.pbf', 14, 8801, 5371)
GROUP BY layer ORDER BY features DESC;

-- MBTiles rows through DuckDB's sqlite extension (MBTiles rows are TMS, so flip them).
SELECT layer, count(*) FROM mvt.main.mvt_decode((
  SELECT tile_data AS tile, zoom_level AS z, tile_column AS x, (1 << zoom_level) - 1 - tile_row AS y
  FROM sqlite_scan('countries.mbtiles', 'tiles') WHERE zoom_level = 3))
GROUP BY layer;

-- Save a layer as GeoParquet.
COPY (SELECT * FROM mvt.main.read_tiles('countries.mbtiles', zoom := 4, layer := 'country'))
TO 'countries.parquet';
```

Other options: `coordinates := 'web_mercator'` (EPSG:3857 metres, typed
`GEOMETRY('EPSG:3857')`) or `'tile'` (raw tile pixels, untyped `GEOMETRY`); `max_tiles := n` raises the 10,000-tile guard. URL templates (`{z}/{x}/{y}`) need
a bbox and an explicit `zoom`. Use them only with tile servers whose usage policy allows
bulk reads.

Not supported yet: PMTiles with Brotli or Zstandard internal compression (gzip and none
work; nearly all archives use gzip), raster tiles, and MBTiles over HTTP (download it, or
convert it with `pmtiles convert`).

## Quick start

**Prerequisites:** Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Query-farm/vgi-mvt && cd vgi-mvt
make build
```

Attach the worker from **DuckDB 2.0**. `LOCATION` is the command DuckDB runs, so give it
an absolute path:

```sql
SET allow_extension_repositories = 'allowed';
CREATE EXTENSION REPOSITORY IF NOT EXISTS query_farm WITH PREFIX 'https://duckdb-extensions.query.farm';
INSTALL vgi FROM query_farm;
LOAD vgi FROM query_farm;
ATTACH 'mvt' (TYPE vgi, LOCATION '/abs/path/to/vgi-mvt/.venv/bin/vgi-mvt-worker');

SELECT layer, count(*) FROM mvt.main.read_tiles('/abs/path/to/vgi-mvt/test/fixtures/sample.pmtiles') GROUP BY layer;
```

Or attach it from **Haybarn 2.0** (`uvx haybarn-cli`): `INSTALL vgi FROM community; LOAD vgi;`, then use the same `ATTACH`.

Once a release is tagged, you can run it without cloning:
`LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-mvt@v0.3.0 vgi-mvt-worker'`.

## Development

```text
src/mvt_worker/
  mvt.py          # MVT protobuf decoder, geometry commands, ring classification, WKB, projection
  sources.py      # PMTiles (local/HTTP range), MBTiles, XYZ templates; tile enumeration + fetching
  rows.py         # output schema (GEOMETRY + JSON + typed attribute columns) and row building
  read_tiles.py   # read_tiles, read_mvt
  decode.py       # mvt_decode (table in-out)
  layers.py       # tileset_layers, tileset_metadata
  catalog.py      # ATTACH a tileset: one table per layer, scans redirected to read_tiles
  pushdown.py     # pushed ST_Intersects bounding box -> tile selection
  edges.py        # tile_edges: owned points, clip to tile, join pieces across tiles (shapely)
  write_tiles.py  # COPY ... TO (FORMAT 'mvt.pmtiles'): column roles, attributes, zoom loop
  tiler.py        # project, simplify, quadtree-clip and snap geometries per zoom (vectorised)
  encode.py       # MVT encoder: layers/keys/values, vectorised geometry commands
  archive.py      # PMTiles v3 archive writer (clustered, dedup, atomic rename)
  meta.py         # catalog metadata
  worker.py       # catalog assembly / entry point
tests/            # pytest: decoder edge cases (tests/mvt_encode.py builds tiles) + worker calls
test/sql/         # sqllogictest suite, run through DuckDB over stdio, launch and http
test/fixtures/    # sample.pmtiles, sample.mbtiles, 0-0-0.mvt, xyz/ (generated from src/*.geojson)
test/tile_server.py  # range-capable static server, for the offline HTTP tests
scripts/make_fixtures.sh  # regenerate the fixtures with GDAL
scripts/conformance.py    # decoder + writer conformance, fuzz (make conformance)
```

```bash
make test-unit      # pytest
make test-sql       # SQL suite over stdio (TRANSPORT=launch|http for the others)
make lint-explain   # metadata quality (vgi-lint)
make check          # everything CI runs
make conformance    # decoder vs mvt-fixtures and mapbox-vector-tile, plus fuzzing (downloads; run before a release)
```

The worker depends on `vgi-python` (with its `haybarn` extra: an embedded engine, about 43 MB,
that the SDK uses to apply the filters DuckDB pushes into `read_tiles`), `pmtiles` (for the
PMTiles header codec and Hilbert tile IDs) and `shapely` (GEOS clipping and union for
`tile_edges`). The MVT decoder is pure Python.

`stdout` carries the Arrow protocol, so log to stderr (`logging`; `--debug` turns it up).
The worker also serves HTTP (`vgi-mvt-worker --http --port 8000`, or the `Dockerfile`).

## Authorship & License

Written by [Query.Farm](https://query.farm). Released under the BSD Zero Clause License
(see [LICENSE](LICENSE)).

Copyright 2026 Query Farm LLC - https://query.farm
