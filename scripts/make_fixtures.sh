#!/usr/bin/env bash
# Regenerate the committed vector tile fixtures from test/fixtures/src/*.geojson with GDAL
# (needs GDAL >= 3.8 for the PMTiles driver). The outputs are committed, so CI needs no GDAL.
set -euo pipefail
cd "$(dirname "$0")/../test/fixtures"
CONF='{"cities":{"target_name":"cities"},"regions":{"target_name":"regions"}}'
rm -rf sample.mbtiles sample.pmtiles tiles0 0-0-0.mvt xyz
# Merge the two GeoJSON layers into one source with one layer each.
VRT="$(mktemp -t fixtures).vrt"
cat > "$VRT" <<VRTEOF
<OGRVRTDataSource>
  <OGRVRTLayer name="cities"><SrcDataSource>$(pwd)/src/cities.geojson</SrcDataSource><SrcLayer>cities</SrcLayer></OGRVRTLayer>
  <OGRVRTLayer name="regions"><SrcDataSource>$(pwd)/src/regions.geojson</SrcDataSource><SrcLayer>regions</SrcLayer></OGRVRTLayer>
</OGRVRTDataSource>
VRTEOF
common=(-dsco MINZOOM=0 -dsco MAXZOOM=3 -dsco NAME=vgi-mvt-sample -dsco "DESCRIPTION=vgi-mvt test fixture" -dsco COMPRESS=YES)
ogr2ogr -f MBTILES sample.mbtiles "$VRT" "${common[@]}" -dsco "CONF=$CONF"
ogr2ogr -f PMTiles sample.pmtiles "$VRT" -dsco MINZOOM=0 -dsco MAXZOOM=3 -dsco NAME=vgi-mvt-sample -dsco "CONF=$CONF"
ogr2ogr -f MVT tiles0 "$VRT" -dsco MINZOOM=0 -dsco MAXZOOM=0 -dsco COMPRESS=NO -dsco TILE_EXTENSION=mvt -dsco "CONF=$CONF"
mv tiles0/0/0/0.mvt 0-0-0.mvt
# An XYZ directory tree (gzip'd tiles), served over HTTP by test/tile_server.py in the SQL tests.
ogr2ogr -f MVT xyz "$VRT" -dsco MINZOOM=0 -dsco MAXZOOM=2 -dsco COMPRESS=YES -dsco TILE_EXTENSION=mvt -dsco "CONF=$CONF"
rm -f xyz/metadata.json
rm -rf tiles0 "$VRT"
# Features that cross tile edges (no feature ids, as GDAL writes them), for tile_edges.
rm -f edges.pmtiles
cat > "$VRT" <<VRTEOF
<OGRVRTDataSource>
  <OGRVRTLayer name="roads"><SrcDataSource>$(pwd)/src/edges_roads.geojson</SrcDataSource><SrcLayer>edges_roads</SrcLayer></OGRVRTLayer>
  <OGRVRTLayer name="areas"><SrcDataSource>$(pwd)/src/edges_areas.geojson</SrcDataSource><SrcLayer>edges_areas</SrcLayer></OGRVRTLayer>
</OGRVRTDataSource>
VRTEOF
ogr2ogr -f PMTiles edges.pmtiles "$VRT" -dsco MINZOOM=0 -dsco MAXZOOM=3 -dsco NAME=vgi-mvt-edges \
  -dsco 'CONF={"roads":{"target_name":"roads"},"areas":{"target_name":"areas"}}'
rm -f "$VRT"
ls -la sample.mbtiles sample.pmtiles edges.pmtiles 0-0-0.mvt
