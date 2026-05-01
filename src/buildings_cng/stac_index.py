"""STAC-driven spatial index over Overture Buildings GeoParquet files.

Overture publishes a static STAC catalog at https://stac.overturemaps.org/.
Each item's JSON carries the file-level `bbox` plus the s3://… asset href,
so we can build an in-memory `bbox -> s3 href` index at startup and only
hand DuckDB the files whose bboxes intersect the requested tile bbox.

This bypasses DuckDB's wildcard expansion + per-file metadata fetch (which
otherwise pays a 1-2 minute cold-start penalty on the 512-file Overture
buildings collection).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

logger = logging.getLogger("buildings-cng")

STAC_ROOT = "https://stac.overturemaps.org"


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def _resolve(base: str, href: str) -> str:
    """Resolve a STAC `href` (relative or absolute) against `base`."""
    if href.startswith(("http://", "https://")):
        return href
    return urljoin(base + "/", href)


class STACSpatialIndex:
    """In-memory spatial index of Overture Parquet files for one (release, theme, type).

    Holds a flat list of `(bbox, s3_href)` tuples. `files_intersecting(bbox)`
    is O(n) over the items, which is fine for n=512 and well under the
    DuckDB query cost.
    """

    def __init__(
        self,
        release: str,
        theme: str,
        type_: str,
        max_workers: int = 32,
    ) -> None:
        self.release = release
        self.theme = theme
        self.type_ = type_
        self.max_workers = max_workers
        self._items: list[tuple[tuple[float, float, float, float], str]] = []
        self._build_lock = threading.Lock()
        self._built = False

    @property
    def collection_url(self) -> str:
        return f"{STAC_ROOT}/{self.release}/{self.theme}/{self.type_}/collection.json"

    def build(self) -> None:
        """Fetch the collection + every item.json in parallel; populate `_items`."""
        with self._build_lock:
            if self._built:
                return
            t0 = time.time()
            coll = _fetch_json(self.collection_url)
            base = self.collection_url.rsplit("/", 1)[0]
            item_links = [l for l in coll.get("links", []) if l.get("rel") == "item"]
            logger.info(
                "STAC index: fetching %d items for %s/%s/%s",
                len(item_links), self.release, self.theme, self.type_,
            )

            def _fetch_one(link: dict) -> tuple[tuple[float, float, float, float], str] | None:
                url = _resolve(base, link["href"])
                try:
                    item = _fetch_json(url)
                except Exception:
                    logger.warning("STAC item fetch failed: %s", url)
                    return None
                bbox = item.get("bbox")
                if not bbox or len(bbox) != 4:
                    return None
                # Prefer the s3:// alternate href for DuckDB httpfs.
                aws = item.get("assets", {}).get("aws", {})
                s3_href = aws.get("alternate", {}).get("s3", {}).get("href") or aws.get("href")
                if not s3_href:
                    return None
                return (tuple(bbox), s3_href)

            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                rows = list(pool.map(_fetch_one, item_links))

            self._items = [r for r in rows if r is not None]
            self._built = True
            logger.info(
                "STAC index ready: %d files indexed in %.1fs",
                len(self._items), time.time() - t0,
            )

    def files_intersecting(
        self,
        query_bbox: tuple[float, float, float, float],
    ) -> list[str]:
        """Return s3 hrefs of files whose bbox intersects `query_bbox`."""
        if not self._built:
            self.build()
        west, south, east, north = query_bbox
        out = []
        for (xmin, ymin, xmax, ymax), s3 in self._items:
            # Standard 2D AABB intersection (inclusive on both ends).
            if xmin <= east and xmax >= west and ymin <= north and ymax >= south:
                out.append(s3)
        return out

    def stats(self) -> dict:
        return {
            "release": self.release,
            "theme": self.theme,
            "type": self.type_,
            "indexed_files": len(self._items),
            "ready": self._built,
        }
