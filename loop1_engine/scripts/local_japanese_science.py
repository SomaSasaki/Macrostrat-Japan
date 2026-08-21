# -*- coding: utf-8 -*-
"""Deterministic scientific extraction from the Japanese GSJ PDF body.

The normalized PDF page index is useful for routing but intentionally removes
punctuation and therefore destroys decimal points.  This module re-opens only
the routed source pages with pdfplumber, restores the two-column reading order,
and promotes explicit section-level thickness and basal-contact statements.
It never reads a review workbook or a GOLD snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from compiled_layer import build_canonical_layer, write_canonical_layer
from pilot_llm import _canonical_evidence_row


SCHEMA_VERSION = "local-japanese-science/1.0"

PageTextGetter = Callable[[int], Sequence[str]]

_SPACE = re.compile(r"[ \t\u3000]+")
_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_SECTION_CUE = re.compile(
    r"(?m)^[ \t]*(?:地層名及び定義|定義及び名称|模式地|分布(?:・地形)?|"
    r"層序関係|層序|層厚|層相|岩相|化石|古環境|年代|時代及び対比|対比)"
    r"(?=[ \t]|$)"
)
_NEXT_UNIT_HEADING = re.compile(
    r"(?m)^[ \t]*[0-9₀-₉]+[ \t]*\.[ \t]*[0-9₀-₉]+"
    r"(?:[ \t]*\.[ \t]*[0-9₀-₉]+)?[ \t]+[^\n]{1,90}"
)
_NUMBERED_ALIAS = re.compile(
    r"[0-9₀-₉]+\s*\.\s*[0-9₀-₉]+(?:\s*\.\s*[0-9₀-₉]+)?\s*$"
)
_NUMBER = r"\d[\d,，]*(?:\.\d+)?"
_THICKNESS_VALUE = re.compile(
    rf"(?P<prefix>最大(?:約|で|でも)?|最小(?:約|で)?|約|およそ)?\s*"
    rf"(?P<lower>{_NUMBER})\s*"
    rf"(?:[〜～~–—−-]\s*(?P<upper>{_NUMBER})\s*)?"
    rf"(?P<unit>km|m|cm)\s*"
    rf"(?P<qualifier>以上|以下|程度|前後|ほど)?",
    re.IGNORECASE,
)


def _clean(text: Any) -> str:
    lines = [_SPACE.sub(" ", line).strip() for line in str(text or "").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _japanese_aliases(context: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for raw in context.get("aliases") or []:
        text = _SPACE.sub("", str(raw or "")).strip()
        if text and _JAPANESE.search(text) and text not in values:
            values.append(text)
    return values


def _ordered_chunks(get_page_text: PageTextGetter, start_page: int) -> list[tuple[int, int, str]]:
    chunks: list[tuple[int, int, str]] = []
    # Long bedrock descriptions can span several pages.  Five pages is enough
    # to reach the next numbered unit in this GSJ series while keeping the
    # operation tightly source-routed.
    for page_number in range(start_page, start_page + 5):
        try:
            columns = list(get_page_text(page_number) or [])
        except (IndexError, KeyError):
            break
        for column_index, text in enumerate(columns):
            cleaned = _clean(text)
            if cleaned:
                chunks.append((page_number, column_index, cleaned))
    return chunks


def _target_section(
    context: Mapping[str, Any],
    get_page_text: PageTextGetter,
) -> tuple[str, int] | None:
    start_page = int(context.get("pdf_page") or 0)
    if start_page <= 0:
        return None
    chunks = _ordered_chunks(get_page_text, start_page)
    aliases = _japanese_aliases(context)
    candidates: list[tuple[int, int, int, int]] = []
    for chunk_index, (_page, _column, text) in enumerate(chunks):
        compact = re.sub(r"\s+", "", text)
        for alias in aliases:
            search_from = 0
            while True:
                compact_position = compact.find(alias, search_from)
                if compact_position < 0:
                    break
                # Map a whitespace-free offset back to the original text.
                seen = 0
                position = 0
                for position, character in enumerate(text):
                    if not character.isspace():
                        if seen == compact_position:
                            break
                        seen += 1
                before = text[max(0, position - 55):position]
                following = text[position + len(alias):position + len(alias) + 1500]
                heading = bool(_NUMBERED_ALIAS.search(before))
                cue_count = len(_SECTION_CUE.findall(following))
                candidates.append((100 if heading else 0, cue_count, chunk_index, position))
                search_from = compact_position + len(alias)
    if not candidates:
        return None

    _heading, _cues, chunk_index, position = max(candidates)
    page_number = chunks[chunk_index][0]
    parts = [chunks[chunk_index][2][position:]]
    parts.extend(chunk[2] for chunk in chunks[chunk_index + 1:])
    joined = "\n".join(parts)
    next_heading = _NEXT_UNIT_HEADING.search(joined, 1)
    if next_heading:
        joined = joined[:next_heading.start()]
    return _clean(joined)[:16000], page_number


def _named_block(section: str, heading: str, following_headings: str) -> str:
    pattern = re.compile(
        rf"(?ms)^[ \t]*(?:{heading})(?:[ \t]+|\r?\n)(.*?)"
        rf"(?=^[ \t]*(?:{following_headings})(?=[ \t]|\r?$)|\Z)"
    )
    match = pattern.search(section)
    return _clean(match.group(1)) if match else ""


def _sentence_with(text: str, pattern: re.Pattern[str]) -> str:
    for sentence in re.split(r"(?<=[。．])\s*|\n+", text):
        cleaned = _clean(sentence)
        if cleaned and pattern.search(cleaned):
            return cleaned
    return ""


def _metres(value: str, unit: str) -> float:
    number = float(value.replace(",", "").replace("，", ""))
    unit_lower = unit.casefold()
    if unit_lower == "km":
        return number * 1000.0
    if unit_lower == "cm":
        return number / 100.0
    return number


def _numeric_value(value: float) -> int | float:
    return int(value) if float(value).is_integer() else round(value, 6)


def _thickness_candidates(section: str) -> list[tuple[str, int | float, str]]:
    block = ""
    thickness_heading = re.compile(r"(?m)^[ \t]*層厚[ \t]+")
    for match in thickness_heading.finditer(section):
        prior_cues = list(_SECTION_CUE.finditer(section[:match.start()]))
        previous_heading = prior_cues[-1].group(0).strip() if prior_cues else ""
        # A wrapped lithology sentence can also begin with the word 層厚.  A
        # true unit-level subhead follows definition/distribution/contact (or
        # occurs near the start of a terse unit section), not 岩相/層相/層序.
        if (
            previous_heading.startswith(("地層名", "定義", "模式地", "分布", "層序関係"))
            or (not previous_heading and match.start() < 1500)
        ):
            tail = section[match.end():]
            next_heading = re.search(
                r"(?m)^[ \t]*(?:地層名及び定義|定義及び名称|模式地|分布(?:・地形)?|"
                r"層序関係|層序|層相|岩相|化石|古環境|年代|時代及び対比|対比)"
                r"(?=[ \t]|$)",
                tail,
            )
            block = _clean(tail[:next_heading.start()] if next_heading else tail)
            break
    if block:
        flat_block = re.sub(r"\s+", " ", block).strip()
        sentences = [
            _clean(value)
            for value in re.split(r"(?<=[。．])\s*", flat_block)
            if _clean(value)
        ]
        relevant = sentences[:1]
        relevant.extend(
            sentence for sentence in sentences[1:]
            if not re.search(r"高さ|標高|比高|幅", sentence)
        )
        quote = "層厚 " + " ".join(relevant)
    else:
        stratigraphy = _named_block(
            section,
            "層序",
            "地層名及び定義|定義及び名称|模式地|分布(?:・地形)?|層序関係|"
            "層厚|層相|岩相|化石|古環境|年代|時代及び対比|対比",
        )
        stratigraphy_sentences = [
            _clean(value)
            for value in re.split(
                r"(?<=[。．])\s*",
                re.sub(r"\s+", " ", stratigraphy).strip(),
            )
            if _clean(value)
        ]
        overall = [
            sentence for sentence in stratigraphy_sentences
            if re.search(r"正確な層厚|全層厚|本層の層厚|本地域における.{0,20}層厚", sentence)
        ]
        if overall:
            quote = "層序 " + " ".join(overall)
        elif not stratigraphy and len(section) < 2500:
            # Some very short Holocene units are prose-only and contain no
            # named 層厚 subhead.  Use only their explicit thickness sentence.
            quote = _sentence_with(section, re.compile(r"層厚\s*" + _NUMBER))
        else:
            quote = ""
    if not quote:
        return []

    minimums: list[float] = []
    maximums: list[float] = []
    measurements = list(_THICKNESS_VALUE.finditer(quote))
    unknown_total = bool(re.search(r"(?:正確な)?層厚は不明", re.sub(r"\s+", "", quote)))
    for match in measurements:
        lower = _metres(match.group("lower"), match.group("unit"))
        upper = _metres(match.group("upper"), match.group("unit")) if match.group("upper") else lower
        lo, hi = sorted((lower, upper))
        prefix = str(match.group("prefix") or "")
        qualifier = str(match.group("qualifier") or "")
        preceding = quote[max(0, match.start() - 35):match.start()]
        following = quote[match.end():match.end() + 80]
        compact_preceding = re.sub(r"\s+", "", preceding)
        compact_following = re.sub(r"\s+", "", following)
        is_maximum = prefix.startswith("最大") or bool(
            re.search(r"最大(?:層厚)?[^0-9]{0,25}$", compact_preceding)
            or re.search(r"^[^0-9]{0,60}(?:本層の)?最大層厚", compact_following)
        )
        is_minimum = prefix.startswith("最小") or bool(
            re.search(r"最小(?:層厚)?[^0-9]{0,25}$", compact_preceding)
            or re.search(r"少なくとも[^0-9]{0,25}$", compact_preceding)
        )
        if is_maximum:
            maximums.append(hi)
        elif is_minimum:
            minimums.append(lo)
        elif qualifier == "以上":
            minimums.append(lo)
        elif qualifier == "以下":
            maximums.append(hi)
        elif unknown_total and len(measurements) == 1:
            # If the lower contact lies outside the map and total thickness is
            # explicitly unknown, a single within-map estimate is an observed
            # upper extent, not a defensible minimum.
            maximums.append(hi)
        else:
            minimums.append(lo)
            maximums.append(hi)

    rows: list[tuple[str, int | float, str]] = []
    if minimums:
        rows.append(("min_thickness", _numeric_value(min(minimums)), quote[:1600]))
    if maximums:
        rows.append(("max_thickness", _numeric_value(max(maximums)), quote[:1600]))
    return rows


_CONTACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("intrusive", re.compile(r"(?:本岩体|本深成岩体|本層|本堆積物|岩体).{0,120}に貫入(?:する|して|し)")),
    ("fault", re.compile(r"(?:下位|基盤|境界).{0,100}(?:断層で接|断層関係|断層により)")),
    ("unconformable", re.compile(r"を不整合に覆(?:う|い|って|った)")),
    ("conformable", re.compile(r"を(?:ほぼ)?整合(?:的)?に覆(?:う|い|って)|(?:下位|基底).{0,100}と整合")),
    ("gradational", re.compile(r"(?:下位|基底).{0,100}(?:から漸移|と漸移|漸移する)")),
)

_JAPANESE_LITHOLOGY = (
    ("軽石火山礫凝灰岩", "tuff"),
    ("流紋岩質溶結凝灰岩", "tuff"),
    ("火山礫凝灰岩", "tuff"),
    ("溶結凝灰岩", "tuff"),
    ("凝灰角礫岩", "tuffaceous breccia"),
    ("火山砕屑岩", "volcaniclastic"),
    ("火砕岩", "volcaniclastic"),
    ("珪藻質泥岩", "mudstone"),
    ("千枚岩質泥岩", "phyllite; mudstone"),
    ("粘板岩質泥岩", "slate"),
    ("葉理質泥岩", "laminated mudstone"),
    ("珪質泥岩", "mudstone"),
    ("砂質泥岩", "sandy mudstone"),
    ("泥質砂岩", "muddy sandstone"),
    ("石灰質礫岩", "calcareous conglomerate"),
    ("砂岩泥岩互層", "sandstone; mudstone"),
    ("砂泥互層", "sandstone; mudstone"),
    ("砂礫層", "gravel"),
    ("モンゾ斑れい岩", "gabbro"),
    ("石英モンゾニ岩", "quartz monzonite"),
    ("花崗閃緑岩", "granodiorite"),
    ("石英安山岩", "dacite"),
    ("硬質頁岩", "shale"),
    ("ポーセラナイト", "porcellanite"),
    ("陶器岩", "porcellanite"),
    ("珪藻岩", "diatomite"),
    ("珪藻土", "diatomite"),
    ("シルト岩", "siltstone"),
    ("凝灰岩", "tuff"),
    ("角礫岩", "breccia"),
    ("礫岩", "conglomerate"),
    ("砂岩", "sandstone"),
    ("泥岩", "mudstone"),
    ("チャート", "chert"),
    ("石灰岩", "limestone"),
    ("苦鉄質岩", "mafic"),
    ("玄武岩", "basalt"),
    ("安山岩", "andesite"),
    ("デイサイト", "dacite"),
    ("流紋岩", "rhyolite"),
    ("貫入岩", "igneous"),
    ("亜炭", "lignite"),
    ("軽石", "pumice"),
    ("シルト層", "silt"),
    ("砂層", "sand"),
    ("泥層", "mud"),
    ("礫層", "gravel"),
)
_JAPANESE_LITH_RE = re.compile(
    "|".join(re.escape(source) for source, _target in _JAPANESE_LITHOLOGY)
)
_JAPANESE_LITH_MAP = dict(_JAPANESE_LITHOLOGY)


def _split_terms(value: Any) -> list[str]:
    return [term.strip() for term in str(value or "").split(";") if term.strip()]


def _japanese_lith_terms(text: str) -> list[str]:
    values: list[str] = []
    for match in _JAPANESE_LITH_RE.finditer(str(text or "")):
        for term in _split_terms(_JAPANESE_LITH_MAP[match.group(0)]):
            if term not in values:
                values.append(term)
    return values


def _lithology_candidates(
    section: str,
    unit_name: str,
    current_values: Mapping[str, Any] | None,
) -> list[tuple[str, str, str]]:
    if not unit_name.casefold().endswith((" formation", " pluton")):
        return []
    definition = _named_block(
        section,
        "地層名及び定義|定義及び名称|定義",
        "地層名|先行層序区分|模式地|分布(?:・地形)?|層序関係|層序|層厚|"
        "層相|岩相|化石|古環境|年代|時代及び対比|対比",
    )
    if not definition:
        definition = _named_block(
            section,
            "層相|岩相",
            "地層名及び定義|定義及び名称|定義|地層名|模式地|分布(?:・地形)?|"
            "層序関係|層序|層厚|化石|古環境|年代|時代及び対比|対比",
        )
    # PDF text extraction can interleave a figure/table caption from the
    # neighbouring column with the definition.  Those captions often describe
    # another unit and must not be promoted to this unit's lithology.
    intrusion = re.search(
        r"(?m)^[ \t]*(?:第\s*[0-9０-９]+(?:\s*[.・-]\s*[0-9０-９]+)*\s*[図表]|"
        r"[−―ー-]\s*[0-9０-９]+\s*[−―ー-]?)",
        definition,
    )
    if intrusion:
        definition = definition[:intrusion.start()]
    quote = _clean(definition)[:1500]
    all_terms = _japanese_lith_terms(quote)
    if not quote or not all_terms:
        return []

    strong: list[str] = []
    flat = re.sub(r"\s+", " ", quote)
    for sentence in re.split(r"(?<=[。．])\s*", flat):
        without_parentheses = re.sub(r"（[^）]{0,100}）", "", sentence)
        compact_sentence = re.sub(r"\s+", "", without_parentheses)
        for cue in re.finditer(r"を主体(?:とする|とし|する)|が卓越|半分以上を占める", compact_sentence):
            phrase = compact_sentence[max(0, cue.start() - 180):cue.end()]
            strong = _japanese_lith_terms(phrase)
            break
        if strong:
            # Later dominance statements commonly belong to a member or local
            # facies.  The first explicit definition-level statement is the
            # defensible formation-scale role assignment.
            break

    current = current_values or {}
    existing_major = _split_terms(current.get("lithology"))
    existing_minor = _split_terms(current.get("minor_lith"))
    compact_flat = re.sub(r"\s+", "", quote)
    if re.search(r"相と.{0,120}相とに分かれ|側方変化の関係", compact_flat):
        # A short Japanese excerpt may omit a lithology independently attested
        # by the abstract.  Expand/validate only when the current controlled
        # terms are all present in this excerpt; never silently drop one.
        final_major = (
            all_terms
            if not existing_major or set(existing_major).issubset(set(all_terms))
            else existing_major
        )
    elif len(strong) >= 2:
        final_major = strong
    elif (
        len(existing_major) > 1
        and strong
        and set(existing_major).issubset(set(all_terms))
    ):
        final_major = strong
    elif existing_major:
        final_major = existing_major
    else:
        final_major = strong or all_terms[:1]

    output: list[tuple[str, str, str]] = []
    if final_major and final_major != existing_major:
        output.append(("lithology", "; ".join(final_major), quote))
    minor = [term for term in all_terms if term not in final_major]
    # The compiled layer treats a candidate as a replacement, not an additive
    # patch.  Preserve an existing minor-lithology list rather than replacing
    # it with a narrower definition excerpt.
    if minor and not existing_minor:
        output.append(("minor_lith", "; ".join(minor), quote))
    return output


def _contact_candidate(section: str) -> tuple[str, str] | None:
    block = _named_block(
        section,
        "層序関係",
        "地層名及び定義|定義及び名称|模式地|分布(?:・地形)?|層厚|層相|岩相|年代",
    )
    search_text = f"層序関係 {block}" if block else section
    for value, pattern in _CONTACT_PATTERNS:
        quote = _sentence_with(search_text, pattern)
        if quote and re.search(r"陸中関地域|隣接地域", quote) and "本地域" not in quote:
            continue
        if quote:
            return value, quote[:1600]
    return None


def _environment_candidate(section: str) -> tuple[str, str] | None:
    block = _named_block(
        section,
        "層相|岩相",
        "地層名及び定義|定義及び名称|模式地|分布(?:・地形)?|層序関係|層厚|年代",
    )
    if block:
        quote = _sentence_with(block, re.compile(r"河川性|扇状地"))
        if "河川性" in quote:
            return "fluvial indet.", quote[:1600]
        if "扇状地" in quote:
            return "alluvial fan", quote[:1600]
    return None


def extract_japanese_candidates(
    routed: Mapping[str, Any],
    get_page_text: PageTextGetter,
    current_units: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Extract source-bound candidates from routed raw PDF column text."""

    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for context in routed.get("contexts") or []:
        if not isinstance(context, Mapping):
            continue
        unit_id = str(context.get("unit_id") or "").strip()
        section_hit = _target_section(context, get_page_text)
        if not unit_id or section_hit is None:
            continue
        section, source_page = section_hit
        candidates: list[tuple[str, Any, str, str]] = []
        candidates.extend((*row, "explicit") for row in _thickness_candidates(section))
        contact = _contact_candidate(section)
        current_basal = str(
            ((current_units or {}).get(unit_id) or {}).get("basal_surface") or ""
        ).strip()
        if contact and (not current_basal or contact[0] == current_basal):
            # Do not let a member-to-member or facies relation inside the raw
            # Japanese section replace an already source-bound formation-scale
            # basal relation from the English abstract.
            candidates.append(("basal_surface", contact[0], contact[1], "explicit"))
        environment = _environment_candidate(section)
        if environment and environment[0] == "alluvial fan":
            unit_name = str(context.get("unit_name") or "").casefold()
            if "fan" not in unit_name and not any(
                "扇状地" in alias for alias in _japanese_aliases(context)
            ):
                environment = None
        if environment:
            candidates.append(("environment", environment[0], environment[1], "explicit"))
        for field, candidate, quote in _lithology_candidates(
            section,
            str(context.get("unit_name") or ""),
            (current_units or {}).get(unit_id),
        ):
            candidates.append((field, candidate, quote, "explicit"))

        for field, candidate, quote, assertion in candidates:
            key = (unit_id, field, str(candidate).casefold())
            if key in seen or not quote:
                continue
            seen.add(key)
            output.append({
                "unit_id": unit_id,
                "unit_name": context.get("unit_name"),
                "field": field,
                "candidate": candidate,
                "quote": quote,
                "assertion": assertion,
                "confidence_class": "B",
                "pdf_page": source_page,
                "printed_page": context.get("printed_page"),
            })
    return output


