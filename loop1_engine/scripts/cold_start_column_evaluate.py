# -*- coding: utf-8 -*-
"""Blindly score generated Columns after cold-start generation is complete.

The candidate must live outside the immutable GOLD snapshot.  GOLD is opened
only after that candidate exists.  Column detection, unit membership, and
relative order are reported separately so one failure cannot hide another.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Sequence

from gold_snapshot import GOLD_SNAPSHOT_ROOT, bound_path
from llm_constrained_vision import membership_item_id
from pdf_unit_bootstrap import _inventory_name_key


COLUMN_EVALUATION_SCHEMA = "cold-start-column-evaluation/1.0"


class ColdStartColumnEvaluationError(ValueError):
    """The evaluation boundary, candidate, or GOLD contract is invalid."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ColdStartColumnEvaluationError(f"Cannot read {label}: {path}") from exc
    if not isinstance(document, dict):
        raise ColdStartColumnEvaluationError(f"{label} must be a JSON object")
    return document


def _normalise(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def _column_key(row: Mapping[str, Any]) -> str:
    values = " ".join(str(row.get(key) or "") for key in (
        "column_id", "col_id", "column_name", "col_name", "column_name_ja",
    ))
    normalized = _normalise(values)
    directions = {
        "west": bool(re.search(r"\bwest(?:ern)?\b|西部", normalized)),
        "central": bool(re.search(r"\bcentr(?:al|e)\b|中央部", normalized)),
        "east": bool(re.search(r"\beast(?:ern)?\b|東部", normalized)),
        "north": bool(re.search(r"\bnorth(?:ern)?\b|北部", normalized)),
        "south": bool(re.search(r"\bsouth(?:ern)?\b|南部", normalized)),
    }
    matched = [name for name, present in directions.items() if present]
    if len(matched) == 1:
        return "direction:" + matched[0]
    name = _normalise(row.get("column_name") or row.get("col_name"))
    if not name:
        name = _normalise(row.get("column_id") or row.get("col_id"))
    return "name:" + name


def _column_rows(document: Mapping[str, Any], label: str) -> list[dict[str, Any]]:
    rows = document.get("columns")
    if not isinstance(rows, list) or not rows:
        raise ColdStartColumnEvaluationError(f"{label} has no columns[]")
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    keys: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ColdStartColumnEvaluationError(f"{label} contains a malformed Column")
        column_id = str(raw.get("column_id") or raw.get("col_id") or "").strip()
        column_name = str(raw.get("column_name") or raw.get("col_name") or "").strip()
        if not column_id or not column_name:
            raise ColdStartColumnEvaluationError(
                f"{label} Columns require non-empty IDs and names"
            )
        key = _column_key(raw)
        if column_id in ids or key in keys:
            raise ColdStartColumnEvaluationError(
                f"{label} contains a duplicate Column identity: {column_id}"
            )
        ids.add(column_id)
        keys.add(key)
        result.append({
            "column_id": column_id,
            "column_name": column_name,
            "semantic_key": key,
        })
    return result


def _gold_memberships(compiled: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = compiled.get("units")
    if not isinstance(rows, list):
        raise ColdStartColumnEvaluationError("GOLD compiled layer has no units[]")
    result: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        values = raw.get("review_values")
        if not isinstance(values, Mapping):
            values = raw.get("values")
        if not isinstance(values, Mapping):
            continue
        unit_name = str(values.get("unit_name") or "").strip()
        column_id = str(values.get("column_id") or "").strip()
        if not unit_name or not column_id:
            continue
        semantic_identity = f"{_inventory_name_key(unit_name)}|{column_id}"
        if semantic_identity in result:
            raise ColdStartColumnEvaluationError(
                f"GOLD contains a duplicate membership: {unit_name} / {column_id}"
            )
        order = values.get("sort_order")
        result[semantic_identity] = {
            "semantic_identity": semantic_identity,
            "fixture_style_item_id": membership_item_id(unit_name, column_id),
            "unit_name": unit_name,
            "column_id": column_id,
            "sort_order": int(order) if isinstance(order, (int, float)) else None,
        }
    if not result:
        raise ColdStartColumnEvaluationError("GOLD compiled layer has no memberships")
    return result


def _assert_fixture_memberships(
    fixture: Mapping[str, Any], gold: Mapping[str, Mapping[str, Any]],
) -> None:
    declared_counts = {
        str(case.get("column_id") or ""): len(case.get("expected_items") or [])
        for case in fixture.get("cases") or [] if isinstance(case, Mapping)
    }
    compiled_counts: dict[str, int] = {}
    for row in gold.values():
        column_id = str(row["column_id"])
        compiled_counts[column_id] = compiled_counts.get(column_id, 0) + 1
    if declared_counts != compiled_counts:
        raise ColdStartColumnEvaluationError(
            "GOLD fixture membership contract disagrees with the bound compiled layer"
        )


def _candidate_memberships(
    candidate: Mapping[str, Any],
    candidate_columns: Sequence[Mapping[str, Any]],
    column_map: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    rows = candidate.get("units")
    if not isinstance(rows, list):
        raise ColdStartColumnEvaluationError("Candidate has no units[]")
    declared = {str(row["column_id"]) for row in candidate_columns}
    result: dict[str, dict[str, Any]] = {}
    seen_units: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ColdStartColumnEvaluationError("Candidate contains a malformed unit")
        unit_id = str(raw.get("unit_id") or "").strip()
        unit_name = str(raw.get("unit_name") or "").strip()
        if not unit_id or not unit_name or unit_id in seen_units:
            raise ColdStartColumnEvaluationError(
                f"Candidate contains a blank or duplicate unit: {unit_id}"
            )
        seen_units.add(unit_id)
        memberships = raw.get("memberships")
        if not isinstance(memberships, list):
            raise ColdStartColumnEvaluationError(
                f"Candidate unit has no memberships[]: {unit_id}"
            )
        for membership in memberships:
            if not isinstance(membership, Mapping):
                raise ColdStartColumnEvaluationError(
                    f"Candidate unit has a malformed membership: {unit_id}"
                )
            source_column = str(membership.get("column_id") or "").strip()
            if source_column not in declared:
                raise ColdStartColumnEvaluationError(
                    f"Candidate membership uses an undeclared Column: {source_column}"
                )
            gold_column = column_map.get(source_column)
            scoring_column = gold_column or f"unmatched:{source_column}"
            semantic_identity = f"{_inventory_name_key(unit_name)}|{scoring_column}"
            if semantic_identity in result:
                raise ColdStartColumnEvaluationError(
                    f"Candidate contains a duplicate membership: {unit_name} / {source_column}"
                )
            order = membership.get("sort_order")
            result[semantic_identity] = {
                "semantic_identity": semantic_identity,
                "fixture_style_item_id": membership_item_id(unit_name, scoring_column),
                "unit_id": unit_id,
                "unit_name": unit_name,
                "candidate_column_id": source_column,
                "gold_column_id": gold_column,
                "sort_order": (
                    int(order)
                    if isinstance(order, (int, float)) and int(order) > 0
                    else None
                ),
            }
    return result


def _prf(actual: set[str], expected: set[str]) -> dict[str, Any]:
    matched = actual & expected
    precision = len(matched) / len(actual) if actual else 0.0
    recall = len(matched) / len(expected) if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "actual": len(actual),
        "expected": len(expected),
        "matched": len(matched),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "exact": actual == expected,
    }


def _order_score(
    actual: Mapping[str, Mapping[str, Any]],
    gold: Mapping[str, Mapping[str, Any]],
    column_id: str,
) -> dict[str, Any]:
    common = sorted(
        item_id for item_id in set(actual) & set(gold)
        if gold[item_id]["column_id"] == column_id
        and actual[item_id].get("sort_order") is not None
        and gold[item_id].get("sort_order") is not None
    )
    comparable = 0
    concordant = 0
    for left, right in itertools.combinations(common, 2):
        gold_delta = int(gold[left]["sort_order"]) - int(gold[right]["sort_order"])
        actual_delta = int(actual[left]["sort_order"]) - int(actual[right]["sort_order"])
        if gold_delta == 0 or actual_delta == 0:
            continue
        comparable += 1
        # Review sort_order grows upward/younger; Column Vision rank 1 is the
        # youngest/top.  Their numeric directions are intentionally opposite.
        if (gold_delta > 0) == (actual_delta < 0):
            concordant += 1
    return {
        "shared_memberships": len(common),
        "comparable_pairs": comparable,
        "concordant_pairs": concordant,
        "pairwise_accuracy": concordant / comparable if comparable else None,
        "exact_pairwise_order": comparable > 0 and concordant == comparable,
    }


def evaluate_columns(candidate_path: Path, fixture_path: Path) -> dict[str, Any]:
    candidate_path = Path(candidate_path).resolve()
    fixture_path = Path(fixture_path).resolve()
    if _inside(candidate_path, GOLD_SNAPSHOT_ROOT):
        raise ColdStartColumnEvaluationError("Candidate must not come from the GOLD snapshot")
    if not candidate_path.is_file():
        raise FileNotFoundError(candidate_path)

    # The candidate boundary is validated before any GOLD-bound path is opened.
    candidate = _read_object(candidate_path, "candidate")
    fixture = _read_object(fixture_path, "GOLD fixture")
    if fixture.get("schema_version") != "column-vision-gold-fixture/1.0":
        raise ColdStartColumnEvaluationError("Unsupported Column GOLD fixture")
    expected_columns = _column_rows(
        {"columns": fixture.get("expected_columns")}, "GOLD fixture",
    )
    compiled_path = bound_path(fixture, "compiled")
    gold_memberships = _gold_memberships(_read_object(compiled_path, "GOLD compiled layer"))
    _assert_fixture_memberships(fixture, gold_memberships)

    candidate_columns = _column_rows(candidate, "candidate")
    expected_by_key = {str(row["semantic_key"]): row for row in expected_columns}
    candidate_by_key = {str(row["semantic_key"]): row for row in candidate_columns}
    matched_keys = set(expected_by_key) & set(candidate_by_key)
    column_map = {
        str(candidate_by_key[key]["column_id"]): str(expected_by_key[key]["column_id"])
        for key in matched_keys
    }
    candidate_memberships = _candidate_memberships(candidate, candidate_columns, column_map)

    actual_items = set(candidate_memberships)
    expected_items = set(gold_memberships)
    membership_metrics = _prf(actual_items, expected_items)
    missing = sorted(expected_items - actual_items)
    extra = sorted(actual_items - expected_items)

    per_column: list[dict[str, Any]] = []
    for expected_column in expected_columns:
        column_id = str(expected_column["column_id"])
        expected_set = {
            item_id for item_id, row in gold_memberships.items()
            if row["column_id"] == column_id
        }
        actual_set = {
            item_id for item_id, row in candidate_memberships.items()
            if row.get("gold_column_id") == column_id
        }
        candidate_column = next((
            source for source, target in column_map.items() if target == column_id
        ), None)
        per_column.append({
            "gold_column_id": column_id,
            "candidate_column_id": candidate_column,
            "membership": _prf(actual_set, expected_set),
            "order": _order_score(candidate_memberships, gold_memberships, column_id),
        })

    column_metrics = _prf(set(candidate_by_key), set(expected_by_key))
    column_metrics.update({
        "mapping": [
            {
                "candidate_column_id": source,
                "gold_column_id": target,
                "semantic_key": next(
                    row["semantic_key"] for row in candidate_columns
                    if row["column_id"] == source
                ),
            }
            for source, target in sorted(column_map.items())
        ],
        "extra_columns": [candidate_by_key[key] for key in sorted(set(candidate_by_key) - set(expected_by_key))],
        "missing_columns": [expected_by_key[key] for key in sorted(set(expected_by_key) - set(candidate_by_key))],
    })
    return {
        "schema_version": COLUMN_EVALUATION_SCHEMA,
        "map_id": str(fixture.get("map_id") or ""),
        "candidate": str(candidate_path),
        "gold_snapshot": str(compiled_path),
        "boundary": {
            "gold_used_after_generation_only": True,
            "candidate_validated_before_gold_open": True,
            "candidate_outside_gold_snapshot": True,
        },
        "column_detection": column_metrics,
        "memberships": {
            **membership_metrics,
            "missing": [gold_memberships[item] for item in missing],
            "extra": [candidate_memberships[item] for item in extra],
        },
        "per_column": per_column,
    }


__all__ = [
    "COLUMN_EVALUATION_SCHEMA", "ColdStartColumnEvaluationError", "evaluate_columns",
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate_columns(args.candidate, args.fixture)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.name + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, output)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
