# -*- coding: utf-8 -*-
"""Bootstrap a reviewable unit inventory when a map has PDF only.

The stage is deliberately conservative: Gemini may propose the inventory, but
every retained unit name must occur in the English Abstract and every retained
field must pass the existing verbatim-quote verifier.  The response is cached
per map/source/prompt/model before canonical JSON is rebuilt.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

from common import best_interval_for_age, load_secret, valid_strat_name
from compiled_layer import build_canonical_layer
from formation_consolidation import formation_key
from llm_extract import (
    FIELD_QUOTES,
    MODEL,
    PROMPT,
    verify,
    vocab_hint,
)
from llm_router import LLMRequest, LLMRouter, ValidationReport, single_provider_router
from pilot_llm import PilotBudgetExceeded, PilotLLMError, SourceDocument, estimate_tokens, preflight_budget


SCHEMA_VERSION = "pdf-unit-bootstrap/1.0"
VALIDATOR_VERSION = "pdf-unit-bootstrap-validator/1.2"
STAGE = "pdf_unit_bootstrap"
PROMPT_VERSION = "pdf-unit-inventory-v2"

BOOTSTRAP_RULES = """

Additional inventory rules:
- This map has no usable ZFK or Shapefile unit inventory. Return EVERY
  geological unit, formation, member, lava, pluton, terrace deposit,
  pyroclastic-flow deposit, and surficial/alluvial deposit stated in the
  Abstract, once only.
- Include young and minor deposits such as river-bed deposits, flood-plain and
  valley-floor deposits, landslide deposits, and colluvial/alluvial-cone
  deposits whenever the Abstract text or its figure explanation lists them.
- In coordinated lists, expand a shared suffix. For example, "Asanai and the
  Mukaikawara terrace deposits" names BOTH Asanai terrace deposits and
  Mukaikawara terrace deposits. Likewise, a final plural "members" applies to
  every named item in that coordinated member list.
- Preserve the Abstract's stratigraphic presentation order, youngest first
  when the text makes that order clear. Do not invent an order from ages.
- Generic rank labels such as higher, middle, lower, higher-lower, and
  lower-lower terrace deposits are inventory containers, not named units.
  Return the specific named terrace deposits beneath them, not the labels.
- unit_name must be copied exactly from the Abstract. A name that cannot be
  traced to either a direct phrase or a grammatically shared suffix in one
  Abstract sentence will be rejected automatically.
