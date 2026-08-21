# -*- coding: utf-8 -*-
"""Apply reviewed Column proposals and select auditable representative points.

Column membership comes from a validated PDF-figure proposal.  Coordinates do
not come from the language model: candidates are interior points of assigned
GSJ Shape polygons and PDF-derived geographic constraints only rank them.
Named places are resolved through the cached, map-bounded geocoder.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from column_map import (
    ColumnDefinition,
    UnitAssignment,
    _assign_components,
    _attach_attributes,
    _build_components,
    _component_point,
    _match_assignments,
    _record_code,
    _record_name,
    point_in_component,
    read_polygon_shapefile,
)
from compiled_layer import build_canonical_layer, split_column_ids
from formation_consolidation import consolidate_pdf_formations, formation_key
from geocode_util import resolve_place_names


SCHEMA_VERSION = "column-geography/1.0"

# Column を判定できなかったユニットの置き場。
# 提案に含まれなかったユニットを黙って捨てると Excel から地層が消えるため、
# ここに集めて status と comments で警告する。地理的な実体ではないので、
# 提出前チェック（export_submission.validate）はこの Column をエラーにする。
UNASSIGNED_COLUMN_ID = "unassigned"
UNASSIGNED_COMMENT = (
    "[要確認] Column 未割当。層序図から所属 Column を判定できませんでした。"
    "提出前に正しい Column へ割り当ててください。"
)


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _rows(bundle: Mapping[str, Any], name: str, review_key: str) -> list[dict[str, Any]]:
    direct = bundle.get(name)
    if isinstance(direct, list):
        return [dict(row) for row in direct if isinstance(row, Mapping)]
    review = bundle.get("review_v2_input")
    value = review.get(review_key) if isinstance(review, Mapping) else None
    return [dict(row) for row in value or [] if isinstance(row, Mapping)]


def canonical_units(bundle: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return the exact unit inventory sent to the multimodal extractor."""

    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in _rows(bundle, "units", "unit_rows"):
        unit_id = str(row.get("unit_id") or "").strip()
        unit_name = str(
            row.get("unit_name") or (row.get("values") or {}).get("unit_name") or ""
        ).strip()
        if unit_id and unit_name and unit_id not in seen:
            output.append({"unit_id": unit_id, "unit_name": unit_name})
            seen.add(unit_id)
    return output


