"""buildings-cng: dynamic MVT server for Overture Buildings.

Endpoints:
    GET /health               -> {"ok": true}
    GET /tiles/{z}/{x}/{y}.mvt
        Query parameters:
            filter_by_height   minimum building height, e.g. "10m" (optional)
            limit              max features per tile (default 5000)

Run: `uv run python -m buildings_cng.server`
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import mercantile
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware

from buildings_cng.duckdb_query import (
    OVERTURE_RELEASE,
    get_connection,
    get_stac_index,
    query_buildings_in_bbox,
)
from buildings_cng.filters import parse_height
from buildings_cng.mvt import encode_buildings_mvt

logger = logging.getLogger("buildings-cng")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MVT_MEDIA_TYPE = "application/vnd.mapbox-vector-tile"
ZOOM_MIN = 0
ZOOM_MAX = 22

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm up DuckDB extensions and the STAC spatial index before the first
    # request lands. With this, cold start is bounded by the STAC fetch (5-10s)
    # instead of DuckDB's 1-2 minute wildcard expansion at request time.
    try:
        get_connection()
        get_stac_index().build()
    except Exception:
        logger.exception("startup warmup failed")
    yield


app = FastAPI(
    title="buildings-cng",
    version="0.1.0",
    description=(
        "On-the-fly dynamic vector tile server for Overture Buildings "
        f"(release {OVERTURE_RELEASE})."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {
        "ok": True,
        "release": OVERTURE_RELEASE,
        "stac": get_stac_index().stats(),
    }


@app.get("/tiles/{z}/{x}/{y}.mvt")
def tile(
    z: int = PathParam(..., ge=ZOOM_MIN, le=ZOOM_MAX),
    x: int = PathParam(..., ge=0),
    y: int = PathParam(..., ge=0),
    filter_by_height: str | None = Query(
        None,
        description="Minimum building height (e.g. '10m'). Omit to disable.",
    ),
    limit: int = Query(5000, ge=1, le=50000),
):
    max_xy = (1 << z) - 1
    if x > max_xy or y > max_xy:
        raise HTTPException(400, f"x/y out of range for zoom {z}")

    min_height = 0.0
    if filter_by_height is not None:
        try:
            min_height = parse_height(filter_by_height)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    bounds = mercantile.bounds(x, y, z)
    bbox = (bounds.west, bounds.south, bounds.east, bounds.north)

    try:
        rows = query_buildings_in_bbox(bbox, min_height=min_height, limit=limit)
    except Exception as exc:
        logger.exception("DuckDB query failed for z=%d x=%d y=%d", z, x, y)
        raise HTTPException(502, f"upstream query failed: {exc}") from exc

    mvt_bytes = encode_buildings_mvt(rows, bbox)
    logger.info(
        "tile z=%d/%d/%d min_height=%.1f rows=%d bytes=%d",
        z, x, y, min_height, len(rows), len(mvt_bytes),
    )
    return Response(content=mvt_bytes, media_type=MVT_MEDIA_TYPE)


if __name__ == "__main__":
    import os

    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    # 8005 / 8003 are commonly grabbed by VS Code / other services on macOS dev
    # boxes, so default to 8006. Override with PORT env var.
    port = int(os.environ.get("PORT", "8006"))
    uvicorn.run(app, host=host, port=port)
