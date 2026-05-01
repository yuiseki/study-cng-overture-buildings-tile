"""DuckDB Spatial layer: read Overture Buildings GeoParquet from S3.

We pre-build a STAC-driven spatial index of all Overture building Parquet
files at startup (`STACSpatialIndex.build()`). At request time we look up
the small set of files whose file-level bbox intersects the tile bbox and
hand that explicit list to `read_parquet`, bypassing DuckDB's wildcard
expansion + per-file metadata fetch (the source of the ~1-2 minute cold
start on the 512-file `*` query).
"""

from __future__ import annotations

import logging
import threading

import duckdb

from buildings_cng.stac_index import STACSpatialIndex

logger = logging.getLogger("buildings-cng")

OVERTURE_RELEASE = "2026-04-15.0"
OVERTURE_THEME = "buildings"
OVERTURE_TYPE = "building"

_conn: duckdb.DuckDBPyConnection | None = None
_conn_lock = threading.Lock()

# DuckDB + spatial extension is not safe under concurrent queries from
# uvicorn's thread pool (we hit SIGSEGV inside _duckdb.cpython-*.so when
# the browser fires several tile requests in parallel). Serialise all
# query() calls with this lock until we move to per-request cursors or
# replace the spatial extension.
_query_lock = threading.Lock()

_stac_index: STACSpatialIndex | None = None
_stac_lock = threading.Lock()


def get_connection() -> duckdb.DuckDBPyConnection:
    """Lazy-init a process-wide DuckDB connection with spatial + httpfs loaded."""
    global _conn
    if _conn is not None:
        return _conn
    with _conn_lock:
        if _conn is None:
            con = duckdb.connect()
            con.execute("INSTALL spatial; LOAD spatial;")
            con.execute("INSTALL httpfs; LOAD httpfs;")
            con.execute("SET s3_region='us-west-2';")
            _conn = con
    return _conn


def get_stac_index() -> STACSpatialIndex:
    """Return the singleton STAC index (caller is responsible for `build()`)."""
    global _stac_index
    if _stac_index is not None:
        return _stac_index
    with _stac_lock:
        if _stac_index is None:
            _stac_index = STACSpatialIndex(
                release=OVERTURE_RELEASE,
                theme=OVERTURE_THEME,
                type_=OVERTURE_TYPE,
            )
    return _stac_index


def query_buildings_in_bbox(
    bbox: tuple[float, float, float, float],
    min_height: float = 0.0,
    limit: int = 5000,
) -> list[dict]:
    """Return building rows whose bbox intersects `bbox`.

    `bbox` is `(west, south, east, north)` in EPSG:4326.

    Performance:
    - The STAC index pre-resolves the small set of Parquet files whose
      file-level bbox intersects the tile (typically 1-3 of the 512 total).
    - DuckDB then does row-group pruning inside those files using Overture's
      `bbox` column statistics, so the actual bytes pulled from S3 are
      proportional to the buildings in the tile, not to file size.
    """
    files = get_stac_index().files_intersecting(bbox)
    if not files:
        return []
    west, south, east, north = bbox
    con = get_connection()
    file_list_sql = "[" + ", ".join(f"'{f}'" for f in files) + "]"
    sql = f"""
      SELECT
        id,
        ST_AsWKB(geometry)        AS geom_wkb,
        height,
        num_floors,
        class
      FROM read_parquet({file_list_sql})
      WHERE bbox.xmin <= ?
        AND bbox.xmax >= ?
        AND bbox.ymin <= ?
        AND bbox.ymax >= ?
        AND (? <= 0 OR (height IS NOT NULL AND height >= ?))
      LIMIT ?
    """
    with _query_lock:
        rows = con.execute(
            sql,
            [east, west, north, south, min_height, min_height, limit],
        ).fetchall()
    cols = ("id", "geom_wkb", "height", "num_floors", "class")
    logger.info(
        "duckdb query: files=%d rows=%d bbox=%s min_height=%.1f",
        len(files), len(rows), bbox, min_height,
    )
    return [dict(zip(cols, r, strict=True)) for r in rows]
