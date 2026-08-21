# -*- coding: utf-8 -*-
"""Recover Column memberships from vector box geometry in a PDF figure.

The vision model is only asked to match canonical units to closed-world box
IDs.  Column membership itself is computed from the PDF's dashed Column
boundaries and each matched box's horizontal extent.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


BOX_LOCATOR_PROMPT_VERSION = "column-box-locator-single-v4"
TEXT_BOX_LOCATOR_PROMPT_VERSION = "column-box-text-locator-batched-v2"


class ColumnGeometryError(RuntimeError):
    """The selected PDF page does not expose a usable vector Column chart."""


def load_pdfplumber():
    """Import pdfplumber from the active runtime or the workspace venv."""

    try:
        import pdfplumber
        return pdfplumber
    except ImportError:
        site_packages = Path(__file__).resolve().parents[2] / ".venv" / "Lib" / "site-packages"
        if site_packages.is_dir() and str(site_packages) not in sys.path:
            sys.path.insert(0, str(site_packages))
        try:
            import pdfplumber
            return pdfplumber
        except ImportError as exc:  # pragma: no cover - environment guard
            raise ColumnGeometryError(
                "pdfplumber is required for vector Column geometry."
            ) from exc


@dataclass(frozen=True)
class FigureBox:
    box_id: str
    x0: float
    x1: float
    top: float
    bottom: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0


@dataclass(frozen=True)
class ColumnFigureGeometry:
    page_width: float
    page_height: float
    outer_left: float
    outer_right: float
    boundaries: tuple[float, ...]
    plot_top: float
    plot_bottom: float
    boxes: tuple[FigureBox, ...]

    @property
    def column_intervals(self) -> tuple[tuple[float, float], ...]:
        points = (self.outer_left, *self.boundaries, self.outer_right)
        return tuple(zip(points, points[1:]))


def _dash_values(row: Mapping[str, Any]) -> tuple[float, ...]:
    raw = row.get("dash")
    if not isinstance(raw, (tuple, list)) or not raw:
        return ()
    values = raw[0] if isinstance(raw[0], (tuple, list)) else raw
    try:
        return tuple(float(value) for value in values)
    except (TypeError, ValueError):
        return ()


def _is_vertical(row: Mapping[str, Any], *, tolerance: float = 0.2) -> bool:
    return abs(float(row.get("x1") or 0) - float(row.get("x0") or 0)) <= tolerance


def _bbox(row: Mapping[str, Any]) -> tuple[float, float, float, float]:
    return (
        float(row["x0"]), float(row["x1"]),
        float(row["top"]), float(row["bottom"]),
    )


def _same_bbox(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    *,
    tolerance: float = 0.35,
) -> bool:
    return all(abs(left - right) <= tolerance for left, right in zip(first, second))


def extract_column_geometry(
    pdf_path: str | Path,
    pdf_page: int,
    *,
    column_count: int,
) -> ColumnFigureGeometry:
    """Extract dashed boundaries and closed unit boxes from a one-based page."""

    if column_count < 2:
        raise ColumnGeometryError("Column box geometry requires at least two Columns.")
    pdfplumber = load_pdfplumber()

    path = Path(pdf_path).expanduser().resolve()
    if not path.is_file() or pdf_page < 1:
        raise ColumnGeometryError("Column geometry requires an existing PDF and page.")

    with pdfplumber.open(path) as document:
        if pdf_page > len(document.pages):
            raise ColumnGeometryError("Column geometry page is outside the PDF.")
        page = document.pages[pdf_page - 1]
        page_height = float(page.height)
        vertical = [row for row in page.lines if _is_vertical(row)]
        dashed = [
            row for row in vertical
            if _dash_values(row)
            and float(row["bottom"]) - float(row["top"]) >= page_height * 0.35
        ]
        dashed.sort(key=lambda row: float(row["x0"]))
        expected_boundaries = column_count - 1
        if len(dashed) != expected_boundaries:
            raise ColumnGeometryError(
                f"Expected {expected_boundaries} long dashed boundaries; found {len(dashed)}."
            )
        boundaries = tuple(float(row["x0"]) for row in dashed)
        plot_top = min(float(row["top"]) for row in dashed)
        plot_bottom = max(float(row["bottom"]) for row in dashed)

        solid = [
            row for row in vertical
            if not _dash_values(row)
            and float(row["bottom"]) - float(row["top"]) >= page_height * 0.45
        ]
        left_candidates = [float(row["x0"]) for row in solid if float(row["x0"]) < boundaries[0]]
        right_candidates = [float(row["x0"]) for row in solid if float(row["x0"]) > boundaries[-1]]
        if not left_candidates or not right_candidates:
            raise ColumnGeometryError("Could not bracket dashed boundaries with solid plot edges.")
        outer_left = max(left_candidates)
        outer_right = min(right_candidates)
        chart_width = outer_right - outer_left
        if chart_width <= 0:
            raise ColumnGeometryError("Column plot has invalid horizontal bounds.")

        candidates: list[tuple[float, float, float, float]] = []
        objects = [("rect", row) for row in page.rects]
        objects.extend(("curve", row) for row in page.curves)
        for kind, row in objects:
            x0, x1, top, bottom = _bbox(row)
            width = x1 - x0
            height = bottom - top
            if x0 < outer_left - 0.75 or x1 > outer_right + 0.75:
                continue
            if top <= plot_top + 4 or bottom >= plot_bottom + 1:
                continue
            if width >= chart_width * 0.95 or height >= (plot_bottom - plot_top) * 0.25:
                continue
            # Normal horizontal boxes and narrow vertical boxes are both
            # meaningful.  Tiny symbol strokes and glyph outlines are not.
            if not ((width >= 15 and height >= 5) or (width >= 5 and height >= 15)):
                continue
            if kind == "curve":
                path_ops = {str(item[0]) for item in row.get("path") or [] if item}
                if path_ops - {"m", "l", "h"}:
                    continue
            box = (x0, x1, top, bottom)
            if any(_same_bbox(box, existing) for existing in candidates):
                continue
            candidates.append(box)

    candidates.sort(key=lambda row: (round(row[2], 2), round(row[0], 2), row[3], row[1]))
    boxes = tuple(
        FigureBox(
            box_id=f"B{index:02d}",
            x0=row[0], x1=row[1], top=row[2], bottom=row[3],
        )
        for index, row in enumerate(candidates, start=1)
    )
    if len(boxes) < column_count * 3:
        raise ColumnGeometryError(f"Only {len(boxes)} candidate unit boxes were found.")
    return ColumnFigureGeometry(
        page_width=float(page.width), page_height=float(page.height),
        outer_left=outer_left, outer_right=outer_right,
        boundaries=boundaries, plot_top=plot_top, plot_bottom=plot_bottom,
        boxes=boxes,
    )


def columns_for_box(
    geometry: ColumnFigureGeometry,
    box: FigureBox,
    column_ids: Sequence[str],
) -> tuple[str, ...]:
    if len(column_ids) != len(geometry.column_intervals):
        raise ColumnGeometryError("Column IDs do not match the extracted geometry.")
    minimum_overlap = max(0.75, min(3.0, box.width * 0.02))
    result = []
    for column_id, (left, right) in zip(column_ids, geometry.column_intervals):
        overlap = max(0.0, min(box.x1, right) - max(box.x0, left))
        if overlap >= minimum_overlap:
            result.append(str(column_id))
    return tuple(result)


def render_box_catalog(
    image_path: str | Path,
    geometry: ColumnFigureGeometry,
    output_path: str | Path,
) -> Path:
    """Render enlarged full-row tiles with one PDF box highlighted per tile."""

    from PIL import Image, ImageDraw, ImageFont

    source = Image.open(Path(image_path)).convert("RGB")
    scale_x = source.width / geometry.page_width
    scale_y = source.height / geometry.page_height
    chart_left = max(0, int((geometry.outer_left - 4) * scale_x))
    chart_right = min(source.width, int((geometry.outer_right + 4) * scale_x))
    font = ImageFont.load_default()
    tiles: list[Image.Image] = []
    for box in geometry.boxes:
        top = max(0, int((box.top - 5) * scale_y))
        bottom = min(source.height, int((box.bottom + 5) * scale_y))
        marked = source.copy()
        draw = ImageDraw.Draw(marked)
        draw.rectangle(
            (
                int(box.x0 * scale_x), int(box.top * scale_y),
                int(box.x1 * scale_x), int(box.bottom * scale_y),
            ),
            outline=(230, 0, 0), width=max(3, round(scale_x)),
        )
        crop = marked.crop((chart_left, top, chart_right, max(top + 1, bottom)))
        target_width = 560
        ratio = target_width / max(1, crop.width)
        crop = crop.resize(
            (target_width, max(36, round(crop.height * ratio))),
            Image.Resampling.LANCZOS,
        )
        tile = Image.new("RGB", (target_width, crop.height + 24), "white")
        tile.paste(crop, (0, 24))
        label = ImageDraw.Draw(tile)
        label.rectangle((0, 0, 54, 23), fill=(20, 80, 180))
        label.text((7, 4), box.box_id, fill="white", font=font)
        tiles.append(tile)

    columns = 2
    gap = 8
    rows = (len(tiles) + columns - 1) // columns
    row_heights = [
        max(tiles[index].height for index in range(row * columns, min(len(tiles), (row + 1) * columns)))
        for row in range(rows)
    ]
    sheet = Image.new(
        "RGB",
        (columns * 560 + (columns - 1) * gap, sum(row_heights) + max(0, rows - 1) * gap),
        (240, 240, 240),
    )
    y = 0
    for row, row_height in enumerate(row_heights):
        for column in range(columns):
            index = row * columns + column
            if index >= len(tiles):
                break
            sheet.paste(tiles[index], (column * (560 + gap), y))
        y += row_height + gap
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, format="PNG", optimize=True)
    return destination


def extract_box_text_catalog(
    pdf_path: str | Path,
    pdf_page: int,
    geometry: ColumnFigureGeometry,
    column_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Extract Unicode label text inside and alongside each vector box."""

    pdfplumber = load_pdfplumber()

    path = Path(pdf_path).expanduser().resolve()
    with pdfplumber.open(path) as document:
        page = document.pages[pdf_page - 1]
        rows = []
        for box in geometry.boxes:
            direct = page.crop((box.x0, box.top, box.x1, box.bottom)).extract_text(
                x_tolerance=1, y_tolerance=1,
            ) or ""
            row_text = page.crop((
                geometry.outer_left, max(0.0, box.top - 2.0),
                geometry.outer_right, min(geometry.page_height, box.bottom + 2.0),
            )).extract_text(x_tolerance=1, y_tolerance=1) or ""
            rows.append({
                "box_id": box.box_id,
                "column_ids": list(columns_for_box(geometry, box, column_ids)),
                "label_text": "".join(direct.split()),
                "row_text": " ".join(row_text.split()),
            })
    return rows


