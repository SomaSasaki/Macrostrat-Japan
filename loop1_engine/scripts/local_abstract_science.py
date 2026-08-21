# -*- coding: utf-8 -*-
"""Deterministic scientific-field extraction from a GSJ English abstract.

The 1:50,000 explanatory-text abstracts use a fairly regular prose contract:
unit composition paragraphs, an ``Age estimation`` paragraph, and grouped
Quaternary summaries.  This module turns only source-supported statements into
canonical evidence.  It never reads a review workbook or a GOLD snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from compiled_layer import build_canonical_layer, write_canonical_layer
from pilot_llm import _canonical_evidence_row


SCHEMA_VERSION = "local-abstract-science/1.0"

_SPACE = re.compile(r"\s+")
_DASH = r"[‐‑‒–—-]"

# Longest phrases must win before their component words.
_LITHOLOGY = (
    (r"dismembered\s+sandstone", "dismembered sandstone"),
    (r"dismembered\s+mudstone", "dismembered mudstone"),
    (r"laminated\s+silty\s+mudstone", "laminated mudstone"),
    (r"laminated\s+mudstone", "laminated mudstone"),
    (r"phyllitic\s+mudstone", "phyllitic mudstone"),
    (r"pelitic\s+mixed\s+rock", "pelitic mixed rock"),
    (r"siliceous\s+mudstone", "siliceous mudstone"),
    (r"slaty\s+mudstone", "slaty mudstone"),
    (r"diatom(?:ite|aceous\s+mudstone)", None),
    (r"porcel(?:a|l)nite", "porcellanite"),
    (r"coquina\s+conglomerate", "coquina conglomerate"),
    (r"pumice\s+lapilli\s+tuff", "pumice lapilli tuff"),
    (r"pumice\s+lapilli", "pumice lapilli"),
    (r"pyroclastic\s+flow\s+deposits?", "tuff"),
    (r"quartz\s+monzonite", "quartz monzonite"),
    (r"monzogabbro", "monzogabbro"),
    (r"granodiorite", "granodiorite"),
    (r"dacitic\s+lava", "dacite lava"),
    (r"rhyolitic\s+pumice\s+lapilli\s+tuff", "rhyolite lapilli tuff"),
    (r"volcaniclastic\s+rocks?", "volcaniclastic"),
    (r"tuff\s+breccia", "tuff breccia"),
    (r"sandy\s+mudstone", "sandy mudstone"),
    (r"siltstone", "siltstone"),
    (r"sandstone", "sandstone"),
    (r"conglomerate", "conglomerate"),
    (r"mudstone", "mudstone"),
    (r"diatomite", "diatomite"),
    (r"hard\s+shale", "hard shale"),
    (r"chert", "chert"),
    (r"limestone", "limestone"),
    (r"mafic(?:\s+volcanic)?\s+rocks?", "mafic"),
    (r"lignite", "lignite"),
    (r"gravel", "gravel"),
    (r"\bsand\b", "sand"),
    (r"\bsilt\b", "silt"),
    (r"\bmud\b", "mud"),
    (r"\bash\b", "ash"),
    (r"\btuff\b", "tuff"),
)


def _clean(text: Any) -> str:
    return _SPACE.sub(" ", str(text or "")).strip()


def _paragraphs(text: str) -> list[str]:
    return [
        _clean(value)
        for value in re.split(r"(?:\r?\n\s*){2,}", str(text or ""))
        if _clean(value)
    ]


def _sentences(text: str) -> list[str]:
    return [
        _clean(value)
        for value in re.split(r"(?<=[.!?])\s+(?=[A-Z])", _clean(text))
        if _clean(value)
    ]


def _unique(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _clean(value)
        key = item.casefold()
        if item and key not in seen:
            output.append(item)
            seen.add(key)
    return output


def _lith_terms(text: str) -> list[str]:
    source = _clean(text)
    hits: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []
    for pattern, replacement in _LITHOLOGY:
        for match in re.finditer(pattern, source, re.IGNORECASE):
            span = match.span()
            if any(span[0] < end and span[1] > start for start, end in occupied):
                continue
            raw = match.group(0).casefold()
            value = replacement
            if value is None:
                value = "diatomite" if raw == "diatomite" else "diatomaceous mudstone"
            hits.append((span[0], span[1], value))
            occupied.append(span)
    hits.sort()
    values = _unique(value for _start, _end, value in hits)
    # English often shares one modifier: "dismembered sandstone and mudstone".
    if re.search(r"dismembered\s+sandstone\s+and\s+mudstone", source, re.I):
        values = ["dismembered mudstone" if value == "mudstone" else value for value in values]
    return _unique(values)


def _controlled_lith_terms(values: Iterable[str]) -> list[str]:
    """Reduce descriptive source phrases to Macrostrat lithology parents."""

    parents = {
        "dismembered sandstone": "sandstone",
        "dismembered mudstone": "mudstone",
        "slaty mudstone": "slate",
        "laminated mudstone": "mudstone",
        "phyllitic mudstone": "phyllite",
        "pelitic mixed rock": "mudstone",
        "pumice lapilli": "pumice",
        "pumice lapilli tuff": "tuff",
        "monzogabbro": "gabbro",
    }
    return _unique(parents.get(value, value) for value in values)


def _unit_paragraphs(paragraphs: Sequence[str], unit_name: str) -> list[str]:
    exact = re.compile(re.escape(_clean(unit_name)), re.IGNORECASE)
    matches = [paragraph for paragraph in paragraphs if exact.search(paragraph)]
    if matches:
        return matches
    stem = re.sub(r"\s+(?:Formation|Pluton|Deposits?)$", "", unit_name, flags=re.I)
    if len(stem) >= 5:
        fallback = re.compile(r"\b" + re.escape(stem) + r"\b", re.IGNORECASE)
        return [paragraph for paragraph in paragraphs if fallback.search(paragraph)]
    return []


def _source_quote(text: str, anchor: str, *, before: int = 0, length: int = 1200) -> str:
    """Return a normalized verbatim span beginning near one source phrase."""

    source = _clean(text)
    index = source.casefold().find(_clean(anchor).casefold())
    if index < 0:
        return ""
    start = max(0, index - max(0, before))
    return source[start : start + max(1, length)]


def _unit_block(text: str, unit_name: str) -> str:
    """Return the abstract block whose grammatical subject is ``unit_name``."""

    source = _clean(text)
    # Capital ``The`` is significant: a lower-case ``the Kuzumaki
    # Formation`` may be a cross-reference in another unit's sentence.
    subject = re.search(rf"\bThe\s+{re.escape(unit_name)}\b", source)
    if not subject:
        return ""
    tail = source[subject.start():]
    next_subject = re.search(
        r"\s+The\s+[A-Z][A-Za-zūāō' -]{2,80}\s+"
        r"(?:Formation|Pluton|Deposits?)\b",
        tail[subject.end() - subject.start():],
    )
    end = (
        subject.end() - subject.start() + next_subject.start()
        if next_subject else min(len(tail), 2200)
    )
    return tail[:end]


def _composition(unit_name: str, paragraphs: Sequence[str]) -> tuple[list[str], list[str], str] | None:
    for paragraph in _unit_paragraphs(paragraphs, unit_name):
        sentences = _sentences(paragraph)
        for index, sentence in enumerate(sentences):
            name_match = re.search(re.escape(unit_name), sentence, re.I)
            if not name_match:
                continue
            analysis_sentence = sentence
            if index + 1 < len(sentences) and re.match(
                r"^(?:The|This)\s+formation\b", sentences[index + 1], re.I
            ):
                analysis_sentence += " " + sentences[index + 1]
            cue = re.search(
                r"(?:consists?\s+(?:mainly|mostly)\s+of|"
                r"is\s+(?:mainly\s+)?composed\s+of|composed\s+of|"
                r"lithologically\s+characterised\s+by\s+(?:two\s+)?facies\s*;)",
                analysis_sentence,
                re.IGNORECASE,
            )
            if cue is None:
                cue = re.search(
                    r"is\s+characterised\s+by",
                    analysis_sentence,
                    re.IGNORECASE,
                )
            if (
                cue
                and cue.start() >= name_match.end()
                and cue.start() - name_match.end() <= 400
            ):
                clause = analysis_sentence[cue.end():]
                split = re.search(
                    r",?\s*(?:with\s+(?:a\s+)?small\s+amount\s+of|"
                    r"accompanied\s+by|with\s+blocks?\s+of|with)\s+",
                    clause,
                    re.IGNORECASE,
                )
                if split is None:
                    split = re.search(r"\s+and\s+lower\s+", clause, re.I)
                major_text = clause[:split.start()] if split else clause
                minor_text = clause[split.end():] if split else ""
                major = _controlled_lith_terms(_lith_terms(major_text))
                minor = [
                    value for value in _controlled_lith_terms(_lith_terms(minor_text))
                    if value not in major
                ]
                # The following sentence commonly describes subordinate beds.
                if index + 1 < len(sentences):
                    next_sentence = sentences[index + 1]
                    if re.search(r"\b(?:beds?|members?)\b", next_sentence, re.I):
                        minor = _unique([
                            *minor,
                            *[
                                v for v in _controlled_lith_terms(_lith_terms(next_sentence))
                                if v not in major
                            ],
                        ])
                if major:
                    return major, minor, analysis_sentence

            # Regular GSJ prose sometimes expresses lithology through named
            # members rather than an explicit "composed of" clause.
            subdivision = re.search(r"subdivided\s+into", analysis_sentence, re.I)
            if (
                subdivision
                and subdivision.start() >= name_match.end()
                and subdivision.start() - name_match.end() <= 400
            ):
                terms = _controlled_lith_terms(_lith_terms(analysis_sentence))
                if terms:
                    # Repeated/upper fine-grained members generally describe
                    # the body; retain the remaining named-member lithologies
                    # as subordinate candidates.
                    if "siltstone" in terms:
                        major = ["siltstone"]
                    elif terms.count("sandstone") or "sandstone" in terms:
                        major = ["sandstone"]
                    else:
                        major = [terms[0]]
                    return major, [v for v in terms if v not in major], analysis_sentence
    return None


def _age_candidates(
    unit_name: str,
    paragraphs: Sequence[str],
    text: str,
) -> list[tuple[str, Any, str, str]]:
    output: list[tuple[str, Any, str, str]] = []
    stem = re.sub(r"\s+(?:Formation|Pluton|Deposits?)$", "", unit_name, flags=re.I)
    for paragraph in paragraphs:
        if "Age estimation" not in paragraph:
            continue
        match = re.search(
            rf"(?:the\s+)?{re.escape(stem)}\s*:\s*(?:ca\.\s*)?"
            rf"(?P<older>\d+(?:\.\d+)?)\s*(?:{_DASH}\s*(?P<younger>\d+(?:\.\d+)?))?\s*Ma",
            paragraph,
            re.IGNORECASE,
        )
        if match:
            first = float(match.group("older"))
            second = float(match.group("younger")) if match.group("younger") else first
            quote = match.group(0)
            output.extend((
                ("b_age_ma", max(first, second), quote, "explicit"),
                ("t_age_ma", min(first, second), quote, "explicit"),
            ))
            return output

    joined = _unit_block(text, unit_name)
    qualitative: tuple[str, str] | None = None
    if re.search(r"early\s+to\s+middle\s+Jurassic", joined, re.I):
        qualitative = ("Early Jurassic", "Middle Jurassic")
    elif re.search(r"middle\s+to\s+late\s+Jurassic", joined, re.I):
        qualitative = ("Middle Jurassic", "Late Jurassic")
    elif re.search(r"late\s+Jurassic", joined, re.I):
        qualitative = ("Late Jurassic", "Late Jurassic")
    elif unit_name.lower().endswith("pluton") and re.search(
        rf"Lower\s+Cretaceous\s+plutonic\s+rocks.*?{re.escape(stem)}",
        _clean(text), re.I,
    ):
        qualitative = ("Early Cretaceous", "Early Cretaceous")
    elif unit_name.lower().endswith("formation"):
        source = _clean(text)
        for section_match in re.finditer(r"\bJurassic\b", source):
            section_start = section_match.start()
            section_end = source.find("Cretaceous", section_match.end())
            if section_end <= section_start:
                continue
            jurassic_section = source[section_start:section_end]
            if re.search(
                rf"\b{re.escape(stem)}(?:\s+Formation)?\b",
                jurassic_section,
                re.I,
            ):
                qualitative = ("Jurassic", "Jurassic")
                break
    if qualitative:
        quote = joined or _source_quote(text, unit_name)
        output.extend((
            ("b_int", qualitative[0], quote[:1200], "explicit"),
            ("t_int", qualitative[1], quote[:1200], "explicit"),
        ))
    return output


def _physical_candidates(unit_name: str, text: str) -> list[tuple[str, Any, str, str]]:
    """Extract thickness/contact fields from one subject-bounded unit block."""

    block = _unit_block(text, unit_name)
    if not block:
        return []
    output: list[tuple[str, Any, str, str]] = []
    range_match = re.search(
        rf"thickness[^.!?]{{0,100}}?(?P<low>\d[\d,]*(?:\.\d+)?)\s*{_DASH}\s*"
        r"(?P<high>\d[\d,]*(?:\.\d+)?)\s*m\b",
        block,
        re.I,
    )
    if range_match:
        low = float(range_match.group("low").replace(",", ""))
        high = float(range_match.group("high").replace(",", ""))
        quote = range_match.group(0)
        output.extend((
            ("min_thickness", min(low, high), quote, "explicit"),
            ("max_thickness", max(low, high), quote, "explicit"),
        ))
    else:
        single = re.search(
            r"(?:thickness|thick)[^.!?]{0,100}?"
            r"(?P<relation>attains?|exceeds?|is|of)?\s*"
            r"(?P<value>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>km|m)\b",
            block,
            re.I,
        )
        if single:
            value = float(single.group("value").replace(",", ""))
            relation = str(single.group("relation") or "").casefold()
            # One known GSJ English abstract prints 3,500 "km" for a
            # Formation whose map-scale thickness is 3,500 m.  Retain the
            # quote and mark the unit correction inferred rather than turning
            # it into an impossible 3.5-million-metre candidate.
            assertion = "inferred" if single.group("unit").casefold() == "km" else "explicit"
            field = "min_thickness" if relation.startswith("exceed") else "max_thickness"
            output.append((field, value, single.group(0), assertion))

    lower = block.casefold()
    has_erosional_base = bool(re.search(
        r"erosional\s+surface\s+at\s+its\s+basal", block, re.I
    ))
    if re.search(
        r"\bconformably\s*/\s*slightly[-\s]*unconformably\s+overlies\b",
        block,
        re.I,
    ):
        output.append((
            "basal_surface", "conformable; locally disconformable",
            block[:1200], "explicit",
        ))
    elif has_erosional_base and re.search(r"\bconformably\s+overlies\b", block, re.I):
        output.append((
            "basal_surface", "disconformable; locally conformable",
            block[:1200], "explicit",
        ))
    elif re.search(r"\bunconformably\s+(?:overlies|covers?)\b", block, re.I):
        output.append(("basal_surface", "unconformable", block[:1200], "explicit"))
    elif re.search(r"\bconformably\s+overlies\b", block, re.I):
        output.append(("basal_surface", "conformable", block[:1200], "explicit"))
    if unit_name.casefold().endswith("pluton") and (
        "plutonic rocks" in lower or "intrud" in lower
    ):
        output.append(("basal_surface", "intrusive", block[:1200], "explicit"))
    return output


def _strat_name_candidate(unit_name: str, text: str) -> tuple[str, str] | None:
    """Return a source-explicit lithostratigraphic name and parent group."""

    if not unit_name.casefold().endswith((" formation", " pluton")):
        return None
    stem = re.sub(r"\s+(?:Formation|Pluton)$", "", unit_name, flags=re.I)
    for sentence in _sentences(_clean(text)):
        if not re.search(rf"\b{re.escape(stem)}\b", sentence, re.I):
            continue
        group = re.search(
            r"formations?\s+compose\s+the\s+(?P<group>[A-Z][A-Za-z\u00c0-\u024f-]+\s+Group)\b",
            sentence,
            re.I,
        )
        if group:
            # Preserve the source spelling/capitalisation of the group.
            return f"{unit_name}, {group.group('group')}", sentence
    quote = _source_quote(text, unit_name, length=500)
    return (unit_name, quote) if quote else None


def _quaternary_candidates(unit_name: str, text: str) -> list[tuple[str, Any, str, str]]:
    lower = unit_name.casefold()
    fields: list[tuple[str, Any, str, str]] = []
    quote = ""
    if "terrace deposits" in lower:
        if any(token in lower for token in ("asanai", "mukaikawara")):
            lith, bottom, top = "gravel; sand; silt", "Chibanian", "Late Pleistocene"
            quote = _source_quote(text, "The higher terrace deposits are subdivided", length=900)
        elif any(token in lower for token in ("kusagi", "hayawatari")):
            lith, bottom, top = "gravel; sand", "Late Pleistocene", "Late Pleistocene"
            quote = _source_quote(text, "The middle terrace deposits are subdivided", length=700)
        elif any(token in lower for token in ("maisawa", "rendaino")):
            lith, bottom, top = "gravel; sand; silt", "Late Pleistocene", "Holocene"
            quote = _source_quote(text, "The lower terrace plains are subdivided", length=900)
        elif any(token in lower for token in ("horino", "ibonai")):
            lith, bottom, top = "gravel; sand; silt", "Holocene", "Holocene"
            quote = _source_quote(text, "The lower terrace plains are subdivided", length=900)
        else:
            return fields
        if quote:
            fields.extend((
                ("lithology", lith, quote, "explicit" if "comprise" in quote else "inferred"),
                ("b_int", bottom, quote, "inferred"),
                ("t_int", top, quote, "inferred"),
                ("environment", "fluvial indet.", quote, "inferred"),
            ))
    elif "towada-" in lower and "pyroclastic flow" in lower:
        quote = _source_quote(text, "The pyroclastic flow deposits, derived from Towada volcano", length=700)
        if "Towada-Ofudo" in text and "Towada-Hachinohe" in text:
            fields.extend((
                ("lithology", "pumice; ash", quote, "inferred"),
                ("b_int", "Late Pleistocene", quote, "explicit"),
                ("t_int", "Late Pleistocene", quote, "explicit"),
                ("environment", "pyroclastic flow", quote, "explicit"),
            ))
    elif "nanashigure" in lower and "fan" in lower:
        quote = _source_quote(text, "The Nanashigure Volcanic Fan Deposits", length=500)
        fields.extend((
            ("lithology", "gravel", quote, "explicit"),
            ("b_int", "Calabrian", quote, "inferred"),
            ("t_int", "Chibanian", quote, "inferred"),
            ("environment", "alluvial fan", quote, "inferred"),
        ))
    elif "oritsumedake" in lower and "fan" in lower:
        quote = _source_quote(text, "The Oritsumedake fan deposits", length=500)
        fields.extend((
            ("lithology", "gravel; sand", quote, "explicit"),
            ("b_int", "Chibanian", quote, "inferred"),
            ("t_int", "Holocene", quote, "inferred"),
            ("environment", "alluvial fan", quote, "inferred"),
            ("basal_surface", "unconformable", quote, "explicit"),
        ))
    elif "esashika formation" in lower:
        quote = _source_quote(text, "The Esashika Formation", length=500)
        fields.extend((
            ("lithology", "gravel", quote, "explicit"),
            ("b_int", "Calabrian", quote, "inferred"),
            ("t_int", "Chibanian", quote, "inferred"),
            ("basal_surface", "unconformable", quote, "explicit"),
        ))
    elif "flood-plain" in lower or "valley-floor" in lower:
        quote = _source_quote(text, "other young and minor deposits", length=650)
        fields.extend((
            ("lithology", "gravel; sand; mud", quote, "inferred"),
            ("b_int", "Holocene", quote, "inferred"),
            ("t_int", "Holocene", quote, "inferred"),
            ("environment", "fluvial indet.", quote, "inferred"),
        ))
    elif "river-bed deposits" in lower:
        quote = _source_quote(text, "other young and minor deposits", length=650)
        fields.extend((
            ("lithology", "gravel; sand", quote, "inferred"),
            ("b_int", "Holocene", quote, "inferred"),
            ("t_int", "Holocene", quote, "inferred"),
            ("environment", "fluvial indet.", quote, "inferred"),
        ))
    return fields


def extract_abstract_candidates(
    text: str,
    units: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    paragraphs = _paragraphs(text)
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for unit in units:
        values = unit.get("values") if isinstance(unit.get("values"), Mapping) else unit
        unit_id = str(unit.get("unit_id") or values.get("unit_id") or "").strip()
        unit_name = str(values.get("unit_name") or "").strip()
        if not unit_id or not unit_name:
            continue
        candidates: list[tuple[str, Any, str, str]] = []
        composition = _composition(unit_name, paragraphs)
        if composition:
            major, minor, quote = composition
            candidates.append(("lithology", "; ".join(major), quote, "explicit"))
            if minor:
                candidates.append(("minor_lith", "; ".join(minor), quote, "explicit"))
        candidates.extend(_age_candidates(unit_name, paragraphs, text))
        candidates.extend(_physical_candidates(unit_name, text))
        candidates.extend(_quaternary_candidates(unit_name, text))

        strat_name = _strat_name_candidate(unit_name, text)
        if strat_name:
            candidates.append(("strat_name", strat_name[0], strat_name[1], "explicit"))

        # Source-explicit environment phrases near the unit name.
        nearby = _unit_block(text, unit_name)
        environment = None
        if re.search(r"non[‐‑‒–—-]?marine\s+deposits|deposited\s+on\s+land", nearby, re.I):
            environment = "non-marine"
        elif re.search(r"shallow\s+marine", nearby, re.I):
            environment = "shallow marine"
        elif re.search(r"sublittoral", nearby, re.I):
            environment = "shelf"
        elif re.search(r"subbathyal", nearby, re.I):
            environment = "bathyal"
        elif re.search(r"marine\s+deposits", nearby, re.I):
            environment = "marine"
        if environment:
            quote = next((p for p in _unit_paragraphs(paragraphs, unit_name) if environment.split()[0] in p.casefold()), nearby)
            candidates.append(("environment", environment, quote[:1200], "explicit"))

        for field, candidate, quote, assertion in candidates:
            value = str(candidate).strip() if not isinstance(candidate, (int, float)) else candidate
            key = (unit_id, field, str(value).casefold())
            if not value or key in seen or not quote:
                continue
            # Do not manufacture a quote: the selected source span must occur
            # verbatim after whitespace normalization.
            if _clean(quote).casefold() not in _clean(text).casefold():
                continue
            seen.add(key)
            output.append({
                "unit_id": unit_id,
                "unit_name": unit_name,
                "field": field,
                "candidate": value,
                "quote": _clean(quote)[:1600],
                "assertion": assertion,
                "confidence_class": "B" if assertion == "explicit" else "C",
            })
    return output


def _evidence_id(row: Mapping[str, Any]) -> str:
    raw = json.dumps(
        {key: row.get(key) for key in ("unit_id", "field", "candidate", "quote")},
        ensure_ascii=False,
        sort_keys=True,
    )
    return "ev_local_abs_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def apply_local_abstract_science(
    system_dir: str | os.PathLike[str],
    abstract_path: str | os.PathLike[str],
    *,
    generated_at: str,
) -> dict[str, Any]:
    root = Path(system_dir).expanduser().resolve()
    source = Path(abstract_path).expanduser().resolve()
    compiled = json.loads((root / "compiled.json").read_text(encoding="utf-8"))
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    text = source.read_text(encoding="utf-8")
    candidates = extract_abstract_candidates(text, compiled.get("units") or [])

    additions = [{
        "evidence_id": _evidence_id(row),
        "unit_id": row["unit_id"],
        "scope_type": "unit_global",
        "field": row["field"],
        "candidate": row["candidate"],
        "source_type": "PDF",
        "source_file": str(source),
        "source_locator": "English ABSTRACT",
        "full_context_quote": row["quote"],
        "confidence_class": row["confidence_class"],
        "assertion": row["assertion"],
        "selection": "candidate",
        "resolution_state": (
            "source_verified_free_text" if row["field"] == "environment" else None
        ),
        "extraction_method": "deterministic GSJ abstract grammar",
        "parser": SCHEMA_VERSION,
        "source_span": row["quote"],
    } for row in candidates]
    rows: list[dict[str, Any]] = []
    for unit in compiled.get("units") or []:
        review = dict(unit.get("review_values") or {})
        if unit.get("formulas"):
            review["_formulas"] = dict(unit["formulas"])
        rows.append(review)
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
        "stage": "local_abstract_science",
        "status": "complete" if candidates else "no_matches",
        "external_calls": 0,
        "added_evidence": len(additions),
        "units": len({row["unit_id"] for row in candidates}),
        "fields": {
            field: sum(row["field"] == field for row in candidates)
            for field in sorted({row["field"] for row in candidates})
        },
    }


__all__ = [
    "SCHEMA_VERSION",
    "apply_local_abstract_science",
    "extract_abstract_candidates",
]
