<p align="center">
  <img src="https://raw.githubusercontent.com/Query-farm/vgi/main/docs/vgi-logo.png" alt="Vector Gateway Interface (VGI)" width="320">
</p>

<p align="center"><em>A Query.Farm VGI worker for DuckDB.</em></p>

# Read Mapbox Vector Tiles, PMTiles & MBTiles as Tables in DuckDB

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

```sql
-- 4.6 GB of Overture places, scoped to central Florence at attach time.
ATTACH 'mvt' AS places (TYPE vgi, LOCATION '/abs/path/to/vgi-mvt-worker',
  tileset 'https://r2-public.protomaps.com/protomaps-sample-datasets/overture-pois.pmtiles',
  bbox '11.24,43.76,11.27,43.78');
SELECT category_main, count(*) AS n FROM places.pois GROUP BY ALL ORDER BY n DESC LIMIT 10;
```

A `WHERE ST_Intersects(geometry, …)` filter on a layer table is applied correctly, but it does
not yet narrow which tiles are read: the worker does not yet accept the bounding box that the
VGI extension can push down. Use the `bbox` option to scope a large archive. A bad path, a `{z}/{x}/{y}`
template (which has no layer list) or a malformed `bbox` fails at `ATTACH` time.

**Or call the reader functions directly:**

```sql
SELECT name, population, capital, geometry
FROM mvt.main.read_tiles('cities.pmtiles', layer := 'cities')
ORDER BY population DESC;
```

| Function | Reads |
|---|---|
| `read_tiles(source, zoom :=, layer :=, west/south/east/north :=, coordinates :=, max_tiles :=)` | a whole tileset at one zoom: **PMTiles** (file or `https://` URL, via range requests), **MBTiles** (file), or a `{z}/{x}/{y}` **tile-server URL template** |
| `read_mvt(source, z, x, y, layer :=, coordinates :=)` | one `.mvt` / `.pbf` tile (file or URL, gzip detected) |
| `mvt_decode((SELECT tile, z, x, y FROM …), layer :=, coordinates :=)` | tile blobs already in DuckDB, for example MBTiles rows read with `sqlite_scan` |
| `tileset_layers(source)` | the layers and typed attributes a tileset declares (`vector_layers`) |
| `tileset_metadata(source)` | format, zoom range, bounds, attribution and the rest of the metadata |

Every feature-reading function returns `layer, z, x, y, feature_id, geometry_type,
geometry, properties`. With `layer := 'name'`, one typed column per attribute of that
layer follows.

## What survives the round trip

Vector tiles are a display format, so the decoding is exact but the data it recovers has
already lost some detail:

- **Coordinates are quantised.** A tile stores integers on a grid, usually 4096×4096 per
  tile. Decoding inverts the projection exactly, so the error is the grid cell: about 1 m at
  z14 and about 0.6 km at z4. Read the highest zoom (the default) for the most faithful
  geometry.
- **Features are clipped per tile.** A road or polygon that crosses tiles comes back once per
  tile, clipped with a small buffer. Merge the pieces with
  `GROUP BY feature_id` and `ST_Union_Agg(geometry)` (when the tileset sets ids), or clip the
  pieces to their tile with `ST_Intersection(geometry, ST_TileEnvelope(z, x, y))` (in Web
  Mercator) before you sum areas or lengths.
- **Low zooms are simplified and filtered.** Generators such as tippecanoe and planetiler
  drop small features and vertices at low zoom.
- **Attributes are exact.** Strings, numbers and booleans decode to what the encoder wrote.
- **A bbox selects tiles, not features.** `west/south/east/north` decide which tiles are
  read. Filter the features with `ST_Intersects(geometry, ST_MakeEnvelope(…))`.
- **Old (MVT v1) tilesets** have no ring-winding rule. This worker groups their polygon rings
  by nesting instead, but encoders of that era often wrote self-intersecting polygons:
  `ST_MakeValid` repairs them.

The decoder was cross-checked against GDAL's MVT reader. On the Protomaps Florence basemap
at z14, the roads (4,092 features) and landuse (1,574) layers match GDAL exactly in
feature count, vertex count, total length and total area.

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
`LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-mvt@v0.1.0 vgi-mvt-worker'`.

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
  meta.py         # catalog metadata
  worker.py       # catalog assembly / entry point
tests/            # pytest: decoder edge cases (tests/mvt_encode.py builds tiles) + worker calls
test/sql/         # sqllogictest suite, run through DuckDB over stdio, launch and http
test/fixtures/    # sample.pmtiles, sample.mbtiles, 0-0-0.mvt, xyz/ (generated from src/*.geojson)
test/tile_server.py  # range-capable static server, for the offline HTTP tests
scripts/make_fixtures.sh  # regenerate the fixtures with GDAL
```

```bash
make test-unit      # pytest
make test-sql       # SQL suite over stdio (TRANSPORT=launch|http for the others)
make lint-explain   # metadata quality (vgi-lint)
make check          # everything CI runs
```

The worker depends on `vgi-python` and `pmtiles` (for the PMTiles header codec and
Hilbert tile IDs) and nothing else. The MVT decoder is pure Python. It decodes the
2,759-feature Mapbox Streets sample tile in about 25 ms.

`stdout` carries the Arrow protocol, so log to stderr (`logging`; `--debug` turns it up).
The worker also serves HTTP (`vgi-mvt-worker --http --port 8000`, or the `Dockerfile`).

## Authorship & License

Written by [Query.Farm](https://query.farm). Released under the BSD Zero Clause License
(see [LICENSE](LICENSE)).

Copyright 2026 Query Farm LLC - https://query.farm
