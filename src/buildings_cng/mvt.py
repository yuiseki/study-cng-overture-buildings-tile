"""Encode building rows to Mapbox Vector Tile bytes.

`mapbox_vector_tile.encode` quantises geometries to tile-local coordinates
when `quantize_bounds` is given in EPSG:4326. We don't reproject ourselves
since Overture geometries are already EPSG:4326.
"""

from __future__ import annotations

import logging

import mapbox_vector_tile
from shapely.geometry import mapping
from shapely.wkb import loads as wkb_loads

logger = logging.getLogger("buildings-cng")

LAYER_NAME = "buildings"
EXTENT = 4096


def encode_buildings_mvt(
    rows: list[dict],
    tile_bounds: tuple[float, float, float, float],
) -> bytes:
    """Encode `rows` (from `query_buildings_in_bbox`) into MVT bytes.

    Drops rows whose WKB fails to parse (rare, but Overture occasionally
    has degenerate geometries we don't want to 500 on).
    """
    features = []
    for row in rows:
        wkb = row["geom_wkb"]
        if wkb is None:
            continue
        try:
            geom = wkb_loads(bytes(wkb))
        except Exception:
            logger.warning("skip unparseable WKB for id=%s", row.get("id"))
            continue
        features.append(
            {
                "geometry": mapping(geom),
                "properties": {
                    "id": row.get("id"),
                    "height": row.get("height"),
                    "num_floors": row.get("num_floors"),
                    "class": row.get("class"),
                },
            }
        )

    return mapbox_vector_tile.encode(
        [{"name": LAYER_NAME, "features": features}],
        quantize_bounds=tile_bounds,
        extents=EXTENT,
    )
