"""Query parameter parsing for buildings-cng.

Filter values are accepted as human-readable strings (e.g. `"10m"`) and
normalised to numeric SI units (meters) for use in DuckDB SQL bindings.
"""

from __future__ import annotations

import re

_HEIGHT_RE = re.compile(
    r"^\s*([0-9]*\.?[0-9]+)\s*(m|meter|meters)?\s*$",
    re.IGNORECASE,
)


def parse_height(value: str) -> float:
    """`"10m"` / `"10"` / `"3.5 meters"` → `10.0` / `10.0` / `3.5`.

    Raises `ValueError` for unparseable input. Other unit strings (km, ft,
    etc.) are intentionally rejected to keep the API contract narrow; we
    can extend later when a real use case appears.
    """
    m = _HEIGHT_RE.match(value)
    if not m:
        raise ValueError(f"invalid height value {value!r} (expected e.g. '10m')")
    return float(m.group(1))
