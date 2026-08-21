# -*- coding: utf-8 -*-
"""Consolidate PDF-only Formation rows without changing stable unit IDs.

The PDF bootstrap creates immutable ``unit_id`` values before Column Vision
runs.  A shared Formation must therefore be represented by one review row
whose per-Column values are aligned with ``column_id``.  If an older cache did
produce multiple IDs for the same normalized PDF name, the first ID remains
canonical and evidence is rebound while retaining ``source_unit_id``.

This module is deliberately restricted to callers that have already decided
the inventory is PDF-only.  Structured ZFK/Shape inventories can contain
legitimate same-name facies and must not be collapsed by name alone.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Sequence


PER_COLUMN_FIELDS = (
    "sort_order",
    "min_thickness",
    "max_thickness",
    "section_id",
    "t_pos",
)


def formation_key(value: Any) -> str:
    """Return a stable comparison key for a PDF inventory name."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.casefold().replace("\u00a0", " ")
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split())


def _split_columns(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _aligned(value: Any, count: int) -> list[Any]:
    if count <= 0:
        return []
    if isinstance(value, (list, tuple)):
        parts = list(value)
    elif count > 1 and isinstance(value, str) and "," in value:
        # Empty elements are significant: ``"4, "`` aligns to two Columns.
        parts = [part.strip() for part in value.split(",")]
    else:
        parts = [value]
    if len(parts) == count:
        return parts
    if len(parts) == 1:
        return parts * count
    return [value] * count


def _display(values: Sequence[Any]) -> Any:
    if len(values) == 1:
        return values[0]
    return ", ".join("" if value is None else str(value).strip() for value in values)


def consolidate_pdf_formations(
    rows: Sequence[Mapping[str, Any]],
    evidence_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """Return one row per normalized PDF Formation plus rebound evidence.

    The first encountered ID is immutable and becomes the canonical ID.  The
    returned mapping records every old ID to its canonical ID.
    """

    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for raw in rows:
        row = dict(raw)
        unit_id = str(row.get("unit_id") or "").strip()
        key = formation_key(row.get("unit_name") or row.get("REF_unit_name_en"))
        # A blank name can never justify a cross-ID merge.  Duplicate rows of
        # the same stable ID are still safe to consolidate.
        group_key = key or f"__unit_id__:{unit_id}"
        if group_key not in grouped:
            grouped[group_key] = []
            order.append(group_key)
        grouped[group_key].append(row)

    result: list[dict[str, Any]] = []
    id_map: dict[str, str] = {}
    merged_source_ids: set[str] = set()
    for key in order:
        items = grouped[key]
        base = dict(items[0])
        canonical_id = str(base.get("unit_id") or "").strip()
        source_ids: list[str] = []
        if len(items) > 1:
            merged_source_ids.update(
                str(row.get("unit_id") or "").strip() for row in items
                if str(row.get("unit_id") or "").strip()
            )
        columns: list[str] = []
        per_column: dict[str, dict[str, Any]] = {}
        conflicts: list[str] = []

        for row in items:
            source_id = str(row.get("unit_id") or "").strip()
            if source_id and source_id not in source_ids:
                source_ids.append(source_id)
            if source_id:
                id_map[source_id] = canonical_id
            memberships = _split_columns(row.get("column_id"))
            aligned = {
                field: _aligned(row.get(field), len(memberships))
                for field in PER_COLUMN_FIELDS
            }
            for index, column in enumerate(memberships):
                if column not in columns:
                    columns.append(column)
                    per_column[column] = {}
                target = per_column[column]
                for field in PER_COLUMN_FIELDS:
                    value = aligned[field][index]
                    if value in (None, ""):
                        continue
                    previous = target.get(field)
                    if previous in (None, ""):
                        target[field] = value
                    elif str(previous).strip() != str(value).strip():
                        conflicts.append(f"{column}.{field}: {previous} vs {value}")

            for field, value in row.items():
                if field in {"unit_id", "column_id", *PER_COLUMN_FIELDS}:
                    continue
                if base.get(field) in (None, "") and value not in (None, ""):
                    base[field] = value

        base["unit_id"] = canonical_id
        base["formation_key"] = key
        base["source_unit_ids"] = ", ".join(source_ids or [canonical_id])
        base["column_id"] = ", ".join(columns)
        for field in PER_COLUMN_FIELDS:
            base[field] = _display([per_column[column].get(field) for column in columns])
        if conflicts:
            note = "Column-aligned source conflicts require review: " + "; ".join(conflicts)
            base["comments"] = " ".join(
                value for value in (str(base.get("comments") or "").strip(), note) if value
            )
        result.append(base)

    rebound: list[dict[str, Any]] = []
    for raw in evidence_rows:
        record = dict(raw)
        old_id = str(record.get("unit_id") or record.get("source_unit_id") or "").strip()
        canonical_id = id_map.get(old_id, old_id)
        if old_id and canonical_id and (old_id != canonical_id or old_id in merged_source_ids):
            if old_id != canonical_id:
                record["source_unit_id"] = old_id
            record["unit_id"] = canonical_id
            # A row key contains the pre-consolidation Column tuple.  Keep the
            # explicit Column scope but let the canonical builder create the
            # new row key, otherwise valid evidence becomes orphaned.
            record.pop("row_key", None)
        rebound.append(record)
    return result, rebound, id_map


__all__ = ["PER_COLUMN_FIELDS", "consolidate_pdf_formations", "formation_key"]
