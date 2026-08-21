# -*- coding: utf-8 -*-
"""Small, closed-world multimodal tasks used to qualify backup providers.

The functions in this module deliberately avoid the original all-in-one
Column and Environment prompts.  Every response is limited to supplied IDs
and deterministic candidate values before it can reach a GOLD scorer.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Mapping, Sequence

COLUMN_DETECTION_PROMPT_VERSION = "column-detection-closed-v1"
COLUMN_MEMBERSHIP_PROMPT_VERSION = "column-membership-batched-v2"
ENVIRONMENT_CLASSIFICATION_PROMPT_VERSION = "environment-unit-closed-v1"
CONSTRAINED_VALIDATOR_VERSION = "closed-world-vision-validator-v2"


def _normalise_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode().casefold()
    text = " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())
    # Source inventories use both ``flood-plain`` and ``floodplain`` for the
    # same mapped unit.  Hyphen stripping alone turns the former into two
    # tokens, so canonicalise this established lexical compound explicitly.
    return re.sub(r"\bflood\s+plain\b", "floodplain", text)


def _column_rows(columns: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for row in columns:
        column_id = str(row.get("column_id") or row.get("col_id") or "").strip()
        name = str(row.get("column_name") or row.get("col_name") or "").strip()
        if column_id and name:
            entry = {"column_id": column_id, "column_name": name}
            # 図の見出しは日本語なので、レビュー済みの日本語名があれば併記する。
            # 無い場合は何も足さない（推測で作らない）。
            japanese = str(row.get("column_name_ja") or "").strip()
            if japanese:
                entry["column_name_ja"] = japanese
            rows.append(entry)
    if not rows or len({row["column_id"] for row in rows}) != len(rows):
        raise ValueError("Expected Columns must contain unique non-empty IDs and names")
    return rows


def build_column_detection_prompt(columns: Sequence[Mapping[str, Any]]) -> str:
    supplied = _column_rows(columns)
    return f"""Inspect the attached stratigraphic figure. Decide only whether each
SUPPLIED_COLUMN is visibly represented as a geographic stratigraphic column.
Do not add columns, units, ages, ranks, coordinates, or prose outside JSON.

Return exactly:
{{"columns":[{{"column_id":"supplied ID","present":true}}]}}

Rules:
- Return every supplied column exactly once and preserve its ID.
- present must be true or false, never a string.
- A time period, lithology, formation, diagram panel, or legend is not a Column.

SUPPLIED_COLUMNS:
{json.dumps(supplied, ensure_ascii=False, indent=2)}
"""


def validate_column_detection(
    response: Mapping[str, Any], columns: Sequence[Mapping[str, Any]],
) -> dict[str, bool]:
    expected = {row["column_id"] for row in _column_rows(columns)}
    raw = response.get("columns")
    if not isinstance(raw, list):
        raise ValueError("Column detection requires columns[]")
    result: dict[str, bool] = {}
    for row in raw:
        if not isinstance(row, Mapping):
            raise ValueError("Every detected Column must be an object")
        column_id = str(row.get("column_id") or "").strip()
        present = row.get("present")
        if column_id not in expected or column_id in result or not isinstance(present, bool):
            raise ValueError("Column detection returned an unknown, duplicate, or non-boolean row")
        result[column_id] = present
    if set(result) != expected:
        raise ValueError("Column detection must return every supplied Column")
    return result


def build_membership_prompt(
    units: Sequence[Mapping[str, Any]], columns: Sequence[Mapping[str, Any]],
) -> str:
    supplied_columns = _column_rows(columns)
    supplied_units = []
    for row in units:
        entry = {
            "unit_id": str(row.get("unit_id") or "").strip(),
            "unit_name": str(row.get("unit_name") or "").strip(),
        }
        # 図の地層名は日本語。検証済みalias（出典ページ・引用付き）がある場合だけ
        # 併記する。無い場合は英語名のみで、翻訳を推測して作ることはしない。
        japanese = str(row.get("unit_name_ja") or "").strip()
        if japanese:
            entry["unit_name_ja"] = japanese
        supplied_units.append(entry)
    if not supplied_units or any(not row["unit_id"] or not row["unit_name"] for row in supplied_units):
        raise ValueError("Membership batch requires non-empty unit IDs and names")
    if len(supplied_units) > 8:
        raise ValueError("Membership batch is limited to eight units")
    return f"""Inspect the attached stratigraphic figure and classify only the