def _evidence_id(row: Mapping[str, Any]) -> str:
    raw = json.dumps(
        {key: row.get(key) for key in ("unit_id", "field", "candidate", "quote", "pdf_page")},
        ensure_ascii=False,
        sort_keys=True,
    )
    return "ev_local_ja_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def apply_local_japanese_science(
    system_dir: str | os.PathLike[str],
    routed: Mapping[str, Any],
    *,
    source_file: str | os.PathLike[str],
    generated_at: str,
) -> dict[str, Any]:
    """Append verified Japanese-body candidates and rebuild the canonical layer."""

    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dependency failure path
        raise RuntimeError("pdfplumber is required for raw Japanese PDF extraction") from exc

    root = Path(system_dir).expanduser().resolve()
    source = Path(source_file).expanduser().resolve()
    compiled = json.loads((root / "compiled.json").read_text(encoding="utf-8"))
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))

    cache: dict[int, tuple[str, str]] = {}
    with pdfplumber.open(source) as document:
        def get_page_text(page_number: int) -> Sequence[str]:
            if page_number in cache:
                return cache[page_number]
            if page_number < 1 or page_number > len(document.pages):
                raise IndexError(page_number)
            page = document.pages[page_number - 1]
            width, height = float(page.width), float(page.height)
            left = page.crop((0, 0, width * 0.495, height)).extract_text(
                x_tolerance=2, y_tolerance=3
            ) or ""
            right = page.crop((width * 0.505, 0, width, height)).extract_text(
                x_tolerance=2, y_tolerance=3
            ) or ""
            cache[page_number] = (left, right)
            return cache[page_number]

        candidates = extract_japanese_candidates(
            routed,
            get_page_text,
            {
                str(unit.get("unit_id") or ""): (unit.get("values") or {})
                for unit in compiled.get("units") or []
            },
        )

    additions = [{
        "evidence_id": _evidence_id(row),
        "unit_id": row["unit_id"],
        "scope_type": "unit_global",
        "field": row["field"],
        "candidate": row["candidate"],
        "source_type": "PDF",
        "source_file": str(source),
        "source_locator": " / ".join(value for value in (
            f"PDF p.{row.get('pdf_page')}" if row.get("pdf_page") else "",
            f"printed p.{row.get('printed_page')}" if row.get("printed_page") else "",
        ) if value),
        "PDF_page": row.get("pdf_page"),
        "printed_page": row.get("printed_page"),
        "full_context_quote": row["quote"],
        "confidence_class": row["confidence_class"],
        "assertion": row["assertion"],
        "selection": "validation",
        "resolution_state": (
            "source_verified_free_text" if row["field"] == "environment" else None
        ),
        "extraction_method": "deterministic raw Japanese GSJ section grammar",
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
        "stage": "local_japanese_science",
        "status": "complete" if candidates else "no_matches",
        "external_calls": 0,
        "added_evidence": len(additions),
        "units": len({row["unit_id"] for row in candidates}),
        "raw_pdf_pages_read": len(cache),
        "fields": {
            field: sum(row["field"] == field for row in candidates)
            for field in sorted({row["field"] for row in candidates})
        },
    }


__all__ = [
    "SCHEMA_VERSION",
    "apply_local_japanese_science",
    "extract_japanese_candidates",
]
