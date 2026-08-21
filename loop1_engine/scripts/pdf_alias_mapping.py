# -*- coding: utf-8 -*-
"""Cached English-to-Japanese unit alias mapping for PDF-only GSJ reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from common import load_secret
    from llm_extract import MODEL, load_limits, today_usage
    from llm_router import LLMRequest, LLMRouter, ValidationReport, single_provider_router
    from pdf_locate import normalize
except ImportError:  # pragma: no cover - package-style import
    from .common import load_secret
    from .llm_extract import MODEL, load_limits, today_usage
    from .llm_router import LLMRequest, LLMRouter, ValidationReport, single_provider_router
    from .pdf_locate import normalize


SCHEMA_VERSION = "pdf-unit-aliases/1.0"
PROMPT_VERSION = "pdf-toc-aliases-v1"
STAGE = "pdf_unit_alias_mapping"
Executor = Callable[[str], Mapping[str, Any]]


class PDFAliasError(RuntimeError):
    """Actionable PDF alias-mapping failure."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _contents_pages(pdf_index: Mapping[str, Any]) -> list[dict[str, Any]]:
    pages = pdf_index.get("pages") if isinstance(pdf_index.get("pages"), list) else []
    printed = pdf_index.get("printed") if isinstance(pdf_index.get("printed"), list) else []
    start = next((index for index, page in enumerate(pages[:20]) if "目次" in str(page)), None)
    if start is None:
        start = 0
    output: list[dict[str, Any]] = []
    for index in range(start, min(len(pages), start + 8)):
        text = str(pages[index] or "")
        printed_page = printed[index] if index < len(printed) else None
        if index > start and isinstance(printed_page, int) and printed_page <= 2:
            break
        if text:
            output.append({"pdf_page": index + 1, "text": text})
    return output


def build_prompt(alias_table: Mapping[str, Any], pdf_index: Mapping[str, Any]) -> str:
    targets = [
        {"unit_id": row.get("unit_id"), "unit_name": row.get("unit_name")}
        for row in alias_table.get("units") or []
        if row.get("status") == "alias_mapping_required" and row.get("unit_name")
    ]
    contents = _contents_pages(pdf_index)
    return f"""Map English geological unit names from a GSJ 1:50,000 report to
their exact Japanese unit headings in the report table of contents.

Return JSON only:
{{"aliases":[{{"unit_id":"m0000_p001","japanese_alias":"日本語の地層名",
"toc_quote":"the exact continuous text fragment containing that alias",
"pdf_page":4}}]}}

Rules:
- Use only the supplied unit_id values.
- japanese_alias must be copied exactly from the supplied contents text.
- toc_quote must be an exact continuous substring of that same page.
- Match by geological name/transliteration and formation/member meaning.
- Do not guess. Omit uncertain targets.
- Do not use generic words such as 地層, 堆積物 or 火山岩 alone.

English targets:
{json.dumps(targets, ensure_ascii=False, indent=2)}

Japanese contents pages:
{json.dumps(contents, ensure_ascii=False, indent=2)}
"""


