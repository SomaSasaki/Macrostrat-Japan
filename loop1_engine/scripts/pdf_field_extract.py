# -*- coding: utf-8 -*-
"""Cached, quote-verified extraction from targeted Japanese PDF body contexts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from compiled_layer import build_canonical_layer, write_canonical_layer
    from common import load_secret, resolve_lithology_value, valid_strat_name
    from llm_extract import MODEL, load_limits, today_usage, vocab_hint
    from llm_router import LLMRequest, LLMRouter, ValidationReport, single_provider_router
    from pilot_llm import _canonical_evidence_row
    from local_age_extract import is_local_measurement_quote, supports_formation_boundary
except ImportError:  # pragma: no cover - package-style import
    from .compiled_layer import build_canonical_layer, write_canonical_layer
    from .common import load_secret, resolve_lithology_value, valid_strat_name
    from .llm_extract import MODEL, load_limits, today_usage, vocab_hint
    from .llm_router import LLMRequest, LLMRouter, ValidationReport, single_provider_router
    from .pilot_llm import _canonical_evidence_row
    from .local_age_extract import is_local_measurement_quote, supports_formation_boundary


SCHEMA_VERSION = "pdf-field-enrichment/1.0"
PROMPT_VERSION = "japanese-body-fields-v2-lithology-roles"
STAGE = "pdf_body_field_enrichment"
NUMERIC_FIELDS = {"t_age_ma", "b_age_ma", "min_thickness", "max_thickness"}
INFERRED_FIELDS = {
    "strat_name", "environment", "unit_description", "lithology",
    "minor_lith", "basal_surface", "lateral_relationship",
}
ALLOWED_FIELDS = NUMERIC_FIELDS | INFERRED_FIELDS
Executor = Callable[[str], Mapping[str, Any]]


class PDFFieldError(RuntimeError):
    """Actionable body-enrichment failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def build_prompt(contexts: Sequence[Mapping[str, Any]]) -> str:
    payload = [{
        "context_id": row.get("context_id"),
        "unit_id": row.get("unit_id"),
        "unit_name": row.get("unit_name"),
        "column_ids": row.get("column_ids") or [],
        "requested_fields": [field for field in row.get("requested_fields") or [] if field in ALLOWED_FIELDS],
        "source_text": row.get("text"),
    } for row in contexts]
    return f"""You are extracting Macrostrat review candidates from selected Japanese
body passages in a Geological Survey of Japan 1:50,000 report.

Return JSON only in this exact structure:
{{"units":[{{"unit_id":"m0000_u001","fields":{{"lithology":"sandstone"}},
"quotes":{{"lithology":"verbatim Japanese source sentence"}},
"field_meta":{{"lithology":{{"role":"major","role_cue":"主に"}}}},
"absent_fields":{{"minor_lith":{{"quote":"verbatim sentence explicitly saying only sandstone is present",
"reason":"source explicitly limits the unit to one lithology"}}}}}}]}}

Rules:
- Process only the unit_id and requested_fields supplied below.
- Every non-null field requires a verbatim quote copied from that unit's
  source_text. Never cite another unit or the target list.
- Translate or summarize into concise English, but do not add facts.
- unit_description must be a self-contained English description beginning
  with the English unit name. It is an inferred candidate, not a direct quote.
- t_age_ma is the younger/top boundary; b_age_ma is the older/bottom boundary.
  Convert ka to Ma. An isolated date is not automatically both boundaries,
  except a clearly instantaneous lava, tephra, fall or pyroclastic-flow event.
- lithology is the main material; minor_lith is subordinate/intercalated
  material. Use semicolons between multiple Macrostrat terms.
- Every returned lithology/minor_lith requires field_meta. Use role=major for
  lithology and role=minor for minor_lith. role_cue must be a verbatim phrase
  inside that field's quote, such as 主体とする, 主に, 伴う, 挟在, with or minor.
- Preserve valid Macrostrat lithology attributes such as tuffaceous. Do not
  silently reduce tuffaceous sandstone to sandstone.
- absent_fields is optional and is allowed only for minor_lith. Use it only
  when the source explicitly says the listed main lithology is exclusive
  (for example ～のみからなる, solely or exclusively). Mere omission of a
  subordinate lithology is not evidence of absence.
- min_thickness/max_thickness are metres. A range supplies both; "more than"
  supplies only minimum; "up to" supplies only maximum.
- basal_surface accepts only conformable, disconformable, unconformable,
  fault, gradational, sharp, erosional or intrusive when explicitly supported.
- lateral_relationship accepts only explicit interfingering, transgressive,
  onlap, erosional or gradational lateral relationships. Superposition alone
  is not a lateral relationship.
- Omit unsupported fields. Do not return null placeholders.

{vocab_hint()}

Targeted contexts:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def _normal(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _quote_in_context(quote: str, context: str) -> bool:
    return bool(quote.strip()) and _normal(quote) in _normal(context)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _numeric_supported(field: str, value: Any, quote: str) -> bool:
    candidate = _number(value)
    if candidate is None:
        return False
    normalized = str(quote).replace(",", "")
    hits = [float(raw) for raw in re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", normalized)]
    if any(math.isclose(candidate, hit, rel_tol=0, abs_tol=1e-9) for hit in hits):
        return True
    if field in {"t_age_ma", "b_age_ma"} and re.search(r"\bka\b|千年", normalized, re.IGNORECASE):
        return any(math.isclose(candidate, hit / 1000, rel_tol=0, abs_tol=1e-9) for hit in hits)
    return False


def verify_response(
    contexts: Sequence[Mapping[str, Any]], response: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Verify exact unit, requested field, source quote and numeric support."""
    by_unit = {str(row.get("unit_id") or ""): row for row in contexts}
    raw_units = response.get("units")
    if not isinstance(raw_units, list):
        raise PDFFieldError("Body extraction response must contain units[].")
    accepted: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for raw in raw_units:
        if not isinstance(raw, Mapping):
            continue
        unit_id = str(raw.get("unit_id") or "")
        context = by_unit.get(unit_id)
        if context is None:
            dropped.append({"unit_id": unit_id, "reason": "unknown_unit_id"})
            continue
        fields = raw.get("fields") if isinstance(raw.get("fields"), Mapping) else {}
        quotes = raw.get("quotes") if isinstance(raw.get("quotes"), Mapping) else {}
        field_meta = raw.get("field_meta") if isinstance(raw.get("field_meta"), Mapping) else {}
        requested = set(context.get("requested_fields") or []) & ALLOWED_FIELDS
        for field, value in fields.items():
            if field not in requested or field not in ALLOWED_FIELDS or value in (None, ""):
                dropped.append({"unit_id": unit_id, "field": field, "reason": "field_not_requested"})
                continue
            quote = str(quotes.get(field) or "").strip()
            if not _quote_in_context(quote, str(context.get("text") or "")):
                dropped.append({"unit_id": unit_id, "field": field, "reason": "quote_not_in_context"})
                continue
            if field in NUMERIC_FIELDS and not _numeric_supported(field, value, quote):
                dropped.append({"unit_id": unit_id, "field": field, "reason": "numeric_value_not_in_quote"})
                continue
            if (
                field in {"t_age_ma", "b_age_ma"}
                and is_local_measurement_quote(quote)
                and not supports_formation_boundary(quote)
            ):
                dropped.append({
                    "unit_id": unit_id,
                    "field": field,
                    "reason": "local_measurement_not_formation_boundary",
                })
                continue
            if field == "strat_name" and not valid_strat_name(
                value, context.get("unit_name")
            ):
                dropped.append({
                    "unit_id": unit_id,
                    "field": field,
                    "reason": "not_a_lithostratigraphic_parent_name",
                })
                continue
            metadata: dict[str, Any] = {}
            if field in {"lithology", "minor_lith"}:
                meta = field_meta.get(field) if isinstance(field_meta.get(field), Mapping) else {}
                expected_role = "major" if field == "lithology" else "minor"
                role = str(meta.get("role") or "").strip().casefold()
                role_cue = str(meta.get("role_cue") or "").strip()
                if role != expected_role:
                    dropped.append({"unit_id": unit_id, "field": field, "reason": "missing_or_invalid_role"})
                    continue
                if not role_cue or not _quote_in_context(role_cue, quote):
                    dropped.append({"unit_id": unit_id, "field": field, "reason": "role_cue_not_in_quote"})
                    continue
                resolved = resolve_lithology_value(value)
                if not resolved.get("value"):
                    dropped.append({
                        "unit_id": unit_id, "field": field,
                        "reason": "lithology_not_in_controlled_vocabulary",
                        "unknown": resolved.get("unknown"),
                    })
                    continue
                value = resolved["value"]
                metadata = {
                    "raw_phrase": fields.get(field),
                    "normalized_terms": resolved.get("known"),
                    "role": role,
                    "role_cue": role_cue,
                    "dropped_modifiers": [
                        modifier
                        for detail in resolved.get("details") or []
                        for modifier in detail.get("dropped_modifiers") or []
                    ],
                    "parser": "pdf_body_lithology/v2",
                    "source_span": quote,
                }
            accepted.append({
                "unit_id": unit_id,
                "field": field,
                "candidate": value,
                "quote": quote,
                "context": context,
                "assertion": "explicit" if field in NUMERIC_FIELDS else "inferred",
                **metadata,
            })

        absent_fields = (
            raw.get("absent_fields") if isinstance(raw.get("absent_fields"), Mapping) else {}
        )
        for field, absence in absent_fields.items():
            if field != "minor_lith" or field not in requested or not isinstance(absence, Mapping):
                dropped.append({"unit_id": unit_id, "field": field, "reason": "absence_not_allowed"})
                continue
            quote = str(absence.get("quote") or "").strip()
            if not _quote_in_context(quote, str(context.get("text") or "")):
                dropped.append({"unit_id": unit_id, "field": field, "reason": "absence_quote_not_in_context"})
                continue
            if not re.search(r"のみ|だけ|単一|solely|exclusively|\bonly\b", quote, re.IGNORECASE):
                dropped.append({"unit_id": unit_id, "field": field, "reason": "absence_not_explicit"})
                continue
            accepted.append({
                "unit_id": unit_id,
                "field": field,
                "candidate": None,
                "quote": quote,
                "context": context,
                "assertion": "explicit",
                "resolution_state": "explicitly_absent",
                "role": "minor",
                "role_cue": "exclusive_lithology_statement",
                "parser": "pdf_body_lithology/v2",
                "source_span": quote,
            })
    return accepted, dropped


