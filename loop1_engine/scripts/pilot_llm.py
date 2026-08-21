# -*- coding: utf-8 -*-
"""Budgeted, resumable per-map PDF/LLM enrichment for canonical JSON.

This pilot deliberately does not read or write Excel.  It consumes a canonical
bundle (``compiled.json`` plus ``evidence.json``), extracts only fields that are
still blank, and writes a separate enriched canonical bundle.  A successful
LLM response is cached before bundle generation so an interrupted run can
resume without spending another API call.

The public helpers retain a strict m1050 default for backwards compatibility,
while the nationwide pipeline passes the resolved map id explicitly.  Cache
keys include that id, the exact Abstract, prompt, validator and target fields,
but not provider/model, so results can never leak from one quadrangle into
another and can be reused after a validated provider failover.

Examples::

    python scripts/pilot_llm.py plan --bundle-dir RAW --pdf REPORT.pdf \
        --abstract m1050_abstract.txt

    python scripts/pilot_llm.py run --bundle-dir RAW --pdf REPORT.pdf \
        --abstract m1050_abstract.txt --output-dir ENRICHED

``plan`` never loads an API key or calls an external service.  ``run`` delegates
credential loading, local reservations and sequential failover to the common
LLM router only after the queue is known.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import strip_trailing_paren  # noqa: E402
from compiled_layer import (  # noqa: E402
    build_canonical_layer,
    is_blank,
    write_canonical_layer,
)
from llm_extract import (  # noqa: E402
    FREE_TIER_MODELS,
    MODEL,
    PROMPT,
    load_limits,
    today_usage,
    verify,
    vocab_hint,
)
from llm_router import (  # noqa: E402
    LLMRequest, LLMRouter, ValidationReport, single_provider_router,
)
from pdf_locate import locate  # noqa: E402


# These legacy identifiers remain stable for artifact compatibility.  Current
# validator versioning and strict cache migration prevent stale acceptance.
STAGE_NAME = "towada_pdf_llm"
STAGE_SCHEMA_VERSION = "1.0.0"
PROMPT_VERSION = "towada-canonical-v1"
VALIDATOR_VERSION = "towada-canonical-validator-v2"
TOWADA_MAP_ID = "1050"
MAX_STAGE_CALLS = 1

# Field order is intentional: it is also the order shown in the prompt and in
# deterministic evidence generation.
FIELD_QUOTES = {
    "t_age_ma": "age_quote",
    "b_age_ma": "age_quote",
    "strat_name": "strat_quote",
    "environment": "env_quote",
    "unit_description": "desc_quote",
    "lithology": "lith_quote",
    "minor_lith": "lith_quote",
    "min_thickness": "thickness_quote",
    "max_thickness": "thickness_quote",
    "basal_surface": "basal_quote",
    "lateral_relationship": "lateral_quote",
}
TARGET_FIELDS = tuple(FIELD_QUOTES)
NUMERIC_FIELDS = {"t_age_ma", "b_age_ma", "min_thickness", "max_thickness"}
FIELD_GROUPS = {
    "t_age_ma": "age_evidence",
    "b_age_ma": "age_evidence",
    "strat_name": "context_evidence",
    "environment": "context_evidence",
    "unit_description": "context_evidence",
    "lithology": "context_evidence",
    "minor_lith": "context_evidence",
    "min_thickness": "physical_evidence",
    "max_thickness": "physical_evidence",
    "basal_surface": "physical_evidence",
    "lateral_relationship": "physical_evidence",
}


class PilotLLMError(RuntimeError):
    """User-facing pilot-stage failure."""


class PilotBudgetExceeded(PilotLLMError):
    """The local preflight stopped the run before an external request."""


@dataclass(frozen=True)
class CanonicalBundle:
    root: Path
    compiled_path: Path
    evidence_path: Path
    compiled: dict[str, Any]
    evidence: dict[str, Any]
    digest: str


@dataclass(frozen=True)
class SourceDocument:
    text: str
    source_file: Path
    pdf_index: Mapping[str, Any] | None = None
    section: str = "English Abstract"

    @property
    def sha256(self) -> str:
        return _sha256_text(self.text)


@dataclass(frozen=True)
class TargetUnit:
    unit_id: str
    row_key: str | None
    column_ids: tuple[str, ...]
    unit_name: str
    fields: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "row_key": self.row_key,
            "column_ids": list(self.column_ids),
            "unit_name": self.unit_name,
            "fields": list(self.fields),
        }


@dataclass(frozen=True)
class PilotJob:
    job_id: str
    map_id: str
    model: str
    source_sha256: str
    prompt_sha256: str
    prompt: str
    targets: tuple[TargetUnit, ...]
    estimated_input_tokens: int
    reserved_output_tokens: int
    estimated_total_tokens: int


@dataclass(frozen=True)
class StageResult:
    compiled: dict[str, Any]
    evidence: dict[str, Any]
    paths: dict[str, str]
    manifest_path: str
    jobs: int
    external_calls: int
    cache_hits: int
    added_evidence: int


Executor = Callable[[PilotJob, SourceDocument], Mapping[str, Any]]


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
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_bundle(
    bundle_dir: str | os.PathLike[str],
    *,
    expected_map_id: str | None = TOWADA_MAP_ID,
) -> CanonicalBundle:
    """Load and minimally validate a canonical JSON pair."""
    root = Path(bundle_dir).expanduser().resolve()
    compiled_path = root / "compiled.json"
    evidence_path = root / "evidence.json"
    if not compiled_path.is_file() or not evidence_path.is_file():
        raise PilotLLMError(
            f"Canonical bundle requires compiled.json and evidence.json: {root}"
        )
    try:
        compiled = json.loads(compiled_path.read_text(encoding="utf-8"))
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PilotLLMError(f"Canonical bundle is not readable JSON: {exc}") from exc
    if not isinstance(compiled, dict) or not isinstance(compiled.get("units"), list):
        raise PilotLLMError("compiled.json has no canonical units array.")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("evidence"), list):
        raise PilotLLMError("evidence.json has no canonical evidence array.")
    map_id = str((compiled.get("map") or {}).get("map_id") or "").strip()
    evidence_map_id = str(evidence.get("map_id") or map_id).strip()
    if map_id != evidence_map_id:
        raise PilotLLMError(
            f"Canonical pair map mismatch: compiled={map_id!r}, evidence={evidence_map_id!r}"
        )
    expected = (
        str(expected_map_id).strip().lstrip("mM")
        if expected_map_id is not None
        else None
    )
    if expected and map_id != expected:
        raise PilotLLMError(
            f"Canonical bundle is for map {map_id or '(blank)'}, expected m{expected}."
        )
    digest = _sha256_text(_sha256_file(compiled_path) + _sha256_file(evidence_path))
    return CanonicalBundle(
        root=root,
        compiled_path=compiled_path,
        evidence_path=evidence_path,
        compiled=compiled,
        evidence=evidence,
        digest=digest,
    )


def load_source(
    pdf_path: str | os.PathLike[str],
    abstract_path: str | os.PathLike[str] | None = None,
    *,
    pdf_index: Mapping[str, Any] | None = None,
) -> SourceDocument:
    """Load the English Abstract, extracting it locally when no text is supplied."""
    pdf = Path(pdf_path).expanduser().resolve()
    if not pdf.is_file() or pdf.suffix.casefold() != ".pdf":
        raise PilotLLMError(f"PDF source not found: {pdf}")
    if pdf_index:
        indexed_pdf = str(pdf_index.get("pdf") or "").strip()
        if indexed_pdf and Path(indexed_pdf).name.casefold() != pdf.name.casefold():
            raise PilotLLMError(
                f"PDF index belongs to {indexed_pdf!r}, not {pdf.name!r}."
            )
    if abstract_path is not None:
        abstract = Path(abstract_path).expanduser().resolve()
        if not abstract.is_file():
            raise PilotLLMError(f"Abstract text not found: {abstract}")
        text = abstract.read_text(encoding="utf-8")
    else:
        from extract_abstract import extract

        text, _pages = extract(str(pdf))
    if not text.strip():
        raise PilotLLMError("The PDF English Abstract is empty or could not be detected.")
    return SourceDocument(text=text, source_file=pdf, pdf_index=pdf_index)


def _normalise_name(value: Any) -> str:
    """Strict canonical/LLM matching without the legacy lossy type-word removal.

    Keeping words such as ``Terrace`` and ``Deposits`` is essential: removing
    them collapses both "Towada Deposits" and "Towada Terrace Deposits" to the
    same key and can attach evidence to the wrong canonical unit.
    """
    text = strip_trailing_paren(str(value or ""))
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode().casefold()
    text = re.sub(r"^\s*the\s+", "", text)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _value(unit: Mapping[str, Any], field: str) -> Any:
    values = unit.get("values") if isinstance(unit.get("values"), Mapping) else {}
    review = (
        unit.get("review_values")
        if isinstance(unit.get("review_values"), Mapping)
        else {}
    )
    candidate = values.get(field)
    return review.get(field) if is_blank(candidate) else candidate


def select_targets(
    compiled: Mapping[str, Any],
    *,
    target_fields: Sequence[str] = TARGET_FIELDS,
    expected_map_id: str | None = TOWADA_MAP_ID,
) -> tuple[TargetUnit, ...]:
    """Select only canonical units/fields that are still unresolved."""
    map_id = str((compiled.get("map") or {}).get("map_id") or "").strip()
    expected = (
        str(expected_map_id).strip().lstrip("mM")
        if expected_map_id is not None
        else None
    )
    if expected and map_id != expected:
        raise PilotLLMError(
            f"Canonical bundle is for map {map_id or '(blank)'}, expected m{expected}."
        )
    if not map_id:
        raise PilotLLMError("Canonical bundle has no map_id.")
    unknown = [field for field in target_fields if field not in FIELD_QUOTES]
    if unknown:
        raise PilotLLMError(f"Unsupported pilot fields: {', '.join(unknown)}")

    targets: list[TargetUnit] = []
    for unit in compiled.get("units") or []:
        if not isinstance(unit, Mapping):
            continue
        unit_id = str(unit.get("unit_id") or "").strip()
        unit_name = str(_value(unit, "unit_name") or "").strip()
        if not unit_id or not unit_name or unit_name.upper() == "NO_DATA":
            continue
        missing = tuple(field for field in target_fields if is_blank(_value(unit, field)))
        if not missing:
            continue
        targets.append(TargetUnit(
            unit_id=unit_id,
            row_key=(str(unit.get("row_key")).strip() if unit.get("row_key") else None),
            column_ids=tuple(str(v).strip() for v in unit.get("column_ids") or () if str(v).strip()),
            unit_name=unit_name,
            fields=missing,
        ))
    return tuple(targets)


def build_prompt(source_text: str, targets: Sequence[TargetUnit]) -> str:
    """Build the exact prompt whose digest and token estimate enter the cache key."""
    target_payload = [
        {"unit_name": target.unit_name, "requested_fields": list(target.fields)}
        for target in targets
    ]
    stage_rules = """