SUPPLIED_UNITS against the closed list of SUPPLIED_COLUMNS.

Return exactly:
{{"assignments":[{{"unit_id":"supplied ID","column_ids":["supplied column ID"]}}]}}

Rules:
- Return every supplied unit exactly once and preserve its ID.
- column_ids is a JSON array containing only supplied IDs; use [] when the
  figure does not visibly establish a membership.
- Shared units may contain multiple supplied IDs.
- Do not add units or columns. Do not return ages, ranks, coordinates,
  descriptions, quotations, confidence, or explanation.
- Treat visual placement/brackets as evidence; do not assign every unit to a
  Column merely because it appears in the same figure.
- The figure is printed in Japanese. When unit_name_ja or column_name_ja is
  supplied, match that Japanese label in the figure; the English name is only
  a transliteration and may not appear at all.
- A unit that the figure does not show belongs to no Column: return [].

SUPPLIED_COLUMNS:
{json.dumps(supplied_columns, ensure_ascii=False, indent=2)}

SUPPLIED_UNITS:
{json.dumps(supplied_units, ensure_ascii=False, indent=2)}
"""


def validate_membership_batch(
    response: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
    columns: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[str, ...]]:
    expected_units = {
        str(row.get("unit_id") or "").strip(): str(row.get("unit_name") or "").strip()
        for row in units
    }
    allowed_columns = {row["column_id"] for row in _column_rows(columns)}
    raw = response.get("assignments")
    if not isinstance(raw, list):
        raise ValueError("Membership response requires assignments[]")
    result: dict[str, tuple[str, ...]] = {}
    for row in raw:
        if not isinstance(row, Mapping):
            raise ValueError("Every assignment must be an object")
        unit_id = str(row.get("unit_id") or "").strip()
        values = row.get("column_ids")
        if unit_id not in expected_units or unit_id in result or not isinstance(values, list):
            raise ValueError("Membership returned an unknown, duplicate, or malformed unit")
        column_ids = tuple(sorted({str(value).strip() for value in values}))
        if len(column_ids) != len(values) or any(value not in allowed_columns for value in column_ids):
            raise ValueError("Membership returned a duplicate or unknown Column ID")
        result[unit_id] = column_ids
    if set(result) != set(expected_units):
        raise ValueError("Membership response must return every supplied unit")
    return result


def membership_item_id(unit_name: Any, column_id: Any) -> str:
    import hashlib

    identity = f"{_normalise_name(unit_name)}|{str(column_id or '').strip()}"
    return "membership_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


_CANDIDATE_RULES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"fan|扇状地", re.I), ("alluvial fan", "fluvial indet.", "non-marine")),
    (re.compile(r"sublittoral|浅海|shallow", re.I), ("sublittoral", "shallow subtidal", "marine")),
    (re.compile(r"bathyal|半深海|深海|deep", re.I), ("bathyal", "deep-water indet.", "marine")),
    (re.compile(r"pluton|intrusive|granite|花崗|深成", re.I), ()),
    (re.compile(r"lake|lacustr|湖", re.I), ("lacustrine indet.", "non-marine")),
    (re.compile(r"marine|海成|海底", re.I), ("marine", "inferred marine")),
)


def environment_candidates(target: Mapping[str, Any]) -> tuple[str, ...]:
    """Derive a small candidate list from supplied source evidence, never GOLD."""

    identity_text = " ".join(str(target.get(key) or "") for key in (
        "unit_name", "lithology", "minor_lith", "unit_description",
    ))
    if re.search(r"pluton|intrusive|granite|granodiorite|花崗|深成", identity_text, re.I):
        return ()
    haystack = identity_text + " " + str(target.get("source_text") or "")
    output: list[str] = []
    for pattern, values in _CANDIDATE_RULES:
        if pattern.search(haystack):
            for value in values:
                if value not in output:
                    output.append(value)
    # The routed PDF context can contain neighbouring units.  Evidence matches
    # therefore rank candidates but never become the whole list.  A stable
    # six-term sedimentary shortlist prevents one neighbouring fan paragraph
    # from excluding a reviewed shallow/deep marine alternative.
    for value in (
        "alluvial fan", "fluvial indet.", "sublittoral", "bathyal", "marine", "non-marine",
    ):
        if value not in output:
            output.append(value)
    return tuple(output[:6])


def build_environment_unit_prompt(
    target: Mapping[str, Any],
    figures: Sequence[Mapping[str, Any]],
    candidates: Sequence[str] | None = None,
) -> str:
    allowed = list(candidates if candidates is not None else environment_candidates(target))
    target_payload = {key: target.get(key) for key in (
        "context_id", "unit_id", "unit_name", "column_ids", "lithology",
        "minor_lith", "unit_description", "source_text",
    )}
    figure_payload = [{key: row.get(key) for key in (
        "figure_id", "pdf_page", "printed_page", "matched_terms",
    )} for row in figures]
    return f"""Classify the depositional environment of exactly one supplied unit
