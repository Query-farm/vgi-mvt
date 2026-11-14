"""Render SQL-returned GeoJSON without computing analytical geometry measures."""

import matplotlib.patches as patches
from matplotlib.path import Path


def draw_geometry(ax, geometry, color):
    """Draw line and polygon GeoJSON from this fixture, preserving all rings."""
    kind = geometry["type"]
    coordinates = geometry.get("coordinates", [])
    if kind == "LineString":
        ax.plot(*zip(*coordinates, strict=True), color=color, linewidth=2, zorder=3)
    elif kind == "MultiLineString":
        for line in coordinates:
            draw_geometry(ax, {"type": "LineString", "coordinates": line}, color)
    elif kind == "Polygon":
        vertices, codes = [], []
        for ring in coordinates:
            vertices.extend(ring)
            codes.extend([Path.MOVETO, *([Path.LINETO] * (len(ring) - 2)), Path.CLOSEPOLY])
        ax.add_patch(
            patches.PathPatch(Path(vertices, codes), facecolor=color, edgecolor=color, alpha=0.35, linewidth=1.2)
        )
    elif kind == "MultiPolygon":
        for polygon in coordinates:
            draw_geometry(ax, {"type": "Polygon", "coordinates": polygon}, color)
    elif kind == "GeometryCollection":
        for part in geometry["geometries"]:
            draw_geometry(ax, part, color)
    else:
        raise ValueError(f"Unsupported fixture geometry for drawing: {kind}")
