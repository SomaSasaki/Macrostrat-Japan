# -*- coding: utf-8 -*-
"""Cached multimodal depositional-environment analysis for GSJ 1:50,000 maps.

The stage sends only unresolved unit contexts and a small ranked set of
stratigraphic/correlation figures.  Model output is never trusted directly:
unit identities, Column scopes, source quotations, figure identifiers, and
Macrostrat controlled terms are verified before evidence is merged.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from common import check_vocab, load_vocab, normalize_vocab
    from compiled_layer import build_canonical_layer, write_canonical_layer
    from llm_column_vision import _image_token_estimate
    from llm_extract import MODEL, check_budget
    from llm_router import (
        AllProvidersFailed, LLMImage, LLMRequest, LLMRouter, ValidationReport,
        single_provider_router,
    )
    from llm_constrained_vision import (
        CONSTRAINED_VALIDATOR_VERSION,
        ENVIRONMENT_CLASSIFICATION_PROMPT_VERSION,
        build_environment_unit_prompt,
        environment_candidates,
        validate_environment_unit,
    )
    from pilot_llm import _canonical_evidence_row
except ImportError:  # pragma: no cover - package-style import
    from .common import check_vocab, load_vocab, normalize_vocab
    from .compiled_layer import build_canonical_layer, write_canonical_layer
    from .llm_column_vision import _image_token_estimate
    from .llm_extract import MODEL, check_budget
    from .llm_router import (
        AllProvidersFailed, LLMImage, LLMRequest, LLMRouter, ValidationReport,
        single_provider_router,
    )
    from .llm_constrained_vision import (
        CONSTRAINED_VALIDATOR_VERSION,
        ENVIRONMENT_CLASSIFICATION_PROMPT_VERSION,
        build_environment_unit_prompt,
        environment_candidates,
        validate_environment_unit,
    )
    from .pilot_llm import _canonical_evidence_row


SCHEMA_VERSION = "pdf-environment-analysis/1.0"
PROMPT_VERSION = "body-figure-environment-v3"
LEGACY_PROMPT_VERSIONS = {"body-figure-environment-v2"}
VALIDATOR_VERSION = "pdf-environment-validator-v2"
CONSTRAINED_PROMPT_VERSION = ENVIRONMENT_CLASSIFICATION_PROMPT_VERSION
STAGE = "pdf_environment_multimodal"
APPLICABILITY = {"applicable", "not_applicable", "unresolved"}
ASSERTIONS = {"explicit", "inferred"}


class PDFEnvironmentError(RuntimeError):
    """The environment stage could not produce a safely verifiable result."""


@dataclass(frozen=True)
class EnvironmentJob:
    job_id: str
    map_id: str
    model: str
    source_sha256: str
    prompt_sha256: str
    target_sha256: str
    image_sha256s: tuple[str, ...]
    prompt: str
    estimated_input_tokens: int
    reserved_output_tokens: int
    estimated_total_tokens: int


Executor = Callable[[EnvironmentJob, Sequence[Path]], Mapping[str, Any]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _normal(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _quote_in_context(quote: str, context: str) -> bool:
    return bool(quote.strip()) and _normal(quote) in _normal(context)


def _value(unit: Mapping[str, Any], field: str) -> Any:
    values = unit.get("values") if isinstance(unit.get("values"), Mapping) else {}
    return values.get(field)


def build_targets(
    compiled: Mapping[str, Any], routed: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select unique units whose resolved environment is still blank."""

    contexts = {
        str(row.get("unit_id") or ""): row
        for row in routed.get("contexts") or []
        if isinstance(row, Mapping) and str(row.get("unit_id") or "")
    }
    targets: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for unit in compiled.get("units") or []:
        if not isinstance(unit, Mapping):
            continue
        unit_id = str(unit.get("unit_id") or "").strip()
        if not unit_id or unit_id in seen:
            continue
        seen.add(unit_id)
        if _value(unit, "environment") not in (None, ""):
            continue
        applicability = (
            ((unit.get("context_evidence") or {}).get("best_by_field") or {})
            .get("environment_applicability")
            or {}
        )
        if str(applicability.get("candidate") or "").strip().casefold() == "not_applicable":
            continue
        context = contexts.get(unit_id)
        if context is None:
            unresolved.append({
                "unit_id": unit_id,
                "reason": "verified_body_context_unavailable",
            })
            continue
        columns = sorted({
            str(value).strip()
            for value in [*(unit.get("column_ids") or []), *(context.get("column_ids") or [])]
            if str(value).strip()
        })
        targets.append({
            "context_id": context.get("context_id"),
            "unit_id": unit_id,
            "unit_name": _value(unit, "unit_name"),
            "column_ids": columns,
            "lithology": _value(unit, "lithology"),
            "minor_lith": _value(unit, "minor_lith"),
            "unit_description": _value(unit, "unit_description"),
            "section": context.get("section"),
            "pdf_page": context.get("pdf_page"),
            "printed_page": context.get("printed_page"),
            "source_text": context.get("text"),
            "text_sha256": context.get("text_sha256") or _sha_text(str(context.get("text") or "")),
        })
    return targets, unresolved


def _environment_vocab() -> list[str]:
    return [str(value).strip() for value in load_vocab().get("environment") or [] if str(value).strip()]


