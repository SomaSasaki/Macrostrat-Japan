# -*- coding: utf-8 -*-
"""Cached multimodal extraction of stratigraphic Columns and geography.

The model reads one locally selected stratigraphic figure together with the
English report text.  It may interpret column membership and geographic
language, but it never supplies coordinates.  Every unit is matched by its
canonical ``unit_id`` and every textual geographic statement is checked
against the supplied report text before it can become evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from common import intervals_for_excel
from llm_extract import MODEL, check_budget
from llm_router import (
    LLMImage, LLMRequest, LLMRouter, ValidationReport, single_provider_router,
)
from llm_constrained_vision import (
    COLUMN_DETECTION_PROMPT_VERSION,
    COLUMN_MEMBERSHIP_PROMPT_VERSION,
    CONSTRAINED_VALIDATOR_VERSION,
    build_column_detection_prompt,
    build_membership_prompt,
    validate_column_detection,
    validate_membership_batch,
)
from pdf_locate import locate


SCHEMA_VERSION = "column-vision/1.0"
STAGE = "column_geography_vision"
PROMPT_VERSION = "column-geography-age-v3"
VALIDATOR_VERSION = "column-geography-validator-v2"
CONSTRAINED_PROMPT_VERSION = (
    f"{COLUMN_DETECTION_PROMPT_VERSION}+{COLUMN_MEMBERSHIP_PROMPT_VERSION}"
)

ALLOWED_CONSTRAINT_KINDS = {
    "map_direction",
    "near_place",
    "along_feature",
    "relative_to_place",
    "coastal",
    "margin_of",
}
ALLOWED_DIRECTIONS = {
    "west", "east", "north", "south",
    "northwest", "northeast", "southwest", "southeast", "central",
}


PROMPT_TEMPLATE = """\
You are reading an official Geological Survey of Japan 1:50,000 report.
Analyze the attached stratigraphic summary figure and the supplied English
report text. Return JSON only.

Tasks:
1. Identify every regional stratigraphic Column shown by the figure. A Column
   is a geographic subdivision such as Western Area or Eastern Area, not an
   age, lithology, tectonic event, or diagram panel.
2. For every canonical unit, return all Column memberships and the rank within
   each Column, where 1 is the youngest/top unit.
3. Read the figure's geological-time axis/brackets. For every canonical unit,
   return its younger/top interval as t_int and older/bottom interval as
   b_int. Use only ALLOWED_INTERVALS. A bracket spanning several intervals may
   have different t_int and b_int. Return null when the visual bracket is not
   legible; do not infer an age merely from vertical order.
4. For every Column, find the report sentence(s) that describe where that
   geographic area is located. Copy an exact quote from REPORT_TEXT. Do not
   paraphrase the quote.
5. Convert only the quoted geographic meaning to controlled spatial
   constraints. Never invent coordinates.

Allowed constraint kinds:
- map_direction: value is one of west/east/north/south/northwest/northeast/
  southwest/southeast/central.
- near_place: place_name is a named city, town, mountain, river or district.
- along_feature: place_name is a named river, coast, valley or range.
- relative_to_place: place_name plus direction.
- coastal: optional direction.
- margin_of: place_name plus direction when stated.

Return this exact structure:
{
  "columns": [
    {
      "column_id": "existing ID when supplied, otherwise a short English slug",
      "column_name": "English display name",
      "region_description": "short English summary",
      "region_quote": "exact quotation from REPORT_TEXT",
      "constraints": [
        {"kind": "map_direction", "direction": "west", "place_name": null}
      ]
    }
  ],
  "units": [
    {
      "unit_id": "exact canonical unit_id",
      "unit_name": "exact canonical unit_name",
      "memberships": [
        {"column_id": "column ID", "sort_order": 1}
      ],
      "age": {
        "t_int": "controlled younger/top interval or null",
        "b_int": "controlled older/bottom interval or null",
        "visual_label": "short label visibly read from the figure"
      }
    }
  ]
}

Rules:
- Return every canonical unit exactly once. Do not add or omit units.
- Preserve supplied column IDs when EXPECTED_COLUMNS is non-empty.
- A shared unit has multiple membership objects.
- sort_order must be a positive integer for each membership.
- t_int and b_int must be exact names from ALLOWED_INTERVALS. Named intervals
  do not authorize numeric Ma values or t_prop/b_prop.
- If the report gives only a directional description, use map_direction.
- Do not use geological unit distribution sentences as Column-region evidence
  unless the sentence explicitly describes the regional subdivision.
- If no exact region sentence exists, use an empty region_quote and empty
  constraints; do not fabricate evidence.

EXPECTED_COLUMNS:
{expected_columns}

CANONICAL_UNITS:
{units}

ALLOWED_INTERVALS:
{allowed_intervals}

