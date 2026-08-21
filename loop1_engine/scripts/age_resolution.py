# -*- coding: utf-8 -*-
"""Conservative Formation-age interpolation for canonical PDF inventories.

An unresolved Formation is filled only when its immediately bracketing dated
Formations in every assigned Column carry the same complete interval pair.
The inferred values remain confidence-C evidence and therefore keep the row in
CHECK state.  No numeric ages are invented.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from common import is_blank, split_aligned_values
from compiled_layer import build_canonical_layer, write_canonical_layer
from pilot_llm import _canonical_evidence_row


SCHEMA_VERSION = "age-interpolation/1.0"


def _number(value: Any) -> float | None:
    if is_blank(value):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _pair(unit: Mapping[str, Any]) -> tuple[str, str] | None:
    values = unit.get("values") if isinstance(unit.get("values"), Mapping) else {}
    top = str(values.get("t_int") or "").strip()
    bottom = str(values.get("b_int") or "").strip()
    return (top, bottom) if top and bottom else None


def _same_pair(left: tuple[str, str], right: tuple[str, str]) -> bool:
    return tuple(value.casefold() for value in left) == tuple(value.casefold() for value in right)


def _placements(compiled: Mapping[str, Any]) -> dict[str, list[tuple[float, int]]]:
    result: dict[str, list[tuple[float, int]]] = {}
    for index, unit in enumerate(compiled.get("units") or []):
        columns = [str(value).strip() for value in unit.get("column_ids") or [] if str(value).strip()]
        values = unit.get("values") if isinstance(unit.get("values"), Mapping) else {}
        sorts = split_aligned_values(values.get("sort_order"), len(columns))
        for column, raw_sort in zip(columns, sorts):
            sort = _number(raw_sort)
            if sort is not None:
                result.setdefault(column, []).append((sort, index))
    for rows in result.values():
        rows.sort(key=lambda value: value[0])
    return result


def infer_interval_pairs(compiled: Mapping[str, Any]) -> tuple[dict[int, tuple[str, str]], list[str]]:
    """Return safe interval-pair candidates keyed by compiled unit index."""

    units = compiled.get("units") or []
    placements = _placements(compiled)
    candidates_by_index: dict[int, list[tuple[str, str]]] = {}
    unresolved: list[str] = []

    for index, unit in enumerate(units):
        values = unit.get("values") if isinstance(unit.get("values"), Mapping) else {}
        if not is_blank(values.get("t_int")) and not is_blank(values.get("b_int")):
            continue
        columns = [str(value).strip() for value in unit.get("column_ids") or [] if str(value).strip()]
        sorts = split_aligned_values(values.get("sort_order"), len(columns))
        column_candidates: list[tuple[str, str]] = []
        eligible = bool(columns)
        for column, raw_sort in zip(columns, sorts):
            target_sort = _number(raw_sort)
            if target_sort is None:
                eligible = False
                break
            lower = [item for item in placements.get(column, []) if item[0] < target_sort and _pair(units[item[1]])]
            upper = [item for item in placements.get(column, []) if item[0] > target_sort and _pair(units[item[1]])]
            if not lower or not upper:
                eligible = False
                break
            lower_index = max(lower, key=lambda item: item[0])[1]
            upper_index = min(upper, key=lambda item: item[0])[1]

            # 上下が同じユニットなら、区間が一致するのは当たり前で、
            # 「上下が一致するから安全」という判断が保護として働かない。
            # unit_id が重複していると、同一ユニットが上下の両方に現れて
            # これが起きる。実際に m1286 で第四紀の15層に中新世末の年代が
            # 流し込まれた。
            if lower_index == upper_index:
                eligible = False
                break
            lower_id = str(units[lower_index].get("unit_id") or "")
            upper_id = str(units[upper_index].get("unit_id") or "")
            if lower_id and lower_id == upper_id:
                eligible = False
                break

            lower_pair = _pair(units[lower_index])
            upper_pair = _pair(units[upper_index])
            if lower_pair is None or upper_pair is None or not _same_pair(lower_pair, upper_pair):
                eligible = False
                break
            column_candidates.append(lower_pair)

        if eligible and column_candidates and all(
            _same_pair(column_candidates[0], pair) for pair in column_candidates[1:]
        ):
            candidate = column_candidates[0]
            existing_top = str(values.get("t_int") or "").strip()
            existing_bottom = str(values.get("b_int") or "").strip()
            if ((not existing_top or existing_top.casefold() == candidate[0].casefold())
                    and (not existing_bottom or existing_bottom.casefold() == candidate[1].casefold())):
                candidates_by_index[index] = candidate
                continue
        unresolved.append(str(unit.get("unit_id") or f"row-{index + 1}"))
    return candidates_by_index, unresolved


def _evidence_id(unit_id: str, field: str, candidate: str) -> str:
    raw = f"{SCHEMA_VERSION}|{unit_id}|{field}|{candidate}"
    return "ev_age_interp_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def apply_age_interpolation(
    system_dir: str | os.PathLike[str],
    *,
    generated_at: str,
) -> dict[str, Any]:
    root = Path(system_dir).expanduser().resolve()
    compiled = json.loads((root / "compiled.json").read_text(encoding="utf-8"))
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    inferred, unresolved = infer_interval_pairs(compiled)

    rows: list[dict[str, Any]] = []
    additions: list[dict[str, Any]] = []
    for index, unit in enumerate(compiled.get("units") or []):
        row = dict(unit.get("review_values") or {})
        if unit.get("formulas"):
            row["_formulas"] = dict(unit["formulas"])
        rows.append(row)
        pair = inferred.get(index)
        if pair is None:
            continue
        unit_id = str(unit.get("unit_id") or "")
        values = unit.get("values") if isinstance(unit.get("values"), Mapping) else {}
        for field, candidate in zip(("t_int", "b_int"), pair):
            if not is_blank(values.get(field)):
                continue
            additions.append({
                "evidence_id": _evidence_id(unit_id, field, candidate),
                "unit_id": unit_id,
                "scope_type": "unit_global",
                "field": field,
                "candidate": candidate,
                "source_type": "Derived",
                "source_file": str(root / "compiled.json"),
                "source_locator": "nearest bracketing Formations in every assigned Column",
                "full_context_quote": (
                    "Both immediately bracketing dated Formations have the identical "
                    f"interval pair {pair[0]} / {pair[1]}."
                ),
                "confidence_class": "C",
                "assertion": "inferred",
                "selection": "candidate",
                "extraction_method": SCHEMA_VERSION,
            })

    existing = [_canonical_evidence_row(row) for row in evidence.get("evidence") or []]
    map_doc = compiled.get("map") or {}
    rebuilt, evidence_doc = build_canonical_layer(
        rows,
        column_rows=map_doc.get("columns") or [],
        evidence_rows=[*existing, *additions],
        metadata=map_doc.get("metadata") or {},
        map_id=map_doc.get("map_id"),
        source_review=map_doc.get("source_review"),
        generated_at=generated_at,
    )
    write_canonical_layer(rebuilt, evidence_doc, root)
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": "age_interpolation",
        "status": "complete",
        "external_calls": 0,
        "interpolated_units": len(inferred),
        "added_evidence": len(additions),
        "unresolved_units": unresolved,
    }


__all__ = ["SCHEMA_VERSION", "apply_age_interpolation", "infer_interval_pairs"]
