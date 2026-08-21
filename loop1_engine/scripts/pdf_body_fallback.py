# -*- coding: utf-8 -*-
"""Targeted Japanese-body evidence for units omitted from summary figures.

The full report is never sent to a language model.  For each canonical unit
missing from a Column proposal, GSJ ZFK Japanese labels and section headings
are matched against the local page-text index.  The official ZFK section text
is retained as readable context after the corresponding PDF page is verified.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from pdf_locate import normalize


SCHEMA_VERSION = "japanese-body-fallback/1.0"
_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


def _rows(bundle: Mapping[str, Any], name: str, review_key: str) -> list[dict[str, Any]]:
    direct = bundle.get(name)
    if isinstance(direct, list):
        return [dict(row) for row in direct if isinstance(row, Mapping)]
    review = bundle.get("review_v2_input")
    value = review.get(review_key) if isinstance(review, Mapping) else None
    return [dict(row) for row in value or [] if isinstance(row, Mapping)]


def _unique_strings(values: Sequence[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = normalize(text)
        if text and key and key not in seen:
            output.append(text)
            seen.add(key)
    return output


def _zfk_document(zfk_root: str | Path, unit_id: str) -> dict[str, Any] | None:
    path = Path(zfk_root).expanduser().resolve() / "units" / f"{unit_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _nested(document: Mapping[str, Any], *keys: str) -> Any:
    value: Any = document
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _aliases(document: Mapping[str, Any]) -> list[str]:
    anchors = _nested(document, "target", "anchors")
    anchor_values: list[Any] = []
    for row in anchors or []:
        if isinstance(row, Mapping):
            anchor_values.extend((row.get("title"), row.get("label")))
    values = [
        _nested(document, "target", "title"),
        _nested(document, "derived", "section_title"),
        _nested(document, "legend", "parent_facies", "label_ja"),
        _nested(document, "legend", "parent_facies", "text_ja"),
        _nested(document, "self", "label_ja"),
        *anchor_values,
    ]
    return [value for value in _unique_strings(values) if _JAPANESE.search(value)]


def _page_match(
    pdf_index: Mapping[str, Any], aliases: Sequence[str]
) -> tuple[int, int | None, str] | None:
    pages = pdf_index.get("pages")
    printed = pdf_index.get("printed")
    if not isinstance(pages, list):
        return None
    printed_values = printed if isinstance(printed, list) else []
    candidates: list[tuple[int, int, int, int, str, int]] = []
    # Earlier aliases are more specific (normally the section heading with its
    # map symbol).  Exact heading matches outrank repeated generic unit names.
    for alias_rank, alias in enumerate(aliases):
        key = normalize(alias)
        if len(key) < 3:
            continue
        for index, page in enumerate(pages):
            count = str(page or "").count(key)
            if not count:
                continue
            printed_page = printed_values[index] if index < len(printed_values) else None
            has_printed = int(isinstance(printed_page, int))
            candidates.append((alias_rank, -len(key), -count, -has_printed, alias, index))
    if not candidates:
        return None
    _rank, _length, _count, _printed_rank, alias, index = min(candidates)
    printed_page = printed_values[index] if index < len(printed_values) else None
    return index + 1, printed_page if isinstance(printed_page, int) else None, alias


def find_missing_unit_body_evidence(
    bundle: Mapping[str, Any],
    proposal: Mapping[str, Any],
    *,
    pdf_index: Mapping[str, Any] | None,
    zfk_root: str | Path,
    source_pdf: str | Path,
) -> list[dict[str, Any]]:
    """Return page-verified Japanese body sections for proposal omissions."""

    if not isinstance(pdf_index, Mapping):
        return []
    accepted = {
        str(row.get("unit_id") or "")
        for row in proposal.get("units") or []
        if isinstance(row, Mapping) and row.get("memberships")
    }
    inventory = {
        str(row.get("unit_id") or ""): row
        for row in _rows(bundle, "units", "unit_rows")
        if str(row.get("unit_id") or "")
    }
    output: list[dict[str, Any]] = []
    for unit_id in sorted(set(inventory) - accepted):
        document = _zfk_document(zfk_root, unit_id)
        if document is None:
            continue
        aliases = _aliases(document)
        hit = _page_match(pdf_index, aliases)
        context = str(_nested(document, "target", "text") or "").strip()
        if hit is None or not context:
            continue
        pdf_page, printed_page, matched_alias = hit
        output.append({
            "schema_version": SCHEMA_VERSION,
            "unit_id": unit_id,
            "unit_name": str(inventory[unit_id].get("unit_name") or ""),
            "japanese_unit_name": str(
                _nested(document, "legend", "parent_facies", "label_ja")
                or _nested(document, "target", "title")
                or matched_alias
            ).strip(),
            "section": str(_nested(document, "target", "label") or "").strip(),
            "matched_alias": matched_alias,
            "pdf_page": pdf_page,
            "printed_page": printed_page,
            "full_context_quote": context,
            "source_file": str(Path(source_pdf).expanduser().resolve()),
            "zfk_unit_file": str(
                (Path(zfk_root).expanduser().resolve() / "units" / f"{unit_id}.json")
            ),
            "match_method": "exact Japanese section-heading match in local PDF page index",
        })
    return output


def completion_evidence_rows(
    body_matches: Sequence[Mapping[str, Any]],
    completions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Convert body matches and spatial completions to canonical evidence."""

    completion_by_unit = {
        str(row.get("unit_id") or ""): row
        for row in completions
        if isinstance(row, Mapping)
    }
    evidence: list[dict[str, Any]] = []
    for match in body_matches:
        unit_id = str(match.get("unit_id") or "")
        if not unit_id:
            continue
        page = f"PDF p.{match.get('pdf_page')}"
        if match.get("printed_page") is not None:
            page += f" / printed p.{match.get('printed_page')}"
        section = str(match.get("section") or "").strip()
        locator = " / ".join(value for value in (section, page) if value)
        completion = completion_by_unit.get(unit_id)
        evidence.append({
            "evidence_id": f"jpbody_{unit_id}_context",
            "unit_id": unit_id,
            "column_id": None,
            "field": "description_context",
            "candidate": "context only",
            "source_type": "PDF",
            "source_file": match.get("source_file"),
            "source_locator": locator,
            "matched_sentence": match.get("matched_alias"),
            "full_context_quote": match.get("full_context_quote"),
            "confidence_class": "B",
            "explicit": True,
            "selection": "context",
            "extraction_method": (
                "deterministic Japanese body match using official GSJ ZFK aliases"
            ),
        })
        if completion is None:
            continue
        column_ids = [str(value) for value in completion.get("column_ids") or [] if value]
        evidence.append({
            "evidence_id": f"jpbody_{unit_id}_column_candidate",
            "unit_id": unit_id,
            "column_id": ", ".join(column_ids),
            "field": "column_id",
            "candidate": ", ".join(column_ids),
            "source_type": "PDF",
            "source_file": match.get("source_file"),
            "source_locator": locator,
            "matched_sentence": match.get("matched_alias"),
            "full_context_quote": match.get("full_context_quote"),
            "confidence_class": "C",
            "explicit": False,
            "selection": "candidate",
            "extraction_method": (
                "Japanese body distribution plus Shape proximity to PDF-derived Column seeds; "
                "human review required"
            ),
        })
    return evidence


__all__ = [
    "SCHEMA_VERSION",
    "completion_evidence_rows",
    "find_missing_unit_body_evidence",
]