REPORT_TEXT:
{report_text}
"""


class ColumnVisionError(RuntimeError):
    """The Column/geography extraction stage could not produce a safe result."""


@dataclass(frozen=True)
class VisionJob:
    job_id: str
    map_id: str
    model: str
    pdf_sha256: str
    image_sha256: str
    source_text_sha256: str
    prompt_sha256: str
    pdf_page: int
    printed_page: int | None
    prompt: str
    estimated_input_tokens: int
    reserved_output_tokens: int
    estimated_total_tokens: int


@dataclass(frozen=True)
class VisionStageResult:
    proposal: dict[str, Any]
    manifest: dict[str, Any]
    manifest_path: Path
    cache_path: Path
    external_calls: int
    cache_hits: int


Executor = Callable[[VisionJob, Path], Mapping[str, Any]]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalise_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    for dash in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212"):
        text = text.replace(dash, "-")
    for apostrophe in ("\u2018", "\u2019", "\u2032"):
        text = text.replace(apostrophe, "'")
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip().casefold()


def _normalise_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode().casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _slug(value: Any) -> str:
    output = re.sub(r"[^a-z0-9]+", "-", _normalise_name(value)).strip("-")
    return output[:40]


def build_prompt(
    report_text: str,
    units: Sequence[Mapping[str, Any]],
    expected_columns: Sequence[Mapping[str, Any]],
) -> str:
    unit_payload = [
        {
            "unit_id": str(unit.get("unit_id") or "").strip(),
            "unit_name": str(unit.get("unit_name") or "").strip(),
        }
        for unit in units
    ]
    column_payload = [
        {
            "column_id": str(column.get("column_id") or column.get("col_id") or "").strip(),
            "column_name": str(column.get("column_name") or column.get("col_name") or "").strip(),
        }
        for column in expected_columns
    ]
    # Use literal replacement because the prompt intentionally contains a JSON
    # example with braces that must not be interpreted by ``str.format``.
    return (
        PROMPT_TEMPLATE
        .replace("{expected_columns}", json.dumps(column_payload, ensure_ascii=False, indent=2))
        .replace("{units}", json.dumps(unit_payload, ensure_ascii=False, indent=2))
        .replace(
            "{allowed_intervals}",
            json.dumps(sorted(intervals_for_excel()), ensure_ascii=False),
        )
        .replace("{report_text}", report_text)
    )


def build_job(
    *,
    map_id: str,
    pdf_path: str | os.PathLike[str],
    image_path: str | os.PathLike[str],
    pdf_page: int,
    printed_page: int | None,
    report_text: str,
    units: Sequence[Mapping[str, Any]],
    expected_columns: Sequence[Mapping[str, Any]] = (),
    model: str = MODEL,
) -> VisionJob:
    pdf = Path(pdf_path).expanduser().resolve()
    image = Path(image_path).expanduser().resolve()
    if not pdf.is_file() or not image.is_file():
        raise ColumnVisionError("Column Vision requires an existing PDF and rendered image.")
    prompt = build_prompt(report_text, units, expected_columns)
    identity = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "prompt_version": PROMPT_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "map_id": str(map_id).strip().lstrip("mM"),
        "pdf_sha256": _sha256_file(pdf),
        "image_sha256": _sha256_file(image),
        "source_text_sha256": _sha256_text(report_text),
        "prompt_sha256": _sha256_text(prompt),
        "pdf_page": int(pdf_page),
        "printed_page": printed_page,
    }
    job_id = "cv_" + _sha256_text(
        json.dumps(identity, sort_keys=True, separators=(",", ":"))
    )[:20]
    estimated_input = math.ceil(len(prompt.encode("utf-8")) / 3) + _image_token_estimate(image)
    reserved_output = max(2048, len(units) * 160)
    return VisionJob(
        job_id=job_id,
        map_id=identity["map_id"],
        model=model,
        pdf_sha256=identity["pdf_sha256"],
        image_sha256=identity["image_sha256"],
        source_text_sha256=identity["source_text_sha256"],
        prompt_sha256=identity["prompt_sha256"],
        pdf_page=int(pdf_page),
        printed_page=printed_page,
        prompt=prompt,
        estimated_input_tokens=estimated_input,
        reserved_output_tokens=reserved_output,
        estimated_total_tokens=estimated_input + reserved_output,
    )


def build_constrained_job(
    *,
    map_id: str,
    pdf_path: str | os.PathLike[str],
    image_path: str | os.PathLike[str],
    pdf_page: int,
    printed_page: int | None,
    report_text: str,
    units: Sequence[Mapping[str, Any]],
    expected_columns: Sequence[Mapping[str, Any]],
    model: str = MODEL,
) -> VisionJob:
    """Build one cache identity over the exact constrained subtask prompts."""

    pdf = Path(pdf_path).expanduser().resolve()
    image = Path(image_path).expanduser().resolve()
    if not pdf.is_file() or not image.is_file():
        raise ColumnVisionError("Column Vision requires an existing PDF and rendered image.")
    if not expected_columns:
        raise ColumnVisionError("Constrained Column Vision requires reviewed expected Columns.")
    prompts = [build_column_detection_prompt(expected_columns)]
    prompts.extend(
        build_membership_prompt(units[start:start + 8], expected_columns)
        for start in range(0, len(units), 8)
    )
    prompt_identity = json.dumps(
        {
            "prompt_version": CONSTRAINED_PROMPT_VERSION,
            "prompt_hashes": [_sha256_text(prompt) for prompt in prompts],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    identity = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "prompt_version": CONSTRAINED_PROMPT_VERSION,
        "validator_version": CONSTRAINED_VALIDATOR_VERSION,
        "map_id": str(map_id).strip().lstrip("mM"),
        "pdf_sha256": _sha256_file(pdf),
        "image_sha256": _sha256_file(image),
        "source_text_sha256": _sha256_text(report_text),
        "prompt_sha256": _sha256_text(prompt_identity),
        "pdf_page": int(pdf_page),
        "printed_page": printed_page,
    }
    job_id = "cvc_" + _sha256_text(
        json.dumps(identity, sort_keys=True, separators=(",", ":"))
    )[:20]
    image_tokens = _image_token_estimate(image)
    estimated_input = sum(
        math.ceil(len(prompt.encode("utf-8")) / 3) + image_tokens
        for prompt in prompts
    )
    reserved_output = 768 + max(0, len(prompts) - 1) * 1024
    return VisionJob(
        job_id=job_id,
        map_id=identity["map_id"],
        model=model,
        pdf_sha256=identity["pdf_sha256"],
        image_sha256=identity["image_sha256"],
        source_text_sha256=identity["source_text_sha256"],
        prompt_sha256=identity["prompt_sha256"],
        pdf_page=int(pdf_page),
        printed_page=printed_page,
        prompt=prompt_identity,
        estimated_input_tokens=estimated_input,
        reserved_output_tokens=reserved_output,
        estimated_total_tokens=estimated_input + reserved_output,
    )


def _cache_path(cache_dir: Path, job: VisionJob) -> Path:
    return cache_dir / f"{job.job_id}.json"


def _load_cache(
    cache_dir: Path,
    job: VisionJob,
    *,
    prompt_version: str = PROMPT_VERSION,
    validator_version: str = VALIDATOR_VERSION,
) -> dict[str, Any] | None:
    path = _cache_path(cache_dir, job)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    expected = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "prompt_version": prompt_version,
        "validator_version": validator_version,
        "job_id": job.job_id,
        "pdf_sha256": job.pdf_sha256,
        "image_sha256": job.image_sha256,
        "source_text_sha256": job.source_text_sha256,
        "prompt_sha256": job.prompt_sha256,
        "status": "complete",
    }
    if any(document.get(key) != value for key, value in expected.items()):
        return None
    return document if isinstance(document.get("proposal"), Mapping) else None


def _mime_type(path: Path) -> str:
    return "image/jpeg" if path.suffix.casefold() in {".jpg", ".jpeg"} else "image/png"


def _image_token_estimate(path: Path) -> int:
    """Estimate image tokens from dimensions instead of compressed bytes.

    Compressed PNG size is not proportional to multimodal model tokens.  The
    conservative tile estimate keeps local preflight meaningful and still
    fails closed for unusually large or unknown images.
    """

    try:
        # Pillow handles JPEG as well as PNG and is already used by the
        # offline map-thumbnail renderer.  Reading dimensions does not decode
        # the full raster into memory.
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
        if width > 0 and height > 0:
            return max(512, math.ceil(width / 768) * math.ceil(height / 768) * 512)
    except (ImportError, OSError, ValueError):
        pass
    try:
        header = path.read_bytes()[:24]
        if header[:8] == b"\x89PNG\r\n\x1a\n" and len(header) >= 24:
            width, height = struct.unpack(">II", header[16:24])
            return max(512, math.ceil(width / 768) * math.ceil(height / 768) * 512)
    except OSError:
        pass
    return max(2048, math.ceil(path.stat().st_size / 12))


def _constraint(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw, Mapping):
        return None, "constraint is not an object"
    kind = str(raw.get("kind") or "").strip().casefold()
    direction = str(raw.get("direction") or raw.get("value") or "").strip().casefold()
    place_name = str(raw.get("place_name") or "").strip() or None
    if kind not in ALLOWED_CONSTRAINT_KINDS:
        return None, f"unsupported constraint kind: {kind or '(blank)'}"
    if direction and direction not in ALLOWED_DIRECTIONS:
        return None, f"unsupported direction: {direction}"
    if kind in {"near_place", "along_feature", "relative_to_place", "margin_of"} and not place_name:
        return None, f"{kind} requires place_name"
    return {"kind": kind, "direction": direction or None, "place_name": place_name}, None


def validate_response(
    response: Mapping[str, Any],
    *,
    units: Sequence[Mapping[str, Any]],
    expected_columns: Sequence[Mapping[str, Any]],
    report_text: str,
    pdf_index: Mapping[str, Any] | None,
    figure_pdf_page: int,
    figure_printed_page: int | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate model structure, canonical identities and verbatim quotations."""

    dropped: list[dict[str, Any]] = []
    raw_columns = response.get("columns")
    raw_units = response.get("units")
    if not isinstance(raw_columns, list) or not isinstance(raw_units, list):
        raise ColumnVisionError("Column Vision response requires columns[] and units[].")

    expected_by_id = {
        str(row.get("column_id") or row.get("col_id") or "").strip(): row
        for row in expected_columns
        if str(row.get("column_id") or row.get("col_id") or "").strip()
    }
    columns: list[dict[str, Any]] = []
    seen_columns: set[str] = set()
    report_haystack = _normalise_text(report_text)
    for raw in raw_columns:
        if not isinstance(raw, Mapping):
            dropped.append({"kind": "column", "reason": "column is not an object"})
            continue
        name = str(raw.get("column_name") or "").strip()
        column_id = str(raw.get("column_id") or "").strip()
        if not column_id and not expected_by_id:
            column_id = _slug(name)
        if not column_id or column_id in seen_columns:
            dropped.append({"kind": "column", "column_id": column_id, "reason": "blank or duplicate column_id"})
            continue
        if expected_by_id and column_id not in expected_by_id:
            dropped.append({"kind": "column", "column_id": column_id, "reason": "not in expected Columns"})
            continue
        if expected_by_id:
            expected_name = str(
                expected_by_id[column_id].get("column_name")
                or expected_by_id[column_id].get("col_name")
                or ""
            ).strip()
            name = expected_name or name
        if not name:
            dropped.append({"kind": "column", "column_id": column_id, "reason": "blank column_name"})
            continue
        quote = str(raw.get("region_quote") or "").strip()
        quote_verified = bool(quote and _normalise_text(quote) in report_haystack)
        if quote and not quote_verified:
            dropped.append({"kind": "column_quote", "column_id": column_id, "reason": "region_quote is not verbatim in report text"})
        constraints: list[dict[str, Any]] = []
        if quote_verified:
            for item in raw.get("constraints") or []:
                clean, error = _constraint(item)
                if clean:
                    constraints.append(clean)
                elif error:
                    dropped.append({"kind": "constraint", "column_id": column_id, "reason": error})
        hit = locate(pdf_index, quote) if quote_verified and pdf_index else None
        columns.append({
            "column_id": column_id,
            "column_name": name,
            "region_description": str(raw.get("region_description") or "").strip(),
            "region_quote": quote if quote_verified else "",
            "quote_verified": quote_verified,
            "pdf_page": hit.get("pdf_page") if hit else None,
            "printed_page": hit.get("printed_page") if hit else None,
            "figure_pdf_page": figure_pdf_page,
            "figure_printed_page": figure_printed_page,
            "constraints": constraints,
            "confidence": "C",
        })
        seen_columns.add(column_id)

    if expected_by_id and seen_columns != set(expected_by_id):
        dropped.append({
            "kind": "column_coverage",
            "reason": "response did not return every expected Column",
            "missing": sorted(set(expected_by_id) - seen_columns),
        })

    inventory = {
        str(unit.get("unit_id") or "").strip(): str(unit.get("unit_name") or "").strip()
        for unit in units
        if str(unit.get("unit_id") or "").strip()
    }
    accepted_units: list[dict[str, Any]] = []
    interval_index = {
        _normalise_text(name): name for name in intervals_for_excel()
    }
    seen_units: set[str] = set()
    for raw in raw_units:
        if not isinstance(raw, Mapping):
            dropped.append({"kind": "unit", "reason": "unit is not an object"})
            continue
        unit_id = str(raw.get("unit_id") or "").strip()
        name = str(raw.get("unit_name") or "").strip()
        if unit_id not in inventory or unit_id in seen_units:
            dropped.append({"kind": "unit", "unit_id": unit_id, "reason": "unknown or duplicate unit_id"})
            continue
        if _normalise_name(name) != _normalise_name(inventory[unit_id]):
            dropped.append({"kind": "unit", "unit_id": unit_id, "reason": "unit_name does not match canonical inventory"})
            continue
        memberships: list[dict[str, Any]] = []
        seen_memberships: set[str] = set()
        for membership in raw.get("memberships") or []:
            if not isinstance(membership, Mapping):
                continue
            column_id = str(membership.get("column_id") or "").strip()
            sort_order = membership.get("sort_order")
            if column_id not in seen_columns or column_id in seen_memberships:
                continue
            if isinstance(sort_order, bool) or not isinstance(sort_order, int) or sort_order < 1:
                continue
            memberships.append({"column_id": column_id, "sort_order": sort_order})
            seen_memberships.add(column_id)
        if not memberships:
            dropped.append({"kind": "unit", "unit_id": unit_id, "reason": "no valid Column membership"})
            continue
        raw_age = raw.get("age") if isinstance(raw.get("age"), Mapping) else {}
        age: dict[str, Any] = {
            "t_int": None,
            "b_int": None,
            "visual_label": str(raw_age.get("visual_label") or "").strip(),
        }
        for field in ("t_int", "b_int"):
            supplied = raw_age.get(field)
            if supplied in (None, ""):
                continue
            canonical = interval_index.get(_normalise_text(supplied))
            if canonical is None:
                dropped.append({
                    "kind": "unit_age",
                    "unit_id": unit_id,
                    "field": field,
                    "reason": "interval_not_in_controlled_list",
                    "candidate": supplied,
                })
                continue
            age[field] = canonical
        accepted_units.append({
            "unit_id": unit_id,
            "unit_name": inventory[unit_id],
            "memberships": memberships,
            "age": age,
            "confidence": "C",
        })
        seen_units.add(unit_id)

    missing_units = sorted(set(inventory) - seen_units)
    if missing_units:
        dropped.append({
            "kind": "unit_coverage",
            "reason": "response did not return every canonical unit",
            "missing": missing_units,
        })

    # 全ユニットの網羅は採用条件にしない。
    #
    # 網羅を required にすると、1ユニットでも欠けた時点で提案ごと却下され、
    # 正しく割り当てられた分まで捨てられて Column 分割そのものが失われる。
    # m1286 では 30/48 が正しく割り当てられていたのに、残り18件を理由に
    # すべてが unsplit（1列）へ落ちていた。
    #
    # 未割当のユニットは下流（column_geography.apply_column_proposal）で
    # unassigned Column として残し、Excel の status と comments に警告を出す。
    # 黙って捨てるより、見える形で残して人に判断させる。
    assignment_ready = bool(columns) and (
        not expected_by_id or seen_columns == set(expected_by_id)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "candidate_review" if assignment_ready else "rejected_review_required",
        "assignment_ready": assignment_ready,
        "columns": columns,
        "units": accepted_units,
        "unassigned_units": missing_units,
        "validation": {
            "canonical_units": len(inventory),
            "matched_units": len(accepted_units),
            "unassigned_units": len(missing_units),
            "columns": len(columns),
            "verified_region_quotes": sum(column["quote_verified"] for column in columns),
        },
    }, dropped