def build_prompt(
    targets: Sequence[Mapping[str, Any]],
    figures: Sequence[Mapping[str, Any]],
    environment_vocab: Sequence[str] | None = None,
) -> str:
    vocab = list(environment_vocab if environment_vocab is not None else _environment_vocab())
    target_payload = [{
        "context_id": row.get("context_id"),
        "unit_id": row.get("unit_id"),
        "unit_name": row.get("unit_name"),
        "column_ids": row.get("column_ids") or [],
        "current_lithology": row.get("lithology"),
        "current_minor_lithology": row.get("minor_lith"),
        "current_description": row.get("unit_description"),
        "source_text": row.get("source_text"),
    } for row in targets]
    figure_payload = [{
        "figure_id": row.get("figure_id"),
        "pdf_page": row.get("pdf_page"),
        "printed_page": row.get("printed_page"),
        "matched_terms": row.get("matched_terms") or [],
    } for row in figures]
    return f"""You are interpreting depositional environments in an official
Geological Survey of Japan 1:50,000 report. Analyze the selected Japanese body
contexts together with the attached stratigraphic/correlation figures.
Attached images are in the same order as FIGURES: the first image is fig_1,
the second is fig_2, and so on.

Return JSON only:
{{"analyses":[{{
  "unit_id":"m0000_u001",
  "column_ids":[],
  "applicability":"applicable",
  "environment":"fluvial indet.",
  "assertion":"inferred",
  "confidence":"B",
  "quote":"verbatim sentence copied from this unit's source_text",
  "figure_ids":["fig_1"],
  "figure_observation":"short English description of the visible evidence",
  "reason":"short English reasoning that combines the evidence",
  "features":["channel deposits","cross stratification"],
  "alternatives":[]
}}]}}

Rules:
- Return one or more analyses for every target unit. Never add a unit.
- Use column_ids=[] when the result applies to every supplied Column. Use a
  non-empty subset only when the body or figure visibly supports a
  Column-specific facies difference.
- applicability=applicable requires exactly one term from ENVIRONMENT_VOCAB.
- applicability=not_applicable is for intrusive, plutonic, metamorphic, or
  other non-depositional units. Set environment=null and support the decision
  with a verbatim quote identifying the unit type.
- applicability=unresolved means the supplied evidence is insufficient. Set
  environment=null and do not guess.
- assertion=explicit only when the quote directly states the depositional
  setting. Otherwise use inferred.
- Inference must combine geologically relevant evidence such as fossils,
  sedimentary structures, lithology, facies relations, or a visible figure.
- Return the most specific supported term that appears verbatim in
  ENVIRONMENT_VOCAB. Source-language setting labels are evidence, not output
  values: never return "subaerial", "subaqueous", or "volcanic indet." as an
  environment value.
- For volcanic, lava, tephra, and pyroclastic units, distinguish eruptive
  process from depositional setting. Use the following controlled-vocabulary
  interpretations only when the cited text or figure supports them:
  * terrestrial/subaerial deposition, land-surface flow, fall tephra, buried
    terrestrial wood, or comparable terrestrial geomorphology -> "non-marine";
  * an explicitly marine, submarine, or sea-floor setting -> "marine";
  * indirect evidence for a marine setting -> "inferred marine";
  * an explicitly shallow-sea setting -> "shallow subtidal";
  * an explicitly lake or freshwater subaqueous setting ->
    "lacustrine indet." unless a more specific lacustrine term is supported.
- "Subaqueous" by itself establishes only that deposition was underwater; it
  does not distinguish marine from lacustrine. Return unresolved unless the
  water body is established by text or figure evidence.
- Marine fossils in reworked clasts do not establish a marine environment.
- Tuff, lava, pyroclastic flow, terrace, sandstone, or mudstone alone does not
  uniquely determine an environment.
- Generic volcanic or pyroclastic wording without setting evidence remains
  unresolved; keep those terms in lithology/description rather than returning
  them as environment.
- current_description is a navigation hint, not independently verified source
  evidence. It supports a result only when the same statement is present in
  source_text or visibly supported by an attached figure.
- A text-supported result requires a verbatim quote copied from that unit's
  source_text. A figure-only inferred result may use an empty quote only when
  figure_ids and figure_observation are both present.
- Never invent a quotation, figure identifier, unit, Column, fossil, or
  structure. Keep reason and figure_observation in English.

ENVIRONMENT_VOCAB:
{json.dumps(vocab, ensure_ascii=False)}

FIGURES:
{json.dumps(figure_payload, ensure_ascii=False, indent=2)}

TARGETS:
{json.dumps(target_payload, ensure_ascii=False, indent=2)}
"""


def _figure_metadata(
    image_paths: Sequence[str | os.PathLike[str]],
    manifest: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    manifest_rows = {
        str(Path(row.get("image_file") or "").resolve()): row
        for row in (manifest or {}).get("candidates") or []
        if isinstance(row, Mapping) and row.get("image_file")
    }
    figures: list[dict[str, Any]] = []
    for index, raw_path in enumerate(image_paths, start=1):
        path = Path(raw_path).expanduser().resolve()
        row = manifest_rows.get(str(path), {})
        figures.append({
            "figure_id": f"fig_{index}",
            "path": str(path),
            "image_sha256": row.get("image_sha256") or _sha_file(path),
            "pdf_page": row.get("pdf_page"),
            "printed_page": row.get("printed_page"),
            "matched_terms": list(row.get("matched_terms") or []),
        })
    return figures


def build_job(
    *,
    map_id: str,
    model: str,
    source_sha256: str,
    targets: Sequence[Mapping[str, Any]],
    figures: Sequence[Mapping[str, Any]],
) -> EnvironmentJob:
    prompt = build_prompt(targets, figures)
    target_identity = [{
        "context_id": row.get("context_id"),
        "unit_id": row.get("unit_id"),
        "column_ids": row.get("column_ids") or [],
        "text_sha256": row.get("text_sha256"),
    } for row in targets]
    target_sha = _sha_text(json.dumps(target_identity, ensure_ascii=False, sort_keys=True))
    image_sha256s = tuple(str(row.get("image_sha256") or "") for row in figures)
    identity = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "prompt_version": PROMPT_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "map_id": str(map_id).strip().lstrip("mM"),
        "source_sha256": source_sha256,
        "target_sha256": target_sha,
        "image_sha256s": image_sha256s,
        "prompt_sha256": _sha_text(prompt),
    }
    job_id = "penv_" + _sha_text(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )[:20]
    image_tokens = sum(_image_token_estimate(Path(row["path"])) for row in figures)
    estimated_input = math.ceil(len(prompt.encode("utf-8")) / 3) + image_tokens
    reserved_output = max(2048, len(targets) * 220)
    return EnvironmentJob(
        job_id=job_id,
        map_id=identity["map_id"],
        model=model,
        source_sha256=source_sha256,
        prompt_sha256=identity["prompt_sha256"],
        target_sha256=target_sha,
        image_sha256s=image_sha256s,
        prompt=prompt,
        estimated_input_tokens=estimated_input,
        reserved_output_tokens=reserved_output,
        estimated_total_tokens=estimated_input + reserved_output,
    )


