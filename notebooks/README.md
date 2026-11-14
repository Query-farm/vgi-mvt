# Tile fragments and feature counts

[Open the executed notebook](01_tile_fragments_and_feature_counts.ipynb). It reads
one synthetic archive with raw, clipped and merged tile-edge policies, compares
row counts and projected measurements, and changes the zoom level.

## Run from this folder

Install [uv](https://docs.astral.sh/uv/) and Git, then:

```bash
uv sync --frozen
uv run python execute.py
```

The locked notebook environment uses Python 3.13–3.14 and published Haybarn
2.0.0rc11. The worker runs in a separate Python 3.13 environment through a literal
remote Git revision:

```sql
ATTACH 'mvt' (TYPE vgi, LOCATION 'uvx --python 3.13 --with vgi-python==0.51.0 --with vgi-rpc==0.53.1 --with haybarn==1.5.5rc1 --with pmtiles==3.8.1 --with shapely==2.2.0 --from git+https://github.com/Query-farm/vgi-mvt@41aa112e5d72f2ee7481e68c45ba6e4e03174690 vgi-mvt-worker');
```

The release ships Python distributions rather than platform-native worker archives,
so this verified Git launcher is used. It pins the source revision and the source
lock's SDK/RPC, internal Haybarn, PMTiles and Shapely versions. The worker's internal
Haybarn 1.5.5rc1 supports SDK filter handling; it is separate from the notebook's
2.0.0rc11 engine. Neither engine is rebuilt. uv downloads/builds the Python worker
package and caches its environment; no surrounding source checkout is needed.
First use requires access to GitHub, package indexes and the VGI/Spatial extension
repositories. No API credentials, tile server, GDAL command-line installation or
live map data is required. This whole folder runs independently of the repository.

For an editor, register and select the notebook environment:

```bash
uv run python -m ipykernel install --user --name mvt-examples \
  --display-name "Python (vgi-mvt notebooks)"
```

Restart and run all after editing parameters. Setup and plotting code are folded;
parameters, connection, retrieval and analytical SQL remain visible. The runner
uses a temporary kernel with this folder as cwd, writes only after success and
requires a separate output directory for overrides. Executed copies receive their
fixtures, assets, helpers and environment/documentation companions. Generated
validation files are ignored.

## Zoom experiment

```bash
uv run python execute.py --parameters '{"ZOOM":2}' \
  --output-dir validation/zoom-2
```

`ZOOM` accepts 2 or 3, default 3. Compare the three-panel map, per-layer row counts
and measurement table. Larger tiles at the lower zoom change the fragment count;
the four labeled source features remain represented. Exact decoded area and length
can change because vector tiles are quantized and may be simplified by their
encoder. Restore 3 for the saved default.

## Exact synthetic provenance

The files under [fixtures/](fixtures/) are unchanged copies from revision
`41aa112e5d72f2ee7481e68c45ba6e4e03174690`:

- `test/fixtures/edges.pmtiles`: committed archive generated with GDAL by
  `scripts/make_fixtures.sh`.
- `test/fixtures/src/edges_roads.geojson`: two invented lines, Diagonal and Short.
- `test/fixtures/src/edges_areas.geojson`: two invented polygons, Four tiles and One tile.

[provenance.json](fixtures/provenance.json) records exact sizes and SHA-256 hashes,
which the notebook verifies. Coordinates and names are synthetic design inputs,
not observations of real roads or areas. There is no capture timestamp. The source
GeoJSON supplies only the expected feature-name inventory for validation; all
charted geometries and measurements come from the decoded worker results.

The GDAL fixture has no encoded feature IDs. Its names are deliberately unique per
layer, so `(layer, name)` is a valid check for this example, not a general identity
rule. The worker joins matching properties and adjoining tile-edge pieces when IDs
are absent; disconnected pieces can remain separate in other archives. A merged
row's tile address is one contributing tile, not its complete coverage footprint.

## What the comparison measures

Raw returns stored buffered copies. Clip removes buffers but retains tile fragments.
Merge joins compatible pieces. We materialize each mode once before capturing any
DataFrames, so plotting does not repeat worker reads. Spatial SQL computes areas,
lengths and output GeoJSON. Python only draws the returned geometry and formats
results; it does not perform analytical merging or geometric measurements.

Coordinates are explicitly EPSG:3857 Web Mercator. Areas and lengths are planar
projected-coordinate measures, not geodesic ground areas or distances. Map axes use
millions of projected metres and equal scales. The chart indexes area and line
length separately to their respective merged totals; it never adds those units.
Dashed map lines come from SQL tile bounds using the standard Web Mercator world
extent. No external basemap is implied.

Clipped and merged polygon area agree within floating-point tolerance in this
fixture. Line length is reported separately: merge snaps nearby endpoints at tile
edges before joining, so exact clip/merge line-length equality is not assumed. At
zoom 3 a small difference is observable; the SQL tables preserve it. The
[implementation](https://github.com/Query-farm/vgi-mvt/blob/41aa112e5d72f2ee7481e68c45ba6e4e03174690/src/mvt_worker/edges.py)
explains endpoint snapping. No claim of source-geometry precision or real-world
measurement accuracy follows from these checks.

To upgrade, change the literal source/dependency pins and repeat the default,
experiment and source gates. See [STANDARD.md](STANDARD.md) and the pinned
[worker README](https://github.com/Query-farm/vgi-mvt/blob/41aa112e5d72f2ee7481e68c45ba6e4e03174690/README.md).