Pilot scope — follow these additional rules exactly:
- Process ONLY the canonical target units listed below. Do not add other units.
- Return unit_name exactly as shown in the target list. The target list is not
  geological evidence; every extracted value still needs its verbatim quote
  from the Abstract.
- Return ONLY each unit's requested_fields (plus unit_name and the matching
  quote keys). Do not spend output tokens repeating already-filled fields.
- lateral_relationship uses lateral_quote. Extract it only when the Abstract
  explicitly states interfingering, transgressive/onlap, erosional, or
  gradational lateral relations. Do not infer it from ordinary superposition.

Canonical targets:
{targets}
""".format(targets=json.dumps(target_payload, ensure_ascii=False, indent=2))
    return (
        PROMPT.replace("{vocab}", vocab_hint() + stage_rules)
        .replace("{abstract}", source_text)
    )


def estimate_tokens(text: str) -> int:
    """Conservative deterministic estimate; UTF-8 bytes/3 rounds upward."""
    return max(1, math.ceil(len(str(text).encode("utf-8")) / 3))


def _reserve_output_tokens(targets: Sequence[TargetUnit]) -> int:
    requested = sum(len(target.fields) for target in targets)
    return max(1024, requested * 96 + len(targets) * 48)


def build_queue(
    compiled: Mapping[str, Any],
    source: SourceDocument,
    *,
    model: str = MODEL,
    target_fields: Sequence[str] = TARGET_FIELDS,
    map_id: str = TOWADA_MAP_ID,
) -> tuple[PilotJob, ...]:
    """Build at most one Abstract job for one explicitly selected map."""
    selected_map_id = str(map_id).strip().lstrip("mM")
    targets = select_targets(
        compiled,
        target_fields=target_fields,
        expected_map_id=selected_map_id,
    )
    if not targets:
        return ()
    prompt = build_prompt(source.text, targets)
    prompt_sha = _sha256_text(prompt)
    identity = {
        "stage": STAGE_NAME,
        "schema_version": STAGE_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "map_id": selected_map_id,
        "source_sha256": source.sha256,
        "prompt_sha256": prompt_sha,
        "targets": [target.as_dict() for target in targets],
    }
    job_id = "pll_" + _sha256_text(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )[:20]
    input_tokens = estimate_tokens(prompt)
    output_tokens = _reserve_output_tokens(targets)
    return (PilotJob(
        job_id=job_id,
        map_id=selected_map_id,
        model=model,
        source_sha256=source.sha256,
        prompt_sha256=prompt_sha,
        prompt=prompt,
        targets=targets,
        estimated_input_tokens=input_tokens,
        reserved_output_tokens=output_tokens,
        estimated_total_tokens=input_tokens + output_tokens,
    ),)


def _cache_path(cache_dir: Path, job: PilotJob) -> Path:
    return cache_dir / f"{job.job_id}.json"


def load_cached_job(cache_dir: str | os.PathLike[str], job: PilotJob) -> dict[str, Any] | None:
    """Return only a complete cache entry for the exact validated logical job."""
    path = _cache_path(Path(cache_dir), job)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    expected = {
        "schema_version": STAGE_SCHEMA_VERSION,
        "stage": STAGE_NAME,
        "job_id": job.job_id,
        "prompt_version": PROMPT_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "prompt_sha256": job.prompt_sha256,
        "source_sha256": job.source_sha256,
        "status": "complete",
    }
    if any(document.get(key) != value for key, value in expected.items()):
        return None
    if not isinstance(document.get("candidates"), list) or not isinstance(document.get("dropped"), list):
        return None
    return document


def _revalidate_cached_candidates(
    job: PilotJob,
    candidates: Sequence[Any],
    source: SourceDocument,
) -> list[dict[str, Any]] | None:
    """Reconstruct raw field assertions and run every current verifier again."""

    targets_by_name: dict[str, list[TargetUnit]] = {}
    for target in job.targets:
        targets_by_name.setdefault(_normalise_name(target.unit_name), []).append(target)
    rebuilt: list[dict[str, Any]] = []
    expected_facts = 0
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            return None
        name = str(candidate.get("unit_name") or "").strip()
        matches = targets_by_name.get(_normalise_name(name)) or []
        if not matches:
            return None
        expected_ids = {target.unit_id for target in matches}
        cached_targets = candidate.get("_matched_targets") or []
        cached_ids = {
            str(target.get("unit_id") or "")
            for target in cached_targets
            if isinstance(target, Mapping)
        }
        if not cached_ids or cached_ids != expected_ids:
            return None
        requested = {field for target in matches for field in target.fields}
        quotes = candidate.get("_quotes")
        if not isinstance(quotes, Mapping):
            return None
        clean: dict[str, Any] = {"unit_name": name, "_quotes": {}}
        for field in FIELD_QUOTES:
            value = candidate.get(field)
            if field not in requested or is_blank(value):
                continue
            expected_facts += 1
            quote = str(quotes.get(field) or "").strip()
            raw = {
                "unit_name": name,
                field: value,
                FIELD_QUOTES[field]: quote,
            }
            verified, rejected = verify([raw], source.text)
            _augment_lateral_relationship(verified, [raw], source.text, rejected)
            verified_field = next(
                (
                    item for item in verified
                    if not is_blank(item.get(field))
                    and not is_blank((item.get("_quotes") or {}).get(field))
                ),
                None,
            )
            if verified_field is None:
                return None
            clean[field] = verified_field[field]
            clean["_quotes"][field] = verified_field["_quotes"][field]
        if len(clean) > 2:
            rebuilt.append(clean)
    if expected_facts == 0:
        return None
    filtered, dropped = _filter_candidates(
        job, {"candidates": rebuilt, "dropped": []},
    )
    accepted_facts = sum(
        1
        for candidate in filtered
        for field in FIELD_QUOTES
        if not is_blank(candidate.get(field))
    )
    if dropped or accepted_facts != expected_facts:
        return None
    return filtered


def migrate_compatible_cached_job(
    cache_dir: str | os.PathLike[str],
    job: PilotJob,
    source: SourceDocument,
) -> Path | None:
    """Re-key a prior cache only after strict source/name revalidation.

    This is primarily a one-time safeguard for m1050 when a freshly extracted
    Abstract differs only in whitespace/layout from the text used for the paid
    pilot call.  No external request is made.  Every retained unit must map to
    the exact current canonical target IDs, and each retained field is rebuilt
    as a raw assertion and rerun through the current quote/numeric validator.
    A single mismatch rejects the whole migration rather than creating a
    partial, ambiguous cache entry.
    """
    root = Path(cache_dir).expanduser().resolve()
    target_path = _cache_path(root, job)
    if target_path.is_file():
        return target_path if load_cached_job(root, job) is not None else None

    for path in sorted(root.glob("pll_*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            document.get("schema_version") != STAGE_SCHEMA_VERSION
            or document.get("stage") != STAGE_NAME
            or document.get("prompt_version") != PROMPT_VERSION
            or document.get("status") != "complete"
            or not isinstance(document.get("candidates"), list)
        ):
            continue
        candidates = document["candidates"]
        if not candidates:
            continue
        filtered = _revalidate_cached_candidates(job, candidates, source)
        if not filtered:
            continue
        migrated = {
            "schema_version": STAGE_SCHEMA_VERSION,
            "stage": STAGE_NAME,
            "job_id": job.job_id,
            "prompt_version": PROMPT_VERSION,
            "validator_version": VALIDATOR_VERSION,
            "prompt_sha256": job.prompt_sha256,
            "source_sha256": job.source_sha256,
            "provider": document.get("provider") or "legacy_gemini",
            "requested_model": document.get("requested_model") or document.get("model"),
            "actual_model": document.get("actual_model") or document.get("model"),
            "model": document.get("actual_model") or document.get("model") or job.model,
            "attempt_id": document.get("attempt_id"),
            "route_attempts": document.get("route_attempts") or [],
            "status": "complete",
            "completed_at": _utc_now(),
            "candidates": filtered,
            "dropped": list(document.get("dropped") or []),
            "migrated_from_job_id": document.get("job_id"),
            "compatible_cache_migration": True,
            "migration_validation": "all cached field assertions reran through current quote, numeric and canonical-target validators",
        }
        _atomic_json(target_path, migrated)
        return target_path
    return None


def preflight_budget(
    jobs: Sequence[PilotJob],
    *,
    limits: Mapping[str, Any] | None = None,
    usage: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """Check the full pending queue against per-call and remaining daily budget."""
    limits_doc = dict(load_limits() if limits is None else limits)
    if usage is None:
        _path, _all_days, _today, usage_doc = today_usage()
    else:
        usage_doc = dict(usage)
    calls = len(jobs)
    tokens = sum(job.estimated_total_tokens for job in jobs)

    # A fully cached resume makes no external request and consumes no tokens;
    # it must remain usable even after the day's live-call budget is exhausted.
    if not jobs:
        return {
            "pending_calls": 0,
            "estimated_tokens": 0,
            "remaining_calls_after": max(
                0,
                int(limits_doc.get("max_calls_per_day") or 0)
                - int(usage_doc.get("calls") or 0),
            ),
            "remaining_tokens_after": max(
                0,
                int(limits_doc.get("max_tokens_per_day") or 0)
                - int(usage_doc.get("tokens") or 0),
            ),
        }

    if calls > MAX_STAGE_CALLS:
        raise PilotBudgetExceeded(
            f"Pilot queue would make {calls} calls; hard limit is {MAX_STAGE_CALLS}."
        )
    for job in jobs:
        if not limits_doc.get("allow_paid_tier", False) and not any(
            marker in job.model.casefold() for marker in FREE_TIER_MODELS
        ):
            raise PilotBudgetExceeded(
                f"Model {job.model!r} is outside the configured free-tier allowlist."
            )
        per_call = int(limits_doc.get("max_tokens_per_call") or 0)
        if per_call and job.estimated_total_tokens > per_call:
            raise PilotBudgetExceeded(
                f"Estimated call size {job.estimated_total_tokens:,} exceeds {per_call:,}."
            )

    used_calls = int(usage_doc.get("calls") or 0)
    used_tokens = int(usage_doc.get("tokens") or 0)
    max_calls = int(limits_doc.get("max_calls_per_day") or 0)
    max_tokens = int(limits_doc.get("max_tokens_per_day") or 0)
    if max_calls and used_calls + calls > max_calls:
        raise PilotBudgetExceeded(
            f"Daily calls would become {used_calls + calls}/{max_calls}."
        )
    if max_tokens and used_tokens + tokens > max_tokens:
        raise PilotBudgetExceeded(
            f"Daily estimated tokens would become {used_tokens + tokens:,}/{max_tokens:,}."
        )
    return {
        "pending_calls": calls,
        "estimated_tokens": tokens,
        "remaining_calls_after": max(0, max_calls - used_calls - calls) if max_calls else 0,
        "remaining_tokens_after": max(0, max_tokens - used_tokens - tokens) if max_tokens else 0,
    }


def _normalise_quote(value: Any) -> str:
    text = str(value or "")
    text = text.replace("–", "-").replace("—", "-").replace("−", "-")
    text = text.replace("’", "'").replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip().casefold()


def _augment_lateral_relationship(
    verified: list[dict[str, Any]],
    raw_units: Sequence[Mapping[str, Any]],
    source_text: str,
    dropped: list[tuple[str, str]],
) -> None:
    """Close the lateral_relationship validation gap in legacy llm_extract.verify."""
    by_name: dict[str, list[Mapping[str, Any]]] = {}
    for raw in raw_units:
        by_name.setdefault(_normalise_name(raw.get("unit_name")), []).append(raw)
    haystack = _normalise_quote(source_text)
    for clean in verified:
        matches = by_name.get(_normalise_name(clean.get("unit_name"))) or []
        raw = matches[0] if matches else {}
        value = raw.get("lateral_relationship")
        if is_blank(value):
            continue
        quote = str(raw.get("lateral_quote") or "").strip()
        if not quote:
            dropped.append((str(clean.get("unit_name") or ""), "lateral_relationship: 引用が無い"))
            continue
        if _normalise_quote(quote) not in haystack:
            dropped.append((
                str(clean.get("unit_name") or ""),
                f"lateral_relationship: 引用が原文に無い ({quote[:40]!r})",
            ))
            continue
        clean["lateral_relationship"] = " ".join(str(value).split())
        clean.setdefault("_quotes", {})["lateral_relationship"] = quote


def _filter_candidates(job: PilotJob, result: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[Any]]:
    """Accept only quote-verified fields for exact canonical targets in this job."""
    raw_candidates = result.get("candidates")
    raw_dropped = result.get("dropped") or []
    if not isinstance(raw_candidates, list) or not isinstance(raw_dropped, list):
        raise PilotLLMError("Executor result must contain candidates[] and dropped[].")
    by_name: dict[str, list[TargetUnit]] = {}
    for target in job.targets:
        by_name.setdefault(_normalise_name(target.unit_name), []).append(target)

    filtered: list[dict[str, Any]] = []
    dropped = list(raw_dropped)
    for item in raw_candidates:
        if not isinstance(item, Mapping):
            dropped.append({"unit_name": None, "reason": "candidate is not an object"})
            continue
        name = str(item.get("unit_name") or "").strip()
        matches = by_name.get(_normalise_name(name)) or []
        if not matches:
            dropped.append({"unit_name": name, "reason": "not an exact canonical target"})
            continue
        requested = {field for target in matches for field in target.fields}
        quotes = item.get("_quotes") if isinstance(item.get("_quotes"), Mapping) else {}
        clean: dict[str, Any] = {
            "unit_name": name,
            "_matched_targets": [target.as_dict() for target in matches],
            "_quotes": {},
        }
        for field in FIELD_QUOTES:
            value = item.get(field)
            quote = quotes.get(field)
            if field not in requested or is_blank(value) or is_blank(quote):
                continue
            clean[field] = value
            clean["_quotes"][field] = str(quote)
        if len(clean) > 3:
            filtered.append(clean)
        else:
            dropped.append({"unit_name": name, "reason": "no requested verified fields"})
    return filtered, dropped


def _validate_router_response(
    job: PilotJob,
    source: SourceDocument,
    response: Mapping[str, Any],
) -> ValidationReport:
    raw_units = response.get("units")
    if not isinstance(raw_units, list) or not all(
        isinstance(item, Mapping) for item in raw_units
    ):
        return ValidationReport(
            decision="reject",
            fatal_errors=("response requires a JSON units[] array",),
        )
    raw_units = [dict(item) for item in raw_units]
    verified, raw_dropped = verify(raw_units, source.text)
    _augment_lateral_relationship(verified, raw_units, source.text, raw_dropped)
    candidates, dropped = _filter_candidates(job, {
        "candidates": verified,
        "dropped": [
            {"unit_name": name, "reason": reason}
            for name, reason in raw_dropped
        ],
    })
    requested = {
        (target.unit_id, field)
        for target in job.targets
        for field in target.fields
    }
    accepted: set[tuple[str, str]] = set()
    for candidate in candidates:
        for target in candidate.get("_matched_targets") or []:
            if not isinstance(target, Mapping):
                continue
            unit_id = str(target.get("unit_id") or "")
            target_fields = set(target.get("fields") or ())
            for field in FIELD_QUOTES:
                if field in target_fields and not is_blank(candidate.get(field)):
                    accepted.add((unit_id, field))
    metrics = {
        "target_units": len(job.targets),
        "requested_fields": len(requested),
        "verified_fields": len(accepted),
        "verified_candidates": len(candidates),
        "coverage": round(len(accepted) / len(requested), 6) if requested else 1.0,
    }
    if not accepted:
        return ValidationReport(
            decision="reject",
            dropped=dropped,
            unresolved=sorted(requested),
            metrics=metrics,
        )
    return ValidationReport(
        decision="accept" if accepted == requested else "partial",
        accepted={"candidates": candidates, "dropped": dropped},
        dropped=dropped,
        unresolved=sorted(requested - accepted),
        metrics=metrics,
    )


def _write_cache(
    cache_dir: Path,
    job: PilotJob,
    candidates: Sequence[Mapping[str, Any]],
    dropped: Sequence[Any],
    *,
    completed_at: str,
    provider: str,
    requested_model: str,
    actual_model: str,
    attempt_id: str | None,
    route_attempts: Sequence[Mapping[str, Any]],
    validation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    document = {
        "schema_version": STAGE_SCHEMA_VERSION,
        "stage": STAGE_NAME,
        "job_id": job.job_id,
        "prompt_version": PROMPT_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "prompt_sha256": job.prompt_sha256,
        "source_sha256": job.source_sha256,
        "provider": provider,
        "requested_model": requested_model,
        "actual_model": actual_model,
        "model": actual_model or requested_model,
        "attempt_id": attempt_id,
        "route_attempts": list(route_attempts),
        "validation": dict(validation or {}),
        "status": "complete",
        "completed_at": completed_at,
        "candidates": list(candidates),
        "dropped": list(dropped),
    }
    _atomic_json(_cache_path(cache_dir, job), document)
    return document


def _assertion_type(field: str, value: Any, quote: str) -> str:
    if field == "unit_description":
        return "inferred"
    if field in NUMERIC_FIELDS:
        return "explicit"
    candidate = _normalise_quote(value)
    return "explicit" if candidate and candidate in _normalise_quote(quote) else "inferred"


def _evidence_id(job: PilotJob, target: Mapping[str, Any], field: str, value: Any, quote: str) -> str:
    identity = {
        "stage": STAGE_NAME,
        "prompt_version": PROMPT_VERSION,
        "job_id": job.job_id,
        "unit_id": target.get("unit_id"),
        "field": field,
        "candidate": value,
        "quote": quote,
    }
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "ev_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def evidence_records(
    job: PilotJob,
    candidates: Sequence[Mapping[str, Any]],
    source: SourceDocument,
) -> list[dict[str, Any]]:
    """Convert verified cached candidates to the canonical evidence schema."""
    records: list[dict[str, Any]] = []
    for candidate in candidates:
        quotes = candidate.get("_quotes") if isinstance(candidate.get("_quotes"), Mapping) else {}
        targets = candidate.get("_matched_targets") or []
        for target in targets:
            requested = set(target.get("fields") or [])
            for field in FIELD_QUOTES:
                value = candidate.get(field)
                quote = str(quotes.get(field) or "").strip()
                if field not in requested or is_blank(value) or not quote:
                    continue
                hit = locate(source.pdf_index, quote) if source.pdf_index else None
                locator_bits = [source.section]
                if hit:
                    locator_bits.append(f"PDF p.{hit['pdf_page']}")
                    if hit.get("printed_page") is not None:
                        locator_bits.append(f"printed p.{hit['printed_page']}")
                records.append({
                    "unit_id": target.get("unit_id"),
                    "row_key": None,
                    "column_ids": [],
                    "scope": {"type": "unit_global", "column_ids": []},
                    "field": field,
                    "group": FIELD_GROUPS[field],
                    "candidate": value,
                    "source": {
                        "type": "PDF",
                        "file": str(source.source_file),
                        "locator": " / ".join(locator_bits),
                        "pdf_page": hit.get("pdf_page") if hit else None,
                        "printed_page": hit.get("printed_page") if hit else None,
                        "section": source.section,
                        "matched_sentence": quote,
                        "quote": quote,
                    },
                    "confidence": {"class": "C", "score": 0.65},
                    "assertion": _assertion_type(field, value, quote),
                    "selection": "candidate",
                    "conflict": False,
                    "conflict_detail": None,
                    "extraction_method": (
                        f"{STAGE_NAME}; {job.model}; {PROMPT_VERSION}; "
                        "verbatim-quote validation"
                    ),
                    "evidence_id": _evidence_id(job, target, field, value, quote),
                })
    return records


def _canonical_evidence_row(record: Mapping[str, Any]) -> dict[str, Any]:
    """Convert durable evidence back to the public build_canonical_layer input shape."""
    source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
    confidence = (
        record.get("confidence") if isinstance(record.get("confidence"), Mapping) else {}
    )
    parse = record.get("parse") if isinstance(record.get("parse"), Mapping) else {}
    return {
        "evidence_id": record.get("evidence_id"),
        "unit_id": record.get("unit_id"),
        "source_unit_id": record.get("source_unit_id"),
        "row_key": record.get("row_key"),
        "column_ids": record.get("column_ids"),
        "scope": record.get("scope"),
        "field": record.get("field"),
        "candidate": record.get("candidate"),
        "source_type": source.get("type"),
        "source_file": source.get("file"),
        "source_locator": source.get("locator"),
        "PDF_page": source.get("pdf_page"),
        "printed_page": source.get("printed_page"),
        "section_or_table": source.get("section"),
        "matched_sentence": source.get("matched_sentence"),
        "full_context_quote": source.get("quote"),
        "confidence_class": confidence.get("class"),
        "assertion": record.get("assertion"),
        "selection": record.get("selection"),
        "conflict": record.get("conflict"),
        "conflict_detail": record.get("conflict_detail"),
        "extraction_method": record.get("extraction_method"),
        "resolution_state": record.get("resolution_state"),
        "raw_phrase": parse.get("raw_phrase"),
        "normalized_terms": parse.get("normalized_terms"),
        "role": parse.get("role"),
        "role_cue": parse.get("role_cue"),
        "dropped_modifiers": parse.get("dropped_modifiers"),
        "parser": parse.get("parser"),
        "source_span": parse.get("source_span"),
    }


def enrich_bundle(
    bundle: CanonicalBundle,
    added_records: Sequence[Mapping[str, Any]],
    output_dir: str | os.PathLike[str],
    *,
    generated_at: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Rebuild a consistent canonical pair in a distinct output directory."""
    destination = Path(output_dir).expanduser().resolve()
    if destination == bundle.root:
        raise PilotLLMError("Pilot output must differ from the raw canonical bundle directory.")

    existing = list(bundle.evidence.get("evidence") or [])
    by_id: dict[str, Mapping[str, Any]] = {}
    for record in [*existing, *added_records]:
        evidence_id = str(record.get("evidence_id") or "").strip()
        if evidence_id:
            by_id[evidence_id] = record

    unit_rows: list[dict[str, Any]] = []
    for unit in bundle.compiled.get("units") or []:
        review = unit.get("review_values")
        if not isinstance(review, Mapping):
            raise PilotLLMError(
                "compiled.json units must retain review_values for lossless enrichment."
            )
        row = dict(review)
        formulas = unit.get("formulas")
        if isinstance(formulas, Mapping) and formulas:
            row["_formulas"] = dict(formulas)
        unit_rows.append(row)

    map_doc = bundle.compiled.get("map") or {}
    compiled, evidence = build_canonical_layer(
        unit_rows,
        column_rows=map_doc.get("columns") or [],
        evidence_rows=[_canonical_evidence_row(record) for record in by_id.values()],
        metadata=map_doc.get("metadata") or {},
        map_id=map_doc.get("map_id"),
        source_review=map_doc.get("source_review"),
        generated_at=generated_at,
    )
    paths = write_canonical_layer(compiled, evidence, destination)
    return compiled, evidence, paths


