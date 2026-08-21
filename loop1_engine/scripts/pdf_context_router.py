# -*- coding: utf-8 -*-
"""Route unresolved unit fields to compact, page-verified PDF body contexts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from pdf_locate import locate, normalize
except ImportError:  # pragma: no cover - package-style import
    from .pdf_locate import locate, normalize


SCHEMA_VERSION = "pdf-context-router/1.0"
BODY_FIELDS = (
    "t_age_ma", "b_age_ma", "strat_name", "environment", "unit_description",
    "lithology", "minor_lith", "min_thickness", "max_thickness",
    "basal_surface", "lateral_relationship",
)

_KEYWORDS = re.compile(
    r"年代|時代|Ma|ka|層厚|厚さ|分布|岩相|岩質|構成|堆積|環境|基底|整合|不整合|"
    r"断層|漸移|指交|側方|覆う|重なる|貫入|age|thick|litholog|composed|"
    r"environment|deposit|conform|unconform|fault|gradat|interfinger|onlap",
    re.IGNORECASE,
)
_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_SECTION_CUES = re.compile(
    r"地層名及び定義|地層名|模式地|分布地形|分布|層序関係|層厚|層相|岩相|年代"
)


def _value(unit: Mapping[str, Any], field: str) -> Any:
    values = unit.get("values") if isinstance(unit.get("values"), Mapping) else {}
    return values.get(field)


def _resolution_state(unit: Mapping[str, Any], field: str) -> str:
    resolution = (
        unit.get("field_resolution")
        if isinstance(unit.get("field_resolution"), Mapping) else {}
    )
    field_state = resolution.get(field) if isinstance(resolution.get(field), Mapping) else {}
    return str(field_state.get("state") or "").strip().casefold()


def _unique(values: Sequence[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = normalize(text)
        if text and key and key not in seen:
            output.append(text)
            seen.add(key)
    return output


def _nested(document: Mapping[str, Any], *keys: str) -> Any:
    value: Any = document
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _aliases(document: Mapping[str, Any], english_name: str) -> list[str]:
    anchors = _nested(document, "target", "anchors") or []
    anchor_values = [
        row.get(key)
        for row in anchors if isinstance(row, Mapping)
        for key in ("title", "label")
    ]
    return _unique([
        english_name,
        _nested(document, "target", "title"),
        _nested(document, "derived", "section_title"),
        _nested(document, "legend", "parent_facies", "label_ja"),
        _nested(document, "legend", "parent_facies", "text_ja"),
        _nested(document, "legend", "parent_facies", "label_en"),
        _nested(document, "legend", "parent_facies", "text_en"),
        _nested(document, "self", "label_ja"),
        _nested(document, "self", "label_en"),
        *anchor_values,
    ])


def _sentences(text: str) -> list[str]:
    return [
        value.strip()
        for value in re.split(r"(?<=[。！？.!?])\s*|\n+", text)
        if value.strip()
    ]


def _compact_context(text: str, *, limit: int = 2600) -> str:
    sentences = _sentences(text)
    selected_indexes = {0, 1}
    for index, sentence in enumerate(sentences):
        if _KEYWORDS.search(sentence):
            selected_indexes.update((max(0, index - 1), index, min(len(sentences) - 1, index + 1)))
    selected = [sentences[index] for index in sorted(selected_indexes) if index < len(sentences)]
    joined = "\n".join(selected)
    if len(joined) <= limit:
        return joined
    clipped: list[str] = []
    size = 0
    for sentence in selected:
        if clipped and size + len(sentence) + 1 > limit:
            break
        clipped.append(sentence)
        size += len(sentence) + 1
    return "\n".join(clipped)


def _pdf_alias_context(
    pdf_index: Mapping[str, Any], aliases: Sequence[str]
) -> tuple[str, int, int | None] | None:
    pages = pdf_index.get("pages") if isinstance(pdf_index.get("pages"), list) else []
    printed = pdf_index.get("printed") if isinstance(pdf_index.get("printed"), list) else []
    candidates: list[tuple[int, int, int, int, int, str]] = []
    for alias in aliases:
        key = normalize(alias)
        if len(key) < 3:
            continue
        for index, page in enumerate(pages):
            text = str(page or "")
            printed_page = printed[index] if index < len(printed) else None
            body_rank = 0 if isinstance(printed_page, int) and printed_page >= 3 else 1
            for match in re.finditer(re.escape(key), text):
                position = match.start()
                window = text[max(0, position - 1300):position + len(key) + 2600]
                preceding = text[max(0, position - 80):position]
                following = text[position + len(key):position + len(key) + 1400]
                section_score = len(_SECTION_CUES.findall(following))
                # A genuine unit section normally contains several of the
                # standard subheads immediately after its title.  Summary and
                # structure pages may repeat the same unit name but do not.
                heading_bonus = 20 if re.search(
                    r"地層名及び定義|模式地", following[:500]
                ) else 0
                # The normalized GSJ index removes punctuation and spaces from
                # numbered headings (``6. 11. 2`` -> ``6112``).  Reward an
                # alias that immediately follows such a heading.  This is the
                # decisive signal for short sections, which may legitimately
                # omit the usual named subheads (for example 現河床堆積物).
                if re.search(r"(?<!第)\d{2,5}$", preceding):
                    heading_bonus += 60
                keyword_score = len(_KEYWORDS.findall(window))
                candidates.append((
                    body_rank,
                    -(section_score + heading_bonus),
                    -keyword_score,
                    index,
                    position,
                    window,
                ))
    if not candidates:
        return None
    _body_rank, _section_rank, _keyword_rank, index, _position, window = min(candidates)
    printed_page = printed[index] if index < len(printed) else None
    return window, index + 1, printed_page if isinstance(printed_page, int) else None


def build_unit_aliases(
    compiled: Mapping[str, Any], zfk_root: str | Path
) -> dict[str, Any]:
    """Build a deterministic bilingual alias table from canonical and ZFK data."""
    root = Path(zfk_root).expanduser().resolve()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for unit in compiled.get("units") or []:
        if not isinstance(unit, Mapping):
            continue
        unit_id = str(unit.get("unit_id") or "").strip()
        if not unit_id or unit_id in seen:
            continue
        seen.add(unit_id)
        english_name = str(_value(unit, "unit_name") or "").strip()
        path = root / "units" / f"{unit_id}.json"
        document = None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            document = loaded if isinstance(loaded, Mapping) else None
        except (OSError, json.JSONDecodeError):
            pass
        aliases = _aliases(document, english_name) if document else _unique([english_name])
        if document:
            status = "ready"
        elif not english_name or english_name == "NO_DATA":
            status = "placeholder_review_required"
        else:
            status = "alias_mapping_required"
        rows.append({
            "unit_id": unit_id,
            "unit_name": english_name,
            "aliases": aliases,
            "japanese_aliases": [value for value in aliases if _JAPANESE.search(value)],
            "map_symbol": (
                str(_nested(document, "legend", "focus", "label") or "").strip()
                if document else ""
            ),
            "section": (
                str(_nested(document, "target", "label") or "").strip()
                if document else ""
            ),
            "zfk_unit_file": str(path) if document else None,
            "status": status,
        })
    return {"schema_version": SCHEMA_VERSION, "units": rows}


def route_pdf_contexts(
    compiled: Mapping[str, Any],
    aliases: Mapping[str, Any],
    *,
    pdf_index: Mapping[str, Any] | None,
    requested_fields: Sequence[str] = BODY_FIELDS,
) -> dict[str, Any]:
    """Create compact contexts for missing fields; never include a whole report."""
    units_by_id = {
        str(unit.get("unit_id") or ""): unit
        for unit in compiled.get("units") or [] if isinstance(unit, Mapping)
    }
    contexts: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for alias_row in aliases.get("units") or []:
        unit_id = str(alias_row.get("unit_id") or "")
        unit = units_by_id.get(unit_id)
        if unit is None:
            continue
        missing = [
            field for field in requested_fields
            if _value(unit, field) in (None, "")
            and not (field == "minor_lith" and _resolution_state(unit, field) == "explicitly_absent")
        ]
        if not missing:
            continue
        zfk_file = alias_row.get("zfk_unit_file")
        if not zfk_file:
            hit = _pdf_alias_context(pdf_index or {}, alias_row.get("japanese_aliases") or [])
            if hit is None:
                unresolved.append({
                    "unit_id": unit_id,
                    "requested_fields": missing,
                    "reason": "japanese_alias_or_body_context_unavailable",
                })
                continue
            compact, pdf_page, printed_page = hit
            digest = hashlib.sha256(compact.encode("utf-8")).hexdigest()
            contexts.append({
                "context_id": f"ctx_{unit_id}_{digest[:12]}",
                "unit_id": unit_id,
                "unit_name": _value(unit, "unit_name"),
                "column_ids": list(unit.get("column_ids") or []),
                "aliases": alias_row.get("aliases") or [],
                "section": None,
                "pdf_page": pdf_page,
                "printed_page": printed_page,
                "requested_fields": missing,
                "text": compact,
                "text_sha256": digest,
                "context_source": "PDF page text via verified Japanese TOC alias",
            })
            continue
        try:
            document = json.loads(Path(zfk_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            unresolved.append({
                "unit_id": unit_id,
                "requested_fields": missing,
                "reason": "zfk_unit_file_unreadable",
            })
            continue
        body = str(_nested(document, "target", "text") or "").strip()
        compact = _compact_context(body)
        if not compact:
            unresolved.append({
                "unit_id": unit_id,
                "requested_fields": missing,
                "reason": "unit_body_text_empty",
            })
            continue
        hit = locate(pdf_index, compact) if pdf_index else None
        if hit is None and pdf_index:
            for alias in alias_row.get("aliases") or []:
                hit = locate(pdf_index, alias)
                if hit is not None:
                    break
        if pdf_index and hit is None:
            unresolved.append({
                "unit_id": unit_id,
                "requested_fields": missing,
                "reason": "body_context_not_verified_in_pdf_index",
            })
            continue
        digest = hashlib.sha256(compact.encode("utf-8")).hexdigest()
        contexts.append({
            "context_id": f"ctx_{unit_id}_{digest[:12]}",
            "unit_id": unit_id,
            "unit_name": _value(unit, "unit_name"),
            "column_ids": list(unit.get("column_ids") or []),
            "aliases": alias_row.get("aliases") or [],
            "section": alias_row.get("section"),
            "pdf_page": hit.get("pdf_page") if hit else None,
            "printed_page": hit.get("printed_page") if hit else None,
            "requested_fields": missing,
            "text": compact,
            "text_sha256": digest,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "map_id": str((compiled.get("map") or {}).get("map_id") or ""),
        "contexts": contexts,
        "unresolved": unresolved,
        "context_characters": sum(len(row["text"]) for row in contexts),
    }


__all__ = ["BODY_FIELDS", "SCHEMA_VERSION", "build_unit_aliases", "route_pdf_contexts"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Route canonical missing fields to verified PDF body contexts without LLM calls."
    )
    parser.add_argument("--compiled", required=True)
    parser.add_argument("--aliases", required=True)
    parser.add_argument("--pdf-index", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    compiled = json.loads(Path(args.compiled).read_text(encoding="utf-8"))
    aliases = json.loads(Path(args.aliases).read_text(encoding="utf-8"))
    pdf_index = json.loads(Path(args.pdf_index).read_text(encoding="utf-8"))
    routed = route_pdf_contexts(compiled, aliases, pdf_index=pdf_index)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(routed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "contexts": len(routed["contexts"]),
        "unresolved": len(routed["unresolved"]),
        "context_characters": routed["context_characters"],
        "output": str(output.resolve()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
