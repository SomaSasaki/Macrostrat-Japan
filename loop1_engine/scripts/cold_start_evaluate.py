# -*- coding: utf-8 -*-
"""Evaluate a completed cold-start inventory against isolated GOLD.

This module is intentionally separate from ``cold_start`` and ``pilot``.  It
may read GOLD only after a candidate artifact exists, and it rejects candidate
or output paths inside the GOLD snapshot root.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from gold_snapshot import GOLD_SNAPSHOT_ROOT, bound_path
from pdf_unit_bootstrap import _inventory_name_key


EVALUATION_SCHEMA = "cold-start-evaluation/1.0"


class ColdStartEvaluationError(ValueError):
    """The evaluation boundary or candidate document is invalid."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _unit_rows(document: Mapping[str, Any]) -> list[dict[str, str]]:
    raw = document.get("units")
    if not isinstance(raw, list):
        raw = (document.get("review_v2_input") or {}).get("unit_rows")
    if not isinstance(raw, list):
        raise ColdStartEvaluationError("Candidate has no units[] inventory")
    result: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        values = row.get("values") if isinstance(row.get("values"), Mapping) else {}
        unit_id = str(row.get("unit_id") or "").strip()
        unit_name = str(row.get("unit_name") or values.get("unit_name") or "").strip()
        if not unit_id or not unit_name or unit_name == "NO_DATA":
            continue
        if unit_id in seen_ids:
            raise ColdStartEvaluationError(f"Candidate contains duplicate unit_id: {unit_id}")
        seen_ids.add(unit_id)
        result.append({"unit_id": unit_id, "unit_name": unit_name})
    if not result:
        raise ColdStartEvaluationError("Candidate has no reviewable units")
    return result


def _by_identity(rows: Sequence[Mapping[str, str]], label: str) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        key = _inventory_name_key(row["unit_name"])
        if not key:
            raise ColdStartEvaluationError(f"{label} contains a blank semantic identity")
        if key in result:
            raise ColdStartEvaluationError(
                f"{label} contains duplicate semantic unit identity: {row['unit_name']}"
            )
        result[key] = dict(row)
    return result


def evaluate_inventory(candidate_path: Path, fixture_path: Path) -> dict[str, Any]:
    candidate_path = Path(candidate_path).resolve()
    fixture_path = Path(fixture_path).resolve()
    if _inside(candidate_path, GOLD_SNAPSHOT_ROOT):
        raise ColdStartEvaluationError("Candidate must not come from the GOLD snapshot")
    if not candidate_path.is_file():
        raise FileNotFoundError(candidate_path)
    fixture = _read(fixture_path)
    if not isinstance(fixture, Mapping):
        raise ColdStartEvaluationError("GOLD fixture must be an object")
    gold_path = bound_path(fixture, "raw_bundle")
    candidate = _read(candidate_path)
    gold = _read(gold_path)
    if not isinstance(candidate, Mapping) or not isinstance(gold, Mapping):
        raise ColdStartEvaluationError("Candidate and GOLD inventory must be objects")
    candidate_rows = _unit_rows(candidate)
    gold_rows = _unit_rows(gold)
    candidate_by_key = _by_identity(candidate_rows, "candidate")
    gold_by_key = _by_identity(gold_rows, "GOLD")
    candidate_keys = set(candidate_by_key)
    gold_keys = set(gold_by_key)
    matched = candidate_keys & gold_keys
    extra = sorted(candidate_keys - gold_keys)
    missing = sorted(gold_keys - candidate_keys)
    true_positive = len(matched)
    precision = true_positive / len(candidate_keys) if candidate_keys else 0.0
    recall = true_positive / len(gold_keys) if gold_keys else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    cosmetic = sorted(
        ({"candidate": candidate_by_key[key]["unit_name"], "gold": gold_by_key[key]["unit_name"]}
         for key in matched
         if candidate_by_key[key]["unit_name"] != gold_by_key[key]["unit_name"]),
        key=lambda row: (row["gold"].casefold(), row["candidate"].casefold()),
    )
    return {
        "schema_version": EVALUATION_SCHEMA,
        "map_id": str(fixture.get("map_id") or ""),
        "candidate": str(candidate_path),
        "gold_snapshot": str(gold_path),
        "boundary": {
            "gold_used_after_generation_only": True,
            "candidate_outside_gold_snapshot": True,
        },
        "inventory": {
            "candidate_units": len(candidate_keys),
            "gold_units": len(gold_keys),
            "matched_units": true_positive,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "exact_semantic_match": not extra and not missing,
            "extra_units": [candidate_by_key[key] for key in extra],
            "missing_units": [gold_by_key[key] for key in missing],
            "cosmetic_name_variants": cosmetic,
        },
    }


__all__ = [
    "EVALUATION_SCHEMA", "ColdStartEvaluationError", "evaluate_inventory",
]