def run_stage(
    bundle_dir: str | os.PathLike[str],
    source: SourceDocument,
    output_dir: str | os.PathLike[str],
    *,
    cache_dir: str | os.PathLike[str] | None = None,
    model: str = MODEL,
    api_key: str | None = None,
    executor: Executor | None = None,
    router: LLMRouter | None = None,
    target_fields: Sequence[str] = TARGET_FIELDS,
    limits: Mapping[str, Any] | None = None,
    usage: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
    map_id: str = TOWADA_MAP_ID,
) -> StageResult:
    """Execute/resume the pilot and write a separate enriched canonical pair."""
    selected_map_id = str(map_id).strip().lstrip("mM")
    bundle = load_bundle(bundle_dir, expected_map_id=selected_map_id)
    destination = Path(output_dir).expanduser().resolve()
    cache_root = (
        Path(cache_dir).expanduser().resolve()
        if cache_dir is not None
        else destination / ".pilot_llm_cache"
    )
    queue = build_queue(
        bundle.compiled,
        source,
        model=model,
        target_fields=target_fields,
        map_id=selected_map_id,
    )
    cached_by_job: dict[str, dict[str, Any]] = {}
    pending: list[PilotJob] = []
    for job in queue:
        cached = load_cached_job(cache_root, job)
        if cached is None and migrate_compatible_cached_job(cache_root, job, source) is not None:
            cached = load_cached_job(cache_root, job)
        if cached is None:
            pending.append(job)
        else:
            cached_by_job[job.job_id] = cached
    if executor is not None:
        preflight_budget(pending, limits=limits, usage=usage)
    elif len(pending) > MAX_STAGE_CALLS:
        raise PilotBudgetExceeded(
            f"Pilot queue would make {len(pending)} logical calls; hard limit is {MAX_STAGE_CALLS}."
        )
    completed_at = generated_at or _utc_now()
    external_calls = 0
    for job in pending:
        if executor is not None:
            result = executor(job, source)
            candidates, dropped = _filter_candidates(job, result)
            provider = "injected_executor"
            requested_model = job.model
            actual_model = job.model
            attempt_id = None
            route_attempts: list[Mapping[str, Any]] = []
            validation_metrics: Mapping[str, Any] = {}
            external_calls += 1
        else:
            active_router = router or (
                single_provider_router(
                    stage=STAGE_NAME, provider="gemini", model=job.model,
                    secret=str(api_key),
                )
                if api_key is not None else LLMRouter()
            )
            routed = active_router.execute(
                LLMRequest(
                    stage=STAGE_NAME,
                    logical_job_id=job.job_id,
                    prompt=job.prompt,
                    estimated_input_tokens=job.estimated_input_tokens,
                    reserved_output_tokens=job.reserved_output_tokens,
                    required_capabilities=("text", "json", "long_context"),
                ),
                lambda response: _validate_router_response(job, source, response),
            )
            accepted = routed.validation.accepted
            if not isinstance(accepted, Mapping):
                raise PilotLLMError("Router accepted a PDF result without validated candidates.")
            candidates = list(accepted.get("candidates") or [])
            dropped = list(accepted.get("dropped") or [])
            provider = routed.provider
            requested_model = routed.requested_model
            actual_model = routed.actual_model
            attempt_id = routed.attempt_id
            route_attempts = list(routed.attempts)
            validation_metrics = routed.validation.metrics
            external_calls += sum(
                1 for attempt in route_attempts if attempt.get("attempt_id")
            )
        cached_by_job[job.job_id] = _write_cache(
            cache_root,
            job,
            candidates,
            dropped,
            completed_at=completed_at,
            provider=provider,
            requested_model=requested_model,
            actual_model=actual_model,
            attempt_id=attempt_id,
            route_attempts=route_attempts,
            validation=validation_metrics,
        )

    added: list[dict[str, Any]] = []
    for job in queue:
        cached = cached_by_job[job.job_id]
        added.extend(evidence_records(job, cached["candidates"], source))
    # Stable dedupe protects resume and enrichment of an already-enriched bundle.
    added = list({record["evidence_id"]: record for record in added}.values())
    compiled, evidence, paths = enrich_bundle(
        bundle,
        added,
        destination,
        generated_at=completed_at,
    )
    cache_hits = len(queue) - len(pending)
    cache_documents = [cached_by_job[job.job_id] for job in queue]
    providers = list(dict.fromkeys(
        str(document.get("provider") or "")
        for document in cache_documents
        if document.get("provider")
    ))
    requested_models = list(dict.fromkeys(
        str(document.get("requested_model") or document.get("model") or "")
        for document in cache_documents
        if document.get("requested_model") or document.get("model")
    ))
    actual_models = list(dict.fromkeys(
        str(document.get("actual_model") or document.get("model") or "")
        for document in cache_documents
        if document.get("actual_model") or document.get("model")
    ))
    manifest = {
        "schema_version": STAGE_SCHEMA_VERSION,
        "stage": STAGE_NAME,
        "prompt_version": PROMPT_VERSION,
        "generated_at": completed_at,
        "map_id": selected_map_id,
        "source_bundle": str(bundle.root),
        "source_bundle_sha256": bundle.digest,
        "source_pdf": str(source.source_file),
        "source_text_sha256": source.sha256,
        "provider": providers[0] if len(providers) == 1 else ("mixed" if providers else None),
        "providers": providers,
        "requested_model": requested_models[0] if len(requested_models) == 1 else None,
        "actual_model": actual_models[0] if len(actual_models) == 1 else None,
        "model": actual_models[0] if len(actual_models) == 1 else model,
        "route_attempts": [
            {**dict(attempt), "logical_job_id": document.get("job_id")}
            for document in cache_documents
            for attempt in document.get("route_attempts") or []
            if isinstance(attempt, Mapping)
        ],
        "validation": {
            str(document.get("job_id")): document.get("validation") or {}
            for document in cache_documents
        },
        "compatible_cache_jobs_migrated": sum(
            bool(document.get("compatible_cache_migration"))
            for document in cache_documents
        ),
        "jobs": len(queue),
        "external_calls": external_calls,
        "cache_hits": cache_hits,
        "estimated_total_tokens": sum(job.estimated_total_tokens for job in pending),
        "added_evidence": len(added),
        "outputs": paths,
    }
    manifest_path = destination / "pilot_llm_stage.json"
    _atomic_json(manifest_path, manifest)
    return StageResult(
        compiled=compiled,
        evidence=evidence,
        paths=paths,
        manifest_path=str(manifest_path),
        jobs=len(queue),
        external_calls=external_calls,
        cache_hits=cache_hits,
        added_evidence=len(added),
    )