def expected_columns(bundle: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return current reviewed Column IDs without coordinates or conclusions."""

    output: list[dict[str, str]] = []
    for row in _rows(bundle, "columns", "column_rows"):
        column_id = str(row.get("col_id") or row.get("column_id") or "").strip()
        name = str(row.get("display_name") or row.get("col_name") or column_id).strip()
        if column_id:
            output.append({"column_id": column_id, "column_name": name})
    return output


def _rebuild(bundle: dict[str, Any], units: list[dict[str, Any]],
             columns: list[dict[str, Any]], evidence_rows: list[dict[str, Any]],
             metadata_updates: Mapping[str, Any]) -> dict[str, Any]:
    review_input = bundle.get("review_v2_input")
    project = review_input.get("project") if isinstance(review_input, Mapping) else None
    metadata = dict(project) if isinstance(project, Mapping) else {}
    metadata.update(metadata_updates)
    generated_at = str(bundle.get("compiled", {}).get("generated_at") or "") or None
    compiled, evidence = build_canonical_layer(
        units,
        column_rows=columns,
        evidence_rows=evidence_rows,
        metadata=metadata,
        map_id=str(bundle.get("map_id") or ""),
        source_review=None,
        generated_at=generated_at,
    )
    bundle["units"] = units
    bundle["columns"] = columns
    bundle["source_evidence"] = evidence_rows
    bundle["compiled"] = compiled
    bundle["evidence"] = evidence
    bundle["review_v2_input"] = {
        "unit_rows": units,
        "column_rows": columns,
        "evidence_rows": evidence_rows,
        "project": metadata,
    }
    return bundle


def apply_vision_assignments(
    bundle: dict[str, Any],
    proposal: Mapping[str, Any],
    *,
    source_pdf: str,
    source_figure: str | None = None,
    preserve_unassigned: bool = True,
) -> dict[str, Any]:
    """Apply a complete validated proposal when no reviewed config exists.

    Shared units always remain one review row.  ``column_id`` and
    ``sort_order`` are stored as aligned values so different regional ranks do
    not duplicate a Formation or mutate its stable ``unit_id``.
    """

    if not proposal.get("assignment_ready"):
        raise ValueError("Column Vision proposal is not assignment-ready")
    proposal_columns = proposal.get("columns")
    proposal_units = proposal.get("units")
    if not isinstance(proposal_columns, list) or not isinstance(proposal_units, list):
        raise ValueError("Column Vision proposal has no columns or units")

    default_ref = str(((bundle.get("refs") or [{}])[0]).get("ref_id") or f"gsj{bundle.get('map_id')}")
    columns = []
    for item in proposal_columns:
        column_id = str(item.get("column_id") or "").strip()
        columns.append({
            "col_id": column_id,
            "col_name": str(item.get("column_name") or column_id).strip(),
            "region_basis": str(item.get("region_description") or "").strip(),
            "status": "CHECK",
            "comments": "PDF/LLM Column candidate; human review required.",
            "ref_ids": default_ref,
            "col_type": "column",
            "axis_type": "age",
        })

    original_by_id = {
        str(row.get("unit_id") or ""): row
        for row in _rows(bundle, "units", "unit_rows")
    }
    evidence_rows = _rows(bundle, "source_evidence", "evidence_rows")
    units: list[dict[str, Any]] = []
    for proposal_unit in proposal_units:
        unit_id = str(proposal_unit.get("unit_id") or "")
        original = original_by_id.get(unit_id)
        if original is None:
            raise ValueError(f"Column proposal references unknown unit: {unit_id}")
        memberships = [
            {
                "column_id": str(item.get("column_id") or ""),
                "sort_order": int(item.get("sort_order")),
            }
            for item in proposal_unit.get("memberships") or []
        ]
        row = dict(original)
        column_ids = [item["column_id"] for item in memberships]
        row["column_id"] = ", ".join(column_ids)
        row["sort_order"] = (
            memberships[0]["sort_order"]
            if len(memberships) == 1
            else ", ".join(str(item["sort_order"]) for item in memberships)
        )
        row["formation_key"] = formation_key(
            row.get("unit_name") or row.get("REF_unit_name_en")
        )
        row["source_unit_ids"] = str(row.get("source_unit_ids") or unit_id)
        units.append(row)
        body_evidence = proposal_unit.get("body_evidence")
        body_evidence = body_evidence if isinstance(body_evidence, Mapping) else None
        source_locator = (
                " / ".join(
                    value
                    for value in (
                        str(body_evidence.get("section") or "").strip(),
                        f"PDF p.{body_evidence.get('pdf_page')}"
                        if body_evidence and body_evidence.get("pdf_page")
                        else "",
                    )
                    if value
                )
            if body_evidence
            else "Validated stratigraphic summary figure"
        )
        context_quote = (
                str(body_evidence.get("full_context_quote") or "")
            if body_evidence
            else "Column membership interpreted from the selected PDF stratigraphic figure."
        )
        extraction_method = str(
                proposal_unit.get("completion_method")
            or "cached multimodal Column proposal; human review required"
        )
        for membership in memberships:
            order_locator = (
                    source_locator
                if body_evidence
                else "Validated stratigraphic summary figure"
            )
            order_context = (
                    "Existing canonical sort_order retained because this unit is "
                    "omitted from the summary figure; human review required."
                if body_evidence
                else "Stratigraphic rank interpreted from the selected PDF figure; "
                "1 is youngest/top."
            )
            evidence_rows.extend([
                    {
                        "evidence_id": f"cv_{unit_id}_{membership['column_id']}",
                        "unit_id": unit_id,
                        "column_id": membership["column_id"],
                        "field": "column_id",
                        "candidate": membership["column_id"],
                        "source_type": "PDF",
                        "source_file": source_pdf,
                        "source_locator": source_locator,
                        "matched_sentence": body_evidence.get("matched_alias") if body_evidence else None,
                        "full_context_quote": context_quote,
                        "confidence_class": "C",
                        "explicit": False,
                        "selection": "candidate",
                        "extraction_method": extraction_method,
                    },
                    {
                        "evidence_id": f"cv_{unit_id}_{membership['column_id']}_order",
                        "unit_id": unit_id,
                        "column_id": membership["column_id"],
                        "field": "sort_order",
                        "candidate": membership["sort_order"],
                        "source_type": "PDF",
                        "source_file": source_pdf,
                        "source_locator": order_locator,
                        "matched_sentence": None,
                        "full_context_quote": order_context,
                        "confidence_class": "C",
                        "explicit": False,
                        "selection": "candidate",
                        "extraction_method": extraction_method,
                    },
            ])
        age = proposal_unit.get("age") if isinstance(proposal_unit.get("age"), Mapping) else {}
        age_locator = (
            "Official GSJ stratigraphic legend/time-axis bracket"
            if source_figure and "_L1" in Path(source_figure).name
            else "Selected PDF stratigraphic figure/time-axis bracket"
        )
        for field in ("t_int", "b_int"):
            candidate = age.get(field)
            if candidate in (None, ""):
                continue
            visual_label = str(age.get("visual_label") or candidate).strip()
            evidence_rows.append({
                "evidence_id": f"cv_{unit_id}_{field}",
                "unit_id": unit_id,
                "scope_type": "unit_global",
                "field": field,
                "candidate": candidate,
                "source_type": "Vision",
                "source_file": source_figure or source_pdf,
                "source_locator": age_locator,
                "matched_sentence": None,
                "full_context_quote": f"Visual bracket label: {visual_label}",
                "confidence_class": "C",
                "explicit": False,
                "selection": "candidate",
                "extraction_method": "cached multimodal Column/age proposal; controlled interval validation",
            })

    # 提案に含まれなかったユニットを落とさない。
    #
    # ここで捨てると Excel から地層が消えるだけで、人は気づけない。
    # unassigned Column に残し、status と comments で警告する。
    # 提出時は export_submission がこの Column をエラーにして止める。
    proposed_ids = {str(item.get("unit_id") or "") for item in proposal_units}
    unassigned_rows: list[dict[str, Any]] = []
    for original in _rows(bundle, "units", "unit_rows"):
        unit_id = str(original.get("unit_id") or "")
        if not unit_id or unit_id in proposed_ids:
            continue
        row = dict(original)
        row["column_id"] = UNASSIGNED_COLUMN_ID
        row["status"] = "CHECK"
        existing = str(row.get("comments") or "").strip()
        row["comments"] = " ".join(part for part in (UNASSIGNED_COMMENT, existing) if part)
        unassigned_rows.append(row)

    if unassigned_rows and preserve_unassigned:
        units.extend(unassigned_rows)
        columns.append({
            "col_id": UNASSIGNED_COLUMN_ID,
            "col_name": "UNASSIGNED - Column 未割当（要確認）",
            "region_basis": (
                "層序図から Column を判定できなかったユニットの置き場。"
                "地理的な実体ではないので、提出前に必ず割り当て直すこと。"
            ),
            "status": "CHECK",
            "comments": (
                f"{len(unassigned_rows)} 件が Column 未割当です。"
                "提出前に正しい Column へ移してください。"
                "この Column が残っていると提出前チェックがエラーで止まります。"
            ),
            "ref_ids": default_ref,
            "col_type": "column",
            "axis_type": "age",
        })
    elif not preserve_unassigned:
        # Source-summary mode deliberately emits only units represented by the
        # chart.  Remove unit-scoped evidence for omitted bootstrap details so
        # the canonical layer has no orphaned evidence.  The complete omission
        # list remains in column_proposal.json for audit/review.
        evidence_rows = [
            record for record in evidence_rows
            if not str(record.get("unit_id") or "")
            or str(record.get("unit_id") or "") in proposed_ids
        ]

    project = (bundle.get("review_v2_input") or {}).get("project") or {}
    if str(project.get("unit_inventory_source") or "").startswith("PDF"):
        units, evidence_rows, _id_map = consolidate_pdf_formations(units, evidence_rows)

    # Display order is independent of immutable IDs.  Rows with the youngest
    # rank in any supplied Column appear first; ties retain proposal order.
    def display_rank(item: Mapping[str, Any]) -> tuple[float, str]:
        numbers = []
        for value in str(item.get("sort_order") or "").split(","):
            try:
                numbers.append(float(value.strip()))
            except (TypeError, ValueError):
                pass
        return (min(numbers) if numbers else float("inf"), str(item.get("unit_id") or ""))

    units.sort(key=display_rank)

    for image in bundle.get("images") or []:
        image["col_ids"] = ", ".join(column["col_id"] for column in columns)
    return _rebuild(
        bundle,
        units,
        columns,
        evidence_rows,
        {
            "column_split_status": "candidate_review",
            "column_assignment_basis": "Validated cached PDF stratigraphic-figure proposal",
        },
    )


def _major_codes(bundle: Mapping[str, Any]) -> dict[str, str]:
    output: dict[str, str] = {}
    canonical = bundle.get("evidence")
    records = canonical.get("evidence") if isinstance(canonical, Mapping) else []
    for record in records or []:
        if not isinstance(record, Mapping):
            continue
        source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
        if str(source.get("type") or "").casefold() != "shapefile":
            continue
        match = re.search(r"\bMAJOR_CODE\s*=\s*([^;,\s]+)", str(source.get("locator") or ""), re.I)
        unit_id = str(record.get("unit_id") or "")
        if match and unit_id:
            output.setdefault(unit_id, match.group(1))
    return output


def _assignments(bundle: Mapping[str, Any]) -> list[UnitAssignment]:
    codes = _major_codes(bundle)
    output: list[UnitAssignment] = []
    for index, row in enumerate(_rows(bundle, "units", "unit_rows"), start=1):
        unit_id = str(row.get("unit_id") or "")
        columns = tuple(split_column_ids(row.get("column_id")))
        if columns:
            output.append(UnitAssignment(
                unit_id=unit_id,
                columns=columns,
                unit_name=str(row.get("unit_name") or ""),
                reference_name=str(row.get("source_unit_name_en") or ""),
                major_code=codes.get(unit_id, ""),
                row_number=index,
            ))
    return output


def _direction_score(direction: str, point: tuple[float, float], bbox: tuple[float, float, float, float]) -> float:
    xmin, ymin, xmax, ymax = bbox
    x = (point[0] - xmin) / max(xmax - xmin, 1e-12)
    y = (point[1] - ymin) / max(ymax - ymin, 1e-12)
    # Direction means a regional zone, not the most extreme map edge.  Targets
    # at 25/75 percent prefer an interior point that remains representative of
    # the named half of the sheet.
    proximity = lambda value, target: max(0.0, 1.0 - abs(value - target) / 0.5)
    values = {
        "west": proximity(x, 0.25),
        "east": proximity(x, 0.75),
        "north": proximity(y, 0.75),
        "south": proximity(y, 0.25),
        "northwest": (proximity(x, 0.25) + proximity(y, 0.75)) / 2,
        "northeast": (proximity(x, 0.75) + proximity(y, 0.75)) / 2,
        "southwest": (proximity(x, 0.25) + proximity(y, 0.25)) / 2,
        "southeast": (proximity(x, 0.75) + proximity(y, 0.25)) / 2,
        "central": 1 - min(1.0, math.hypot(x - 0.5, y - 0.5) / 0.7071),
    }
    return values.get(direction, 0.0)


def _distance_km(first: tuple[float, float], second: tuple[float, float]) -> float:
    longitude_scale = math.cos(math.radians((first[1] + second[1]) / 2))
    dx = (first[0] - second[0]) * 111.32 * longitude_scale
    dy = (first[1] - second[1]) * 110.57
    return math.hypot(dx, dy)


def _proposal_by_column(proposal: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("column_id") or ""): dict(row)
        for row in proposal.get("columns") or []
        if isinstance(row, Mapping) and str(row.get("column_id") or "")
    }


def complete_missing_assignments_from_shape(
    bundle: Mapping[str, Any],
    proposal: Mapping[str, Any],
    *,
    shape_path: str | os.PathLike[str] | None,
    body_matches: Sequence[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Complete figure-omitted units from Shape proximity without another LLM call.

    Some GSJ summary figures intentionally omit surficial units such as talus.
    Accepted figure memberships seed the Columns.  Each omitted unit's Shape
    components are then assigned to the nearest seed, and its existing source
    sort order is retained.  The completion remains a C-confidence candidate.
    """

    completed = json.loads(json.dumps(proposal, ensure_ascii=False))
    shape = Path(shape_path).expanduser().resolve() if shape_path else None
    if shape is None or not shape.is_file() or not completed.get("columns"):
        return completed, []
    inventory = {
        str(row.get("unit_id") or ""): row
        for row in _rows(bundle, "units", "unit_rows")
    }
    accepted = {
        str(row.get("unit_id") or ""): row
        for row in completed.get("units") or []
        if isinstance(row, Mapping) and row.get("memberships")
    }
    missing = [unit_id for unit_id in inventory if unit_id not in accepted]
    if not missing:
        return completed, []
    body_by_unit = {
        str(row.get("unit_id") or ""): dict(row)
        for row in body_matches
        if isinstance(row, Mapping) and str(row.get("unit_id") or "")
    }

    codes = _major_codes(bundle)
    assignments = []
    for unit_id, row in accepted.items():
        memberships = row.get("memberships") or []
        assignments.append(UnitAssignment(
            unit_id=unit_id,
            columns=tuple(str(item.get("column_id") or "") for item in memberships),
            unit_name=str(inventory.get(unit_id, {}).get("unit_name") or ""),
            major_code=codes.get(unit_id, ""),
        ))
    records, bbox = read_polygon_shapefile(shape)
    _attach_attributes(records, shape.with_suffix(".dbf"))
    record_columns, _matched, _unmatched, _warnings = _match_assignments(records, assignments)
    components = _build_components(records, record_columns)
    definitions = [ColumnDefinition(
        col_id=str(row.get("column_id") or ""),
        name=str(row.get("column_name") or row.get("column_id") or ""),
    ) for row in completed.get("columns") or []]
    anchors = _assign_components(components, definitions, bbox)
    latitude = (bbox[1] + bbox[3]) / 2
    x_scale = max(0.1, math.cos(math.radians(latitude)))
    completions: list[dict[str, Any]] = []
    for unit_id in missing:
        source = inventory[unit_id]
        code = codes.get(unit_id, "")
        source_name = str(source.get("unit_name") or "").strip().casefold()
        record_ids = {
            record.record_index
            for record in records
            if (code and _record_code(record) == code)
            or (source_name and _record_name(record).strip().casefold() == source_name)
        }
        source_components = [item for item in components if item.record_index in record_ids]
        inferred_columns: list[str] = []
        for component in source_components:
            point = _component_point(component)
            if not anchors:
                continue
            column_id = min(
                anchors,
                key=lambda candidate: (
                    ((point[0] - anchors[candidate][0]) * x_scale) ** 2
                    + (point[1] - anchors[candidate][1]) ** 2
                ),
            )
            if column_id not in inferred_columns:
                inferred_columns.append(column_id)
        if not inferred_columns:
            continue
        try:
            sort_order = int(source.get("sort_order"))
        except (TypeError, ValueError):
            sort_order = 1
        completed_unit = {
            "unit_id": unit_id,
            "unit_name": str(source.get("unit_name") or ""),
            "memberships": [
                {"column_id": column_id, "sort_order": sort_order}
                for column_id in inferred_columns
            ],
            "confidence": "C",
            "completion_method": "Shape components nearest to PDF-derived Column seeds",
        }
        if unit_id in body_by_unit:
            completed_unit["body_evidence"] = body_by_unit[unit_id]
            completed_unit["completion_method"] = (
                "Japanese body distribution plus Shape components nearest to "
                "PDF-derived Column seeds"
            )
        completed.setdefault("units", []).append(completed_unit)
        completions.append({
            "unit_id": unit_id,
            "column_ids": inferred_columns,
            "sort_order": sort_order,
            "component_count": len(source_components),
            "japanese_body_matched": unit_id in body_by_unit,
        })

    covered = {
        str(row.get("unit_id") or "")
        for row in completed.get("units") or []
        if row.get("memberships")
    }
    # 全ユニットの網羅は採用条件にしない（llm_column_vision と同じ理由）。
    # 割り当てられた分は活かし、残りは unassigned として Excel に警告付きで残す。
    unassigned = sorted(set(inventory) - covered)
    ready = bool(covered)
    completed["assignment_ready"] = ready
    completed["status"] = "candidate_review" if ready else "rejected_review_required"
    completed["unassigned_units"] = unassigned
    validation = completed.setdefault("validation", {})
    validation["matched_units"] = len(covered)
    validation["unassigned_units"] = len(unassigned)
    validation["shape_fallback_units"] = len(completions)
    return completed, completions


def append_candidate_evidence(
    bundle: dict[str, Any],
    additions: Sequence[Mapping[str, Any]],
    *,
    metadata_updates: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append deduplicated review evidence without changing reviewed values."""

    units = _rows(bundle, "units", "unit_rows")
    columns = _rows(bundle, "columns", "column_rows")
    evidence_rows = _rows(bundle, "source_evidence", "evidence_rows")
    existing_ids = {
        str(row.get("evidence_id") or "") for row in evidence_rows if row.get("evidence_id")
    }
    for row in additions:
        if not isinstance(row, Mapping):
            continue
        item = dict(row)
        evidence_id = str(item.get("evidence_id") or "")
        if evidence_id and evidence_id in existing_ids:
            continue
        evidence_rows.append(item)
        if evidence_id:
            existing_ids.add(evidence_id)
    return _rebuild(
        bundle,
        units,
        columns,
        evidence_rows,
        metadata_updates or {},
    )


def select_representative_points(
    bundle: dict[str, Any],
    proposal: Mapping[str, Any],
    *,
    shape_path: str | os.PathLike[str] | None,
    output_dir: str | os.PathLike[str],
    geocode_context: str,
    map_bbox: tuple[float, float, float, float] | None = None,
    geocode_fetch_json: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Select one primary point plus alternatives for each Column."""

    destination = Path(output_dir).expanduser().resolve()
    columns = _rows(bundle, "columns", "column_rows")
    evidence_rows = _rows(bundle, "source_evidence", "evidence_rows")
    units = _rows(bundle, "units", "unit_rows")
    proposal_columns = _proposal_by_column(proposal)
    place_names = [
        str(constraint.get("place_name") or "")
        for column in proposal_columns.values()
        for constraint in column.get("constraints") or []
        if isinstance(constraint, Mapping) and constraint.get("place_name")
    ]

    components = []
    bbox = map_bbox
    shape_warnings: list[str] = []
    if shape_path is not None and Path(shape_path).is_file():
        shape = Path(shape_path).resolve()
        records, bbox = read_polygon_shapefile(shape)
        _attach_attributes(records, shape.with_suffix(".dbf"))
        definitions = [ColumnDefinition(
            col_id=str(row.get("col_id") or ""),
            name=str(row.get("col_name") or row.get("col_id") or ""),
            lat=float(row["lat"]) if row.get("lat") not in (None, "") else None,
            lng=float(row["lng"]) if row.get("lng") not in (None, "") else None,
        ) for row in columns]
        record_columns, _matched, _unmatched, shape_warnings = _match_assignments(records, _assignments(bundle))
        components = _build_components(records, record_columns)
        _assign_components(components, definitions, bbox)

    geocodes = resolve_place_names(
        place_names,
        context=geocode_context,
        bbox=bbox,
        cache_path=destination / "geocode_cache.json",
        fetch_json=geocode_fetch_json,
    ) if place_names else []
    anchors = {
        str(row["name"]).casefold(): row.get("selected")
        for row in geocodes if row.get("selected")
    }

    result_columns: list[dict[str, Any]] = []
    verified_column_ids: list[str] = []
    for column in columns:
        column_id = str(column.get("col_id") or "")
        geography = proposal_columns.get(column_id, {})
        constraints = geography.get("constraints") if isinstance(geography.get("constraints"), list) else []
        assigned = [item for item in components if item.assigned_column == column_id]
        largest_area = max((item.area for item in assigned), default=1.0)
        candidates: list[dict[str, Any]] = []
        for component in assigned:
            point = _component_point(component)
            score = 2.0 + min(1.0, component.area / max(largest_area, 1e-20))
            reasons = ["inside assigned GSJ Shape polygon"]
            if component.assignment_method == "exclusive unit":
                score += 1.0
                reasons.append("exclusive-unit polygon")
            for constraint in constraints:
                kind = str(constraint.get("kind") or "")
                direction = str(constraint.get("direction") or "")
                place_name = str(constraint.get("place_name") or "")
                if kind == "map_direction" and bbox and direction:
                    contribution = 4.0 * _direction_score(direction, point, bbox)
                    score += contribution
                    reasons.append(f"PDF map direction {direction}: +{contribution:.2f}")
                anchor = anchors.get(place_name.casefold()) if place_name else None
                if anchor:
                    anchor_point = (float(anchor["lng"]), float(anchor["lat"]))
                    if kind in {"near_place", "along_feature", "margin_of"}:
                        distance = _distance_km(point, anchor_point)
                        contribution = 3.0 * max(0.0, 1.0 - distance / 35.0)
                        score += contribution
                        reasons.append(f"{distance:.1f} km from {place_name}: +{contribution:.2f}")
                    if kind == "relative_to_place" and direction and bbox:
                        relative_bbox = (
                            anchor_point[0] - (bbox[2] - bbox[0]),
                            anchor_point[1] - (bbox[3] - bbox[1]),
                            anchor_point[0] + (bbox[2] - bbox[0]),
                            anchor_point[1] + (bbox[3] - bbox[1]),
                        )
                        contribution = 3.0 * _direction_score(direction, point, relative_bbox)
                        score += contribution
                        reasons.append(f"PDF direction {direction} of {place_name}: +{contribution:.2f}")
            candidates.append({
                "lng": round(point[0], 8),
                "lat": round(point[1], 8),
                "score": round(score, 4),
                "inside_assigned_region": point_in_component(point, component),
                "major_code": component.major_code,
                "unit_name": component.unit_name,
                "assignment_method": component.assignment_method,
                "reasons": reasons,
            })
        candidates.sort(key=lambda row: (-row["score"], -bool(row["inside_assigned_region"]), row["lng"], row["lat"]))

        selected = candidates[0] if candidates else None
        coordinate_verified = bool(
            selected
            and selected.get("inside_assigned_region")
            and shape_path is not None
            and Path(shape_path).is_file()
        )
        method = "PDF-constrained GSJ Shape interior point"
        if selected is None:
            named_anchors = [
                anchors.get(str(constraint.get("place_name") or "").casefold())
                for constraint in constraints
                if constraint.get("place_name")
            ]
            named_anchors = [anchor for anchor in named_anchors if anchor]
            if named_anchors:
                anchor = named_anchors[0]
                selected = {
                    "lng": round(float(anchor["lng"]), 8),
                    "lat": round(float(anchor["lat"]), 8),
                    "score": float(anchor.get("score") or 0),
                    "inside_assigned_region": False,
                    "major_code": None,
                    "unit_name": None,
                    "assignment_method": "geocoded named-place fallback",
                    "reasons": ["No assigned Shape polygon; human verification required"],
                }
                candidates = [selected]
                method = "Geocoded PDF place-name fallback; no Shape validation"
        if (
            selected is None
            and bbox is not None
            and column_id != UNASSIGNED_COLUMN_ID
        ):
            directions = [
                str(constraint.get("direction") or "")
                for constraint in constraints
                if str(constraint.get("kind") or "") == "map_direction"
            ]
            direction = next((value for value in directions if value), "central")
            targets = {
                "west": (0.25, 0.50), "east": (0.75, 0.50),
                "north": (0.50, 0.75), "south": (0.50, 0.25),
                "northwest": (0.25, 0.75), "northeast": (0.75, 0.75),
                "southwest": (0.25, 0.25), "southeast": (0.75, 0.25),
                "central": (0.50, 0.50),
            }
            x_fraction, y_fraction = targets.get(direction, targets["central"])
            xmin, ymin, xmax, ymax = bbox
            selected = {
                "lng": round(xmin + (xmax - xmin) * x_fraction, 8),
                "lat": round(ymin + (ymax - ymin) * y_fraction, 8),
                "score": 0.0,
                "inside_assigned_region": False,
                "major_code": None,
                "unit_name": None,
                "assignment_method": "PDF-direction anchor in GSJ map extent",
                "reasons": [
                    f"Source summary figure assigns this Column to the {direction} map area"
                ],
            }
            candidates = [selected]
            # A Column defined by an explicit regional header (Western,
            # Central, Eastern) does not claim a sampled outcrop.  Its
            # representative coordinate only needs to be demonstrably inside
            # the corresponding directional part of the official map extent.
            # This is source-verifiable even without a polygon Shapefile and
            # avoids presenting an arbitrary geocode as geological geometry.
            expected_direction = {
                "western": "west",
                "central": "central",
                "eastern": "east",
            }.get(column_id.casefold())
            coordinate_verified = bool(
                geography.get("quote_verified") is True
                and int(geography.get("figure_pdf_page") or 0) > 0
                and expected_direction == direction
            )
            method = (
                "PDF regional-header direction anchored inside the official "
                "GSJ georeferenced map extent"
            )

        quote = str(geography.get("region_quote") or "").strip()
        description = str(geography.get("region_description") or "").strip()
        if description or quote:
            column["region_basis"] = " ".join(part for part in (description, f'PDF quote: "{quote}"' if quote else "") if part)
        if selected is not None:
            column["lng"] = selected["lng"]
            column["lat"] = selected["lat"]
            column["status"] = "VERIFIED" if coordinate_verified else "CHECK"
            column["coordinate_evidence"] = (
                f"{method}. Selected score {selected['score']:.4f}. "
                + " ".join(selected["reasons"])
            )
            column["comments"] = (
                "Representative regional anchor verified against the PDF summary "
                "header and official GSJ map extent; it is not an outcrop locality."
                if coordinate_verified else
                "Representative point is a machine candidate; confirm with PDF and KML."
            )
            if coordinate_verified:
                verified_column_ids.append(column_id)
        result_columns.append({
            "col_id": column_id,
            "col_name": column.get("col_name"),
            "region_quote": quote,
            "constraints": constraints,
            "selected": selected,
            "alternatives": candidates[1:3],
            "candidate_count": len(candidates),
            "coordinate_verified": coordinate_verified,
        })

    bundle = _rebuild(
        bundle,
        units,
        columns,
        evidence_rows,
        {
            "coordinate_selection": "PDF geographic evidence constrained to GSJ Shape interior points",
            "coordinate_review_status": (
                "VERIFIED" if len(verified_column_ids) == len(columns) else "CHECK"
            ),
        },
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": (
            "verified" if len(verified_column_ids) == len(columns) else "candidate_review"
        ),
        "shape": str(Path(shape_path).resolve()) if shape_path else None,
        "shape_warnings": shape_warnings,
        "geocode_context": geocode_context,
        "geocodes": geocodes,
        "columns": result_columns,
    }
    _atomic_json(destination / "coordinate_candidates.json", manifest)
    return bundle, manifest
