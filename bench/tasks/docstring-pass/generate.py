"""Fixture generator for the ``docstring-pass`` benchmark task (see
``add-type-hints/generate.py`` for the pattern this follows)."""

from __future__ import annotations

from pathlib import Path

_MODULES = {
    "distance.py": '''\
import math


def haversine(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def midpoint(lat1, lon1, lat2, lon2):
    return (lat1 + lat2) / 2, (lon1 + lon2) / 2
''',
    "bbox.py": '''\
def contains(box, lat, lon):
    return box["min_lat"] <= lat <= box["max_lat"] and box["min_lon"] <= lon <= box["max_lon"]


def expand(box, margin):
    return {
        "min_lat": box["min_lat"] - margin,
        "max_lat": box["max_lat"] + margin,
        "min_lon": box["min_lon"] - margin,
        "max_lon": box["max_lon"] + margin,
    }
''',
}


def build(repo_root: Path) -> None:
    pkg = repo_root / "pkg" / "geo"
    pkg.mkdir(parents=True, exist_ok=True)
    (repo_root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for name, content in _MODULES.items():
        (pkg / name).write_text(content, encoding="utf-8")