def validate_response(
    contexts: Sequence[Mapping[str, Any]], response: Mapping[str, Any]
) -> ValidationReport:
    """Run the production body-field verifier and summarize target coverage."""
    accepted_rows, dropped_rows = verify_response(contexts, response)
    target_pairs = {
        (str(row.get("unit_id") or ""), str(field))
        for row in contexts
        for field in row.get("requested_fields") or []
        if field in ALLOWED_FIELDS
    }
    accepted_pairs = {
        (str(row.get("unit_id") or ""), str(row.get("field") or ""))
        for row in accepted_rows
    }
    if not accepted_rows:
        decision = "reject"
    elif target_pairs.issubset(accepted_pairs):
        decision = "accept"
    else:
        decision = "partial"
    return ValidationReport(
        decision=decision,
        accepted=accepted_rows,
        dropped=dropped_rows,
        metrics={
            "accepted_fields": len(accepted_pairs),
            "target_fields": len(target_pairs),
            "field_coverage": (
                len(accepted_pairs) / len(target_pairs) if target_pairs else 1.0
            ),
            "dropped_count": len(dropped_rows),
        },
    )


def _cache_candidates(accepted: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Remove duplicated source context from cache while retaining provenance."""
    output: list[dict[str, Any]] = []
    for candidate in accepted:
        row = dict(candidate)
        context = row.pop("context", None)
        if isinstance(context, Mapping):
            row["context_id"] = context.get("context_id")
        output.append(row)
    return output


def _hydrate_candidates(
    accepted: Sequence[Mapping[str, Any]], contexts: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {str(row.get("context_id") or ""): row for row in contexts}
    by_unit = {str(row.get("unit_id") or ""): row for row in contexts}
    output: list[dict[str, Any]] = []
    for candidate in accepted:
        row = dict(candidate)
        old_context = row.pop("context", None)
        context_id = str(row.pop("context_id", "") or "")
        if not context_id and isinstance(old_context, Mapping):
            context_id = str(old_context.get("context_id") or "")
        context = by_id.get(context_id) or by_unit.get(str(row.get("unit_id") or ""))
        if context is None:
            raise PDFFieldError(
                f"Cached body candidate has no current context for {row.get('unit_id')}"
            )
        row["context"] = context
        output.append(row)
    return output


def _response_from_accepted(accepted: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Reconstruct a verifier input for strict legacy-cache migration."""
    grouped: dict[str, dict[str, Any]] = {}
    for candidate in accepted:
        unit_id = str(candidate.get("unit_id") or "")
        field = str(candidate.get("field") or "")
        if not unit_id or not field:
            continue
        unit = grouped.setdefault(
            unit_id,
            {
                "unit_id": unit_id,
                "fields": {},
                "quotes": {},
                "field_meta": {},
                "absent_fields": {},
            },
        )
        quote = str(candidate.get("quote") or "")
        if candidate.get("resolution_state") == "explicitly_absent":
            unit["absent_fields"][field] = {
                "quote": quote,
                "reason": "revalidated legacy explicit absence",
            }
            continue
        unit["fields"][field] = candidate.get("candidate")
        unit["quotes"][field] = quote
        if field in {"lithology", "minor_lith"}:
            unit["field_meta"][field] = {
                "role": candidate.get("role"),
                "role_cue": candidate.get("role_cue"),
            }
    return {"units": list(grouped.values())}


def _candidate_signature(candidate: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            "unit_id": candidate.get("unit_id"),
            "field": candidate.get("field"),
            "candidate": candidate.get("candidate"),
            "quote": candidate.get("quote"),
            "resolution_state": candidate.get("resolution_state"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _load_compatible_cache(
    cache_root: Path,
    cache_path: Path,
    *,
    contexts: Sequence[Mapping[str, Any]],
    prompt_sha: str,
    source_sha256: str,
    job_id: str,
) -> dict[str, Any] | None:
    """Revalidate and merge source-bound candidates from prior routed batches.

    Context routing and stable IDs can change while the underlying PDF and
    verbatim quote remain identical.  A prior candidate is reusable only when
    its field is currently requested and its quote occurs in exactly one
    current unit context (or still verifies against the same current ID).
    """
    collected: list[dict[str, Any]] = []
    migrated_from: list[str] = []
    for candidate_path in cache_root.glob("pfe_*.json") if cache_root.is_dir() else ():
        if candidate_path == cache_path:
            continue
        try:
            document = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            document.get("status") != "complete"
            or document.get("stage") != STAGE
            or document.get("source_sha256") != source_sha256
            or document.get("prompt_version") != PROMPT_VERSION
        ):
            continue
        prior = list(document.get("accepted") or [])
        if document.get("prompt_sha256") == prompt_sha:
            try:
                exact_verified, exact_dropped = verify_response(
                    contexts, _response_from_accepted(prior)
                )
            except (PDFFieldError, TypeError, ValueError):
                exact_verified = []
                exact_dropped = []
            if sorted(map(_candidate_signature, exact_verified)) == sorted(
                map(_candidate_signature, prior)
            ):
                migrated = {
                    **document,
                    "job_id": job_id,
                    "validator_version": SCHEMA_VERSION,
                    "provider": document.get("provider") or "gemini",
                    "requested_model": document.get("requested_model") or document.get("model"),
                    "actual_model": document.get("actual_model") or document.get("model"),
                    "accepted": _cache_candidates(exact_verified),
                    "dropped": list(document.get("dropped") or []) + exact_dropped,
                    "migrated_from_job_id": document.get("job_id"),
                }
                _atomic_json(cache_path, migrated)
                return migrated
        retained = 0
        for old in prior:
            if not isinstance(old, Mapping):
                continue
            field = str(old.get("field") or "")
            quote = str(old.get("quote") or "").strip()
            if not field or not quote:
                continue
            possible = [
                context for context in contexts
                if field in (context.get("requested_fields") or [])
                and _quote_in_context(quote, str(context.get("text") or ""))
            ]
            same_id = [
                context for context in possible
                if str(context.get("unit_id") or "") == str(old.get("unit_id") or "")
            ]
            selected = same_id[0] if len(same_id) == 1 else (possible[0] if len(possible) == 1 else None)
            if selected is None:
                continue
            row = dict(old)
            row["unit_id"] = selected.get("unit_id")
            row["context_id"] = selected.get("context_id")
            collected.append(row)
            retained += 1
        if retained:
            migrated_from.append(str(document.get("job_id") or candidate_path.stem))

    if not collected:
        return None
    unique = {
        _candidate_signature(row): row
        for row in collected
    }
    try:
        verified, migration_dropped = verify_response(
            contexts, _response_from_accepted(list(unique.values()))
        )
    except (PDFFieldError, TypeError, ValueError):
        return None
    if not verified:
        return None
    migrated = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "status": "complete",
        "job_id": job_id,
        "prompt_version": PROMPT_VERSION,
        "validator_version": SCHEMA_VERSION,
        "prompt_sha256": prompt_sha,
        "source_sha256": source_sha256,
        "provider": "revalidated_prior_cache_bundle",
        "requested_model": "source-bound-cache",
        "actual_model": "source-bound-cache",
        "model": "source-bound-cache",
        "accepted": _cache_candidates(verified),
        "dropped": migration_dropped,
        "migrated_from_job_id": migrated_from[0] if len(migrated_from) == 1 else None,
        "migrated_from_job_ids": migrated_from,
        "compatible_cache_migration": True,
        "completed_at": _utc_now(),
        "estimated_tokens": 0,
    }
    _atomic_json(cache_path, migrated)
    return migrated


def _evidence_id(job_id: str, candidate: Mapping[str, Any]) -> str:
    identity = {
        "job_id": job_id,
        "unit_id": candidate.get("unit_id"),
        "field": candidate.get("field"),
        "candidate": candidate.get("candidate"),
        "quote": candidate.get("quote"),
    }
    return "ev_" + _sha(json.dumps(identity, ensure_ascii=False, sort_keys=True))[:16]


def evidence_rows(
    accepted: Sequence[Mapping[str, Any]], *, job_id: str, source_file: str, model: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in accepted:
        context = candidate.get("context") or {}
        rows.append({
            "evidence_id": _evidence_id(job_id, candidate),
            "unit_id": candidate.get("unit_id"),
            "scope_type": "unit_global",
            "field": candidate.get("field"),
            "candidate": candidate.get("candidate"),
            "source_type": "PDF",
            "source_file": source_file,
            "source_locator": " / ".join(value for value in (
                str(context.get("section") or "").strip(),
                f"PDF p.{context.get('pdf_page')}" if context.get("pdf_page") else "",
                f"printed p.{context.get('printed_page')}" if context.get("printed_page") else "",
            ) if value),
            "PDF_page": context.get("pdf_page"),
            "printed_page": context.get("printed_page"),
            "section_or_table": context.get("section"),
            "matched_sentence": candidate.get("quote"),
            "full_context_quote": candidate.get("quote"),
            "confidence_class": "B" if candidate.get("assertion") == "explicit" else "C",
            "assertion": candidate.get("assertion"),
            "selection": "candidate",
            "extraction_method": f"{STAGE}; {model}; {PROMPT_VERSION}; targeted Japanese body",
            "resolution_state": candidate.get("resolution_state"),
            "raw_phrase": candidate.get("raw_phrase"),
            "normalized_terms": candidate.get("normalized_terms"),
            "role": candidate.get("role"),
            "role_cue": candidate.get("role_cue"),
            "dropped_modifiers": candidate.get("dropped_modifiers"),
            "parser": candidate.get("parser"),
            "source_span": candidate.get("source_span"),
        })
    return rows


def _preflight(
    prompt: str,
    *,
    usage: Mapping[str, int] | None = None,
) -> dict[str, int]:
    estimated_tokens = math.ceil(len(prompt.encode("utf-8")) / 3) + 4096
    limits = load_limits()
    if usage is None:
        _path, _all, _date, usage = today_usage()
    per_call = int(limits.get("max_tokens_per_call") or 0)
    max_calls = int(limits.get("max_calls_per_day") or 0)
    max_tokens = int(limits.get("max_tokens_per_day") or 0)
    if per_call and estimated_tokens > per_call:
        raise PDFFieldError(
            f"Body extraction estimate {estimated_tokens:,} exceeds per-call limit {per_call:,}."
        )
    if max_calls and int(usage.get("calls") or 0) + 1 > max_calls:
        raise PDFFieldError("Daily LLM call limit would be exceeded by body extraction.")
    if max_tokens and int(usage.get("tokens") or 0) + estimated_tokens > max_tokens:
        raise PDFFieldError("Daily LLM token limit would be exceeded by body extraction.")
    return {"pending_calls": 1, "estimated_tokens": estimated_tokens}


def run_body_enrichment(
    system_dir: str | Path,
    routed: Mapping[str, Any],
    *,
    source_file: str | Path,
    source_sha256: str,
    cache_dir: str | Path,
    model: str = MODEL,
    api_key: str | None = None,
    executor: Executor | None = None,
    router: LLMRouter | None = None,
    generated_at: str | None = None,
    allow_external_calls: bool = True,
) -> dict[str, Any]:
    """Run or resume one body call, merge verified evidence, and rebuild canonical JSON."""
    root = Path(system_dir).expanduser().resolve()
    contexts = [row for row in routed.get("contexts") or [] if row.get("requested_fields")]
    completed_at = generated_at or _utc_now()
    if not contexts:
        return {
            "schema_version": SCHEMA_VERSION, "stage": STAGE, "status": "no_contexts",
            "external_calls": 0, "cache_hits": 0, "added_evidence": 0,
            "pending_calls": 0, "estimated_tokens": 0,
        }
    prompt = build_prompt(contexts)
    prompt_sha = _sha(prompt)
    identity = {
        "stage": STAGE, "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION, "validator_version": SCHEMA_VERSION,
        "map_id": routed.get("map_id"),
        "source_sha256": source_sha256, "prompt_sha256": prompt_sha,
        "contexts": [{"context_id": row.get("context_id"), "text_sha256": row.get("text_sha256"),
                      "requested_fields": row.get("requested_fields")} for row in contexts],
    }
    job_id = "pfe_" + _sha(json.dumps(identity, ensure_ascii=False, sort_keys=True))[:20]
    cache_root = Path(cache_dir).expanduser().resolve()
    cache_path = cache_root / f"{job_id}.json"
    cached = None
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            loaded.get("status") == "complete"
            and loaded.get("prompt_sha256") == prompt_sha
            and loaded.get("source_sha256") == source_sha256
        ):
            hydrated = _hydrate_candidates(
                list(loaded.get("accepted") or []), contexts
            )
            accepted, newly_dropped = verify_response(
                contexts, _response_from_accepted(hydrated)
            )
            cached = {
                **loaded,
                "accepted": accepted,
                "dropped": [
                    *list(loaded.get("dropped") or []),
                    *newly_dropped,
                ],
                "cache_revalidated": True,
            }
            if (
                len(accepted) != len(loaded.get("accepted") or [])
                or newly_dropped
            ):
                cache_path.write_text(
                    json.dumps(cached, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
    except (OSError, json.JSONDecodeError):
        pass
    if cached is None:
        cached = _load_compatible_cache(
            cache_root,
            cache_path,
            contexts=contexts,
            prompt_sha=prompt_sha,
            source_sha256=source_sha256,
            job_id=job_id,
        )

    seeded_accepted: list[dict[str, Any]] = []
    if (
        cached is not None
        and allow_external_calls
        and cached.get("provider") == "revalidated_prior_cache_bundle"
    ):
        # A migrated bundle is a safe starting point, not proof that the
        # current target set has been attempted.  On an explicitly LLM-enabled
        # run, supplement it once and persist the merged verified result.
        seeded_accepted = _hydrate_candidates(
            list(cached.get("accepted") or []), contexts
        )
        cached = None

    if cached is None:
        if not allow_external_calls:
            raise PDFFieldError(
                "No source-compatible body cache passed current quote validation; "
                "external calls are disabled."
            )
        estimated_tokens = math.ceil(len(prompt.encode("utf-8")) / 3) + 4096
        if executor:
            # Injected executors must be reproducible regardless of unrelated
            # real provider calls made earlier on the same day.
            budget = _preflight(prompt, usage={"calls": 0, "tokens": 0})
            response = executor(prompt)
            accepted, dropped = verify_response(contexts, response)
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
            routed_result = active_router.execute(
                LLMRequest(
                    stage=STAGE,
                    logical_job_id=job_id,
                    prompt=prompt,
                    estimated_input_tokens=max(1, estimated_tokens - 4096),
                    reserved_output_tokens=4096,
                    required_capabilities=("text", "json", "japanese", "long_context"),
                ),
                lambda response: validate_response(contexts, response),
            )
            response = routed_result.response
            accepted = list(routed_result.validation.accepted or [])
            dropped = list(routed_result.validation.dropped or [])
            provider = routed_result.provider
            requested_model = routed_result.requested_model
            actual_model = routed_result.actual_model
            attempt_id = routed_result.attempt_id
            route_attempts = list(routed_result.attempts)
            external_attempts = sum(1 for row in route_attempts if row.get("attempt_id"))
            budget = {
                "pending_calls": external_attempts,
                "estimated_tokens": estimated_tokens,
            }
        if not isinstance(response, Mapping):
            raise PDFFieldError("Body extraction response is not a JSON object.")
        if seeded_accepted:
            accepted = list({
                _candidate_signature(row): row
                for row in [*seeded_accepted, *accepted]
            }.values())
        cache_document = {
            "schema_version": SCHEMA_VERSION, "stage": STAGE, "status": "complete",
            "job_id": job_id, "prompt_version": PROMPT_VERSION,
            "validator_version": SCHEMA_VERSION,
            "prompt_sha256": prompt_sha, "source_sha256": source_sha256,
            "provider": provider, "requested_model": requested_model,
            "actual_model": actual_model, "model": actual_model,
            "attempt_id": attempt_id, "route_attempts": route_attempts,
            "completed_at": completed_at,
            "accepted": _cache_candidates(accepted), "dropped": dropped,
            "estimated_tokens": budget["estimated_tokens"],
        }
        _atomic_json(cache_path, cache_document)
        cached = cache_document
        external_calls = (
            sum(1 for row in route_attempts if row.get("attempt_id"))
            if route_attempts else 1
        )
        cache_hits = 0
    else:
        accepted = _hydrate_candidates(list(cached.get("accepted") or []), contexts)
        dropped = list(cached.get("dropped") or [])
        external_calls, cache_hits = 0, 1
        budget = {"pending_calls": 0, "estimated_tokens": 0}

    compiled = json.loads((root / "compiled.json").read_text(encoding="utf-8"))
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    additions = evidence_rows(
        accepted,
        job_id=job_id,
        source_file=str(Path(source_file).resolve()),
        model=(
            f"{cached.get('provider')}:{cached.get('actual_model') or cached.get('model')}"
            if cached.get("provider") else str(cached.get("actual_model") or cached.get("model") or model)
        ),
    )
    existing_rows = [_canonical_evidence_row(row) for row in evidence.get("evidence") or []]
    unit_rows = []
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
    return {
        "schema_version": SCHEMA_VERSION, "stage": STAGE, "status": "complete",
        "job_id": job_id,
        "provider": cached.get("provider"),
        "requested_model": cached.get("requested_model") or cached.get("model") or model,
        "actual_model": cached.get("actual_model") or cached.get("model") or model,
        "model": cached.get("actual_model") or cached.get("model") or model,
        "external_calls": external_calls, "cache_hits": cache_hits,
        "added_evidence": len(additions), "dropped_count": len(dropped),
        "pending_calls": budget["pending_calls"], "estimated_tokens": budget["estimated_tokens"],
        "cache_file": str(cache_path),
        "route_attempts": list(cached.get("route_attempts") or []),
    }


__all__ = [
    "PDFFieldError", "PROMPT_VERSION", "SCHEMA_VERSION", "STAGE",
    "build_prompt", "evidence_rows", "run_body_enrichment", "validate_response",
    "verify_response",
]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run targeted PDF-body enrichment without creating or overwriting Excel."
    )
    parser.add_argument("--system-dir", required=True)
    parser.add_argument("--routed", required=True)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args(argv)
    pdf = Path(args.pdf).expanduser().resolve()
    routed = json.loads(Path(args.routed).read_text(encoding="utf-8"))
    result = run_body_enrichment(
        args.system_dir,
        routed,
        source_file=pdf,
        source_sha256=_file_sha256(pdf),
        cache_dir=args.cache_dir,
        model=args.model,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
