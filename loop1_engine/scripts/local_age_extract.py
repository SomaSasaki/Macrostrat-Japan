# -*- coding: utf-8 -*-
"""Deterministic capture of analytical ages and biozones from routed text."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from compiled_layer import build_canonical_layer, write_canonical_layer
from pilot_llm import _canonical_evidence_row


SCHEMA_VERSION = "local-age-notes/1.0"
METHOD_CUE = re.compile(
    r"K[\s‐‑‒–—-]*Ar|Ar[\s‐‑‒–—-]*Ar|40Ar\s*/\s*39Ar|U[\s‐‑‒–—-]*Pb|"
    r"fission[\s‐‑‒–—-]*track|\bFT\b|フィッション[・\s-]*トラック|"
    r"放射年代|年代測定|ジルコン",
    re.IGNORECASE,
)
AGE_VALUE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:±\s*\d+(?:\.\d+)?\s*)?(?:Ma|ka|kyr|年\s*BP)",
    re.IGNORECASE,
)
BIOZONE = re.compile(
    r"\b(?:NPD|CN|NN|N\.?|CP)\s*\d+[A-Za-z]?\b|"
    r"[A-Za-z][A-Za-z. -]{2,50}(?:Zone|zone)|化石帯|珪藻帯|有孔虫帯",
    re.IGNORECASE,
)
FORMATION_BOUNDARY_CUE = re.compile(
    r"本(?:層|累層|部層|岩体)の年代|堆積年代|形成年代|活動年代|"
    r"(?:上限|下限|基底|最上部|最下部).*年代|年代は|年代を示す|"
    r"age\s+of\s+(?:the\s+)?(?:formation|member|unit|pluton)|"
    r"(?:formation|member|unit|pluton)\s+(?:is|was)\s+dated",
    re.IGNORECASE,
)


def _sentences(text: str) -> list[str]:
    return [
        " ".join(value.split()).strip()
        # Do not split the decimal point in values such as ``10.2 Ma``.
        for value in re.split(
            r"(?<=[。！？!?])\s*|(?<!\d)\.(?=\s|$)|\n+",
            str(text or ""),
        )
        if value.strip()
    ]


def is_local_measurement_quote(quote: str) -> bool:
    return bool((METHOD_CUE.search(quote) and AGE_VALUE.search(quote)) or BIOZONE.search(quote))


def supports_formation_boundary(quote: str) -> bool:
    return bool(FORMATION_BOUNDARY_CUE.search(str(quote or "")))


def extract_local_age_notes(routed: Mapping[str, Any]) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for context in routed.get("contexts") or []:
        if not isinstance(context, Mapping):
            continue
        unit_id = str(context.get("unit_id") or "").strip()
        for sentence in _sentences(str(context.get("text") or "")):
            if not is_local_measurement_quote(sentence):
                continue
            key = (unit_id, re.sub(r"\s+", "", sentence).casefold())
            if key in seen:
                continue
            seen.add(key)
            kinds = []
            if METHOD_CUE.search(sentence) and AGE_VALUE.search(sentence):
                kinds.append("analytical_age")
            if BIOZONE.search(sentence):
                kinds.append("biozone")
            notes.append({
                "unit_id": unit_id,
                "unit_name": context.get("unit_name"),
                "kinds": kinds,
                "quote": sentence[:1200],
                "formation_boundary_supported": supports_formation_boundary(sentence),
                "section": context.get("section"),
                "pdf_page": context.get("pdf_page"),
                "printed_page": context.get("printed_page"),
            })
    return notes


def _evidence_id(note: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {key: note.get(key) for key in ("unit_id", "kinds", "quote", "pdf_page")},
        ensure_ascii=False,
        sort_keys=True,
    )
    return "ev_local_age_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def apply_local_age_notes(
    system_dir: str | os.PathLike[str],
    routed: Mapping[str, Any],
    *,
    source_file: str | os.PathLike[str],
    generated_at: str,
) -> dict[str, Any]:
    """Append local notes to comments/evidence without an LLM call."""

    root = Path(system_dir).expanduser().resolve()
    compiled = json.loads((root / "compiled.json").read_text(encoding="utf-8"))
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    notes = extract_local_age_notes(routed)
    notes_by_id: dict[str, list[dict[str, Any]]] = {}
    for note in notes:
        notes_by_id.setdefault(str(note.get("unit_id") or ""), []).append(note)

    unit_rows = []
    for unit in compiled.get("units") or []:
        row = dict(unit.get("review_values") or {})
        additions = notes_by_id.get(str(unit.get("unit_id") or ""), [])
        if additions:
            local_text = " | ".join(
                f"Local age evidence ({'/'.join(note['kinds'])}): {note['quote']}"
                for note in additions
            )
            current = str(row.get("comments") or "").strip()
            if local_text not in current:
                row["comments"] = " ".join(value for value in (current, local_text) if value)
        if unit.get("formulas"):
            row["_formulas"] = dict(unit["formulas"])
        unit_rows.append(row)

    additions = []
    for note in notes:
        additions.append({
            "evidence_id": _evidence_id(note),
            "unit_id": note.get("unit_id"),
            "scope_type": "unit_global",
            "field": "local_age_notes",
            "candidate": "; ".join(note.get("kinds") or []),
            "source_type": "PDF",
            "source_file": str(Path(source_file).resolve()),
            "source_locator": " / ".join(value for value in (
                str(note.get("section") or "").strip(),
                f"PDF p.{note.get('pdf_page')}" if note.get("pdf_page") else "",
                f"printed p.{note.get('printed_page')}" if note.get("printed_page") else "",
            ) if value),
            "PDF_page": note.get("pdf_page"),
            "printed_page": note.get("printed_page"),
            "full_context_quote": note.get("quote"),
            "confidence_class": "B",
            "assertion": "explicit",
            "selection": "validation",
            "extraction_method": "deterministic local analytical-age/biozone parser",
            "role": "formation_boundary" if note.get("formation_boundary_supported") else "local_measurement",
            "parser": SCHEMA_VERSION,
            "source_span": note.get("quote"),
        })

    existing = [_canonical_evidence_row(row) for row in evidence.get("evidence") or []]
    map_doc = compiled.get("map") or {}
    rebuilt, evidence_doc = build_canonical_layer(
        unit_rows,
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
        "stage": "local_age_notes",
        "status": "complete" if notes else "no_matches",
        "external_calls": 0,
        "added_evidence": len(additions),
        "notes": len(notes),
        "units": len(notes_by_id),
        "formation_boundary_supported": sum(bool(note.get("formation_boundary_supported")) for note in notes),
    }


__all__ = [
    "SCHEMA_VERSION",
    "apply_local_age_notes",
    "extract_local_age_notes",
    "is_local_measurement_quote",
    "supports_formation_boundary",
]