- Do not add regional headings, periods, or groups used only as headings.
"""

# These are not inferred geological names.  They are explicit inventory terms
# that abstracts often call "young and minor deposits", which generative
# extraction has repeatedly treated as prose rather than units.  A candidate
# is added only when its exact phrase is present in the supplied Abstract.
SURFICIAL_UNIT_PATTERNS = (
    ("Landslide deposits", re.compile(r"\blandslide\s+deposits\b", re.IGNORECASE)),
    (
        "Floodplain and valley-floor deposits",
        re.compile(r"\bflood-?plain\s+and\s+valley-?floor\s+deposits\b", re.IGNORECASE),
    ),
    (
        "Colluvial and alluvial cone deposits",
        re.compile(r"\bcolluvial\s+and\s+alluvial\s+cone\s+deposits\b", re.IGNORECASE),
    ),
    ("River bed deposits", re.compile(r"\briver-?bed\s+deposits\b", re.IGNORECASE)),
)

# Aggregate prose labels organize named deposits but are not review units.
# Providers sometimes echo them from relationship sentences even though the
# prompt forbids headings.  Drop only this closed, evidence-reviewed set; all
# other unexpected names still count as invalid candidates and can fail over.
AGGREGATE_INVENTORY_HEADINGS = frozenset({
    "terrace deposits",
    "higher terrace deposits",
    "middle terrace deposits",
    "lower terrace deposits",
    "higher lower terrace deposits",
    "lower lower terrace deposits",
    "pyroclastic flow deposits",
    "other young and minor deposits",
})

# Conservative completeness hints.  These are never inserted automatically;
# they only detect obvious, directly named proper units that a provider omitted.
_PROPER_UNIT_WORD = r"[A-Z][A-Za-z]*(?:[-'][A-Za-z]+)*"
# 2026-08-12: 物差しから Group を外した。BOOTSTRAP_RULES は「見出しとしてのみ
# 使われる group は加えない」と明示しており、group名は strat_name 側が持つ。
# ヒントに残すと、規則どおり除外したモデルを完全性不足として罰してしまう。
_UNIT_SUFFIXES = (
    "Formation|Member|Lava|Volcanics|Complex|Pluton|Granite|Granodiorite|Tuff|Deposits"
)
DIRECT_UNIT_HINT_PATTERN = re.compile(
    rf"\b((?:{_PROPER_UNIT_WORD}\s+){{1,5}}(?:{_UNIT_SUFFIXES}))\b"
)
_UNIT_SUFFIX_PATTERN = re.compile(rf"\b(?:{_UNIT_SUFFIXES}|Group)\b")
_HINT_PREFIXES = {"abstract", "the", "other", "higher", "flows", "they"}
# 地質時代・堆積様式の修飾語が頭に付いた形は同じユニットの言い換えでしかない。
# 例: "Early Pliocene Toya Formation" -> "Toya Formation"
_HINT_AGE_PREFIXES = {
    "early", "middle", "late", "lower", "upper", "latest", "earliest",
    "holocene", "pleistocene", "pliocene", "miocene", "oligocene", "eocene",
    "paleocene", "cretaceous", "jurassic", "triassic", "neogene", "paleogene",
    "quaternary", "subaerial", "submarine", "terrestrial",
}

# PDF tables are flattened by text extraction.  A value in the depositional-
# environment column can therefore be inserted into the adjacent unit name,
# e.g. ``Toya | Subaerial | Formation`` becomes ``Toya Subaerial Formation``.
# This list is only used to remove the longer candidate when the same response
# also contains the shorter, directly traceable unit name.  It never invents a
# unit and never drops a unique name on the adjective alone.
TABLE_ENVIRONMENT_WORDS = frozenset({
    "aerial", "alluvial", "bathyal", "continental", "deep", "fluvial",
    "lacustrine", "littoral", "marine", "nonmarine", "shelf", "shallow",
    "subaerial", "subbathyal", "submarine", "terrestrial",
})
TABLE_UNIT_SUFFIXES = frozenset({
    "complex", "deposits", "formation", "granite", "granodiorite", "lava",
    "member", "pluton", "tuff", "volcanics",
})

COORDINATED_SUFFIXES = (
    (" Member", re.compile(r"\bmembers?\b", re.IGNORECASE)),
    (" terrace deposits", re.compile(r"\bterrace\s+deposits\b", re.IGNORECASE)),
    (
        " Pyroclastic Flow Deposits",
        re.compile(r"\bpyroclastic\s+flow\s+deposits\b", re.IGNORECASE),
    ),
)

Executor = Callable[[str, SourceDocument], Mapping[str, Any]]


@dataclass(frozen=True)
class BootstrapResult:
    bundle: dict[str, Any]
    manifest: dict[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _normalise(value: Any) -> str:
    text = str(value or "").replace("–", "-").replace("—", "-").replace("−", "-")
    text = text.replace("’", "'").replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip().casefold()


def _name_pattern(value: str) -> re.Pattern[str] | None:
    """Build a PDF-line-wrap-tolerant literal word-sequence pattern."""

    value = re.sub(r"\bfloodplain\b", "flood plain", str(value or ""), flags=re.IGNORECASE)
    tokens = re.findall(r"[^\W_]+(?:['’][^\W_]+)?", str(value or ""), re.UNICODE)
    if not tokens:
        return None
    # A PDF may represent an ordinary separator as a space, a hyphen, or a
    # line-ending hyphen.  Word order and every word must still match.
    separator = r"(?:\s+|-\s*)"
    return re.compile(
        r"(?<![A-Za-z0-9])" + separator.join(re.escape(token) for token in tokens)
        + r"(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


def _context_for_span(text: str, start_at: int, end_at: int) -> str:
    """Return the surrounding prose sentence while ignoring PDF line wraps."""

    paragraph = text.rfind("\n\n", 0, start_at)
    period = max(text.rfind(". ", 0, start_at), text.rfind(".\n", 0, start_at))
    paragraph_start = paragraph + 2 if paragraph >= 0 else 0
    period_start = period + 2 if period >= 0 else 0
    start = max(paragraph_start, period_start, start_at - 350, 0)
    endings = [
        value for value in (
            text.find(". ", end_at), text.find(".\n", end_at), text.find("\n\n", end_at)
        ) if value >= 0
    ]
    end = min(endings) + 1 if endings else min(len(text), end_at + 500)
    return re.sub(r"\s+", " ", text[start:end]).strip()[:900]


def _name_context(text: str, name: str) -> str | None:
    pattern = _name_pattern(name)
    match = pattern.search(text) if pattern else None
    if match:
        return _context_for_span(text, match.start(), match.end())

    # English geological inventories routinely omit a repeated suffix from
    # all but the final item in a coordinated list.  Accept that ellipsis only
    # for three explicit unit-type suffixes, and only when the stem, suffix,
    # and a coordination cue occur in the same surrounding sentence.
    for suffix, suffix_pattern in COORDINATED_SUFFIXES:
        if not str(name).casefold().endswith(suffix.casefold()):
            continue
        stem = str(name)[:-len(suffix)].strip()
        stem_pattern = _name_pattern(stem)
        stem_match = stem_pattern.search(text) if stem_pattern else None
        if not stem_match:
            continue
        context = _context_for_span(text, stem_match.start(), stem_match.end())
        if suffix_pattern.search(context) and (
            re.search(r"\b(?:and|namely)\b", context, re.IGNORECASE) or "," in context
        ):
            return context
    return None


def _surficial_candidates(text: str) -> list[dict[str, Any]]:
    """Return every explicitly listed generic surface unit exactly once."""

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for canonical_name, pattern in SURFICIAL_UNIT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        name = canonical_name
        key = formation_key(name)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "unit_name": name,
            "_coverage_method": "explicit_surficial_scan",
            "_source_phrase": re.sub(r"\s+", " ", match.group(0)).strip(),
        })
    return candidates


_COORDINATED_MEMBER_CLAUSE = re.compile(
    r"\b(?:subdivided|divided)\s+into\s+"
    r"(?P<body>.{1,700}?)\bmembers?\b"
    r"\s*,?\s*(?:generally\s+)?(?:in\s+)?(?:ascending|descending)\s+order",
    re.IGNORECASE,
)
_LEADING_MEMBER_COUNT = re.compile(
    r"^(?:the\s+)?(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)?"
    r"\s*members?\s*,?\s*(?:namely\s+)?",
    re.IGNORECASE,
)


def _coordinated_member_candidates(text: str) -> list[dict[str, Any]]:
    """Expand Abstract lists whose ``Member`` suffix appears only at the end.

    GSJ English abstracts commonly write ``the Tate Sandstone and Sikonai
    Siltstone members``.  Each proper-name stem is explicit primary-source
    evidence, but a simple suffix regex sees only the final item.  Expansion
    is restricted to ``subdivided/divided into ... members ... order`` clauses;
    every result must also pass the existing same-sentence ``_name_context``
    verifier.  No geological name is translated or inferred.
    """

    flattened = re.sub(r"\s+", " ", str(text or "")).strip()
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _COORDINATED_MEMBER_CLAUSE.finditer(flattened):
        body = str(match.group("body") or "").strip(" ,;")
        body = _LEADING_MEMBER_COUNT.sub("", body).strip(" ,;")
        if "namely" in body.casefold():
            body = re.split(r"\bnamely\b", body, flags=re.IGNORECASE)[-1].strip(" ,;")
        for raw_item in re.split(r"\s*,\s*|\s+and\s+", body, flags=re.IGNORECASE):
            stem = re.sub(
                r"^(?:(?:and|or)\s+)?(?:(?:the|a|an)\s+)?",
                "",
                raw_item.strip(),
                flags=re.IGNORECASE,
            )
            stem = re.sub(r"\s+", " ", stem).strip(" ,;:-")
            words = stem.split()
            if not (2 <= len(words) <= 6) or not words[0][:1].isupper():
                continue
            if any(word.casefold() in {"formation", "member", "members"} for word in words):
                continue
            name = stem + " Member"
            key = _inventory_name_key(name)
            context = _name_context(text, name)
            if not key or key in seen or context is None:
                continue
            seen.add(key)
            candidates.append({
                "unit_name": name,
                "_coverage_method": "coordinated_member_suffix_expansion",
                "_source_phrase": context,
            })
    return candidates


def _inventory_hints(text: str) -> list[str]:
    """Return a conservative minimum set of directly visible inventory names."""
    output: list[str] = []
    seen: set[str] = set()
    # Preserve real compound-name hyphens while removing only PDF line wraps.
    # Without this, ``Towada-\nHachinohe`` is reduced to the false hint
    # ``Hachinohe Pyroclastic Flow Deposits`` and a correct response is rejected.
    hint_text = re.sub(r"-\s*\n\s*", "-", text)
    for match in DIRECT_UNIT_HINT_PATTERN.finditer(hint_text):
        words = match.group(1).split()
        while words and words[0].casefold() in (_HINT_PREFIXES | _HINT_AGE_PREFIXES):
            words.pop(0)
        name = " ".join(words).strip()
        if not name or _name_context(text, name) is None:
            continue
        # 正規表現が2つのユニット名を接着した形（"Toya Subaerial Formation
        # Sannohe Group" など）は、どちらの実在ユニットとも一致しない。
        if len(_UNIT_SUFFIX_PATTERN.findall(name)) > 1:
            continue
        key = _inventory_name_key(name)
        if key and key not in seen:
            seen.add(key)
            output.append(name)
    for candidate in _surficial_candidates(text):
        name = str(candidate.get("unit_name") or "")
        key = _inventory_name_key(name)
        if key and key not in seen:
            seen.add(key)
            output.append(name)
    for candidate in _coordinated_member_candidates(text):
        name = str(candidate.get("unit_name") or "")
        key = _inventory_name_key(name)
        if key and key not in seen:
            seen.add(key)
            output.append(name)
    keys = {_inventory_name_key(name) for name in output}
    return [
        name for name in output
        if not (
            (shorter := _table_column_contamination_key(name))
            and shorter in keys
        )
    ]


def _inventory_name_key(value: Any) -> str:
    """Comparison key tolerant of cosmetic hyphen/spacing/diacritic differences.

    2026-08-12: 完全性ヒントが `Zyumonzi Formation` を、モデルが原文どおりの
    `Zyūmonzi Formation` を返し、同じ地層が別物として数えられていた。
    長音記号の有無は表記の違いでしかないので、比較キーからは落とす。
    """

    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return formation_key(text).replace(" ", "")


def stable_unit_identity(map_id: Any, unit_name: Any) -> str:
    """Return a cosmetic-spelling-tolerant identity independent of row order."""

    identity = f"{str(map_id).strip().lstrip('mM')}|{_inventory_name_key(unit_name)}"
    return "unit_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _table_column_contamination_key(value: Any) -> str | None:
    """Return the shorter unit key implied by an interleaved table label."""

    tokens = formation_key(value).split()
    if len(tokens) < 3 or tokens[-1] not in TABLE_UNIT_SUFFIXES:
        return None
    end = len(tokens) - 1
    start = end
    while start > 0 and tokens[start - 1] in TABLE_ENVIRONMENT_WORDS:
        start -= 1
    if start == end or start == 0:
        return None
    return _inventory_name_key(" ".join(tokens[:start] + [tokens[-1]]))


def _drop_table_column_contamination(
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop only a longer table-flattened alias when its shorter unit exists."""

    keys = {_inventory_name_key(row.get("unit_name")) for row in candidates}
    accepted: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for candidate in candidates:
        shorter = _table_column_contamination_key(candidate.get("unit_name"))
        if shorter and shorter in keys:
            dropped.append({
                "unit_name": str(candidate.get("unit_name") or ""),
                "reason": (
                    "table column label was interleaved into a unit name; "
                    "the shorter directly traceable unit is also present"
                ),
            })
            continue
        accepted.append(dict(candidate))
    return accepted, dropped


