# -*- coding: utf-8 -*-
"""Offline GSJ GeoTIFF bounds and annotated Review thumbnail support."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class MapThumbnail:
    source_image: Path
    world_file: Path
    output_png: Path
    bbox: tuple[float, float, float, float]
    source_sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_geologic_raster(references: str | os.PathLike[str]) -> tuple[Path, Path] | None:
    root = Path(references).expanduser().resolve()
    for image in sorted(root.rglob("*_F1_geotiff.tif")):
        world = image.with_name(image.name.replace("_geotiff.tif", ".tfw"))
        if image.is_file() and world.is_file():
            return image, world
    return None


def discover_stratigraphic_legend(references: str | os.PathLike[str]) -> Path | None:
    """Return the official GSJ L1 legend/stratigraphic chart when supplied."""

    root = Path(references).expanduser().resolve()
    candidates = sorted(path for path in root.rglob("*_L1.jpg") if path.is_file())
    return candidates[0] if candidates else None


def world_file_bbox(
    world_file: str | os.PathLike[str], width: int, height: int
) -> tuple[float, float, float, float]:
    """Return the outer pixel-edge bbox for an unrotated world file."""

    values = [
        float(line.strip())
        for line in Path(world_file).read_text(encoding="ascii").splitlines()
        if line.strip()
    ]
    if len(values) != 6:
        raise ValueError("World file must contain six numeric lines")
    a, d, b, e, c, f = values
    if abs(b) > 1e-12 or abs(d) > 1e-12:
        raise ValueError("Rotated world files are not supported for Review thumbnails")
    x0, y0 = c - a / 2, f - e / 2
    x1, y1 = x0 + a * width, y0 + e * height
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _point_xy(
    lng: float, lat: float, bbox: tuple[float, float, float, float], size: tuple[int, int]
) -> tuple[int, int]:
    xmin, ymin, xmax, ymax = bbox
    width, height = size
    x = round((lng - xmin) / max(xmax - xmin, 1e-12) * width)
    y = round((ymax - lat) / max(ymax - ymin, 1e-12) * height)
    return x, y


def _open_source_image(source: Path) -> tuple[Any, tuple[int, int]]:
    # GSJ 1:50,000 KMZ files contain the exact F1 map raster in JPEG format (parts/*_F1.jpg).
    # Pillow's C-based TiffImagePlugin on Windows can crash when decoding LZW-compressed GeoTIFFs.
    from PIL import Image
    kmz_candidates = sorted(source.parent.glob("*.kmz"))
    if kmz_candidates:
        try:
            import io, zipfile
            with zipfile.ZipFile(kmz_candidates[0]) as z:
                for name in z.namelist():
                    if name.endswith("_F1.jpg") or name.endswith("_F1.png"):
                        img = Image.open(io.BytesIO(z.read(name)))
                        size = img.size
                        return img.convert("RGB"), size
        except Exception:
            pass
    with Image.open(source) as opened:
        size = opened.size
        return opened.convert("RGB"), size


def render_thumbnail(
    references: str | os.PathLike[str],
    output_png: str | os.PathLike[str],
    *,
    columns: Sequence[Mapping[str, Any]] = (),
    max_size: tuple[int, int] = (1400, 1000),
) -> MapThumbnail | None:
    """Render/copy a local GeoTIFF preview and draw candidate Column pins."""

    discovered = discover_geologic_raster(references)
    if discovered is None:
        return None
    source, world = discovered
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover - deployment guard
        raise RuntimeError("Pillow is required to render the GSJ map thumbnail") from exc

    destination = Path(output_png).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    image, original_size = _open_source_image(source)
    bbox = world_file_bbox(world, *original_size)
    image.thumbnail(max_size)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    colors = ("#D73027", "#1A9850", "#4575B4", "#984EA3", "#FF7F00")
    for index, row in enumerate(columns):
        try:
            lat, lng = float(row.get("lat")), float(row.get("lng"))
        except (TypeError, ValueError):
            continue
        x, y = _point_xy(lng, lat, bbox, image.size)
        color = colors[index % len(colors)]
        radius = 9
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color, outline="white", width=3)
        label = str(row.get("col_name") or row.get("col_id") or "").strip()
        if label:
            draw.rectangle((x + 12, y - 9, x + 18 + len(label) * 7, y + 10), fill="white")
            draw.text((x + 15, y - 7), label, fill="#111111", font=font)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    image.save(temporary, format="PNG", optimize=True)
    os.replace(temporary, destination)
    return MapThumbnail(source, world, destination, bbox, _sha256(source))


def write_map_metadata(
    path: str | os.PathLike[str],
    thumbnail: MapThumbnail,
    columns: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    rows = []
    for column in columns:
        if column.get("lat") in (None, "") or column.get("lng") in (None, ""):
            continue
        rows.append({
            "col_id": column.get("col_id"),
            "col_name": column.get("col_name"),
            "lat": column.get("lat"),
            "lng": column.get("lng"),
            "method": column.get("coordinate_evidence") or "GSJ map-bbox candidate",
            "inside_region": False,
        })
    document = {
        "schema_version": "column-map-thumbnail/1.0",
        "source_image": str(thumbnail.source_image),
        "source_sha256": thumbnail.source_sha256,
        "bbox": list(thumbnail.bbox),
        "png": str(thumbnail.output_png),
        "columns": rows,
    }
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return document


__all__ = [
    "MapThumbnail",
    "discover_geologic_raster",
    "discover_stratigraphic_legend",
    "render_thumbnail",
    "world_file_bbox",
    "write_map_metadata",
]