def _normalise_figure_label(value: Any) -> str:
    text = "".join(str(value or "").split())
    text = re.sub(r"(?i)dp\.?", "堆積物", text)
    text = text.replace("及び", "")
    return re.sub(r"[・,，。．/／()（）?？:：]", "", text)


def _rect_distance(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    ax0, ax1, ay0, ay1 = first
    bx0, bx1, by0, by1 = second
    dx = max(bx0 - ax1, ax0 - bx1, 0.0)
    dy = max(by0 - ay1, ay0 - by1, 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _is_subsequence(needle: str, haystack: str) -> bool:
    iterator = iter(haystack)
    return all(character in iterator for character in needle)


def _ascii_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def _english_unit_keys(value: Any) -> tuple[str, ...]:
    full = _ascii_key(value)
    base = full
    suffixes = (
        "pyroclasticflowdeposits", "volcanicfandeposits",
        "coquinaconglomeratemember", "volcaniclasticrockmember",
        "siliciclasticrockmember", "volcanicrockmember",
        "conglomeratemember", "sandstonemember", "siltstonemember",
        "mudstonemember", "terracedeposits", "fandeposits",
        "formation", "pluton", "deposits", "member",
    )
    for suffix in suffixes:
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    keys = [full, base]
    # Source summary figures define the standard T-* abbreviations in their
    # own footnote.  Derive the closed-world abbreviation from the name.
    if base.startswith("towada") and len(base) > len("towada"):
        keys.append("t" + base[len("towada"):])
    # Japanese reports mix Hepburn and government-style romanisation in the
    # English abstract (for example Oritsume/Oritume).
    for key in tuple(keys):
        simplified = (
            key.replace("tsu", "tu").replace("shi", "si")
            .replace("chi", "ti").replace("fu", "hu")
        )
        keys.append(simplified)
    return tuple(dict.fromkeys(key for key in keys if len(key) >= 3))


def resolve_box_assignments_from_english_summary(
    pdf_path: str | Path,
    pdf_page: int,
    geometry: ColumnFigureGeometry,
    units: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, tuple[str, ...]], list[dict[str, Any]]]:
    """Match a report's English summary figure without GOLD or translation.

    GSJ reports commonly repeat the Japanese overview chart in the English
    abstract.  Full labels, chart-defined abbreviations, reversed vertical
    labels and leader-line marker boxes are all resolved against that source.
    """

    pdfplumber = load_pdfplumber()

    catalog = extract_box_text_catalog(
        pdf_path, pdf_page, geometry,
        [str(index) for index in range(len(geometry.column_intervals))],
    )
    by_box = {box.box_id: box for box in geometry.boxes}
    text_by_box = {str(row["box_id"]): str(row["label_text"]) for row in catalog}
    label_keys = {
        box_id: (_ascii_key(label), _ascii_key(label[::-1]))
        for box_id, label in text_by_box.items()
    }
    assignments: dict[str, tuple[str, ...]] = {}
    evidence: list[dict[str, Any]] = []
    claimed: dict[str, str] = {}

    with pdfplumber.open(Path(pdf_path).expanduser().resolve()) as document:
        page = document.pages[pdf_page - 1]
        page_lines = [
            (
                float(row["x0"]), float(row["x1"]),
                float(row["top"]), float(row["bottom"]),
            )
            for row in page.lines
            if max(
                abs(float(row["x1"]) - float(row["x0"])),
                abs(float(row["bottom"]) - float(row["top"])),
            ) <= 80.0
        ]
        for unit in units:
            unit_id = str(unit.get("unit_id") or "").strip()
            name = str(unit.get("unit_name") or "").strip()
            if not unit_id:
                continue
            if " member" in name.casefold():
                assignments[unit_id] = ()
                evidence.append({
                    "unit_id": unit_id, "box_ids": [],
                    "method": "member_not_boxed",
                })
                continue
            probes = [name]
            for suffix in (
                " Pyroclastic Flow Deposits", " Volcanic Fan Deposits",
                " terrace deposits", " Fan Deposits", " fan deposits",
                " Formation", " Pluton", " deposits",
            ):
                if name.casefold().endswith(suffix.casefold()) and len(name) > len(suffix):
                    probes.append(name[:-len(suffix)])
            matched: list[str] = []
            for probe in dict.fromkeys(probes):
                try:
                    hits = page.search(probe, case=False)
                except TypeError:  # older pdfplumber
                    hits = page.search(probe)
                for hit in hits:
                    hit_rect = (
                        float(hit["x0"]), float(hit["x1"]),
                        float(hit["top"]), float(hit["bottom"]),
                    )
                    center_x = (hit_rect[0] + hit_rect[1]) / 2
                    center_y = (hit_rect[2] + hit_rect[3]) / 2
                    direct = [
                        box.box_id for box in geometry.boxes
                        if box.x0 - 0.5 <= center_x <= box.x1 + 0.5
                        and box.top - 0.5 <= center_y <= box.bottom + 0.5
                    ]
                    if direct:
                        matched.extend(direct)
                        continue
                    line_distances = [
                        (_rect_distance(hit_rect, line), line)
                        for line in page_lines
                    ]
                    nearest = min((distance for distance, _line in line_distances), default=999.0)
                    for distance, line in line_distances:
                        if nearest > 13.0 or distance > nearest + 0.5:
                            continue
                        matched.extend(
                            box.box_id for box in geometry.boxes
                            if _rect_distance(
                                line, (box.x0, box.x1, box.top, box.bottom),
                            ) <= 1.5
                        )
                if matched:
                    break
            unique = tuple(dict.fromkeys(
                box_id for box_id in matched if box_id not in claimed
            ))
            if unique:
                assignments[unit_id] = unique
                for box_id in unique:
                    claimed[box_id] = unit_id
                evidence.append({
                    "unit_id": unit_id, "box_ids": list(unique),
                    "method": "english_pdf_text_or_leader",
                })

    for unit in units:
        unit_id = str(unit.get("unit_id") or "").strip()
        if unit_id in assignments:
            continue
        name = str(unit.get("unit_name") or "").strip()
        folded = name.casefold()
        if "floodplain" in folded or "valley-floor" in folded:
            values = tuple(
                box_id for box_id, keys in label_keys.items()
                if "fld" in keys[0] and "deposits" in keys[0]
            )
        elif "river bed" in folded or "river-bed" in folded:
            values = tuple(
                box_id for box_id, keys in label_keys.items()
                if keys[0].startswith("rfld")
            )
        else:
            keys = _english_unit_keys(name)
            scored: list[tuple[int, str]] = []
            for box_id, variants in label_keys.items():
                if box_id in claimed:
                    continue
                score = max((
                    len(key) for key in keys
                    if any(key in label for label in variants)
                ), default=0)
                if score:
                    scored.append((score, box_id))
            best = max((score for score, _box_id in scored), default=0)
            values = tuple(
                box_id for score, box_id in scored if score == best
            )
        values = tuple(dict.fromkeys(values))
        assignments[unit_id] = values
        for box_id in values:
            claimed.setdefault(box_id, unit_id)
        evidence.append({
            "unit_id": unit_id, "box_ids": list(values),
            "method": "english_summary_abbreviation" if values else "no_visible_box",
        })

    return assignments, evidence


def _blank_twins(
    box_ids: Sequence[str],
    geometry: ColumnFigureGeometry,
    text_by_box: Mapping[str, str],
) -> tuple[str, ...]:
    by_box = {box.box_id: box for box in geometry.boxes}
    expanded = list(box_ids)
    for box_id in tuple(expanded):
        source = by_box[box_id]
        # Repeated Quaternary boxes are short horizontal rectangles. Marker
        # boxes for plutons and structural units must not be paired this way.
        if source.bottom - source.top > 12.0:
            continue
        for candidate in geometry.boxes:
            if candidate.box_id in expanded or text_by_box[candidate.box_id]:
                continue
            if (
                candidate.bottom - candidate.top <= 12.0
                and abs(source.top - candidate.top) <= 0.4
                and abs(source.bottom - candidate.bottom) <= 0.4
            ):
                expanded.append(candidate.box_id)
    return tuple(dict.fromkeys(expanded))


def resolve_box_assignments_locally(
    pdf_path: str | Path,
    pdf_page: int,
    geometry: ColumnFigureGeometry,
    units: Sequence[Mapping[str, Any]],
    *,
    fallback: Mapping[str, Sequence[str]] | None = None,
) -> tuple[dict[str, tuple[str, ...]], list[dict[str, Any]]]:
    """Resolve source-backed aliases to boxes without consulting GOLD data."""

    pdfplumber = load_pdfplumber()

    catalog = extract_box_text_catalog(
        pdf_path, pdf_page, geometry,
        [str(index) for index in range(len(geometry.column_intervals))],
    )
    by_box = {box.box_id: box for box in geometry.boxes}
    text_by_box = {str(row["box_id"]): str(row["label_text"]) for row in catalog}
    normal_by_box = {
        box_id: _normalise_figure_label(value)
        for box_id, value in text_by_box.items()
    }
    assignments: dict[str, tuple[str, ...]] = {}
    evidence: list[dict[str, Any]] = []

    with pdfplumber.open(Path(pdf_path).expanduser().resolve()) as document:
        page = document.pages[pdf_page - 1]
        page_lines = [
            (
                float(row["x0"]), float(row["x1"]),
                float(row["top"]), float(row["bottom"]),
            )
            for row in page.lines
            if max(
                abs(float(row["x1"]) - float(row["x0"])),
                abs(float(row["bottom"]) - float(row["top"])),
            ) <= 80.0
        ]
        claimed: dict[str, str] = {}
        for unit in units:
            unit_id = str(unit.get("unit_id") or "").strip()
            alias = str(unit.get("unit_name_ja") or "").strip()
            if not unit_id or not alias:
                continue
            hits: list[Mapping[str, Any]] = []
            probes = [alias]
            for suffix in ("堆積物", "深成岩体", "層"):
                if alias.endswith(suffix) and len(alias) > len(suffix) + 1:
                    probes.append(alias[:-len(suffix)])
            for probe in probes:
                hits = page.search(probe)
                if hits:
                    break
            matched: list[str] = []
            for hit in hits:
                hit_rect = (
                    float(hit["x0"]), float(hit["x1"]),
                    float(hit["top"]), float(hit["bottom"]),
                )
                center_x = (hit_rect[0] + hit_rect[1]) / 2
                center_y = (hit_rect[2] + hit_rect[3]) / 2
                direct = [
                    box.box_id for box in geometry.boxes
                    if box.x0 - 0.5 <= center_x <= box.x1 + 0.5
                    and box.top - 0.5 <= center_y <= box.bottom + 0.5
                ]
                if direct:
                    matched.extend(direct)
                    continue
                # Labels connected to a marker box by a short leader line
                # live outside the marker. Follow touching vector lines.
                touching_lines = [
                    line for line in page_lines if _rect_distance(hit_rect, line) <= 3.5
                ]
                for line in touching_lines:
                    matched.extend(
                        box.box_id for box in geometry.boxes
                        if _rect_distance(line, (box.x0, box.x1, box.top, box.bottom)) <= 1.0
                    )
            if not matched:
                alias_normal = _normalise_figure_label(alias)
                matched = [
                    box_id for box_id, label in normal_by_box.items()
                    if alias_normal == label
                    or (len(alias_normal) >= 3 and alias_normal in label)
                    or (len(alias_normal) >= 3 and _is_subsequence(alias_normal, label))
                ]
            unique = _blank_twins(matched, geometry, text_by_box)
            if unique:
                assignments[unit_id] = unique
                for box_id in unique:
                    claimed.setdefault(box_id, unit_id)
                evidence.append({
                    "unit_id": unit_id, "box_ids": list(unique),
                    "method": "pdf_unicode_alias",
                })

    fallback = fallback or {}
    for unit in units:
        unit_id = str(unit.get("unit_id") or "").strip()
        if unit_id in assignments:
            continue
        name = str(unit.get("unit_name") or "").strip()
        values = tuple(
            box_id for box_id in fallback.get(unit_id, ()) if box_id in by_box
        )
        if " member" in name.casefold():
            assignments[unit_id] = ()
            evidence.append({"unit_id": unit_id, "box_ids": [], "method": "member_not_boxed"})
            continue
        # A model fallback cannot steal a box already proven to name another
        # unit by an exact source alias.  The sole exception is a slash-grouped
        # source box: its printed phrases intentionally describe more than one
        # canonical unit (river bed plus floodplain/valley floor).
        name_folded = name.casefold()
        shares_slash_group = (
            "river-bed" in name_folded
            or "river bed" in name_folded
            or "floodplain" in name_folded
            or "valley-floor" in name_folded
        )
        values = tuple(
            box_id for box_id in values
            if box_id not in claimed
            or (shares_slash_group and "/" in text_by_box[box_id])
        )
        # The slash-grouped Quaternary box represents both the river-bed and
        # floodplain/valley-floor phrases printed inside it.  A neighbouring
        # same-row box belongs only to river bed, so retain the slash box for
        # floodplain while retaining the whole row for river bed.
        same_row_has_slash = any(
            "/" in text_by_box[other.box_id]
            and abs(other.top - by_box[box_id].top) <= 0.4
            for box_id in values for other in geometry.boxes
        )
        is_river_bed = "river-bed" in name_folded or "river bed" in name_folded
        is_floodplain = "floodplain" in name_folded or "valley-floor" in name_folded
        if same_row_has_slash and is_floodplain:
            values = tuple(box_id for box_id in values if "/" in text_by_box[box_id])
        elif same_row_has_slash and not is_river_bed:
            values = ()
        values = _blank_twins(values, geometry, text_by_box)
        assignments[unit_id] = values
        for box_id in values:
            claimed.setdefault(box_id, unit_id)
        evidence.append({
            "unit_id": unit_id, "box_ids": list(values),
            "method": "validated_model_fallback" if values else "no_visible_box",
        })

    unresolved_formations = [
        str(unit.get("unit_id") or "").strip()
        for unit in units
        if str(unit.get("unit_name") or "").casefold().endswith(" formation")
        and not assignments.get(str(unit.get("unit_id") or "").strip())
    ]
    unused_label_boxes = [
        box.box_id for box in geometry.boxes
        if text_by_box[box.box_id] and box.box_id not in claimed
    ]
    # After exact aliases and validated fallbacks, a unique remaining
    # Formation/label pair is a closed-world sequence completion.
    if len(unresolved_formations) == len(unused_label_boxes) == 1:
        unit_id = unresolved_formations[0]
        box_id = unused_label_boxes[0]
        assignments[unit_id] = (box_id,)
        evidence.append({
            "unit_id": unit_id, "box_ids": [box_id],
            "method": "unique_remaining_formation_box",
        })
    return assignments, evidence


def build_text_box_locator_prompt(
    units: Sequence[Mapping[str, Any]],
    box_catalog: Sequence[Mapping[str, Any]],
) -> str:
    supplied_units = []
    for row in units:
        entry = {
            "unit_id": str(row.get("unit_id") or "").strip(),
            "unit_name": str(row.get("unit_name") or "").strip(),
        }
        japanese = str(row.get("unit_name_ja") or "").strip()
        if japanese:
            entry["unit_name_ja"] = japanese
        supplied_units.append(entry)
    return f"""Match canonical geological units to a closed catalog extracted
locally from one Japanese stratigraphic figure. This is text matching, not
geological inference. Return JSON only.

Return exactly:
{{"assignments":[{{"unit_id":"supplied ID","box_ids":["B01"]}}]}}

Rules:
- Return every supplied unit exactly once and preserve its ID.
- Use only box IDs in BOX_CATALOG. Use [] when the figure does not directly
  label the unit.
- label_text is text inside the box. row_text is nearby context for narrow
  marker boxes whose label is connected by a leader line.
- Match exact Japanese aliases first. Treat "dp." as an abbreviation of
  deposits/堆積物 and ignore whitespace introduced by vertical writing.
- English names are official transliterations/translations of Japanese names.
- A Member is not its parent Formation. Do not map a Member when only the
  parent Formation is printed.
- A slash-grouped box represents only its leading/head canonical unit. A
  secondary phrase and "other" are not separate box labels.
- A repeated label may map one unit to multiple boxes in different columns.
- Do not use age, vertical order, or GOLD/expected memberships.

BOX_CATALOG:
{json.dumps(list(box_catalog), ensure_ascii=False, indent=2)}

SUPPLIED_UNITS:
{json.dumps(supplied_units, ensure_ascii=False, indent=2)}
"""


def build_box_locator_prompt(
    units: Sequence[Mapping[str, Any]],
    boxes: Sequence[FigureBox],
) -> str:
    supplied_units = []
    for row in units:
        entry = {
            "unit_id": str(row.get("unit_id") or "").strip(),
            "unit_name": str(row.get("unit_name") or "").strip(),
        }
        japanese = str(row.get("unit_name_ja") or "").strip()
        if japanese:
            entry["unit_name_ja"] = japanese
        supplied_units.append(entry)
    if not supplied_units or any(not row["unit_id"] or not row["unit_name"] for row in supplied_units):
        raise ValueError("Box locator requires non-empty unit IDs and names.")
    if len(supplied_units) > 8:
        raise ValueError("Box locator batches are limited to eight units.")
    box_ids = [box.box_id for box in boxes]
    return f"""The attached image is a catalog of boxes extracted from one
Japanese stratigraphic figure. Each tile is labeled with a closed-world BOX_ID
and the target box is outlined in red. Match only SUPPLIED_UNITS to boxes whose
printed Japanese label names that unit.

Return exactly:
{{"assignments":[{{"unit_id":"supplied ID","box_ids":["B01"]}}]}}

Rules:
- Return every supplied unit exactly once and preserve its ID.
- box_ids may contain only AVAILABLE_BOX_IDS. Use [] when no catalog box
  directly labels the supplied unit.
- A Member is not the same as its parent Formation. Do not attach a Member to
  a box that labels only the parent Formation.
- When a printed box groups several concepts with slash or punctuation, map
  it only to the canonical unit matching the leading/head label. A secondary
  phrase and a vague word such as "other" are not separate box labels.
- The same geological unit may have repeated boxes in different regions; in
  that case return every matching box ID.
- English names are transliterations and translations. When unit_name_ja is
  supplied it is the source-backed Japanese alias and must match the actual
  text in the red box. Do not infer from age, position, or neighboring boxes.
- Do not infer from age, vertical order, parent-child relationships, or nearby
  boxes. Return JSON only, with no explanation.

AVAILABLE_BOX_IDS:
{json.dumps(box_ids, ensure_ascii=False)}

SUPPLIED_UNITS:
{json.dumps(supplied_units, ensure_ascii=False, indent=2)}
"""


def validate_box_locator(
    response: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
    boxes: Sequence[FigureBox],
) -> dict[str, tuple[str, ...]]:
    expected = {str(row.get("unit_id") or "").strip() for row in units}
    allowed = {box.box_id for box in boxes}
    raw = response.get("assignments")
    if not isinstance(raw, list):
        raise ValueError("Box locator response requires assignments[].")
    result: dict[str, tuple[str, ...]] = {}
    for row in raw:
        if not isinstance(row, Mapping):
            raise ValueError("Every box assignment must be an object.")
        unit_id = str(row.get("unit_id") or "").strip()
        values = row.get("box_ids")
        if unit_id not in expected or unit_id in result or not isinstance(values, list):
            raise ValueError("Box locator returned an unknown, duplicate, or malformed unit.")
        box_ids = tuple(str(value).strip() for value in values)
        if len(set(box_ids)) != len(box_ids) or any(value not in allowed for value in box_ids):
            raise ValueError("Box locator returned a duplicate or unknown box ID.")
        result[unit_id] = box_ids
    if set(result) != expected:
        raise ValueError("Box locator must return every supplied unit.")
    return result


def derive_memberships(
    assignments: Mapping[str, Sequence[str]],
    geometry: ColumnFigureGeometry,
    column_ids: Sequence[str],
) -> dict[str, tuple[str, ...]]:
    by_id = {box.box_id: box for box in geometry.boxes}
    result: dict[str, tuple[str, ...]] = {}
    for unit_id, values in assignments.items():
        memberships: list[str] = []
        for box_id in values:
            box = by_id.get(str(box_id))
            if box is None:
                raise ColumnGeometryError(f"Unknown box ID: {box_id}")
            for column_id in columns_for_box(geometry, box, column_ids):
                if column_id not in memberships:
                    memberships.append(column_id)
        result[str(unit_id)] = tuple(memberships)
    return result


__all__ = [
    "BOX_LOCATOR_PROMPT_VERSION",
    "TEXT_BOX_LOCATOR_PROMPT_VERSION",
    "ColumnFigureGeometry",
    "ColumnGeometryError",
    "FigureBox",
    "build_box_locator_prompt",
    "build_text_box_locator_prompt",
    "columns_for_box",
    "derive_memberships",
    "extract_column_geometry",
    "extract_box_text_catalog",
    "load_pdfplumber",
    "render_box_catalog",
    "resolve_box_assignments_locally",
    "validate_box_locator",
]
