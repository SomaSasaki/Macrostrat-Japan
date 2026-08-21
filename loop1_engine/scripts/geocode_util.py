# -*- coding: utf-8 -*-
"""Cached, map-bounded geocoding for Column geography evidence.

The geocoder never decides a representative Column coordinate.  It only
resolves named places extracted from verified PDF quotations.  The caller
uses these anchors to score candidate points that remain inside assigned GSJ
Shape polygons.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCHEMA_VERSION = "geocode-cache/1.1"
USER_AGENT = {"User-Agent": "MacroStrat-GSJ-Pipeline/2.0"}
FetchJson = Callable[[str, int], Any]
BBox = tuple[float, float, float, float]


def _default_fetch_json(url: str, timeout: int) -> Any:
    request = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _expanded_bbox(bbox: BBox, ratio: float = 0.20) -> BBox:
    xmin, ymin, xmax, ymax = bbox
    dx = max(xmax - xmin, 0.01) * ratio
    dy = max(ymax - ymin, 0.01) * ratio
    return xmin - dx, ymin - dy, xmax + dx, ymax + dy


def _inside(lng: float, lat: float, bbox: BBox) -> bool:
    return bbox[0] <= lng <= bbox[2] and bbox[1] <= lat <= bbox[3]


def _cache_key(name: str, context: str, bbox: BBox | None) -> str:
    payload = json.dumps(
        {"name": name, "context": context, "bbox": bbox},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _read_cache(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "entries": {}}
    if document.get("schema_version") != SCHEMA_VERSION or not isinstance(document.get("entries"), dict):
        return {"schema_version": SCHEMA_VERSION, "entries": {}}
    return document


def _gsi_rows(payload: Any, query: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in payload if isinstance(payload, list) else []:
        geometry = item.get("geometry") if isinstance(item, Mapping) else None
        coordinates = geometry.get("coordinates") if isinstance(geometry, Mapping) else None
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        try:
            lng, lat = float(coordinates[0]), float(coordinates[1])
        except (TypeError, ValueError):
            continue
        properties = item.get("properties") if isinstance(item.get("properties"), Mapping) else {}
        rows.append({
            "provider": "GSI",
            "query": query,
            "display_name": str(properties.get("title") or properties.get("addressCode") or query),
            "lat": lat,
            "lng": lng,
        })
    return rows


def _osm_rows(payload: Any, query: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, Mapping):
            continue
        try:
            lat, lng = float(item.get("lat")), float(item.get("lon"))
        except (TypeError, ValueError):
            continue
        rows.append({
            "provider": "OpenStreetMap Nominatim",
            "query": query,
            "display_name": str(item.get("display_name") or query),
            "lat": lat,
            "lng": lng,
        })
    return rows


def geocode_candidates(
    name: str,
    *,
    context: str = "",
    bbox: BBox | None = None,
    timeout: int = 8,
    fetch_json: FetchJson | None = None,
) -> list[dict[str, Any]]:
    """Return ranked candidates within the map sheet or its small margin."""

    place = str(name or "").strip()
    if not place:
        return []
    fetch = fetch_json or _default_fetch_json
    contextual = " ".join(part for part in (place, context.strip()) if part)
    queries = [contextual]
    if contextual != place:
        queries.append(place)
    candidates: list[dict[str, Any]] = []
    for query in queries:
        encoded = urllib.parse.quote(query)
        requests = (
            (
                f"https://msearch.gsi.go.jp/address-search/AddressSearch?q={encoded}",
                _gsi_rows,
            ),
            (
                "https://nominatim.openstreetmap.org/search?"
                f"q={encoded}&format=json&limit=8&countrycodes=jp",
                _osm_rows,
            ),
        )
        for url, parser in requests:
            try:
                candidates.extend(parser(fetch(url, timeout), query))
            except Exception:
                continue

    expanded = _expanded_bbox(bbox) if bbox is not None else None
    unique: dict[tuple[int, int], dict[str, Any]] = {}
    for candidate in candidates:
        lat, lng = float(candidate["lat"]), float(candidate["lng"])
        if not math.isfinite(lat) or not math.isfinite(lng):
            continue
        inside_map = bool(bbox and _inside(lng, lat, bbox))
        inside_margin = bool(expanded and _inside(lng, lat, expanded))
        if bbox is not None and not inside_margin:
            continue
        query_score = 2 if candidate["query"] == contextual else 0
        provider_score = 2 if candidate["provider"] == "GSI" else 1
        score = (10 if inside_map else 5 if inside_margin else 0) + query_score + provider_score
        candidate = {
            **candidate,
            "inside_map": inside_map,
            "inside_margin": inside_margin,
            "score": score,
        }
        key = (round(lat * 1_000_000), round(lng * 1_000_000))
        previous = unique.get(key)
        if previous is None or candidate["score"] > previous["score"]:
            unique[key] = candidate
    return sorted(
        unique.values(),
        key=lambda row: (-float(row["score"]), row["provider"], row["display_name"]),
    )


def resolve_place_names(
    place_names: Sequence[str],
    *,
    context: str,
    bbox: BBox | None,
    cache_path: str | os.PathLike[str],
    timeout: int = 8,
    fetch_json: FetchJson | None = None,
) -> list[dict[str, Any]]:
    """Resolve each unique place once and persist the ranked result set."""

    path = Path(cache_path).expanduser().resolve()
    cache = _read_cache(path)
    entries = cache["entries"]
    output: list[dict[str, Any]] = []
    changed = False
    seen: set[str] = set()
    for value in place_names:
        name = str(value or "").strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        key = _cache_key(name, context, bbox)
        entry = entries.get(key)
        if not isinstance(entry, Mapping):
            candidates = geocode_candidates(
                name,
                context=context,
                bbox=bbox,
                timeout=timeout,
                fetch_json=fetch_json,
            )
            entry = {
                "name": name,
                "context": context,
                "bbox": list(bbox) if bbox else None,
                "candidates": candidates,
            }
            entries[key] = entry
            changed = True
        candidates = entry.get("candidates") if isinstance(entry.get("candidates"), list) else []
        output.append({
            "name": name,
            "selected": candidates[0] if candidates else None,
            "candidates": candidates,
            "cache_key": key,
        })
    if changed or not path.is_file():
        _atomic_json(path, cache)
    return output


def geocode_place_name(name: str, timeout: int = 5) -> tuple[float, float] | None:
    """Compatibility wrapper returning the best unbounded result."""

    candidates = geocode_candidates(name, timeout=timeout)
    if not candidates:
        return None
    return float(candidates[0]["lat"]), float(candidates[0]["lng"])


def batch_geocode(place_names: Sequence[str]) -> list[dict[str, Any]]:
    """Compatibility wrapper for callers that do not need persistent cache."""

    results: list[dict[str, Any]] = []
    for name in place_names:
        coordinates = geocode_place_name(str(name))
        if coordinates is not None:
            results.append({"name": str(name), "lat": coordinates[0], "lng": coordinates[1]})
    return results


def compute_centroid(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Return the arithmetic mean of latitude/longitude pairs."""

    if not points:
        return None
    return (
        round(sum(point[0] for point in points) / len(points), 6),
        round(sum(point[1] for point in points) / len(points), 6),
    )
