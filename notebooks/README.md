# Mapbox Vector Tile Reader examples

Two executed SQL stories use the public **v0.3.0** worker and unchanged repository test fixtures:

- [Tile fragments are not source features](01_tile_fragments_and_feature_counts.ipynb) compares raw, clipped and merged rows, metadata fields and directly attached layer tables. Change `ZOOM` from 3 to 2.
- [Keep identities through a retile, then measure coordinate changes](02_retiled_identity_and_coordinates.ipynb) compares file/BLOB decoding, assigns controlled IDs, writes a local archive through COPY and measures its readback. Change `EXTENT` from 512 to 256.

The first story uses two invented roads and two invented areas. The second uses four city point records with fixture/historical attribute values and two invented polygons. Neither is a current map or observed infrastructure dataset. [Provenance](fixtures/provenance.json) records exact published fixture bytes. Source GeoJSON in the first example supplies only expected identity keys; all plotted geometry and measurements come from worker results and visible Spatial SQL.

## Run from this folder or a single notebook

Install [uv](https://docs.astral.sh/uv/) and Git, then run:

```bash
uv sync --locked
uv run --locked vgi-notebooks 01_tile_fragments_and_feature_counts.ipynb
uv run --locked vgi-notebooks 02_retiled_identity_and_coordinates.ipynb
```

The local environment pins shared tools **v0.3.31** with the `[runner]` extra. Each notebook's Colab link opens a single file; its conditional setup installs only the shared base package into that kernel. `prepare("mvt")` retrieves the seven companion files from immutable commit `a3b37be2154a02e553d1c2b6b19b736d5c63afd5`, verifies their SHA-256 hashes and makes the fixtures available. No parent repository, worker source tree or root `.venv` discovery is used.

Visible SQL launches the worker in its separate Python 3.13 environment:

```sql
INSTALL vgi FROM community;
LOAD vgi;
INSTALL spatial;
LOAD spatial;
ATTACH 'mvt' (TYPE vgi, LOCATION 'uvx --python 3.13 --from git+https://github.com/Query-farm/vgi-mvt@v0.3.0 vgi-mvt-worker');
```

Release v0.3.0 resolves to `3f013b731056f751a7168d0f833dfdc7607b0c63`. uv installs the released Python package with its declared dependencies and caches it; no transitive override stack is needed. The notebook uses published Haybarn 2.0.0rc11. Initial retrieval needs GitHub, Python package indexes and VGI/Spatial extension access. No API credentials, live tile server, GDAL installation or engine build is needed. Shared connection setup uses a fresh empty SQL-secret directory and disables persistent secrets before ATTACH; cleanup checks the inventory and closes the session.

For an editor, select a kernel registered from this environment:

```bash
uv run --locked python -m ipykernel install --user --name mvt-examples \
  --display-name "Python (vgi-mvt notebooks)"
```

Restart and Run All after editing the visible parameter cell. Setup and chart implementation are folded; the ATTACH, data retrieval, COPY and analytical SQL remain visible. Figures use native SVG plus matching GitHub image links. Analytical geometry stays in SQL; the companion only renders returned GeoJSON and numeric results.

## Separate experiments

```bash
uv run --locked vgi-notebooks 01_tile_fragments_and_feature_counts.ipynb \
  --parameters '{"ZOOM":2}' --output-dir artifacts/zoom-2
uv run --locked vgi-notebooks 02_retiled_identity_and_coordinates.ipynb \
  --parameters '{"EXTENT":256}' --output-dir artifacts/extent-256
```

Overrides require a separate output directory; the runner rejects overwriting the saved default. Experiments receive their own notebook/companions and branding. Default parameter values are 3 and 512 respectively.

The first experiment changes fragment counts and projected measures while all four fixture names remain covered. The archive has no encoded IDs: unique `(layer, name)` keys are a deliberate property of this fixture, not a general identity rule. Raw contains buffered copies; clip removes buffers; merge joins compatible pieces and can snap nearby line endpoints. Clipped and merged line lengths need not be identical. An output row's tile address identifies one contributing tile, not its full footprint.

The second experiment keeps zoom 2 and changes the integer coordinate grid. File and BLOB readers see identical tile bytes and XYZ address 0/0/0. The six IDs are explicitly assigned notebook identities, not recovered real-world IDs. COPY writes only `artifacts/retiled.pmtiles`, a generated local file recreated by each run; the immutable inputs remain untouched. Six IDs and complete properties survive both experiments. Point displacement and polygon area are measured separately; the known excluded hole centre is a local control, not a general topology proof. The baseline is already quantized decoded geometry, not original observations.

All distances and areas are planar EPSG:3857 comparisons, **not ground-distance or location-accuracy estimates**. The first map uses equal scales without a basemap. The round-trip figure uses fixed separate axes for point displacement and polygon area change. Zero buffer/simplification focuses the experiment on quantization and retile assembly; a preserved feature count never proves exact geometry.

## Catalog scope and evidence

The actual unconfigured release catalog has **six fixed named surfaces**: `mvt_decode`, `read_mvt`, `read_tiles`, `tileset_layers`, `tileset_metadata` and `write_pmtiles`. The writer is invoked through `COPY ... FORMAT 'mvt.pmtiles'`; its 17 option rows describe one format, not 17 functions. The notebook checks real readback rather than relying on JupySQL to expose the COPY status count.

Selecting `edges.pmtiles` exposes two additional typed tables, `areas` and `roads`. The first story actually reads both and reconciles them with the function result. [coverage.json](coverage.json) therefore records **8/8 for the explicitly selected fixture configuration**, including **6/6 fixed surfaces**. Another archive can add different layer names and columns; no universal static denominator is claimed for arbitrary future data.

[Complete runtime catalogs](coverage/catalog-released.json), full arguments, COPY registry, table/output DESCRIBE exports and [source review](coverage/source-compatibility.json) record both release and current public revision `81282ed09ae9793b1ca91860e1f4cb32d157aeb6`. Their signatures match; current source adds executable metadata examples/docs only. Notebook runs use the release, so current-interface coverage is compatible evidence rather than a claim that these notebooks were rerun against main.

This notebook-only update reuses [successful source CI](https://github.com/Query-farm/vgi-mvt/actions/runs/43573972996) for the unchanged worker. Validation runs each default and its detached one-parameter experiment, a fresh notebook-only bootstrap in a compatible Python 3.12 kernel with eight libraries preloaded, six overwrite/parameter guards, output/figure review and coverage validation. Hosted Colab itself is not manually operated. See the [shared standard](STANDARD.md).
