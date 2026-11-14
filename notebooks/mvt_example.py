"""Validate tile fixtures and render SQL-returned geometry/accounting."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, MaxNLocator
from vgi_notebooks import finish

from map_support import draw_geometry

SOURCE = (
    "Source: unchanged synthetic GDAL tile-edge fixtures · invented roads and areas · no observed infrastructure\n"
    "VGI MVT v0.3.0 through Haybarn/VGI · tile fragmentation is not a count of source features"
)


def validate_zoom(value):
    """Keep the edge-policy comparison within the two documented fixture zooms."""
    if type(value) is not int or value not in {2, 3}:
        raise ValueError("ZOOM must be integer two or three")


def validate_extent(value):
    """Keep the retile experiment within its two explicit quantization grids."""
    if type(value) is not int or value not in {256, 512}:
        raise ValueError("EXTENT must be integer 256 or 512")


def verify_fixtures():
    """Compare every immutable input with its recorded published bytes."""
    for item in json.loads(Path("fixtures/provenance.json").read_text())["files"]:
        b = (Path("fixtures") / item["file"]).read_bytes()
        if len(b) != item["bytes"] or hashlib.sha256(b).hexdigest() != item["sha256"]:
            raise ValueError("Tile fixture differs from published bytes")


def edge_map(shapes, extents, tile_bounds, ZOOM):
    """Render the actual decoded result without calculating analytical geometry."""
    colors = {"Four tiles": "#4d92b2", "One tile": "#6f9d75", "Diagonal": "#c87338", "Short": "#8964a3"}
    limits = extents.iloc[0]
    padx = (limits.xmax - limits.xmin) * 0.08
    pady = (limits.ymax - limits.ymin) * 0.07
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 6), sharex=True, sharey=True)
    for ax, mode, title in zip(
        axes, ["raw", "clip", "merge"], ["Raw buffered copies", "Clipped tile pieces", "Merged features"], strict=True
    ):
        subset = shapes[shapes["mode"] == mode]
        for row in subset.itertuples():
            draw_geometry(ax, json.loads(row.geojson), colors[row.name])
        for x in set(tile_bounds.west).union(tile_bounds.east):
            ax.axvline(x, color="#9b9f9a", linestyle="--", linewidth=0.7, zorder=0)
        for y in set(tile_bounds.south).union(tile_bounds.north):
            ax.axhline(y, color="#9b9f9a", linestyle="--", linewidth=0.7, zorder=0)
        ax.set_title(f"{title}\n{len(subset)} rows", fontsize=11)
        ax.set_xlim(limits.xmin - padx, limits.xmax + padx)
        ax.set_ylim(limits.ymin - pady, limits.ymax + pady)
        ax.set_aspect("equal")
        ax.grid(False)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value / 1e6:g}"))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value / 1e6:g}"))
        ax.set_xlabel("Easting · million metres")
    axes[0].set_ylabel("Northing · million metres")
    fig.legend(
        [Line2D([0], [0], color=color, lw=3) for color in colors.values()],
        list(colors),
        loc="upper center",
        bbox_to_anchor=(0.5, 0.88),
        ncol=4,
        frameon=False,
        fontsize=10,
    )
    finish(
        fig,
        "One source feature can appear in several tile rows",
        f"Zoom {ZOOM} · EPSG:3857 schematic · dashed tile boundaries · equal scales; no basemap",
        SOURCE,
        name="01-tile-edge-policies",
    )


def edge_accounting(summary):
    """Render the actual decoded result without calculating analytical geometry."""
    modes = ["raw", "clip", "merge"]
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    for layer, offset, color, label in [
        ("areas", -0.18, "#4d92b2", "Area layer"),
        ("roads", 0.18, "#c87338", "Road layer"),
    ]:
        group = summary[summary.layer == layer].set_index("mode").loc[modes]
        x = [i + offset for i in range(3)]
        bars = axes[0].bar(x, group.rows, width=0.32, color=color, label=label)
        axes[0].bar_label(bars, padding=3, fontsize=10)
        bars = axes[1].bar(x, group.percent_of_merged, width=0.32, color=color, label=label)
        axes[1].bar_label(bars, labels=[f"{v:.3f}" for v in group.percent_of_merged], padding=3, fontsize=9)
    axes[0].set_ylabel("Decoded rows")
    axes[0].set_title("Fragment counts by layer")
    axes[0].yaxis.set_major_locator(MaxNLocator(integer=True))
    axes[0].set_ylim(0, float(summary.rows.max()) + 1.5)
    axes[1].set_ylabel("Percent of merged measure")
    axes[1].set_title("Each layer normalized independently")
    axes[1].axhline(100, color="#526b5b", linestyle="--", linewidth=1)
    axes[1].set_ylim(0, float(summary.percent_of_merged.max()) + 22)
    for ax in axes:
        ax.set_xticks(range(3), ["Raw", "Clip", "Merge"])
        ax.set_xlabel("Tile-edge policy")
    axes[0].legend(loc="upper right", frameon=False, fontsize=9)
    finish(
        fig,
        "Buffers and fragmentation affect different totals",
        "Left: row counts · right: area and length each indexed to merge = 100 · projected measures only",
        SOURCE,
        name="02-fragments-and-measures",
    )


def roundtrip_changes(measures, extent):
    """Compare point displacement and polygon area change on separate fixed scales."""
    fig, axes = plt.subplots(1, 2, figsize=(12.5, 6.3))
    points = measures.loc[measures.layer == "cities"].sort_values("fixture_label")
    areas = measures.loc[measures.layer == "regions"].sort_values("fixture_label")
    bars = axes[0].barh(points.fixture_label, points.point_shift_m / 1000, color="#426db3", height=0.58)
    axes[0].bar_label(bars, labels=[f"{v / 1000:.2f}" for v in points.point_shift_m], padding=4)
    axes[0].invert_yaxis()
    axes[0].set(xlim=(0, 30), xlabel="Point displacement in EPSG:3857 (km)", title="Four decoded point features")
    bars = axes[1].barh(areas.fixture_label, areas.area_change_pct, color="#a77038", height=0.5)
    axes[1].bar_label(bars, labels=[f"{v:+.2f}%" for v in areas.area_change_pct], padding=4)
    axes[1].invert_yaxis()
    axes[1].axvline(0, color="#526b5b", linewidth=1)
    axes[1].set(xlim=(-5, 5), xlabel="Projected area change from input (%)", title="Two invented polygon features")
    finish(
        fig,
        "Stable feature identities do not promise exact geometry",
        f"Zoom 2 · extent {extent} units per tile edge · compare with the already-decoded source tile",
        "Source: unchanged GDAL test tile · city labels/attributes are fixture values, not a current place dataset\n"
        "VGI MVT v0.3.0 · planar Web Mercator comparisons; these are not ground-distance or accuracy estimates",
        name="03-retile-coordinate-changes",
    )