using its source text and attached figures. This is a closed-world decision.

Return exactly:
{{"classification":{{
  "unit_id":"supplied ID",
  "column_ids":[],
  "applicability":"applicable|not_applicable|unresolved",
  "environment":"one ALLOWED_ENVIRONMENT or null",
  "assertion":"explicit|inferred",
  "quote":"verbatim source_text quote or empty",
  "figure_ids":[],
  "figure_observation":"short visible evidence or empty",
  "reason":"short reason"
}}}}

Rules:
- Never add a unit, Column, figure ID, quote, or environment.
- applicable requires exactly one ALLOWED_ENVIRONMENT.
- not_applicable is only for intrusive, plutonic, metamorphic or otherwise
  non-depositional units and requires a verbatim identifying quote.
- unresolved uses environment=null when the supplied evidence cannot choose.
- A figure claim requires supplied figure_ids and a concrete observation.
- Tuff, lava, sandstone or mudstone alone does not establish environment.

ALLOWED_ENVIRONMENT:
{json.dumps(allowed, ensure_ascii=False)}

FIGURES:
{json.dumps(figure_payload, ensure_ascii=False, indent=2)}

TARGET:
{json.dumps(target_payload, ensure_ascii=False, indent=2)}
"""


def validate_environment_unit(
    response: Mapping[str, Any],
    target: Mapping[str, Any],
    figures: Sequence[Mapping[str, Any]],
    candidates: Sequence[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    # Lazy import avoids a cycle: pdf_environment reuses Column image-token
    # estimation while llm_column_vision imports the Column-only helpers here.
    from pdf_environment import verify_response

    raw = response.get("classification")
    if not isinstance(raw, Mapping):
        raise ValueError("Environment unit response requires classification{}")
    allowed = list(candidates if candidates is not None else environment_candidates(target))
    applicability = str(raw.get("applicability") or "").strip().casefold()
    if applicability == "applicable" and str(raw.get("environment") or "") not in allowed:
        raise ValueError("Environment is outside the closed candidate list")
    return verify_response([target], figures, {"analyses": [dict(raw)]}, environment_vocab=allowed)


__all__ = [
    "COLUMN_DETECTION_PROMPT_VERSION",
    "COLUMN_MEMBERSHIP_PROMPT_VERSION",
    "CONSTRAINED_VALIDATOR_VERSION",
    "ENVIRONMENT_CLASSIFICATION_PROMPT_VERSION",
    "build_column_detection_prompt",
    "build_environment_unit_prompt",
    "build_membership_prompt",
    "environment_candidates",
    "membership_item_id",
    "validate_column_detection",
    "validate_environment_unit",
    "validate_membership_batch",
]