def _migrate_compatible_cache(
    cache_dir: Path,
    job: VisionJob,
    *,
    units: Sequence[Mapping[str, Any]],
    expected_columns: Sequence[Mapping[str, Any]],
    report_text: str,
    pdf_index: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Revalidate a provider-bound v1 cache before adopting its proposal."""

    expected = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "prompt_version": PROMPT_VERSION,
        "pdf_sha256": job.pdf_sha256,
        "image_sha256": job.image_sha256,
        "source_text_sha256": job.source_text_sha256,
        "prompt_sha256": job.prompt_sha256,
        "status": "complete",
    }
    for path in sorted(cache_dir.glob("cv_*.json")) if cache_dir.is_dir() else ():
        if path == _cache_path(cache_dir, job):
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(document, Mapping):
            continue
        if any(document.get(key) != value for key, value in expected.items()):
            continue
        old_proposal = document.get("proposal")
        if not isinstance(old_proposal, Mapping):
            continue
        try:
            proposal, dropped = validate_response(
                old_proposal,
                units=units,
                expected_columns=expected_columns,
                report_text=report_text,
                pdf_index=pdf_index,
                figure_pdf_page=job.pdf_page,
                figure_printed_page=job.printed_page,
            )
        except ColumnVisionError:
            continue
        if not proposal.get("assignment_ready"):
            continue
        migrated = {
            "schema_version": SCHEMA_VERSION,
            "stage": STAGE,
            "prompt_version": PROMPT_VERSION,
            "validator_version": VALIDATOR_VERSION,
            "job_id": job.job_id,
            "pdf_sha256": job.pdf_sha256,
            "image_sha256": job.image_sha256,
            "source_text_sha256": job.source_text_sha256,
            "prompt_sha256": job.prompt_sha256,
            "status": "complete",
            "completed_at": document.get("completed_at") or _utc_now(),
            "proposal": proposal,
            "dropped": dropped,
            "provider": document.get("provider") or "legacy_gemini",
            "requested_model": document.get("requested_model") or document.get("model"),
            "actual_model": document.get("actual_model") or document.get("model"),
            "attempt_id": document.get("attempt_id"),
            "route_attempts": document.get("route_attempts") or [],
            "usage_tokens": int(document.get("usage_tokens") or 0),
            "compatible_cache_migration": True,
            "migrated_from": path.name,
        }
        _atomic_json(_cache_path(cache_dir, job), migrated)
        return migrated
    return None


def _execute_constrained_column(
    *,
    router: LLMRouter,
    job: VisionJob,
    image: Path,
    units: Sequence[Mapping[str, Any]],
    expected_columns: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Execute detection plus bounded membership batches and assemble a proposal."""

    image_message = (LLMImage(path=image, mime_type=_mime_type(image)),)
    image_tokens = _image_token_estimate(image)
    all_attempts: list[dict[str, Any]] = []
    routed_results = []
    detection_prompt = build_column_detection_prompt(expected_columns)

    def validate_detection(response: Mapping[str, Any]) -> ValidationReport:
        try:
            detected = validate_column_detection(response, expected_columns)
        except ValueError as exc:
            return ValidationReport(decision="reject", fatal_errors=(str(exc),))
        missing = sorted(key for key, present in detected.items() if not present)
        if missing:
            return ValidationReport(
                decision="reject",
                accepted=None,
                unresolved=missing,
                fatal_errors=("Reviewed expected Columns were not all detected.",),
            )
        return ValidationReport(decision="accept", accepted=detected)

    detected_result = router.execute(
        LLMRequest(
            stage=STAGE,
            logical_job_id=f"{job.job_id}_detection",
            prompt=detection_prompt,
            estimated_input_tokens=(
                math.ceil(len(detection_prompt.encode("utf-8")) / 3) + image_tokens
            ),
            reserved_output_tokens=768,
            required_capabilities=("text", "json", "japanese", "vision"),
            images=image_message,
        ),
        validate_detection,
    )
    routed_results.append(detected_result)
    all_attempts.extend(
        {**dict(row), "subtask": "column_detection"}
        for row in detected_result.attempts
    )

    memberships: dict[str, tuple[str, ...]] = {}
    for batch_index, start in enumerate(range(0, len(units), 8), start=1):
        batch = units[start:start + 8]
        prompt = build_membership_prompt(batch, expected_columns)

        def validate_batch(
            response: Mapping[str, Any], batch=batch,
        ) -> ValidationReport:
            try:
                accepted = validate_membership_batch(response, batch, expected_columns)
            except ValueError as exc:
                return ValidationReport(decision="reject", fatal_errors=(str(exc),))
            unresolved = sorted(key for key, value in accepted.items() if not value)
            if len(unresolved) == len(batch):
                return ValidationReport(
                    decision="reject",
                    unresolved=unresolved,
                    fatal_errors=("Membership batch assigned no supplied unit.",),
                    metrics={"batch_units": len(batch), "assigned_units": 0},
                )
            return ValidationReport(
                decision="partial" if unresolved else "accept",
                accepted=accepted,
                unresolved=unresolved,
                metrics={
                    "batch_units": len(batch),
                    "assigned_units": len(batch) - len(unresolved),
                },
            )

        result = router.execute(
            LLMRequest(
                stage=STAGE,
                logical_job_id=f"{job.job_id}_membership_{batch_index}",
                prompt=prompt,
                estimated_input_tokens=(
                    math.ceil(len(prompt.encode("utf-8")) / 3) + image_tokens
                ),
                reserved_output_tokens=1024,
                required_capabilities=("text", "json", "japanese", "vision"),
                images=image_message,
            ),
            validate_batch,
        )
        routed_results.append(result)
        all_attempts.extend(
            {**dict(row), "subtask": "column_membership", "batch": batch_index}
            for row in result.attempts
        )
        memberships.update(dict(result.validation.accepted or {}))

    columns = []
    for row in expected_columns:
        column_id = str(row.get("column_id") or row.get("col_id") or "").strip()
        column_name = str(row.get("column_name") or row.get("col_name") or "").strip()
        columns.append({
            "column_id": column_id,
            "column_name": column_name,
            "region_description": "",
            "region_quote": "",
            "quote_verified": False,
            "pdf_page": None,
            "printed_page": None,
            "figure_pdf_page": job.pdf_page,
            "figure_printed_page": job.printed_page,
            "constraints": [],
            "confidence": "C",
        })

    order_by_column = {row["column_id"]: 0 for row in columns}
    accepted_units = []
    unassigned = []
    for unit in units:
        unit_id = str(unit.get("unit_id") or "").strip()
        column_ids = memberships.get(unit_id, ())
        if not column_ids:
            unassigned.append(unit_id)
            continue
        member_rows = []
        for column_id in column_ids:
            order_by_column[column_id] += 1
            member_rows.append({
                "column_id": column_id,
                "sort_order": order_by_column[column_id],
            })
        accepted_units.append({
            "unit_id": unit_id,
            "unit_name": str(unit.get("unit_name") or "").strip(),
            "memberships": member_rows,
            "age": {"t_int": None, "b_int": None, "visual_label": ""},
            "confidence": "C",
        })

    providers = list(dict.fromkeys(result.provider for result in routed_results))
    requested_models = list(dict.fromkeys(result.requested_model for result in routed_results))
    actual_models = list(dict.fromkeys(result.actual_model for result in routed_results))
    proposal = {
        "schema_version": SCHEMA_VERSION,
        "status": "candidate_review",
        "assignment_ready": True,
        "columns": columns,
        "units": accepted_units,
        "unassigned_units": sorted(unassigned),
        "validation": {
            "canonical_units": len(units),
            "matched_units": len(accepted_units),
            "unassigned_units": len(unassigned),
            "columns": len(columns),
            "verified_region_quotes": 0,
            "subtasks": 1 + math.ceil(len(units) / 8),
        },
    }
    return {
        "proposal": proposal,
        "dropped": ([{
            "kind": "unit_coverage",
            "reason": "constrained membership returned no visible Column",
            "missing": sorted(unassigned),
        }] if unassigned else []),
        "provider": providers[0] if len(providers) == 1 else "composite",
        "requested_model": requested_models[0] if len(requested_models) == 1 else "composite",
        "actual_model": actual_models[0] if len(actual_models) == 1 else "composite",
        "attempt_id": routed_results[-1].attempt_id,
        "route_attempts": all_attempts,
        "providers_used": providers,
        "requested_models_used": requested_models,
        "actual_models_used": actual_models,
        "usage_tokens": sum(int(result.total_tokens or 0) for result in routed_results),
        "external_calls": sum(
            1 for row in all_attempts if row.get("attempt_id")
        ),
    }


def run_column_vision(
    *,
    map_id: str,
    pdf_path: str | os.PathLike[str],
    image_path: str | os.PathLike[str],
    pdf_page: int,
    printed_page: int | None,
    report_text: str,
    units: Sequence[Mapping[str, Any]],
    expected_columns: Sequence[Mapping[str, Any]] = (),
    pdf_index: Mapping[str, Any] | None = None,
    cache_dir: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    model: str = MODEL,
    api_key: str | None = None,
    executor: Executor | None = None,
    router: LLMRouter | None = None,
    generated_at: str | None = None,
    constrained: bool = False,
) -> VisionStageResult:
    """Execute or resume exactly one cached Column/geography Vision job."""

    image = Path(image_path).expanduser().resolve()
    cache_root = Path(cache_dir).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    constrained_mode = bool(constrained and executor is None)
    prompt_version = CONSTRAINED_PROMPT_VERSION if constrained_mode else PROMPT_VERSION
    validator_version = (
        CONSTRAINED_VALIDATOR_VERSION if constrained_mode else VALIDATOR_VERSION
    )
    job_builder = build_constrained_job if constrained_mode else build_job
    job = job_builder(
        map_id=map_id, pdf_path=pdf_path, image_path=image,
        pdf_page=pdf_page, printed_page=printed_page,
        report_text=report_text, units=units,
        expected_columns=expected_columns, model=model,
    )
    cached = _load_cache(
        cache_root, job,
        prompt_version=prompt_version,
        validator_version=validator_version,
    )
    if cached is None and not constrained_mode:
        cached = _migrate_compatible_cache(
            cache_root,
            job,
            units=units,
            expected_columns=expected_columns,
            report_text=report_text,
            pdf_index=pdf_index,
        )
    external_calls = 0
    usage_tokens = 0
    cache_hit = cached is not None
    if cached is None:
        route_result = None
        if constrained_mode:
            active_router = router or (
                single_provider_router(
                    stage=STAGE, provider="gemini", model=job.model,
                    secret=str(api_key),
                )
                if api_key is not None else LLMRouter()
            )
            constrained_result = _execute_constrained_column(
                router=active_router,
                job=job,
                image=image,
                units=units,
                expected_columns=expected_columns,
            )
            proposal = dict(constrained_result["proposal"])
            dropped = list(constrained_result["dropped"])
            usage_tokens = int(constrained_result["usage_tokens"])
            provider = str(constrained_result["provider"])
            requested_model = str(constrained_result["requested_model"])
            actual_model = str(constrained_result["actual_model"])
            attempt_id = constrained_result["attempt_id"]
            route_attempts = list(constrained_result["route_attempts"])
            providers_used = list(constrained_result["providers_used"])
            requested_models_used = list(constrained_result["requested_models_used"])
            actual_models_used = list(constrained_result["actual_models_used"])
            external_calls = int(constrained_result["external_calls"])
        elif executor is not None:
            check_budget(job.model, est_tokens=job.estimated_total_tokens)
            raw = executor(job, image)
            response = raw.get("response") if isinstance(raw.get("response"), Mapping) else raw
            proposal, dropped = validate_response(
                response,
                units=units,
                expected_columns=expected_columns,
                report_text=report_text,
                pdf_index=pdf_index,
                figure_pdf_page=pdf_page,
                figure_printed_page=printed_page,
            )
            usage_tokens = int(raw.get("usage_tokens") or 0)
            provider = "injected_executor"
            requested_model = job.model
            actual_model = job.model
            attempt_id = None
            route_attempts: list[Mapping[str, Any]] = []
            providers_used = [provider]
            requested_models_used = [requested_model]
            actual_models_used = [actual_model]
            external_calls = 1
        else:
            active_router = router or (
                single_provider_router(
                    stage=STAGE, provider="gemini", model=job.model,
                    secret=str(api_key),
                )
                if api_key is not None else LLMRouter()
            )

            def validate_candidate(response: Mapping[str, Any]) -> ValidationReport:
                try:
                    candidate, rejected = validate_response(
                        response,
                        units=units,
                        expected_columns=expected_columns,
                        report_text=report_text,
                        pdf_index=pdf_index,
                        figure_pdf_page=pdf_page,
                        figure_printed_page=printed_page,
                    )
                except ColumnVisionError as exc:
                    return ValidationReport(
                        decision="reject",
                        fatal_errors=(str(exc),),
                    )
                if not candidate.get("assignment_ready"):
                    return ValidationReport(
                        decision="reject",
                        accepted=None,
                        dropped=rejected,
                        unresolved=candidate.get("unassigned_units") or [],
                        metrics=candidate.get("validation") or {},
                    )
                decision = "partial" if candidate.get("unassigned_units") else "accept"
                return ValidationReport(
                    decision=decision,
                    accepted=candidate,
                    dropped=rejected,
                    unresolved=candidate.get("unassigned_units") or [],
                    metrics=candidate.get("validation") or {},
                )

            route_result = active_router.execute(
                LLMRequest(
                    stage=STAGE,
                    logical_job_id=job.job_id,
                    prompt=job.prompt,
                    estimated_input_tokens=job.estimated_input_tokens,
                    reserved_output_tokens=job.reserved_output_tokens,
                    required_capabilities=("text", "json", "japanese", "vision"),
                    images=(LLMImage(path=image, mime_type=_mime_type(image)),),
                ),
                validate_candidate,
            )
            proposal = dict(route_result.validation.accepted)
            dropped = list(route_result.validation.dropped or [])
            usage_tokens = int(route_result.total_tokens or 0)
            provider = route_result.provider
            requested_model = route_result.requested_model
            actual_model = route_result.actual_model
            attempt_id = route_result.attempt_id
            route_attempts = list(route_result.attempts)
            providers_used = [provider]
            requested_models_used = [requested_model]
            actual_models_used = [actual_model]
            external_calls = sum(1 for row in route_attempts if row.get("attempt_id"))
        completed_at = generated_at or _utc_now()
        cached = {
            "schema_version": SCHEMA_VERSION,
            "stage": STAGE,
            "prompt_version": prompt_version,
            "validator_version": validator_version,
            "job_id": job.job_id,
            "pdf_sha256": job.pdf_sha256,
            "image_sha256": job.image_sha256,
            "source_text_sha256": job.source_text_sha256,
            "prompt_sha256": job.prompt_sha256,
            "status": "complete",
            "completed_at": completed_at,
            "proposal": proposal,
            "dropped": dropped,
            "provider": provider,
            "requested_model": requested_model,
            "actual_model": actual_model,
            "attempt_id": attempt_id,
            "route_attempts": route_attempts,
            "providers_used": providers_used,
            "requested_models_used": requested_models_used,
            "actual_models_used": actual_models_used,
            "usage_tokens": usage_tokens,
        }
        _atomic_json(_cache_path(cache_root, job), cached)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "prompt_version": prompt_version,
        "validator_version": validator_version,
        "generated_at": cached.get("completed_at"),
        "map_id": job.map_id,
        "provider": cached.get("provider"),
        "model": cached.get("actual_model") or cached.get("requested_model") or job.model,
        "requested_model": cached.get("requested_model"),
        "actual_model": cached.get("actual_model"),
        "attempt_id": cached.get("attempt_id"),
        "route_attempts": cached.get("route_attempts") or [],
        "providers_used": cached.get("providers_used") or [cached.get("provider")],
        "requested_models_used": cached.get("requested_models_used") or [cached.get("requested_model")],
        "actual_models_used": cached.get("actual_models_used") or [cached.get("actual_model")],
        "job_id": job.job_id,
        "source_pdf": str(Path(pdf_path).expanduser().resolve()),
        "pdf_sha256": job.pdf_sha256,
        "figure_image": str(image),
        "image_sha256": job.image_sha256,
        "figure_pdf_page": job.pdf_page,
        "figure_printed_page": job.printed_page,
        "source_text_sha256": job.source_text_sha256,
        "prompt_sha256": job.prompt_sha256,
        "external_calls": external_calls,
        "cache_hits": 1 if cache_hit else 0,
        "usage_tokens": usage_tokens or int(cached.get("usage_tokens") or 0),
        "proposal_status": cached["proposal"].get("status"),
        "assignment_ready": bool(cached["proposal"].get("assignment_ready")),
        "dropped_count": len(cached.get("dropped") or []),
        "compatible_cache_migration": bool(cached.get("compatible_cache_migration")),
        "cache_file": str(_cache_path(cache_root, job)),
    }
    manifest_path = destination / "vision_manifest.json"
    _atomic_json(destination / "column_proposal.json", cached["proposal"])
    _atomic_json(destination / "vision_dropped.json", {"dropped": cached.get("dropped") or []})
    _atomic_json(manifest_path, manifest)
    return VisionStageResult(
        proposal=dict(cached["proposal"]),
        manifest=manifest,
        manifest_path=manifest_path,
        cache_path=_cache_path(cache_root, job),
        external_calls=external_calls,
        cache_hits=1 if cache_hit else 0,
    )