def verify_aliases(
    alias_table: Mapping[str, Any], pdf_index: Mapping[str, Any], response: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    targets = {
        str(row.get("unit_id") or ""): row
        for row in alias_table.get("units") or []
        if row.get("status") == "alias_mapping_required"
    }
    pages = pdf_index.get("pages") if isinstance(pdf_index.get("pages"), list) else []
    accepted: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    raw_aliases = response.get("aliases")
    if not isinstance(raw_aliases, list):
        raise PDFAliasError("Alias response must contain aliases[].")
    for row in raw_aliases:
        if not isinstance(row, Mapping):
            continue
        unit_id = str(row.get("unit_id") or "")
        alias = str(row.get("japanese_alias") or "").strip()
        quote = str(row.get("toc_quote") or "").strip()
        try:
            page_number = int(row.get("pdf_page"))
        except (TypeError, ValueError):
            page_number = 0
        if unit_id not in targets:
            dropped.append({"unit_id": unit_id, "reason": "unknown_unit_id"})
            continue
        page = str(pages[page_number - 1] or "") if 1 <= page_number <= len(pages) else ""
        normalized_alias = normalize(alias)
        generic_aliases = {
            normalize(value) for value in ("層", "地層", "堆積物", "火山岩")
        }
        if (
            not normalized_alias
            or normalized_alias in generic_aliases
            or normalized_alias not in normalize(page)
        ):
            dropped.append({"unit_id": unit_id, "reason": "alias_not_on_cited_page"})
            continue
        if not quote or normalize(quote) not in normalize(page) or normalize(alias) not in normalize(quote):
            dropped.append({"unit_id": unit_id, "reason": "toc_quote_not_on_cited_page"})
            continue
        accepted.append({
            "unit_id": unit_id,
            "unit_name": targets[unit_id].get("unit_name"),
            "japanese_alias": alias,
            "toc_quote": quote,
            "pdf_page": page_number,
        })
    return accepted, dropped


def validate_alias_response(
    alias_table: Mapping[str, Any],
    pdf_index: Mapping[str, Any],
    response: Mapping[str, Any],
) -> ValidationReport:
    """Convert every provider-shape error into a normal validator rejection."""
    try:
        accepted_rows, dropped_rows = verify_aliases(alias_table, pdf_index, response)
    except PDFAliasError as exc:
        return ValidationReport(decision="reject", fatal_errors=(str(exc),))
    required = sum(
        row.get("status") == "alias_mapping_required"
        for row in alias_table.get("units") or []
    )
    if not accepted_rows:
        decision = "reject"
    elif len(accepted_rows) < required:
        decision = "partial"
    else:
        decision = "accept"
    return ValidationReport(
        decision=decision,
        accepted=accepted_rows,
        dropped=dropped_rows,
        metrics={
            "accepted_count": len(accepted_rows),
            "dropped_count": len(dropped_rows),
            "target_count": required,
            "target_coverage": len(accepted_rows) / required if required else 1.0,
        },
    )


def merge_aliases(alias_table: Mapping[str, Any], mappings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result = json.loads(json.dumps(alias_table, ensure_ascii=False))
    by_unit = {str(row.get("unit_id") or ""): row for row in mappings}
    for row in result.get("units") or []:
        mapping = by_unit.get(str(row.get("unit_id") or ""))
        if mapping is None:
            continue
        alias = str(mapping.get("japanese_alias") or "")
        if alias and alias not in row["aliases"]:
            row["aliases"].append(alias)
        if alias and alias not in row["japanese_aliases"]:
            row["japanese_aliases"].append(alias)
        row["pdf_alias_page"] = mapping.get("pdf_page")
        row["pdf_alias_quote"] = mapping.get("toc_quote")
        row["status"] = "pdf_alias_candidate"
    result["pdf_alias_mappings"] = list(mappings)
    return result


def _preflight(
    prompt: str,
    *,
    usage: Mapping[str, int] | None = None,
) -> int:
    estimated = math.ceil(len(prompt.encode("utf-8")) / 3) + 2048
    limits = load_limits()
    if usage is None:
        _path, _all, _date, usage = today_usage()
    if int(limits.get("max_tokens_per_call") or 0) and estimated > int(limits["max_tokens_per_call"]):
        raise PDFAliasError("Alias mapping exceeds the configured per-call token limit.")
    if int(limits.get("max_calls_per_day") or 0) and int(usage.get("calls") or 0) + 1 > int(limits["max_calls_per_day"]):
        raise PDFAliasError("Alias mapping would exceed the daily call limit.")
    if int(limits.get("max_tokens_per_day") or 0) and int(usage.get("tokens") or 0) + estimated > int(limits["max_tokens_per_day"]):
        raise PDFAliasError("Alias mapping would exceed the daily token limit.")
    return estimated


def _load_compatible_cache(
    cache_root: Path,
    cache_path: Path,
    *,
    alias_table: Mapping[str, Any],
    pdf_index: Mapping[str, Any],
    prompt_sha: str,
    source_sha256: str,
    job_id: str,
) -> dict[str, Any] | None:
    """Migrate an old source-bound cache only after current-PDF revalidation.

    Stable source inventories can legitimately receive new ``unit_id`` values
    after de-duplication rules improve.  A cache therefore remains reusable
    when its English unit name maps uniquely to a current target and its
    Japanese alias/TOC quote still verifies on the same PDF page.  The old ID
    itself is never trusted.
    """
    current_by_name: dict[str, list[Mapping[str, Any]]] = {}
    for row in alias_table.get("units") or []:
        if row.get("status") != "alias_mapping_required":
            continue
        key = normalize(row.get("unit_name"))
        if key:
            current_by_name.setdefault(key, []).append(row)

    best: tuple[int, dict[str, Any]] | None = None
    for candidate in cache_root.glob("pam_*.json") if cache_root.is_dir() else ():
        if candidate == cache_path:
            continue
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            document.get("status") != "complete"
            or document.get("stage") != STAGE
            or document.get("source_sha256") != source_sha256
        ):
            continue
        prior = list(document.get("accepted") or [])
        retargeted: list[dict[str, Any]] = []
        for old in prior:
            if not isinstance(old, Mapping):
                continue
            matches = current_by_name.get(normalize(old.get("unit_name"))) or []
            if len(matches) != 1:
                continue
            row = dict(old)
            row["unit_id"] = matches[0].get("unit_id")
            row["unit_name"] = matches[0].get("unit_name")
            retargeted.append(row)
        try:
            accepted, dropped_now = verify_aliases(
                alias_table,
                pdf_index,
                {"aliases": retargeted},
            )
        except (PDFAliasError, TypeError, ValueError):
            continue
        if not accepted:
            continue
        migrated = {
            **document,
            "job_id": job_id,
            "prompt_sha256": prompt_sha,
            "accepted": accepted,
            "dropped": list(document.get("dropped") or []) + dropped_now,
            "provider": document.get("provider") or "gemini",
            "requested_model": document.get("requested_model") or document.get("model"),
            "actual_model": document.get("actual_model") or document.get("model"),
            "migrated_from_job_id": document.get("job_id"),
            "compatible_cache_migration": True,
            "retargeted_by_unit_name": True,
        }
        score = len(accepted)
        if best is None or score > best[0]:
            best = (score, migrated)
    if best is None:
        return None
    _atomic_json(cache_path, best[1])
    return best[1]


def run_alias_mapping(
    alias_table: Mapping[str, Any],
    pdf_index: Mapping[str, Any],
    *,
    source_sha256: str,
    cache_dir: str | Path,
    model: str = MODEL,
    api_key: str | None = None,
    executor: Executor | None = None,
    router: LLMRouter | None = None,
    allow_external_calls: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = build_prompt(alias_table, pdf_index)
    if not any(row.get("status") == "alias_mapping_required" for row in alias_table.get("units") or []):
        return dict(alias_table), {"schema_version": SCHEMA_VERSION, "stage": STAGE, "status": "not_needed", "external_calls": 0, "cache_hits": 0, "mapped_units": 0}
    prompt_sha = _sha(prompt)
    job_id = "pam_" + _sha(json.dumps({
        "stage": STAGE, "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION, "validator_version": SCHEMA_VERSION,
        "source_sha256": source_sha256, "prompt_sha256": prompt_sha,
    }, sort_keys=True))[:20]
    cache_root = Path(cache_dir).expanduser().resolve()
    cache_path = cache_root / f"{job_id}.json"
    cached = None
    try:
        document = json.loads(cache_path.read_text(encoding="utf-8"))
        if document.get("status") == "complete" and document.get("prompt_sha256") == prompt_sha and document.get("source_sha256") == source_sha256:
            cached = document
    except (OSError, json.JSONDecodeError):
        pass
    if cached is None:
        cached = _load_compatible_cache(
            cache_root,
            cache_path,
            alias_table=alias_table,
            pdf_index=pdf_index,
            prompt_sha=prompt_sha,
            source_sha256=source_sha256,
            job_id=job_id,
        )
    if cached is None:
        if not allow_external_calls:
            raise PDFAliasError(
                "No source-compatible alias cache passed current PDF validation; "
                "external calls are disabled."
            )
        estimated = math.ceil(len(prompt.encode("utf-8")) / 3) + 2048
        if executor:
            # An injected executor is a hermetic test/dry-run boundary.  Keep
            # per-call limits, but do not couple it to today's real API usage.
            _preflight(prompt, usage={"calls": 0, "tokens": 0})
            response = executor(prompt)
            provider = "injected"
            requested_model = model
            actual_model = model
            attempt_id = None
            route_attempts: list[Mapping[str, Any]] = []
            accepted, dropped = verify_aliases(alias_table, pdf_index, response)
        else:
            active_router = router or (
                single_provider_router(
                    stage=STAGE, provider="gemini", model=model,
                    secret=str(api_key),
                )
                if api_key is not None else LLMRouter()
            )

            routed = active_router.execute(
                LLMRequest(
                    stage=STAGE,
                    logical_job_id=job_id,
                    prompt=prompt,
                    estimated_input_tokens=max(1, estimated - 2048),
                    reserved_output_tokens=2048,
                    required_capabilities=("text", "json", "japanese"),
                ),
                lambda response: validate_alias_response(alias_table, pdf_index, response),
            )
            response = routed.response
            provider = routed.provider
            requested_model = routed.requested_model
            actual_model = routed.actual_model
            attempt_id = routed.attempt_id
            route_attempts = list(routed.attempts)
            accepted = list(routed.validation.accepted or [])
            dropped = list(routed.validation.dropped or [])
        if not isinstance(response, Mapping):
            raise PDFAliasError("Alias mapping response is not a JSON object.")
        cached = {
            "schema_version": SCHEMA_VERSION, "stage": STAGE, "status": "complete",
            "job_id": job_id, "prompt_version": PROMPT_VERSION,
            "prompt_sha256": prompt_sha, "source_sha256": source_sha256,
            "provider": provider, "requested_model": requested_model,
            "actual_model": actual_model, "model": actual_model, "attempt_id": attempt_id,
            "accepted": accepted, "dropped": dropped, "estimated_tokens": estimated,
            "route_attempts": route_attempts,
        }
        _atomic_json(cache_path, cached)
        calls = sum(1 for row in route_attempts if row.get("attempt_id")) if not executor else 1
        hits = 0
    else:
        accepted = list(cached.get("accepted") or [])
        dropped = list(cached.get("dropped") or [])
        calls, hits = 0, 1
    merged = merge_aliases(alias_table, accepted)
    return merged, {
        "schema_version": SCHEMA_VERSION, "stage": STAGE, "status": "complete",
        "job_id": job_id,
        "provider": cached.get("provider"),
        "requested_model": cached.get("requested_model") or cached.get("model") or model,
        "actual_model": cached.get("actual_model") or cached.get("model") or model,
        "model": cached.get("actual_model") or cached.get("model") or model,
        "external_calls": calls, "cache_hits": hits,
        "mapped_units": len(accepted), "dropped_count": len(dropped), "cache_file": str(cache_path),
        "route_attempts": list(cached.get("route_attempts") or []),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Map PDF-only English units to Japanese TOC aliases without touching Excel.")
    parser.add_argument("--aliases", required=True)
    parser.add_argument("--pdf-index", required=True)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args(argv)
    aliases = json.loads(Path(args.aliases).read_text(encoding="utf-8"))
    pdf_index = json.loads(Path(args.pdf_index).read_text(encoding="utf-8"))
    merged, manifest = run_alias_mapping(
        aliases, pdf_index, source_sha256=_file_sha(Path(args.pdf)),
        cache_dir=args.cache_dir, model=args.model,
    )
    Path(args.output).write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PDFAliasError", "build_prompt", "merge_aliases", "run_alias_mapping",
    "validate_alias_response", "verify_aliases",
]