def build_constrained_job(
    *,
    map_id: str,
    model: str,
    source_sha256: str,
    targets: Sequence[Mapping[str, Any]],
    figures: Sequence[Mapping[str, Any]],
) -> EnvironmentJob:
    """Build a cache identity over exact one-unit closed-world prompts."""

    prompts = [
        build_environment_unit_prompt(
            target, figures, environment_candidates(target),
        )
        for target in targets
    ]
    target_identity = [{
        "context_id": row.get("context_id"),
        "unit_id": row.get("unit_id"),
        "column_ids": row.get("column_ids") or [],
        "text_sha256": row.get("text_sha256"),
    } for row in targets]
    target_sha = _sha_text(json.dumps(target_identity, ensure_ascii=False, sort_keys=True))
    image_sha256s = tuple(str(row.get("image_sha256") or "") for row in figures)
    prompt_identity = json.dumps({
        "prompt_version": CONSTRAINED_PROMPT_VERSION,
        "prompt_hashes": [_sha_text(prompt) for prompt in prompts],
    }, sort_keys=True, separators=(",", ":"))
    identity = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "prompt_version": CONSTRAINED_PROMPT_VERSION,
        "validator_version": CONSTRAINED_VALIDATOR_VERSION,
        "map_id": str(map_id).strip().lstrip("mM"),
        "source_sha256": source_sha256,
        "target_sha256": target_sha,
        "image_sha256s": image_sha256s,
        "prompt_sha256": _sha_text(prompt_identity),
    }
    job_id = "penc_" + _sha_text(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )[:20]
    image_tokens = sum(_image_token_estimate(Path(row["path"])) for row in figures)
    estimated_input = sum(
        math.ceil(len(prompt.encode("utf-8")) / 3) + image_tokens
        for prompt in prompts
    )
    reserved_output = len(targets) * 768
    return EnvironmentJob(
        job_id=job_id,
        map_id=identity["map_id"],
        model=model,
        source_sha256=source_sha256,
        prompt_sha256=identity["prompt_sha256"],
        target_sha256=target_sha,
        image_sha256s=image_sha256s,
        prompt=prompt_identity,
        estimated_input_tokens=estimated_input,
        reserved_output_tokens=reserved_output,
        estimated_total_tokens=estimated_input + reserved_output,
    )


def _mime_type(path: Path) -> str:
    return "image/jpeg" if path.suffix.casefold() in {".jpg", ".jpeg"} else "image/png"


def _verified_environment(value: Any, vocab: Sequence[str]) -> str | None:
    """Resolve one environment term against the vocabulary supplied by the caller.

    ``vocab`` is the authoritative closed world for this call.  The production
    stage passes the Macrostrat ``environments`` table, so its behaviour is
    unchanged.  Closed-world callers pass a short reviewed candidate list that
    may legitimately contain terms outside that table: Macrostrat documents the
    submission field as "free text ... or Macrostrat environment", and
    ``config/vocab.json`` records the same rule.  Rejecting a supplied
    candidate purely because the official table omits it made reviewed answers
    such as "sublittoral" and "bathyal" unreachable for every provider.  A term
    is still refused unless it appears in ``vocab``, so no free text can enter.
    """

    normalized, _changes = normalize_vocab(str(value or "").strip(), "environment")
    vocab_by_key = {str(term).casefold(): str(term) for term in vocab}
    known, unknown = check_vocab(normalized, "environment")
    if not unknown and len(known) == 1 and known[0].casefold() in vocab_by_key:
        return known[0]
    if ";" in normalized or "," in normalized:
        return None
    supplied = vocab_by_key.get(normalized.casefold())
    return supplied


