# -*- coding: utf-8 -*-
"""Build a Column proposal from validated PDF vector-box assignments.

This is a generation-side adapter: it never opens a GOLD fixture or reviewed
workbook.  Unit-to-box matches come from a source-only checkpoint, while
Column membership and vertical order are derived deterministically from the
source PDF's vector geometry.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from column_figure_geometry import (
    derive_memberships,
    extract_column_geometry,
    load_pdfplumber,
    resolve_box_assignments_from_english_summary,
)
from column_geography import canonical_units


def _read_object(path: Path, label: str) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return document


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _raw_bundle(workspace: Path) -> Path:
    canonical = workspace / "system" / "raw" / "raw_bundle.json"
    if canonical.is_file():
        return canonical
    candidates = sorted(
        path for path in (workspace / "system").rglob("raw_bundle.json")
        if "backup" not in {part.casefold() for part in path.parts}
    )
    if len(candidates) != 1:
        raise ValueError(
            f"Expected one system/**/raw_bundle.json, found {len(candidates)}"
        )
    return candidates[0]


def _source_pdf(workspace: Path, proposal: Mapping[str, Any]) -> Path:
    manifest = workspace / "system" / "column_vision" / "vision_manifest.json"
    if manifest.is_file():
        source = str(_read_object(manifest, "Vision manifest").get("source_pdf") or "")
        candidate = Path(source)
        if candidate.is_file():
            return candidate.resolve()
    candidates = sorted((workspace / "references").glob("*.pdf"))
    if len(candidates) != 1:
        raise ValueError(f"Expected one references/*.pdf, found {len(candidates)}")
    return candidates[0].resolve()


def _english_summary_page(workspace: Path) -> int:
    candidates = sorted((workspace / "references").glob("*pdfpages*.json"))
    if len(candidates) != 1:
        raise ValueError(f"Expected one references/*pdfpages*.json, found {len(candidates)}")
    index = _read_object(candidates[0], "PDF index")
    for page_number, value in enumerate(index.get("pages") or [], start=1):
        compact = "".join(str(value or "").casefold().split())
        if "summaryofgeology" in compact and "westerncentraleastern" in compact:
            return page_number
    raise ValueError("PDF index has no English summary-of-geology figure")


def discover_source_only_base_proposal(workspace: Path) -> dict[str, Any]:
    """Discover regional Columns from a vector source PDF without LLM/GOLD."""

    workspace = Path(workspace).resolve()
    page_number = _english_summary_page(workspace)
    pdf = _source_pdf(workspace, {})
    geometry = None
    for column_count in range(2, 9):
        try:
            geometry = extract_column_geometry(
                pdf, page_number, column_count=column_count,
            )
            break
        except (ValueError, RuntimeError):
            continue
    if geometry is None:
        raise ValueError("Could not discover a 2-8 Column vector summary chart")

    pdfplumber = load_pdfplumber()
    with pdfplumber.open(pdf) as document:
        page_text = document.pages[page_number - 1].extract_text() or ""
    direction_words = re.findall(
        r"(?i)\b(western|central|eastern|northern|southern)\b",
        page_text[:2000],
    )
    directions: list[str] = []
    for value in direction_words:
        value = value.casefold()
        if value not in directions:
            directions.append(value)
    discovered_count = len(geometry.column_intervals)
    if len(directions) != discovered_count:
        directions = [
            f"column-{index}" for index in range(1, discovered_count + 1)
        ]

    columns = []
    for index, direction in enumerate(directions, start=1):
        generic = direction.startswith("column-")
        direction_key = {
            "western": "west", "eastern": "east",
            "northern": "north", "southern": "south",
            "central": "central",
        }.get(direction)
        columns.append({
            "column_id": direction,
            "column_name": f"Column {index}" if generic else f"{direction.title()} Area",
            "figure_pdf_page": page_number,
            "region_description": (
                "Regional label read from the source summary figure"
                if not generic else "Unlabelled source-figure region"
            ),
            "quote_verified": not generic,
            "constraints": (
                [{"kind": "map_direction", "direction": direction_key}]
                if direction_key else []
            ),
        })
    return {
        "schema_version": "column-vision/1.0",
        "status": "source_only_discovery",
        "assignment_ready": False,
        "columns": columns,
        "units": [],
        "discovery": {
            "source_pdf": str(pdf),
            "figure_pdf_page": page_number,
            "vector_boundaries": len(geometry.boundaries),
            "gold_inputs_used": False,
        },
    }


def build_source_only_box_result(
    workspace: Path, base_proposal: Mapping[str, Any],
) -> dict[str, Any]:
    units = canonical_units(_read_object(_raw_bundle(workspace), "Raw bundle"))
    columns = [
        row for row in base_proposal.get("columns") or []
        if isinstance(row, Mapping)
    ]
    page = _english_summary_page(workspace)
    pdf = _source_pdf(workspace, base_proposal)
    geometry = extract_column_geometry(pdf, page, column_count=len(columns))
    assignments, evidence = resolve_box_assignments_from_english_summary(
        pdf, page, geometry, units,
    )
    return {
        "schema_version": "column-box-assignments/1.0",
        "prompt_version": "english-summary-pdf-vector-v1",
        "box_locator_mode": "local_english_summary",
        "provider": "local_pdf_vector",
        "requested_model": None,
        "figure_pdf_page": page,
        "geometry_box_count": len(geometry.boxes),
        "box_assignments": {
            unit_id: list(box_ids) for unit_id, box_ids in assignments.items()
        },
        "evidence": evidence,
        "gold_inputs_used": False,
    }


def build_geometry_proposal(
    *,
    workspace: Path,
    box_result: Mapping[str, Any],
    base_proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a proposal whose memberships come only from PDF geometry."""

    workspace = workspace.resolve()
    units = canonical_units(_read_object(_raw_bundle(workspace), "Raw bundle"))
    if not units:
        raise ValueError("Raw bundle has no canonical units")
    columns = [
        dict(row) for row in base_proposal.get("columns") or []
        if isinstance(row, Mapping)
    ]
    if not columns:
        raise ValueError("Base proposal has no columns")
    column_ids = [str(row.get("column_id") or "").strip() for row in columns]
    if any(not value for value in column_ids) or len(set(column_ids)) != len(column_ids):
        raise ValueError("Base proposal has blank or duplicate Column IDs")
    first_page = next((
        row.get("figure_pdf_page") for row in columns
        if isinstance(row.get("figure_pdf_page"), int)
    ), None)
    if not isinstance(first_page, int) or first_page < 1:
        raise ValueError("Base proposal does not identify the figure PDF page")

    pdf = _source_pdf(workspace, base_proposal)
    geometry_page = int(box_result.get("figure_pdf_page") or first_page)
    geometry = extract_column_geometry(
        pdf, geometry_page, column_count=len(columns),
    )
    raw_assignments = box_result.get("box_assignments")
    if not isinstance(raw_assignments, Mapping):
        raise ValueError("Box result has no box_assignments object")
    allowed_units = {str(row["unit_id"]) for row in units}
    allowed_boxes = {box.box_id for box in geometry.boxes}
    assignments: dict[str, tuple[str, ...]] = {}
    for unit_id, raw_box_ids in raw_assignments.items():
        if str(unit_id) not in allowed_units or not isinstance(raw_box_ids, list):
            raise ValueError(f"Unknown or malformed box assignment: {unit_id}")
        box_ids = tuple(str(value) for value in raw_box_ids)
        if len(set(box_ids)) != len(box_ids) or any(value not in allowed_boxes for value in box_ids):
            raise ValueError(f"Duplicate or unknown box ID for {unit_id}")
        assignments[str(unit_id)] = box_ids
    if set(assignments) != allowed_units:
        missing = sorted(allowed_units - set(assignments))
        raise ValueError(f"Box result does not cover the canonical inventory: {missing}")

    memberships = derive_memberships(assignments, geometry, column_ids)
    box_by_id = {box.box_id: box for box in geometry.boxes}
    # Rank 1 is the youngest/topmost visible unit in each output Column.  The
    # lower edge is the stable age-order anchor for both short rows and tall
    # duration boxes (using the top edge would move a long-lived unit ahead of
    # every younger row that it overlaps).
    rank_by_pair: dict[tuple[str, str], int] = {}
    for column_id in column_ids:
        visible = [
            unit_id for unit_id, values in memberships.items()
            if column_id in values
        ]
        def order_anchor(unit_id: str) -> tuple[float, float, str]:
            boxes = [box_by_id[box_id] for box_id in assignments[unit_id]]
            if len(boxes) == 1 and boxes[0].width <= 25.0 and (
                boxes[0].bottom - boxes[0].top
            ) <= 25.0:
                # Overlapping marker boxes encode the structural stack from
                # left to right; group their close vertical positions first.
                return (round(boxes[0].top / 25.0) * 25.0, boxes[0].x0, unit_id)
            return (max(box.bottom for box in boxes), 0.0, unit_id)

        visible.sort(key=order_anchor)
        for rank, unit_id in enumerate(visible, start=1):
            rank_by_pair[(unit_id, column_id)] = rank

    base_by_id = {
        str(row.get("unit_id") or ""): row
        for row in base_proposal.get("units") or []
        if isinstance(row, Mapping)
    }
    proposal_units: list[dict[str, Any]] = []
    unassigned: list[str] = []
    membership_count = 0
    for unit in units:
        unit_id = str(unit["unit_id"])
        member_columns = memberships.get(unit_id, ())
        if not member_columns:
            unassigned.append(unit_id)
            continue
        base = base_by_id.get(unit_id) or {}
        member_rows = [
            {
                "column_id": column_id,
                "sort_order": rank_by_pair[(unit_id, column_id)],
            }
            for column_id in column_ids if column_id in member_columns
        ]
        membership_count += len(member_rows)
        proposal_units.append({
            "unit_id": unit_id,
            "unit_name": str(unit["unit_name"]),
            "memberships": member_rows,
            "age": dict(base.get("age") or {
                "t_int": None, "b_int": None, "visual_label": "",
            }),
            "confidence": "C",
        })

    return {
        "schema_version": "column-vision/1.0",
        "status": "candidate_review",
        "assignment_ready": True,
        "columns": columns,
        "units": proposal_units,
        "unassigned_units": sorted(unassigned),
        "validation": {
            "canonical_units": len(units),
            "matched_units": len(proposal_units),
            "unassigned_units": len(unassigned),
            "columns": len(columns),
            "verified_region_quotes": sum(bool(row.get("quote_verified")) for row in columns),
            "vector_boxes": len(geometry.boxes),
            "memberships": membership_count,
        },
        "geometry_recovery": {
            "source_pdf": str(pdf),
            "figure_pdf_page": geometry_page,
            "box_locator_prompt_version": box_result.get("prompt_version"),
            "box_locator_mode": box_result.get("box_locator_mode"),
            "provider": box_result.get("provider"),
            "requested_model": box_result.get("requested_model"),
            "gold_inputs_used": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--box-result", type=Path)
    parser.add_argument("--base-proposal", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    base_path = args.base_proposal or (
        workspace / "system" / "column_vision" / "column_proposal.json"
    )
    output = args.output or base_path
    box_result = (
        _read_object(args.box_result.resolve(), "Box result")
        if args.box_result is not None
        else build_source_only_box_result(
            workspace, _read_object(base_path.resolve(), "Base proposal"),
        )
    )
    if args.box_result is None:
        _atomic_json(
            workspace / "system" / "column_vision" / "column_box_assignments.json",
            box_result,
        )
    proposal = build_geometry_proposal(
        workspace=workspace,
        box_result=box_result,
        base_proposal=_read_object(base_path.resolve(), "Base proposal"),
    )
    _atomic_json(output.resolve(), proposal)
    print(json.dumps({
        "output": str(output.resolve()),
        "columns": proposal["validation"]["columns"],
        "memberships": proposal["validation"]["memberships"],
        "unassigned_units": proposal["validation"]["unassigned_units"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
