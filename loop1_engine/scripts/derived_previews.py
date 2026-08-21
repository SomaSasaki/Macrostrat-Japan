# -*- coding: utf-8 -*-
"""Build reference-only Macrostrat position and age-property previews.

The durable reviewed values remain untouched.  Previews are attached to each
compiled unit under ``derived`` and are recalculated again during submission
export.  Shared units are expanded per Column before position fields are
calculated, then compressed only for display.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from common import (
        derive_positions, derive_sections, derive_t_pos, props_from_ages,
        split_aligned_values,
    )
except ImportError:  # pragma: no cover - package-style import
    from .common import (
        derive_positions, derive_sections, derive_t_pos, props_from_ages,
        split_aligned_values,
    )


SCHEMA_VERSION = "derived-previews/1.0"
DERIVED_FIELDS = ("position", "section_id", "t_pos", "t_prop", "b_prop")


def _number(value: Any) -> float | int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _columns(unit: Mapping[str, Any]) -> list[str]:
    values = [str(value).strip() for value in unit.get("column_ids") or [] if str(value).strip()]
    if values:
        return values
    raw = (unit.get("values") or {}).get("column_id")
    return [value.strip() for value in str(raw or "").split(",") if value.strip()]


def _compress(per_column: Mapping[str, Any]) -> Any:
    values = list(per_column.values())
    if not values or all(value in (None, "") for value in values):
        return None
    if len(values) == 1:
        return values[0]
    # The mapping insertion order is the unit's column_id order.  Preserve
    # blanks so every derived value remains 1:1 aligned with that list.
    return ", ".join("" if value is None else str(value) for value in values)


def _dependency_hash(dependencies: Mapping[str, Any]) -> str:
    payload = json.dumps(dependencies, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_derived_previews(compiled: Mapping[str, Any]) -> dict[str, Any]:
    """Return a sidecar document without mutating ``compiled``."""
    units = [unit for unit in compiled.get("units") or [] if isinstance(unit, Mapping)]
    memberships = [_columns(unit) for unit in units]
    columns = list(dict.fromkeys(column for values in memberships for column in values))
    per_row: list[dict[str, dict[str, Any]]] = [dict() for _ in units]

    for column in columns:
        indexes = [index for index, values in enumerate(memberships) if column in values]
        sorts = []
        for index in indexes:
            values = units[index].get("values") or {}
            aligned = split_aligned_values(values.get("sort_order"), len(memberships[index]))
            sorts.append(_number(aligned[memberships[index].index(column)]))
        positions = derive_positions(sorts)
        top_positions = derive_t_pos(positions)
        bounds = [
            (
                _number((units[index].get("values") or {}).get("b_age_ma")),
                _number((units[index].get("values") or {}).get("t_age_ma")),
            )
            for index in indexes
        ]
        sections = derive_sections(bounds)
        for local_index, row_index in enumerate(indexes):
            per_row[row_index][column] = {
                "position": positions[local_index],
                "section_id": sections[local_index],
                "t_pos": top_positions[local_index],
            }

    output_rows: list[dict[str, Any]] = []
    for index, unit in enumerate(units):
        values = unit.get("values") or {}
        b_prop, t_prop, is_event = props_from_ages(
            values.get("unit_name"), values.get("t_int"), values.get("b_int"),
            values.get("t_age_ma"), values.get("b_age_ma"),
            values.get("strat_name"), values.get("unit_description"),
        )
        by_column = per_row[index]
        derived = {
            "position": _compress({key: value.get("position") for key, value in by_column.items()}),
            "section_id": _compress({key: value.get("section_id") for key, value in by_column.items()}),
            "t_pos": _compress({key: value.get("t_pos") for key, value in by_column.items()}),
            "t_prop": t_prop,
            "b_prop": b_prop,
        }
        dependencies = {
            "column_ids": memberships[index],
            "sort_order": values.get("sort_order"),
            "unit_name": values.get("unit_name"),
            "strat_name": values.get("strat_name"),
            "t_int": values.get("t_int"),
            "b_int": values.get("b_int"),
            "t_age_ma": values.get("t_age_ma"),
            "b_age_ma": values.get("b_age_ma"),
        }
        missing_reasons = {
            field: "insufficient_evidence"
            for field, value in derived.items()
            if value in (None, "")
        }
        output_rows.append({
            "row_key": unit.get("row_key"),
            "unit_id": unit.get("unit_id"),
            "column_ids": memberships[index],
            "derived": derived,
            "derived_by_column": by_column,
            "dependencies": dependencies,
            "dependency_sha256": _dependency_hash(dependencies),
            "event_age_bracket": bool(is_event),
            "missing_reasons": missing_reasons,
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "map_id": str((compiled.get("map") or {}).get("map_id") or ""),
        "generated_at": compiled.get("generated_at"),
        "rows": output_rows,
    }


def attach_derived_previews(compiled: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a copied compiled document with preview objects attached."""
    result = json.loads(json.dumps(compiled, ensure_ascii=False))
    sidecar = build_derived_previews(result)
    preview_by_key = {str(row.get("row_key")): row for row in sidecar["rows"]}
    for unit in result.get("units") or []:
        preview = preview_by_key.get(str(unit.get("row_key")))
        if preview is None:
            continue
        unit["derived"] = preview["derived"]
        unit["derived_by_column"] = preview["derived_by_column"]
        unit["derived_dependencies"] = preview["dependencies"]
        unit["derived_dependency_sha256"] = preview["dependency_sha256"]
        unit["derived_missing_reasons"] = preview["missing_reasons"]
    result.setdefault("summary", {})["derived_preview_rows"] = len(sidecar["rows"])
    return result, sidecar


def write_derived_previews(directory: str | Path) -> dict[str, Any]:
    """Attach previews to ``compiled.json`` and write the sidecar atomically."""
    root = Path(directory).expanduser().resolve()
    compiled_path = root / "compiled.json"
    compiled = json.loads(compiled_path.read_text(encoding="utf-8"))
    enriched, sidecar = attach_derived_previews(compiled)
    for path, document in (
        (compiled_path, enriched),
        (root / "derived_previews.json", sidecar),
    ):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    return sidecar


__all__ = [
    "DERIVED_FIELDS",
    "SCHEMA_VERSION",
    "attach_derived_previews",
    "build_derived_previews",
    "write_derived_previews",
]