def verify_response(
    targets: Sequence[Mapping[str, Any]],
    figures: Sequence[Mapping[str, Any]],
    response: Mapping[str, Any],
    *,
    environment_vocab: Sequence[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Verify unit/scope identities, evidence locators, quotes, and vocabulary."""

    vocab = list(environment_vocab if environment_vocab is not None else _environment_vocab())
    by_unit = {str(row.get("unit_id") or ""): row for row in targets}
    valid_figures = {str(row.get("figure_id") or "") for row in figures}
    raw_analyses = response.get("analyses")
    if not isinstance(raw_analyses, list):
        raise PDFEnvironmentError("Environment response must contain analyses[].")
    accepted: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    seen_units: set[str] = set()
    seen_scopes: set[tuple[str, tuple[str, ...]]] = set()
    for raw in raw_analyses:
        if not isinstance(raw, Mapping):
            continue
        unit_id = str(raw.get("unit_id") or "").strip()
        target = by_unit.get(unit_id)
        if target is None:
            dropped.append({"unit_id": unit_id, "reason": "unknown_unit_id"})
            continue
        seen_units.add(unit_id)
        allowed_columns = set(target.get("column_ids") or [])
        column_ids = sorted({str(value).strip() for value in raw.get("column_ids") or [] if str(value).strip()})
        if any(value not in allowed_columns for value in column_ids):
            dropped.append({"unit_id": unit_id, "reason": "unknown_column_id", "column_ids": column_ids})
            continue
        scope_key = (unit_id, tuple(column_ids))
        if scope_key in seen_scopes:
            dropped.append({"unit_id": unit_id, "reason": "duplicate_scope", "column_ids": column_ids})
            continue
        seen_scopes.add(scope_key)
        applicability = str(raw.get("applicability") or "").strip().casefold()
        if applicability not in APPLICABILITY:
            dropped.append({"unit_id": unit_id, "reason": "invalid_applicability"})
            continue
        if applicability == "unresolved":
            unresolved.append({
                "unit_id": unit_id,
                "column_ids": column_ids,
                "reason": str(raw.get("reason") or "insufficient evidence").strip(),
            })
            continue
        quote = str(raw.get("quote") or "").strip()
        quote_verified = _quote_in_context(quote, str(target.get("source_text") or ""))
        figure_ids = sorted({str(value).strip() for value in raw.get("figure_ids") or [] if str(value).strip()})
        if any(value not in valid_figures for value in figure_ids):
            dropped.append({"unit_id": unit_id, "reason": "unknown_figure_id"})
            continue
        observation = str(raw.get("figure_observation") or "").strip()
        figure_supported = bool(figure_ids and observation)
        reason = str(raw.get("reason") or "").strip()
        if applicability == "not_applicable":
            if raw.get("environment") not in (None, "") or not quote_verified:
                dropped.append({"unit_id": unit_id, "reason": "not_applicable_requires_verified_quote"})
                continue
            accepted.append({
                "unit_id": unit_id,
                "column_ids": column_ids,
                "field": "environment_applicability",
                "candidate": "not_applicable",
                "assertion": "explicit",
                "confidence": "A",
                "quote": quote,
                "figure_ids": figure_ids,
                "figure_observation": observation,
                "reason": reason,
                "features": list(raw.get("features") or []),
                "context": {key: target.get(key) for key in (
                    "context_id", "section", "pdf_page", "printed_page"
                )},
            })
            continue
        environment = _verified_environment(raw.get("environment"), vocab)
        if environment is None:
            dropped.append({"unit_id": unit_id, "reason": "environment_not_in_controlled_vocab"})
            continue
        assertion = str(raw.get("assertion") or "").strip().casefold()
        if assertion not in ASSERTIONS:
            dropped.append({"unit_id": unit_id, "reason": "invalid_assertion"})
            continue
        if not quote_verified and not figure_supported:
            dropped.append({"unit_id": unit_id, "reason": "no_verified_text_or_figure_evidence"})
            continue
        if assertion == "explicit" and not quote_verified:
            assertion = "inferred"
        confidence = "A" if assertion == "explicit" else ("B" if quote_verified and figure_supported else "C")
        accepted.append({
            "unit_id": unit_id,
            "column_ids": column_ids,
            "field": "environment",
            "candidate": environment,
            "assertion": assertion,
            "confidence": confidence,
            "quote": quote if quote_verified else "",
            "figure_ids": figure_ids,
            "figure_observation": observation,
            "reason": reason,
            "features": list(raw.get("features") or []),
            "alternatives": [
                value for value in raw.get("alternatives") or []
                if _verified_environment(value, vocab) is not None
            ],
            "context": {key: target.get(key) for key in (
                "context_id", "section", "pdf_page", "printed_page"
            )},
        })
    for unit_id in sorted(set(by_unit) - seen_units):
        unresolved.append({"unit_id": unit_id, "reason": "model_omitted_target_unit"})
    return accepted, dropped, unresolved


def _evidence_id(job_id: str, row: Mapping[str, Any]) -> str:
    identity = {
        "job_id": job_id,
        "unit_id": row.get("unit_id"),
        "column_ids": row.get("column_ids"),
        "field": row.get("field"),
        "candidate": row.get("candidate"),
        "quote": row.get("quote"),
        "figure_ids": row.get("figure_ids"),
    }
    return "ev_" + _sha_text(json.dumps(identity, ensure_ascii=False, sort_keys=True))[:16]


def evidence_rows(
    accepted: Sequence[Mapping[str, Any]],
    *,
    job_id: str,
    source_file: str,
    model: str,
    figures: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    figures_by_id = {str(row.get("figure_id")): row for row in figures}
    output: list[dict[str, Any]] = []
    for row in accepted:
        context = row.get("context") or {}
        figure_rows = [figures_by_id[value] for value in row.get("figure_ids") or [] if value in figures_by_id]
        figure_labels = [
            f"{item.get('figure_id')} PDF p.{item.get('pdf_page')}"
            for item in figure_rows
        ]
        locator_parts = [
            str(context.get("section") or "").strip(),
            f"PDF p.{context.get('pdf_page')}" if context.get("pdf_page") else "",
            f"printed p.{context.get('printed_page')}" if context.get("printed_page") else "",
            *figure_labels,
        ]
        quote = str(row.get("quote") or "").strip()
        observation = str(row.get("figure_observation") or "").strip()
        reason = str(row.get("reason") or "").strip()
        full_context = " | ".join(value for value in (
            quote,
            f"Figure observation: {observation}" if observation else "",
            f"Reason: {reason}" if reason else "",
        ) if value)
        column_ids = list(row.get("column_ids") or [])
        output.append({
            "evidence_id": _evidence_id(job_id, row),
            "unit_id": row.get("unit_id"),
            "column_ids": column_ids,
            "scope_type": "column_specific" if column_ids else "unit_global",
            "field": row.get("field"),
            "candidate": row.get("candidate"),
            "source_type": "PDF",
            "source_file": source_file,
            "source_locator": " / ".join(value for value in locator_parts if value),
            "PDF_page": context.get("pdf_page") or (figure_rows[0].get("pdf_page") if figure_rows else None),
            "printed_page": context.get("printed_page"),
            "section_or_table": (
                context.get("section")
                or ("; ".join(figure_labels) if figure_labels else "PDF environment analysis")
            ),
            "matched_sentence": quote or observation,
            "full_context_quote": full_context,
            "confidence_class": row.get("confidence"),
            "assertion": row.get("assertion"),
            "selection": "validation" if row.get("field") == "environment_applicability" else "candidate",
            "extraction_method": (
                f"{STAGE}; {model}; {PROMPT_VERSION}; verified Japanese body + "
                "ranked stratigraphic/correlation figures"
            ),
        })
    return output


def _load_cache(
    cache_dir: Path,
    job: EnvironmentJob,
    *,
    prompt_version: str = PROMPT_VERSION,
    validator_version: str = VALIDATOR_VERSION,
) -> dict[str, Any] | None:
    path = cache_dir / f"{job.job_id}.json"
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
        "source_sha256": job.source_sha256,
        "prompt_sha256": job.prompt_sha256,
        "target_sha256": job.target_sha256,
        "image_sha256s": list(job.image_sha256s),
        "status": "complete",
    }
    return document if all(document.get(key) == value for key, value in expected.items()) else None


def _accepted_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("unit_id") or ""),
        tuple(sorted(str(value) for value in row.get("column_ids") or [])),
        str(row.get("field") or ""),
        str(row.get("candidate") or ""),
        str(row.get("assertion") or ""),
        _normal(row.get("quote")),
        tuple(sorted(str(value) for value in row.get("figure_ids") or [])),
    )


def _revalidate_cached_environment(
    targets: Sequence[Mapping[str, Any]],
    figures: Sequence[Mapping[str, Any]],
    accepted: Sequence[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]] | None:
    analyses: list[dict[str, Any]] = []
    old_signatures: list[tuple[Any, ...]] = []
    for row in accepted:
        if not isinstance(row, Mapping):
            return None
        field = str(row.get("field") or "")
        if field not in {"environment", "environment_applicability"}:
            return None
        old_signatures.append(_accepted_signature(row))
        not_applicable = field == "environment_applicability"
        analyses.append({
            "unit_id": row.get("unit_id"),
            "column_ids": list(row.get("column_ids") or []),
            "applicability": "not_applicable" if not_applicable else "applicable",
            "environment": None if not_applicable else row.get("candidate"),
            "assertion": row.get("assertion") or ("explicit" if not_applicable else "inferred"),
            "quote": row.get("quote") or "",
            "figure_ids": list(row.get("figure_ids") or []),
            "figure_observation": row.get("figure_observation") or "",
            "reason": row.get("reason") or "",
            "features": list(row.get("features") or []),
            "alternatives": list(row.get("alternatives") or []),
        })
    if not analyses:
        return None
    try:
        verified, dropped, unresolved = verify_response(
            targets, figures, {"analyses": analyses},
        )
    except PDFEnvironmentError:
        return None
    if dropped or sorted(_accepted_signature(row) for row in verified) != sorted(old_signatures):
        return None
    return verified, dropped, unresolved


def _migrate_compatible_cache(
    cache_dir: Path,
    job: EnvironmentJob,
    *,
    targets: Sequence[Mapping[str, Any]],
    figures: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    target_path = cache_dir / f"{job.job_id}.json"
    for path in sorted(cache_dir.glob("penv_*.json")) if cache_dir.is_dir() else ():
        if path == target_path:
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(document, Mapping):
            continue
        if (
            document.get("schema_version") != SCHEMA_VERSION
            or document.get("stage") != STAGE
            or document.get("prompt_version") not in {PROMPT_VERSION, *LEGACY_PROMPT_VERSIONS}
            or document.get("source_sha256") != job.source_sha256
            or document.get("target_sha256") != job.target_sha256
            or document.get("image_sha256s") != list(job.image_sha256s)
            or document.get("status") != "complete"
            or not isinstance(document.get("accepted"), list)
        ):
            continue
        revalidated = _revalidate_cached_environment(
            targets, figures, document.get("accepted") or [],
        )
        if revalidated is None:
            continue
        accepted, dropped, unresolved = revalidated
        migrated = {
            "schema_version": SCHEMA_VERSION,
            "stage": STAGE,
            "prompt_version": PROMPT_VERSION,
            "validator_version": VALIDATOR_VERSION,
            "status": "complete",
            "job_id": job.job_id,
            "provider": document.get("provider") or "legacy_gemini",
            "requested_model": document.get("requested_model") or document.get("model"),
            "actual_model": document.get("actual_model") or document.get("model"),
            "model": document.get("actual_model") or document.get("model") or job.model,
            "attempt_id": document.get("attempt_id"),
            "route_attempts": document.get("route_attempts") or [],
            "source_sha256": job.source_sha256,
            "prompt_sha256": job.prompt_sha256,
            "target_sha256": job.target_sha256,
            "image_sha256s": list(job.image_sha256s),
            "completed_at": document.get("completed_at") or _utc_now(),
            "accepted": accepted,
            "dropped": dropped,
            "unresolved": unresolved,
            "estimated_tokens": int(document.get("estimated_tokens") or 0),
            "usage_tokens": int(document.get("usage_tokens") or 0),
            "compatible_cache_migration": True,
            "migrated_from": path.name,
        }
        _atomic_json(target_path, migrated)
        return migrated
    return None


# 1 unitごとに外部送信するため、無料枠のレート制限に当たりやすい。
# 送信の間隔を必ず空ける（環境変数で調整可能）。
UNIT_REQUEST_INTERVAL_SECONDS = float(os.environ.get("MACROSTRAT_UNIT_INTERVAL", "1.5"))


def _execute_constrained_environment(
    *,
    router: LLMRouter,
    job: EnvironmentJob,
    targets: Sequence[Mapping[str, Any]],
    figures: Sequence[Mapping[str, Any]],
    image_files: Sequence[Path],
) -> dict[str, Any]:
    """Classify each unit independently so one bad response cannot poison all units."""

    images = tuple(
        LLMImage(path=image, mime_type=_mime_type(image)) for image in image_files
    )
    image_tokens = sum(_image_token_estimate(image) for image in image_files)
    accepted: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    all_attempts: list[dict[str, Any]] = []
    routed_results = []

    for index, target in enumerate(targets, start=1):
        # 無料枠のレート制限に当てないよう、2件目以降は必ず間隔を空ける。
        # 2026-08-13の実行は49 unitを間断なく送って mistral の rate_limit に当たり、
        # 再試行待ちのまま5時間走り続けた。1.5秒/unitなら49 unitでも約73秒しか増えない。
        if index > 1:
            time.sleep(UNIT_REQUEST_INTERVAL_SECONDS)
        unit_id = str(target.get("unit_id") or "").strip()
        candidates = environment_candidates(target)
        prompt = build_environment_unit_prompt(target, figures, candidates)

        def validate_unit(response: Mapping[str, Any]) -> ValidationReport:
            try:
                rows, rejected, undecided = validate_environment_unit(
                    response, target, figures, candidates,
                )
            except (PDFEnvironmentError, ValueError) as exc:
                return ValidationReport(decision="reject", fatal_errors=(str(exc),))
            if not rows:
                return ValidationReport(
                    decision="reject", dropped=rejected, unresolved=undecided,
                    metrics={"accepted_rows": 0},
                )
            return ValidationReport(
                decision="accept",
                accepted={"accepted": rows, "dropped": rejected, "unresolved": undecided},
                dropped=rejected,
                unresolved=undecided,
                metrics={"accepted_rows": len(rows)},
            )

        try:
            result = router.execute(
                LLMRequest(
                    stage=STAGE,
                    logical_job_id=f"{job.job_id}_unit_{index}_{unit_id}",
                    prompt=prompt,
                    estimated_input_tokens=(
                        math.ceil(len(prompt.encode("utf-8")) / 3) + image_tokens
                    ),
                    reserved_output_tokens=768,
                    required_capabilities=("text", "json", "japanese", "vision"),
                    images=images,
                ),
                validate_unit,
            )
        except AllProvidersFailed as exc:
            attempts = [
                {**dict(row), "subtask": "environment_unit", "unit_id": unit_id}
                for row in exc.attempts
            ]
            all_attempts.extend(attempts)
            unresolved.append({
                "unit_id": unit_id,
                "reason": "all_providers_failed",
                "error_kinds": [str(row.get("error_kind") or "unknown") for row in attempts],
            })
            continue

        routed_results.append(result)
        all_attempts.extend(
            {**dict(row), "subtask": "environment_unit", "unit_id": unit_id}
            for row in result.attempts
        )
        validated = result.validation.accepted
        if not isinstance(validated, Mapping):
            unresolved.append({"unit_id": unit_id, "reason": "accepted_payload_missing"})
            continue
        accepted.extend(list(validated.get("accepted") or []))
        dropped.extend(list(validated.get("dropped") or []))
        unresolved.extend(list(validated.get("unresolved") or []))

    if not accepted:
        raise PDFEnvironmentError(
            "Constrained Environment produced no validated unit result across the route."
        )
    providers = list(dict.fromkeys(result.provider for result in routed_results))
    requested_models = list(dict.fromkeys(result.requested_model for result in routed_results))
    actual_models = list(dict.fromkeys(result.actual_model for result in routed_results))
    successful_units = {str(row.get("unit_id") or "") for row in accepted}
    return {
        "accepted": accepted,
        "dropped": dropped,
        "unresolved": unresolved,
        "provider": providers[0] if len(providers) == 1 else "composite",
        "requested_model": requested_models[0] if len(requested_models) == 1 else "composite",
        "actual_model": actual_models[0] if len(actual_models) == 1 else "composite",
        "attempt_id": routed_results[-1].attempt_id if routed_results else None,
        "route_attempts": all_attempts,
        "providers_used": providers,
        "requested_models_used": requested_models,
        "actual_models_used": actual_models,
        "validation": {
            "target_units": len(targets),
            "accepted_units": len(successful_units),
            "accepted_rows": len(accepted),
            "dropped_rows": len(dropped),
            "unresolved_units": len({str(row.get("unit_id") or "") for row in unresolved}),
            "coverage": round(len(successful_units) / len(targets), 6),
            "subtasks": len(targets),
        },
        "usage_tokens": sum(int(result.total_tokens or 0) for result in routed_results),
        "external_calls": sum(1 for row in all_attempts if row.get("attempt_id")),
    }


def run_environment_enrichment(
    system_dir: str | Path,
    routed: Mapping[str, Any],
    *,
    source_file: str | Path,
    source_sha256: str,
    image_paths: Sequence[str | os.PathLike[str]],
    figure_manifest: Mapping[str, Any] | None,
    cache_dir: str | Path,
    model: str = MODEL,
    api_key: str | None = None,
    executor: Executor | None = None,
    router: LLMRouter | None = None,
    generated_at: str | None = None,
    constrained: bool = False,
) -> dict[str, Any]:
    """Run/resume one environment call and merge verified evidence."""

    root = Path(system_dir).expanduser().resolve()
    output_dir = root / "environment_analysis"
    output_dir.mkdir(parents=True, exist_ok=True)
    compiled = json.loads((root / "compiled.json").read_text(encoding="utf-8"))
    targets, routing_unresolved = build_targets(compiled, routed)
    figures = _figure_metadata(image_paths, figure_manifest)
    completed_at = generated_at or _utc_now()
    if not targets:
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "stage": STAGE,
            "status": "no_targets",
            "external_calls": 0,
            "cache_hits": 0,
            "target_units": 0,
            "accepted": 0,
            "dropped": 0,
            "unresolved": routing_unresolved,
            "figures": figures,
        }
        _atomic_json(output_dir / "environment_manifest.json", manifest)
        return manifest

    map_id = str((compiled.get("map") or {}).get("map_id") or routed.get("map_id") or "")
    constrained_mode = bool(constrained and executor is None)
    prompt_version = CONSTRAINED_PROMPT_VERSION if constrained_mode else PROMPT_VERSION
    validator_version = (
        CONSTRAINED_VALIDATOR_VERSION if constrained_mode else VALIDATOR_VERSION
    )
    job_builder = build_constrained_job if constrained_mode else build_job
    job = job_builder(
        map_id=map_id,
        model=model,
        source_sha256=source_sha256,
        targets=targets,
        figures=figures,
    )
    cache_root = Path(cache_dir).expanduser().resolve()
    cache_path = cache_root / f"{job.job_id}.json"
    cached = _load_cache(
        cache_root, job,
        prompt_version=prompt_version,
        validator_version=validator_version,
    )
    if cached is None and not constrained_mode:
        cached = _migrate_compatible_cache(
            cache_root,
            job,
            targets=targets,
            figures=figures,
        )
    cache_hit = cached is not None
    image_files = [Path(row["path"]) for row in figures]
    if cached is None:
        if constrained_mode:
            active_router = router or (
                single_provider_router(
                    stage=STAGE, provider="gemini", model=job.model,
                    secret=str(api_key),
                )
                if api_key is not None else LLMRouter()
            )
            constrained_result = _execute_constrained_environment(
                router=active_router,
                job=job,
                targets=targets,
                figures=figures,
                image_files=image_files,
            )
            accepted = list(constrained_result["accepted"])
            dropped = list(constrained_result["dropped"])
            unresolved = list(constrained_result["unresolved"])
            provider = str(constrained_result["provider"])
            requested_model = str(constrained_result["requested_model"])
            actual_model = str(constrained_result["actual_model"])
            attempt_id = constrained_result["attempt_id"]
            route_attempts = list(constrained_result["route_attempts"])
            providers_used = list(constrained_result["providers_used"])
            requested_models_used = list(constrained_result["requested_models_used"])
            actual_models_used = list(constrained_result["actual_models_used"])
            validation_metrics = dict(constrained_result["validation"])
            usage_tokens = int(constrained_result["usage_tokens"])
            external_calls = int(constrained_result["external_calls"])
        elif executor is not None:
            # The injected boundary is used by hermetic tests/dry runs.  It
            # observes configured model/per-call constraints, but not real
            # provider usage accumulated elsewhere during the day.
            check_budget(
                job.model,
                est_tokens=job.estimated_total_tokens,
                usage={"calls": 0, "tokens": 0},
            )
            result = executor(job, image_files)
            response = result.get("response") if isinstance(result.get("response"), Mapping) else result
            accepted, dropped, unresolved = verify_response(targets, figures, response)
            provider = "injected_executor"
            requested_model = job.model
            actual_model = job.model
            attempt_id = None
            route_attempts: list[Mapping[str, Any]] = []
            validation_metrics: Mapping[str, Any] = {}
            usage_tokens = int(result.get("usage_tokens") or 0)
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
                    candidate_rows, rejected_rows, unresolved_rows = verify_response(
                        targets, figures, response,
                    )
                except PDFEnvironmentError as exc:
                    return ValidationReport(
                        decision="reject",
                        fatal_errors=(str(exc),),
                    )
                target_units = {str(row.get("unit_id") or "") for row in targets}
                accepted_units = {
                    str(row.get("unit_id") or "") for row in candidate_rows
                }
                metrics = {
                    "target_units": len(target_units),
                    "accepted_units": len(accepted_units),
                    "accepted_rows": len(candidate_rows),
                    "dropped_rows": len(rejected_rows),
                    "unresolved_units": len({
                        str(row.get("unit_id") or "") for row in unresolved_rows
                    }),
                    "coverage": round(
                        len(accepted_units) / len(target_units), 6,
                    ) if target_units else 1.0,
                }
                if not candidate_rows:
                    return ValidationReport(
                        decision="reject",
                        dropped=rejected_rows,
                        unresolved=unresolved_rows,
                        metrics=metrics,
                    )
                return ValidationReport(
                    decision="accept" if accepted_units == target_units else "partial",
                    accepted={
                        "accepted": candidate_rows,
                        "dropped": rejected_rows,
                        "unresolved": unresolved_rows,
                    },
                    dropped=rejected_rows,
                    unresolved=unresolved_rows,
                    metrics=metrics,
                )

            routed_result = active_router.execute(
                LLMRequest(
                    stage=STAGE,
                    logical_job_id=job.job_id,
                    prompt=job.prompt,
                    estimated_input_tokens=job.estimated_input_tokens,
                    reserved_output_tokens=job.reserved_output_tokens,
                    required_capabilities=("text", "json", "japanese", "vision"),
                    images=tuple(
                        LLMImage(path=image, mime_type=_mime_type(image))
                        for image in image_files
                    ),
                ),
                validate_candidate,
            )
            validated = routed_result.validation.accepted
            if not isinstance(validated, Mapping):
                raise PDFEnvironmentError(
                    "Router accepted environment output without validated rows."
                )
            accepted = list(validated.get("accepted") or [])
            dropped = list(validated.get("dropped") or [])
            unresolved = list(validated.get("unresolved") or [])
            provider = routed_result.provider
            requested_model = routed_result.requested_model
            actual_model = routed_result.actual_model
            attempt_id = routed_result.attempt_id
            route_attempts = list(routed_result.attempts)
            validation_metrics = routed_result.validation.metrics
            usage_tokens = int(routed_result.total_tokens or 0)
            providers_used = [provider]
            requested_models_used = [requested_model]
            actual_models_used = [actual_model]
            external_calls = sum(
                1 for attempt in route_attempts if attempt.get("attempt_id")
            )
        cache_document = {
            "schema_version": SCHEMA_VERSION,
            "stage": STAGE,
            "prompt_version": prompt_version,
            "validator_version": validator_version,
            "status": "complete",
            "job_id": job.job_id,
            "provider": provider,
            "requested_model": requested_model,
            "actual_model": actual_model,
            "model": actual_model or requested_model,
            "attempt_id": attempt_id,
            "route_attempts": route_attempts,
            "providers_used": providers_used,
            "requested_models_used": requested_models_used,
            "actual_models_used": actual_models_used,
            "validation": dict(validation_metrics),
            "source_sha256": source_sha256,
            "prompt_sha256": job.prompt_sha256,
            "target_sha256": job.target_sha256,
            "image_sha256s": list(job.image_sha256s),
            "completed_at": completed_at,
            "accepted": accepted,
            "dropped": dropped,
            "unresolved": unresolved,
            "estimated_tokens": job.estimated_total_tokens,
            "usage_tokens": usage_tokens,
        }
        _atomic_json(cache_path, cache_document)
        cached = cache_document
        cache_hits = 0
    else:
        accepted = list(cached.get("accepted") or [])
        dropped = list(cached.get("dropped") or [])
        unresolved = list(cached.get("unresolved") or [])
        external_calls, cache_hits = 0, 1

    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    additions = evidence_rows(
        accepted,
        job_id=job.job_id,
        source_file=str(Path(source_file).expanduser().resolve()),
        model=str(cached.get("actual_model") or cached.get("model") or model),
        figures=figures,
    )
    existing_rows = [_canonical_evidence_row(row) for row in evidence.get("evidence") or []]
    unit_rows: list[dict[str, Any]] = []
    for unit in compiled.get("units") or []:
        row = dict(unit.get("review_values") or {})
        if unit.get("formulas"):
            row["_formulas"] = dict(unit["formulas"])
        unit_rows.append(row)
    map_doc = compiled.get("map") or {}
    rebuilt, evidence_doc = build_canonical_layer(
        unit_rows,
        column_rows=map_doc.get("columns") or [],
        evidence_rows=[*existing_rows, *additions],
        metadata=map_doc.get("metadata") or {},
        map_id=map_doc.get("map_id"),
        source_review=map_doc.get("source_review"),
        generated_at=completed_at,
    )
    write_canonical_layer(rebuilt, evidence_doc, root)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "prompt_version": prompt_version,
        "validator_version": validator_version,
        "status": "complete",
        "job_id": job.job_id,
        "provider": cached.get("provider"),
        "requested_model": cached.get("requested_model"),
        "actual_model": cached.get("actual_model"),
        "model": cached.get("actual_model") or cached.get("model") or model,
        "attempt_id": cached.get("attempt_id"),
        "route_attempts": cached.get("route_attempts") or [],
        "providers_used": cached.get("providers_used") or [cached.get("provider")],
        "requested_models_used": cached.get("requested_models_used") or [cached.get("requested_model")],
        "actual_models_used": cached.get("actual_models_used") or [cached.get("actual_model")],
        "validation": cached.get("validation") or {},
        "compatible_cache_migration": bool(cached.get("compatible_cache_migration")),
        "external_calls": external_calls,
        "cache_hits": cache_hits,
        "target_units": len(targets),
        "accepted": len(accepted),
        "added_evidence": len(additions),
        "not_applicable": sum(row.get("field") == "environment_applicability" for row in accepted),
        "dropped": len(dropped),
        "unresolved": [*routing_unresolved, *unresolved],
        "estimated_tokens": 0 if cache_hit else job.estimated_total_tokens,
        "figures": figures,
        "cache_file": str(cache_path),
    }
    _atomic_json(output_dir / "environment_manifest.json", manifest)
    _atomic_json(output_dir / "environment_dropped.json", {"dropped": dropped})
    return manifest


__all__ = [
    "APPLICABILITY",
    "CONSTRAINED_PROMPT_VERSION",
    "EnvironmentJob",
    "PDFEnvironmentError",
    "PROMPT_VERSION",
    "SCHEMA_VERSION",
    "STAGE",
    "VALIDATOR_VERSION",
    "build_constrained_job",
    "build_job",
    "build_prompt",
    "build_targets",
    "evidence_rows",
    "run_environment_enrichment",
    "verify_response",
]