def _is_aggregate_inventory_heading(value: Any) -> bool:
    return _normalise(value) in AGGREGATE_INVENTORY_HEADINGS


def _prior_stable_ids(
    cache_dir: Path,
    *,
    map_id: str,
    source_sha256: str,
    exclude_job_id: str,
) -> dict[str, str]:
    """Recover IDs implied by older accepted inventories before cache invalidation."""

    result: dict[str, str] = {}
    documents: list[tuple[str, Path, Mapping[str, Any]]] = []
    for path in sorted(cache_dir.glob("pboot_*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            str(document.get("map_id") or "") != str(map_id)
            or document.get("source_sha256") != source_sha256
            or not isinstance(document.get("candidates"), list)
        ):
            continue
        # Older prompt versions define the already-published IDs.  The current
        # job is considered only after them so newly added units cannot shift
        # an existing ordinal.
        priority = "1" if document.get("job_id") == exclude_job_id else "0"
        documents.append((priority + str(document.get("completed_at") or ""), path, document))
    # IDは「そのキャッシュ内での候補の並び順」から作る。キャッシュ間で並びが
    # 食い違うと、別々の地層が同じ番号を主張しうる。setdefault は同じ *キー* の
    # 上書きしか防がないので、値の一意性はここで別途守る必要がある。
    # 守らないと unit_id が重複し、下流の年代補完が「同じユニットで上下を挟む」
    # 状態を安全と誤認して、無関係な年代を伝播させる。
    assigned: set[str] = set()
    for _priority, _path, document in sorted(documents, key=lambda item: (item[0], str(item[1]))):
        for index, candidate in enumerate(document.get("candidates") or [], start=1):
            if not isinstance(candidate, Mapping):
                continue
            key = _inventory_name_key(candidate.get("unit_name"))
            if not key or key in result:
                continue
            unit_id = f"m{map_id}_p{index:03d}"
            if unit_id in assigned:
                # 別の地層が既にこの番号を取っている。ここで渡すと重複するので、
                # この地層にはIDを与えず、_evidence_rows に新しい番号を採らせる。
                continue
            result[key] = unit_id
            assigned.add(unit_id)
    return result


def _build_prompt(source: SourceDocument) -> str:
    return (
        PROMPT.replace("{vocab}", vocab_hint() + BOOTSTRAP_RULES)
        .replace("{abstract}", source.text)
    )


def build_prompt(source: SourceDocument) -> str:
    """Build the production bootstrap prompt for a source document."""

    return _build_prompt(source)


def _job(map_id: str, source: SourceDocument, model: str | None = None) -> dict[str, Any]:
    prompt = _build_prompt(source)
    prompt_sha = _sha(prompt)
    estimated_input = estimate_tokens(prompt)
    reserved_output = max(4096, min(32768, len(source.text) // 2))
    identity = {
        "stage": STAGE,
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "map_id": str(map_id),
        "source_sha256": source.sha256,
        "prompt_sha256": prompt_sha,
    }
    return {
        **identity,
        "job_id": "pboot_" + _sha(json.dumps(identity, sort_keys=True, separators=(",", ":")))[:20],
        "prompt": prompt,
        "estimated_input_tokens": estimated_input,
        "reserved_output_tokens": reserved_output,
        "estimated_tokens": estimated_input + reserved_output,
    }


# ---------------------------------------------------------------------------
# バッチ版 bootstrap（2026-08-12 追加）
#
# 一戸(48 unit)のinventoryを1応答で出させると約11,400 output tokenが要る。
# 実測では mistral-small が8,103でvalidation、gemini-3.5-flash-liteが9,864、
# gemini-3.6-flashも json_parse となり、どのモデルも途中で切れた。
# Column stageで実証済みの「小さく分けて何度も聞く」形へ変える。
#
#   A. 名前の列挙だけを1回   (出力 ≈ 1,000 token)
#   B. 8件ずつ詳細を N 回     (出力 ≈ 2,000 token / 回)
#
# 検証は変えない。全バッチを決定的にマージしてから、従来と同じ
# validate_inventory_response を1回だけ通す。閾値も据え置き。
# ---------------------------------------------------------------------------

BATCH_PROMPT_VERSION = "pdf-unit-inventory-batched-v2"
DETAIL_BATCH_SIZE = 8

NAME_PROMPT = """You are reading the English Abstract of a Geological Survey of Japan
1:50,000 quadrangle explanatory report.

List the name of every named geological unit the Abstract states.  Names only.
Do not return ages, lithology, descriptions, quotations or any other field.

Return JSON only, no markdown fence, in this exact shape:

{"unit_names": ["Shitazaki Formation", "Kadonosawa Formation"]}
{rules}

A member is a unit.  When the Abstract names a member such as
"Kamimetoki Sandstone Member" or "Kawaguchi Porcelanite Member", list that
member as its own entry in addition to the formation that contains it.

DETECTED_PHRASES below were matched mechanically in this Abstract.  Some are
real named units and some are not.  Include every phrase that names a real
geological unit, using the unit's own name.  Do not include a phrase that is
only a heading, a rank label, an age qualifier attached to a unit already
listed, or two unit names joined by the sentence.  Names outside this list are
still required whenever the Abstract states them.

DETECTED_PHRASES:
{hints}

ABSTRACT:
{abstract}
"""

DETAIL_PROMPT = """You are reading the English Abstract of a Geological Survey of Japan
1:50,000 quadrangle explanatory report.

Extract fields for the SUPPLIED_UNITS below and for nothing else.  Return one
object per supplied unit, keeping unit_name exactly as supplied.  Fill a field
**only when the Abstract actually states it**, and always give the quote that
supports it.  Omit a field rather than guessing.

Return JSON only, no markdown fence, in this exact shape:

{"units": [
  {"unit_name": "Shitazaki Formation",
   "b_age_ma": 10.5, "t_age_ma": 8.5,
   "age_quote": "the Shitazaki: 10.5-8.5 Ma",
   "lithology": "siltstone",
   "lith_quote": "mainly composed of siltstone",
   "unit_description": "The Shitazaki Formation is a shallow marine siltstone.",
   "desc_quote": "the Shitazaki Formation ... shallow marine"}
]}

Rules:
- Return every supplied unit exactly once, with its name unchanged.
- Do not add a unit that is not in SUPPLIED_UNITS.
- Every filled field needs its quote key copied verbatim from the Abstract.
{vocab}

SUPPLIED_UNITS:
{units}

ABSTRACT:
{abstract}
"""


def _batch_identity(map_id: str, source: SourceDocument, kind: str,
                    prompt_sha: str, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    identity = {
        "stage": STAGE,
        "schema_version": SCHEMA_VERSION,
        "prompt_version": BATCH_PROMPT_VERSION,
        "validator_version": SCHEMA_VERSION,
        "map_id": str(map_id),
        "source_sha256": source.sha256,
        "prompt_sha256": prompt_sha,
        "batch_kind": kind,
    }
    if extra:
        identity.update(dict(extra))
    return identity


def build_name_job(map_id: str, source: SourceDocument) -> dict[str, Any]:
    """段A: 名前の列挙だけを求めるジョブ。"""

    # 完全性の物差しに使っているヒントをそのまま見せる。ヒントには正規表現の
    # 誤検出（"Early Pliocene Toya Formation" 等）が混ざるので、機械で足さず
    # モデルに取捨させ、結果は本文照合で検証する。
    hints = _inventory_hints(source.text)
    prompt = (
        NAME_PROMPT.replace("{rules}", BOOTSTRAP_RULES)
        .replace("{hints}", json.dumps(hints, ensure_ascii=False, indent=2))
        .replace("{abstract}", source.text)
    )
    prompt_sha = _sha(prompt)
    identity = _batch_identity(map_id, source, "names", prompt_sha)
    estimated_input = estimate_tokens(prompt)
    reserved_output = 4096
    return {
        **identity,
        "job_id": "pbootn_" + _sha(json.dumps(identity, sort_keys=True, separators=(",", ":")))[:20],
        "prompt": prompt,
        "estimated_input_tokens": estimated_input,
        "reserved_output_tokens": reserved_output,
        "estimated_tokens": estimated_input + reserved_output,
    }


def build_detail_job(map_id: str, source: SourceDocument,
                     names: Sequence[str], batch_index: int) -> dict[str, Any]:
    """段B: 供給した名前だけの詳細を求めるジョブ。"""

    supplied = [str(name).strip() for name in names if str(name).strip()]
    if not supplied:
        raise PilotLLMError("detail batch requires at least one unit name")
    prompt = (
        DETAIL_PROMPT.replace("{vocab}", vocab_hint())
        .replace("{units}", json.dumps(supplied, ensure_ascii=False, indent=2))
        .replace("{abstract}", source.text)
    )
    prompt_sha = _sha(prompt)
    identity = _batch_identity(
        map_id, source, "detail", prompt_sha,
        {"batch_index": int(batch_index), "batch_names": list(supplied)},
    )
    estimated_input = estimate_tokens(prompt)
    # 1 unitあたり約250 token。8件でも2,000程度で、どの候補の上限にも収まる。
    reserved_output = max(1024, min(4096, 320 * len(supplied)))
    return {
        **identity,
        "job_id": "pbootd_" + _sha(json.dumps(identity, sort_keys=True, separators=(",", ":")))[:20],
        "prompt": prompt,
        "estimated_input_tokens": estimated_input,
        "reserved_output_tokens": reserved_output,
        "estimated_tokens": estimated_input + reserved_output,
    }


def validate_name_response(response: Mapping[str, Any], source: SourceDocument) -> ValidationReport:
    """段Aの検証。Abstractに実在する名前だけを残す（捏造を通さない）。"""

    raw = response.get("unit_names")
    if raw is None:
        raw = response.get("names")
    if not isinstance(raw, list):
        return ValidationReport(
            decision="reject", accepted=[],
            dropped=[{"reason": "name response must contain unit_names[]"}],
            metrics={"schema_valid": False},
        )
    accepted: list[str] = []
    dropped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        name = str(item or "").strip()
        key = _inventory_name_key(name)
        if not name or not key:
            continue
        if key in seen:
            continue
        if _is_aggregate_inventory_heading(name):
            dropped.append({"unit_name": name, "reason": "inventory container is not a unit"})
            continue
        # 名前がAbstract本文に辿れることを決定的に確認する。
        if _name_context(source.text, name) is None:
            dropped.append({"unit_name": name, "reason": "name is not traceable in the Abstract"})
            continue
        seen.add(key)
        accepted.append(name)
    # 決定的に分かる表層堆積物は、モデルが落としても必ず足す。
    for candidate in _surficial_candidates(source.text):
        name = str(candidate.get("unit_name") or "")
        key = _inventory_name_key(name)
        if key and key not in seen:
            seen.add(key)
            accepted.append(name)
    # Proper member stems in a tightly bounded coordinated list are equally
    # direct primary-source facts.  Add them deterministically when a provider
    # omits the repeated suffix; detail extraction still runs per supplied unit.
    for candidate in _coordinated_member_candidates(source.text):
        name = str(candidate.get("unit_name") or "")
        key = _inventory_name_key(name)
        if key and key not in seen:
            seen.add(key)
            accepted.append(name)
    filtered, contamination_drops = _drop_table_column_contamination(
        [{"unit_name": name} for name in accepted]
    )
    if contamination_drops:
        dropped.extend(contamination_drops)
        accepted = [str(row["unit_name"]) for row in filtered]
        seen = {_inventory_name_key(name) for name in accepted}
    if not accepted:
        return ValidationReport(
            decision="reject", accepted=[], dropped=dropped,
            metrics={"schema_valid": True, "name_count": 0},
        )
    # 完全性はこの段で見る。ここで reject にしないと router が次のproviderへ
    # 切り替えられず、「取りこぼしたまま詳細を集めて最後に失敗」になる。
    # 閾値は従来の最終判定と同じ 0.8 を使う（緩めていない）。
    hints = _inventory_hints(source.text)
    hint_keys = {_inventory_name_key(name) for name in hints}
    hint_keys.discard("")
    matched = hint_keys & seen
    coverage = len(matched) / len(hint_keys) if hint_keys else 1.0
    metrics = {
        "schema_valid": True,
        "name_count": len(accepted),
        "inventory_hint_count": len(hint_keys),
        "inventory_hint_matches": len(matched),
        "inventory_hint_coverage": coverage,
        "missing_inventory_hints": [
            name for name in hints if _inventory_name_key(name) not in seen
        ],
        "table_column_contamination_drop_count": len(contamination_drops),
    }
    if coverage < 0.8:
        return ValidationReport(
            decision="reject", accepted=[], dropped=dropped, metrics=metrics,
        )
    return ValidationReport(
        decision="accept" if coverage >= 1.0 else "partial",
        accepted=accepted, dropped=dropped, metrics=metrics,
    )


def validate_detail_batch(response: Mapping[str, Any], names: Sequence[str]) -> ValidationReport:
    """段Bの検証。供給した名前以外を通さない（フィールド検証は最終マージ後）。"""

    raw = response.get("units")
    if raw is None:
        raw = response.get("candidates")
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        return ValidationReport(
            decision="reject", accepted=[],
            dropped=[{"reason": "detail response must contain units[]"}],
            metrics={"schema_valid": False},
        )
    allowed = {_inventory_name_key(name): str(name) for name in names}
    accepted: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        name = str(item.get("unit_name") or "").strip()
        key = _inventory_name_key(name)
        if key not in allowed:
            dropped.append({"unit_name": name, "reason": "unit was not supplied to this batch"})
            continue
        if key in seen:
            dropped.append({"unit_name": name, "reason": "duplicate unit in batch"})
            continue
        seen.add(key)
        row = dict(item)
        # 供給した綴りに揃える。表記ゆれでマージが割れないようにする。
        row["unit_name"] = allowed[key]
        accepted.append(row)
    if not accepted:
        return ValidationReport(
            decision="reject", accepted=[], dropped=dropped,
            metrics={"schema_valid": True, "returned": 0, "supplied": len(allowed)},
        )
    decision = "accept" if len(accepted) == len(allowed) else "partial"
    return ValidationReport(
        decision=decision, accepted=accepted, dropped=dropped,
        metrics={"schema_valid": True, "returned": len(accepted), "supplied": len(allowed)},
    )


def _load_batch_cache(path: Path, job: Mapping[str, Any]) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, Mapping) or document.get("status") != "complete":
        return None
    for key in ("stage", "prompt_version", "map_id", "source_sha256", "prompt_sha256", "job_id"):
        if str(document.get(key) or "") != str(job.get(key) or ""):
            return None
    return dict(document)


def _write_batch_cache(path: Path, job: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    document = {
        **{key: job[key] for key in (
            "schema_version", "stage", "prompt_version", "validator_version",
            "map_id", "source_sha256", "prompt_sha256", "job_id",
        )},
        "status": "complete",
        "completed_at": _utc_now(),
        **dict(payload),
    }
    _atomic_json(path, document)


def run_batched_inventory(
    map_id: str,
    source: SourceDocument,
    cache_dir: Path,
    *,
    router: LLMRouter,
    batch_size: int = DETAIL_BATCH_SIZE,
) -> dict[str, Any]:
    """名前列挙1回 + 詳細Nバッチを実行し、マージ済みresponseを返す。

    検証は呼び出し側で従来どおり validate_inventory_response に通す。
    ここでは「供給した名前以外を混ぜない」ことだけを保証する。
    """

    cache_dir = Path(cache_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    external_calls = 0
    cache_hits = 0

    name_job = build_name_job(map_id, source)
    name_path = cache_dir / f"{name_job['job_id']}.json"
    cached = _load_batch_cache(name_path, name_job)
    if cached is not None:
        names = [str(value) for value in cached.get("unit_names") or []]
        cache_hits += 1
    else:
        routed = router.execute(
            LLMRequest(
                stage=STAGE,
                logical_job_id=str(name_job["job_id"]),
                prompt=str(name_job["prompt"]),
                estimated_input_tokens=int(name_job["estimated_input_tokens"]),
                reserved_output_tokens=int(name_job["reserved_output_tokens"]),
                required_capabilities=("text", "json", "japanese", "long_context"),
            ),
            lambda response: validate_name_response(response, source),
        )
        external_calls += 1
        names = [str(value) for value in routed.validation.accepted or []]
        _write_batch_cache(name_path, name_job, {"unit_names": names})

    if not names:
        raise PilotLLMError("PDF unit bootstrap could not list any unit name.")

    batches = [names[index:index + batch_size] for index in range(0, len(names), batch_size)]
    candidates: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for index, batch in enumerate(batches, start=1):
        detail_job = build_detail_job(map_id, source, batch, index)
        detail_path = cache_dir / f"{detail_job['job_id']}.json"
        cached = _load_batch_cache(detail_path, detail_job)
        if cached is not None:
            rows = [dict(row) for row in cached.get("units") or []]
            cache_hits += 1
        else:
            routed = router.execute(
                LLMRequest(
                    stage=STAGE,
                    logical_job_id=str(detail_job["job_id"]),
                    prompt=str(detail_job["prompt"]),
                    estimated_input_tokens=int(detail_job["estimated_input_tokens"]),
                    reserved_output_tokens=int(detail_job["reserved_output_tokens"]),
                    required_capabilities=("text", "json", "japanese", "long_context"),
                ),
                lambda response, batch=batch: validate_detail_batch(response, batch),
            )
            external_calls += 1
            rows = [dict(row) for row in routed.validation.accepted or []]
            dropped.extend(dict(row) for row in routed.validation.dropped or [])
            _write_batch_cache(detail_path, detail_job, {"units": rows})
        candidates.extend(rows)

    return {
        "response": {"candidates": candidates, "dropped": dropped},
        "external_calls": external_calls,
        "cache_hits": cache_hits,
        "batch_count": len(batches),
        "name_count": len(names),
        "prompt_version": BATCH_PROMPT_VERSION,
    }


def _load_cache(path: Path, job: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    required = (
        "schema_version", "stage", "prompt_version", "validator_version", "map_id",
        "source_sha256", "prompt_sha256", "job_id",
    )
    if any(value.get(key) != job.get(key) for key in required):
        return None
    if value.get("status") != "complete" or not isinstance(value.get("candidates"), list):
        return None
    return value


def _filter_candidates(candidates: Sequence[Mapping[str, Any]], source: SourceDocument) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        name = str(candidate.get("unit_name") or "").strip()
        context = _name_context(source.text, name)
        key = _inventory_name_key(name)
        if not name or context is None:
            dropped.append({
                "unit_name": name,
                "reason": "unit_name is not traceable to a direct or coordinated-list Abstract phrase",
            })
            continue
        if key in seen:
            dropped.append({"unit_name": name, "reason": "duplicate unit_name"})
            continue
        seen.add(key)
        clean = dict(candidate)
        clean["_name_quote"] = context
        accepted.append(clean)
    return accepted, dropped


def _validate_inventory_response(
    response: Mapping[str, Any], source: SourceDocument
) -> ValidationReport:
    """Apply field, name, duplication and conservative completeness checks."""
    raw_candidates = response.get("candidates")
    if raw_candidates is None:
        raw_candidates = response.get("units")
    raw_dropped = response.get("dropped") or []
    if not isinstance(raw_candidates, list) or not all(
        isinstance(item, Mapping) for item in raw_candidates
    ):
        return ValidationReport(
            decision="reject",
            accepted=[],
            dropped=[{"reason": "bootstrap response must contain units[] or candidates[]"}],
            metrics={"schema_valid": False},
        )
    if not isinstance(raw_dropped, list):
        return ValidationReport(
            decision="reject",
            accepted=[],
            dropped=[{"reason": "bootstrap dropped must be an array"}],
            metrics={"schema_valid": False},
        )

    model_candidates = [dict(candidate) for candidate in raw_candidates]
    aggregate_heading_drops = [
        {
            "unit_name": str(candidate.get("unit_name") or "").strip(),
            "reason": "generic inventory container is not a named geological unit",
        }
        for candidate in model_candidates
        if _is_aggregate_inventory_heading(candidate.get("unit_name"))
    ]
    candidate_units = [
        candidate for candidate in model_candidates
        if not _is_aggregate_inventory_heading(candidate.get("unit_name"))
    ]
    verified, field_rejected = verify(candidate_units, source.text)
    accepted_model, name_dropped = _filter_candidates(verified, source)
    accepted_model, table_column_dropped = _drop_table_column_contamination(
        accepted_model
    )
    accepted = list(accepted_model)
    accepted_keys = {
        _inventory_name_key(candidate.get("unit_name")) for candidate in accepted
    }
    deterministic_additions: list[str] = []
    surface_candidates = [
        candidate for candidate in _surficial_candidates(source.text)
        if _inventory_name_key(candidate.get("unit_name")) not in accepted_keys
    ]
    surface_rejected: list[Any] = []
    surface_name_dropped: list[dict[str, Any]] = []
    if surface_candidates:
        surface_verified, surface_rejected = verify(surface_candidates, source.text)
        surface_accepted, surface_name_dropped = _filter_candidates(
            surface_verified, source
        )
        for candidate in surface_accepted:
            key = _inventory_name_key(candidate.get("unit_name"))
            if key and key not in accepted_keys:
                accepted.append(candidate)
                accepted_keys.add(key)
                deterministic_additions.append(str(candidate.get("unit_name") or ""))

    hints = _inventory_hints(source.text)
    hint_keys = {_inventory_name_key(name) for name in hints}
    hint_matches = hint_keys & accepted_keys
    hint_coverage = len(hint_matches) / len(hint_keys) if hint_keys else 1.0
    invalid_candidate_count = max(
        0,
        len(candidate_units) - len(accepted_model) - len(table_column_dropped),
    )
    invalid_ratio = (
        invalid_candidate_count / len(candidate_units) if candidate_units else 1.0
    )
    dropped = list(raw_dropped) + aggregate_heading_drops + [
        {"unit_name": name, "reason": reason} for name, reason in field_rejected
    ] + name_dropped + table_column_dropped + [
        {"unit_name": name, "reason": reason} for name, reason in surface_rejected
    ] + surface_name_dropped

    if not accepted or hint_coverage < 0.8 or invalid_ratio > 0.5:
        decision = "reject"
    elif hint_coverage < 1.0 or invalid_candidate_count:
        decision = "partial"
    else:
        decision = "accept"
    return ValidationReport(
        decision=decision,
        accepted=accepted,
        dropped=dropped,
        metrics={
            "schema_valid": True,
            "raw_candidate_count": len(model_candidates),
            "aggregate_heading_drop_count": len(aggregate_heading_drops),
            "table_column_contamination_drop_count": len(table_column_dropped),
            "accepted_candidate_count": len(accepted),
            "invalid_candidate_count": invalid_candidate_count,
            "invalid_candidate_ratio": invalid_ratio,
            "inventory_hint_count": len(hint_keys),
            "inventory_hint_matches": len(hint_matches),
            "inventory_hint_coverage": hint_coverage,
            "missing_inventory_hints": [
                name for name in hints if _inventory_name_key(name) not in accepted_keys
            ],
            "deterministic_surficial_additions": deterministic_additions,
        },
    )


def validate_inventory_response(
    response: Mapping[str, Any], source: SourceDocument
) -> ValidationReport:
    """Run the production bootstrap validator (public GOLD/test entrypoint)."""

    return _validate_inventory_response(response, source)


def _reconstruct_cached_response(
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    reconstructed: list[dict[str, Any]] = []
    for candidate in candidates:
        row = dict(candidate)
        quotes = row.get("_quotes") if isinstance(row.get("_quotes"), Mapping) else {}
        for field, quote_key in FIELD_QUOTES.items():
            if row.get(field) not in (None, "") and quotes.get(field):
                row[quote_key] = quotes[field]
        reconstructed.append(row)
    return {"candidates": reconstructed, "dropped": []}


def _candidate_signature(candidate: Mapping[str, Any]) -> str:
    quotes = candidate.get("_quotes") if isinstance(candidate.get("_quotes"), Mapping) else {}
    return json.dumps(
        {
            "unit_name": candidate.get("unit_name"),
            "fields": {
                field: candidate.get(field)
                for field in FIELD_QUOTES
                if candidate.get(field) not in (None, "")
            },
            "quotes": dict(quotes),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _load_compatible_cache(
    cache_dir: Path,
    cache_path: Path,
    *,
    job: Mapping[str, Any],
    source: SourceDocument,
) -> dict[str, Any] | None:
    """Migrate an exact old model-keyed cache only after current validation."""
    for candidate_path in cache_dir.glob("pboot_*.json") if cache_dir.is_dir() else ():
        if candidate_path == cache_path:
            continue
        try:
            document = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            document.get("status") != "complete"
            or str(document.get("map_id") or "") != str(job.get("map_id") or "")
            or document.get("source_sha256") != job.get("source_sha256")
            or document.get("prompt_sha256") != job.get("prompt_sha256")
            or not isinstance(document.get("candidates"), list)
        ):
            continue
        prior = list(document.get("candidates") or [])
        report = validate_inventory_response(
            _reconstruct_cached_response(prior), source
        )
        verified = list(report.accepted or [])
        if report.decision == "reject" or sorted(
            map(_candidate_signature, prior)
        ) != sorted(map(_candidate_signature, verified)):
            continue
        migrated = {
            **document,
            **{
                key: job[key] for key in (
                    "schema_version", "stage", "prompt_version",
                    "validator_version", "map_id", "source_sha256",
                    "prompt_sha256", "job_id",
                )
            },
            "provider": document.get("provider") or "gemini",
            "requested_model": document.get("requested_model") or document.get("model"),
            "actual_model": document.get("actual_model") or document.get("model"),
            "candidates": verified,
            "migrated_from_job_id": document.get("job_id"),
        }
        _atomic_json(cache_path, migrated)
        return migrated
    return None


def _load_revalidated_source_cache(
    cache_dir: Path,
    cache_path: Path,
    *,
    job: Mapping[str, Any],
    source: SourceDocument,
) -> dict[str, Any] | None:
    """Revalidate an older same-source inventory for a cache-only run.

    Prompt and validator versions may advance after a paid/free provider run.
    ``--no-llm`` may reuse that source-bound result only after every candidate
    passes the current quote/name/completeness validator.  No old validation
    decision is trusted.
    """

    candidates: list[tuple[int, str, dict[str, Any], ValidationReport]] = []
    for candidate_path in cache_dir.glob("pboot_*.json") if cache_dir.is_dir() else ():
        if candidate_path == cache_path:
            continue
        try:
            document = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            document.get("status") != "complete"
            or str(document.get("map_id") or "") != str(job.get("map_id") or "")
            or document.get("source_sha256") != job.get("source_sha256")
            or not isinstance(document.get("candidates"), list)
        ):
            continue
        report = validate_inventory_response(
            _reconstruct_cached_response(document.get("candidates") or []), source,
        )
        accepted = list(report.accepted or [])
        if report.decision == "reject" or not accepted:
            continue
        candidates.append((len(accepted), candidate_path.name, document, report))
    if not candidates:
        return None

    _count, _name, prior, report = max(candidates, key=lambda row: (row[0], row[1]))
    migrated = {
        **{key: job[key] for key in (
            "schema_version", "stage", "prompt_version", "validator_version",
            "map_id", "source_sha256", "prompt_sha256", "job_id",
        )},
        "status": "complete",
        "completed_at": prior.get("completed_at") or _utc_now(),
        "provider": "revalidated_prior_cache",
        "requested_model": prior.get("requested_model") or prior.get("model"),
        "actual_model": prior.get("actual_model") or prior.get("model"),
        "model": prior.get("actual_model") or prior.get("model"),
        "attempt_id": None,
        "route_attempts": [],
        "candidates": list(report.accepted or []),
        "dropped": list(report.dropped or []),
        "validation": dict(report.metrics or {}),
        "deterministic_surficial_additions": list(
            (report.metrics or {}).get("deterministic_surficial_additions") or []
        ),
        "migrated_from_job_id": prior.get("job_id"),
        "revalidated_with_current_validator": True,
    }
    _atomic_json(cache_path, migrated)
    return migrated


def _assertion_type(field: str, value: Any, quote: str) -> str:
    """Distinguish a quoted value from an interpretation of quoted context."""
    if field == "unit_description":
        return "inferred"
    if field in {"b_age_ma", "t_age_ma", "min_thickness", "max_thickness"}:
        return "explicit"
    candidate = _normalise(value)
    return "explicit" if candidate and candidate in _normalise(quote) else "inferred"


def _evidence_rows(
    map_id: str,
    candidates: Sequence[Mapping[str, Any]],
    source: SourceDocument,
    *,
    stable_ids: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    stable_ids = dict(stable_ids or {})
    # 二重の防御。古い対応表や外部から壊れた表を渡されても重複IDを出さない。
    # set() にしてしまうと重複が見えなくなるため、集合を作る前に落とす。
    seen_stable: set[str] = set()
    for key in list(stable_ids):
        if stable_ids[key] in seen_stable:
            del stable_ids[key]
        else:
            seen_stable.add(stable_ids[key])
    used_ids = set(stable_ids.values())
    ordinals = [
        int(match.group(1))
        for value in used_ids
        if (match := re.fullmatch(rf"m{re.escape(map_id)}_p(\d+)", value))
    ]
    next_ordinal = max(ordinals, default=0) + 1
    for order, candidate in enumerate(candidates, start=1):
        name = str(candidate.get("unit_name") or "").strip()
        key = _inventory_name_key(name)
        unit_id = stable_ids.get(key)
        if not unit_id:
            while f"m{map_id}_p{next_ordinal:03d}" in used_ids:
                next_ordinal += 1
            unit_id = f"m{map_id}_p{next_ordinal:03d}"
            stable_ids[key] = unit_id
            used_ids.add(unit_id)
            next_ordinal += 1
        rows.append({
            "unit_id": unit_id,
            "unit_identity": stable_unit_identity(map_id, name),
            "formation_key": formation_key(name),
            "source_unit_ids": unit_id,
            "column_id": "unsplit",
            "sort_order": order,
            "unit_name": name,
            "t_int": None,
            "b_int": None,
            "comments": "PDF/LLM unit inventory; verify unit identity, order and Column assignment.",
        })
        name_quote = str(candidate.get("_name_quote") or "")
        evidence.append({
            "unit_id": unit_id,
            "scope_type": "unit_global",
            "field": "unit_name",
            "candidate": name,
            "source_type": "PDF",
            "source_file": str(source.source_file),
            "source_locator": "English Abstract",
            "matched_sentence": name_quote,
            "full_context_quote": name_quote,
            "confidence_class": "C",
            "explicit": True,
            "selection": "selected",
            "extraction_method": (
                f"{STAGE}; {PROMPT_VERSION}; direct/coordinated-list unit-name validation"
            ),
        })
        quotes = candidate.get("_quotes") if isinstance(candidate.get("_quotes"), Mapping) else {}
        for field, quote_key in FIELD_QUOTES.items():
            value = candidate.get(field)
            quote = str(quotes.get(field) or "").strip()
            if value in (None, "") or not quote:
                continue
            if field == "strat_name" and not valid_strat_name(value, name):
                continue
            evidence.append({
                "unit_id": unit_id,
                "scope_type": "unit_global",
                "field": field,
                "candidate": value,
                "source_type": "PDF",
                "source_file": str(source.source_file),
                "source_locator": "English Abstract",
                "matched_sentence": quote,
                "full_context_quote": quote,
                "confidence_class": "C",
                "assertion": _assertion_type(field, value, quote),
                "selection": "candidate",
                "extraction_method": f"{STAGE}; {PROMPT_VERSION}; verbatim-quote validation",
            })
        for interval_field, age_field in (("t_int", "t_age_ma"), ("b_int", "b_age_ma")):
            age = candidate.get(age_field)
            interval = best_interval_for_age(age, None) if age not in (None, "") else None
            if interval:
                evidence.append({
                    "unit_id": unit_id,
                    "scope_type": "unit_global",
                    "field": interval_field,
                    "candidate": interval,
                    "source_type": "Macrostrat",
                    "source_file": "config/intervals.json",
                    "source_locator": "international intervals; numeric-age containment",
                    "full_context_quote": f"{age_field}={age} Ma maps to {interval}.",
                    "confidence_class": "A",
                    "explicit": True,
                    "selection": "candidate",
                    "extraction_method": "deterministic Macrostrat interval mapping",
                })
    return rows, evidence


def _rebuild_bundle(bundle: Mapping[str, Any], rows: list[dict[str, Any]], evidence: list[dict[str, Any]], *, generated_at: str) -> dict[str, Any]:
    result = dict(bundle)
    columns = [dict(row) for row in bundle.get("columns") or []]
    if not columns:
        columns = [{"col_id": "unsplit", "col_name": "Unsplit candidate", "status": "CHECK"}]
    metadata = dict((bundle.get("review_v2_input") or {}).get("project") or {})
    metadata.update({
        "unit_inventory_source": "PDF_LLM",
        "unit_inventory_status": "candidate_review",
    })
    compiled, evidence_doc = build_canonical_layer(
        rows,
        column_rows=columns,
        evidence_rows=evidence,
        metadata=metadata,
        map_id=str(bundle.get("map_id") or ""),
        source_review=None,
        generated_at=generated_at,
    )
    result.update({
        "units": rows,
        "columns": columns,
        "source_evidence": evidence,
        "compiled": compiled,
        "evidence": evidence_doc,
        "review_v2_input": {
            "unit_rows": rows,
            "column_rows": columns,
            "evidence_rows": evidence,
            "project": metadata,
        },
    })
    result["gaps"] = [
        gap for gap in result.get("gaps") or []
        if "PDF unit inventory" not in str(gap)
    ] + ["PDF/LLM unit inventory, order and column subdivision require human review"]
    return result


def bootstrap_pdf_units(
    bundle: Mapping[str, Any],
    source: SourceDocument,
    cache_dir: Path,
    *,
    model: str = MODEL,
    api_key: str | None = None,
    executor: Executor | None = None,
    router: LLMRouter | None = None,
    generated_at: str | None = None,
    allow_external_calls: bool = True,
) -> BootstrapResult:
    """Return a rebuilt bundle plus an auditable one-call/cache manifest."""
    map_id = str(bundle.get("map_id") or "").strip().lstrip("mM")
    if not map_id:
        raise PilotLLMError("PDF unit bootstrap requires map_id.")
    project = (bundle.get("review_v2_input") or {}).get("project") or {}
    inventory_source = str(project.get("unit_inventory_source") or "").strip()
    if inventory_source != "PDF_PENDING":
        raise PilotLLMError(
            "PDF unit bootstrap may only replace a PDF_PENDING inventory "
            f"(current={inventory_source or 'unknown'})."
        )
    job = _job(map_id, source)
    if executor is None:
        # バッチ経路で作った結果は、単発promptの版数で記録しない。
        # 版数を揃えておかないと、次の実行で上位キャッシュが読めず
        # 再マージが走り、generated_at だけ違う別物の bundle ができる。
        identity = {
            "stage": job["stage"],
            "schema_version": job["schema_version"],
            "prompt_version": BATCH_PROMPT_VERSION,
            "validator_version": job["validator_version"],
            "map_id": job["map_id"],
            "source_sha256": job["source_sha256"],
            "prompt_sha256": job["prompt_sha256"],
        }
        job = {
            **job,
            "prompt_version": BATCH_PROMPT_VERSION,
            "job_id": "pboot_" + _sha(
                json.dumps(identity, sort_keys=True, separators=(",", ":"))
            )[:20],
        }
    cache_dir = Path(cache_dir).resolve()
    cache_path = cache_dir / f"{job['job_id']}.json"
    prior_ids = _prior_stable_ids(
        cache_dir,
        map_id=map_id,
        source_sha256=source.sha256,
        exclude_job_id=str(job["job_id"]),
    )
    cached = _load_cache(cache_path, job)
    if cached is None:
        cached = _load_compatible_cache(
            cache_dir,
            cache_path,
            job=job,
            source=source,
        )
    external_calls = 0
    completed_at = generated_at or _utc_now()
    if cached is None:
        if not allow_external_calls:
            cached = _load_revalidated_source_cache(
                cache_dir,
                cache_path,
                job=job,
                source=source,
            )
    if cached is None:
        if not allow_external_calls:
            raise PilotLLMError(
                "PDF unit bootstrap has no compatible cache; external calls are disabled."
            )
        if executor:
            preflight_budget([
                SimpleNamespace(
                    estimated_total_tokens=job["estimated_tokens"],
                    model=model,
                )
            ], usage={"calls": 0, "tokens": 0})
            response = executor(job["prompt"], source)
            report = validate_inventory_response(response, source)
            provider = "injected"
            requested_model = model
            actual_model = model
            attempt_id = None
            route_attempts: list[Mapping[str, Any]] = []
        else:
            active_router = router or (
                single_provider_router(
                    stage=STAGE, provider="gemini", model=model,
                    secret=str(api_key),
                )
                if api_key is not None else LLMRouter()
            )
            # 1応答で全unitを出させると、どのモデルも出力上限で切れる
            # （2026-08-12実測: 必要 約11,400 token に対し 8,103〜9,864 で途絶）。
            # 名前の列挙1回 + 8件ずつの詳細Nバッチに分けて集める。
            batched = run_batched_inventory(
                map_id, source, cache_dir, router=active_router,
            )
            response = batched["response"]
            report = validate_inventory_response(response, source)
            provider = "batched"
            requested_model = model
            actual_model = model
            attempt_id = None
            route_attempts = []
            external_calls += int(batched.get("external_calls") or 0)
            print(
                f"  [bootstrap] {batched['name_count']} unit名 / "
                f"{batched['batch_count']}バッチ / 外部call {batched['external_calls']}"
                f" / cache {batched['cache_hits']}"
            )
        if not isinstance(response, Mapping):
            raise PilotLLMError("PDF unit bootstrap executor must return an object.")
        if report.decision == "reject" or not report.accepted:
            raise PilotLLMError(
                "PDF unit bootstrap produced no sufficiently complete quote-verified inventory."
            )
        accepted = list(report.accepted or [])
        dropped = list(report.dropped or [])
        validation_metrics = dict(report.metrics or {})
        cached = {
            **{key: job[key] for key in (
                "schema_version", "stage", "prompt_version", "validator_version", "map_id",
                "source_sha256", "prompt_sha256", "job_id",
            )},
            "status": "complete",
            "completed_at": completed_at,
            "provider": provider,
            "requested_model": requested_model,
            "actual_model": actual_model,
            "model": actual_model,
            "attempt_id": attempt_id,
            "route_attempts": route_attempts,
            "candidates": accepted,
            "dropped": dropped,
            "validation": validation_metrics,
            "deterministic_surficial_additions": list(
                validation_metrics.get("deterministic_surficial_additions") or []
            ),
        }
        _atomic_json(cache_path, cached)
        if provider != "batched":
            # 単発パス（executor注入など）だけがここに来る。バッチ経路は
            # 名前1回＋詳細Nバッチを既に数え終えている。全部キャッシュに
            # 当たって 0 件だった場合も、その 0 が正しい値なので上書きしない。
            external_calls = (
                sum(1 for row in route_attempts if row.get("attempt_id"))
                if route_attempts else 1
            )
    else:
        # Cache provenance controls deterministic rebuilds; a later invocation
        # timestamp must not change the canonical output.
        completed_at = str(cached.get("completed_at") or completed_at)
    rows, evidence = _evidence_rows(
        map_id,
        cached.get("candidates") or [],
        source,
        stable_ids=prior_ids,
    )
    if not rows:
        raise PilotLLMError("PDF unit bootstrap found no quote-verified geological units.")
    rebuilt = _rebuild_bundle(dict(bundle), rows, evidence, generated_at=completed_at)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "prompt_version": PROMPT_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "map_id": map_id,
        "provider": cached.get("provider"),
        "requested_model": cached.get("requested_model") or cached.get("model") or model,
        "actual_model": cached.get("actual_model") or cached.get("model") or model,
        "model": cached.get("actual_model") or cached.get("model") or model,
        "external_calls": external_calls,
        "cache_hits": 0 if external_calls else 1,
        "units": len(rows),
        "stable_ids_reused": sum(
            _inventory_name_key(candidate.get("unit_name")) in prior_ids
            for candidate in cached.get("candidates") or []
            if isinstance(candidate, Mapping)
        ),
        "deterministic_surficial_additions": list(
            cached.get("deterministic_surficial_additions") or []
        ),
        "added_evidence": len(evidence),
        "cache_file": str(cache_path),
        "source_text_sha256": source.sha256,
        "validation": dict(cached.get("validation") or {}),
        "route_attempts": list(cached.get("route_attempts") or []),
    }
    return BootstrapResult(bundle=rebuilt, manifest=manifest)


__all__ = ["BootstrapResult", "bootstrap_pdf_units"]