def _read_pdf_index(path: Path | None) -> Mapping[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, Mapping) or not isinstance(document.get("pages"), list):
        return None
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "run"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--bundle-dir", type=Path, required=True)
        sub.add_argument("--pdf", type=Path, required=True)
        sub.add_argument("--abstract", type=Path)
        sub.add_argument("--pdf-index", type=Path)
        sub.add_argument("--cache-dir", type=Path)
        sub.add_argument("--model", default=MODEL)
        if command == "run":
            sub.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # The CLI is generic: discover the id from the canonical pair, then pin
    # every later queue/cache operation to that exact id.
    bundle = load_bundle(args.bundle_dir, expected_map_id=None)
    selected_map_id = str((bundle.compiled.get("map") or {}).get("map_id") or "").strip()
    index_path = args.pdf_index
    if index_path is None:
        adjacent = args.pdf.resolve().parent / f"m{selected_map_id}_pdfpages.json"
        index_path = adjacent if adjacent.is_file() else None
    source = load_source(args.pdf, args.abstract, pdf_index=_read_pdf_index(index_path))
    queue = build_queue(bundle.compiled, source, model=args.model, map_id=selected_map_id)
    cache_root = (
        args.cache_dir.resolve()
        if args.cache_dir is not None
        else (
            args.output_dir.resolve() / ".pilot_llm_cache"
            if args.command == "run"
            else bundle.root / ".pilot_llm_cache"
        )
    )
    cached = sum(load_cached_job(cache_root, job) is not None for job in queue)
    pending = [job for job in queue if load_cached_job(cache_root, job) is None]
    budget = {
        "pending_calls": len(pending),
        "estimated_tokens": sum(job.estimated_total_tokens for job in pending),
        "remaining_calls_after": None,
        "remaining_tokens_after": None,
    }
    if args.command == "plan":
        print(json.dumps({
            "stage": STAGE_NAME,
            "map_id": selected_map_id,
            "jobs": len(queue),
            "cache_hits": cached,
            "target_units": sum(len(job.targets) for job in queue),
            "requested_fields": sum(
                len(target.fields) for job in queue for target in job.targets
            ),
            **budget,
            "external_call_made": False,
        }, ensure_ascii=False, indent=2))
        return 0

    result = run_stage(
        bundle.root,
        source,
        args.output_dir,
        cache_dir=cache_root,
        model=args.model,
        map_id=selected_map_id,
    )
    print(json.dumps({
        "compiled": result.paths["compiled"],
        "evidence": result.paths["evidence"],
        "manifest": result.manifest_path,
        "jobs": result.jobs,
        "external_calls": result.external_calls,
        "cache_hits": result.cache_hits,
        "added_evidence": result.added_evidence,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PilotLLMError as exc:
        print(f"[STOP] {exc}", file=sys.stderr)
        raise SystemExit(2)
