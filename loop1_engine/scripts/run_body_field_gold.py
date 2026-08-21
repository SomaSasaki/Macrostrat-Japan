# -*- coding: utf-8 -*-
"""Run one provider through the production PDF body-field validator and GOLD.

The persisted result contains only opaque item IDs and validator decisions.
Workbook values, Japanese text, prompts, and provider responses stay in memory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from llm_router import AllProvidersFailed, LLMRequest, LLMRouter
from pdf_field_extract import (
    ALLOWED_FIELDS,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    STAGE,
    build_prompt,
    validate_response,
)
from gold_snapshot import bound_path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE = ROOT / "config" / "llm_gold_body_fields.json"
DEFAULT_ROUTING = ROOT / "config" / "llm_routing.json"


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    os.replace(temporary, path)


def _normal_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).casefold()
    if isinstance(value, (int, float)):
        number = float(value)
        return str(int(number)) if number.is_integer() else format(number, ".15g")
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


def _field_item_id(unit_id: Any, field: Any, value: Any) -> str:
    identity = f"{str(unit_id or '').strip()}|{str(field or '').strip()}|{_normal_value(value)}"
    return "field_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _forced_config(
    routing: Mapping[str, Any], provider: str, model: str,
) -> dict[str, Any]:
    document = json.loads(json.dumps(routing))
    route = (document.get("routes") or {}).get(STAGE)
    if not isinstance(route, dict):
        raise ValueError(f"Route is not configured: {STAGE}")
    selected = next((
        dict(row) for row in route.get("candidates") or []
        if isinstance(row, Mapping)
        and str(row.get("provider") or "") == provider
        and str(row.get("model") or "") == model
    ), None)
    if selected is None:
        raise ValueError(f"Candidate is not registered for {STAGE}: {provider}:{model}")
    selected["enabled"] = True
    selected.pop("disabled_reason", None)
    selected["max_attempts"] = 1
    route["max_failovers"] = 0
    route["candidates"] = [selected]
    return document


def _source_span(page: str, start: str, end: str) -> str:
    start_index = page.find(start)
    if start_index < 0:
        raise ValueError("GOLD source_start was not found on the cited page")
    end_index = page.find(end, start_index)
    if end_index < 0:
        raise ValueError("GOLD source_end was not found on the cited page")
    return page[start_index:end_index + len(end)]


def _compiled_names(compiled: Mapping[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for unit in compiled.get("units") or []:
        if not isinstance(unit, Mapping):
            continue
        unit_id = str(unit.get("unit_id") or "")
        for container_name in ("review_values", "values"):
            container = unit.get(container_name)
            if isinstance(container, Mapping) and container.get("unit_name"):
                result.setdefault(unit_id, set()).add(str(container["unit_name"]))
    return result


def _validate_fixture(
    fixture: Mapping[str, Any], *, compiled: Mapping[str, Any], pdf_index: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if fixture.get("schema_version") != "body-field-gold-fixture/1.0":
        raise ValueError("Unsupported body-field GOLD fixture")
    cases = fixture.get("cases") or []
    if not isinstance(cases, list) or not cases:
        raise ValueError("Body-field GOLD fixture has no cases")
    pages = pdf_index.get("pages") if isinstance(pdf_index.get("pages"), list) else []
    printed = pdf_index.get("printed") if isinstance(pdf_index.get("printed"), list) else []
    known_names = _compiled_names(compiled)
    case_ids: set[str] = set()
    unit_ids: set[str] = set()
    contexts: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, Mapping):
            raise ValueError("Body-field GOLD cases must be objects")
        case_id = str(case.get("case_id") or "").strip()
        unit_id = str(case.get("unit_id") or "").strip()
        unit_name = str(case.get("unit_name") or "").strip()
        expected = case.get("expected_fields")
        if (
            not case_id or case_id in case_ids or not unit_id or unit_id in unit_ids
            or not unit_name or not isinstance(expected, Mapping) or not expected
        ):
            raise ValueError("Body-field GOLD cases require unique IDs and expected fields")
        if unit_name not in known_names.get(unit_id, set()):
            raise ValueError(f"GOLD unit does not match compiled source: {unit_id}")
        invalid_fields = set(expected) - ALLOWED_FIELDS
        if invalid_fields:
            raise ValueError(f"Unsupported GOLD fields: {sorted(invalid_fields)}")
        try:
            page_number = int(case.get("pdf_page"))
            printed_page = int(case.get("printed_page"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Body-field GOLD pages must be integers") from exc
        if not (1 <= page_number <= len(pages)):
            raise ValueError(f"GOLD PDF page is out of range: {case_id}")
        if printed and int(printed[page_number - 1]) != printed_page:
            raise ValueError(f"GOLD printed-page binding changed: {case_id}")
        page_text = str(pages[page_number - 1] or "")
        source_segments = case.get("source_segments")
        if source_segments is not None:
            if not isinstance(source_segments, list) or not source_segments:
                raise ValueError(f"GOLD source_segments must be a non-empty list: {case_id}")
            verified_segments: list[str] = []
            for segment in source_segments:
                segment_text = str(segment or "")
                if not segment_text or segment_text not in page_text:
                    raise ValueError(f"GOLD source segment was not found: {case_id}")
                verified_segments.append(segment_text)
            text = "\n".join(verified_segments)
        else:
            text = _source_span(
                page_text,
                str(case.get("source_start") or ""),
                str(case.get("source_end") or ""),
            )
        contexts.append({
            "context_id": "gold_" + hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:16],
            "unit_id": unit_id,
            "unit_name": unit_name,
            "column_ids": [],
            "section": "GOLD",
            "pdf_page": page_number,
            "printed_page": printed_page,
            "requested_fields": list(expected),
            "text": text,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        })
        case_ids.add(case_id)
        unit_ids.add(unit_id)
    return contexts


def run(
    *,
    workspace: Path,
    fixture_path: Path,
    provider: str,
    model: str,
    output_path: Path,
    routing_path: Path = DEFAULT_ROUTING,
    output_tokens: int = 4096,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    fixture = _read(fixture_path.resolve())
    if not isinstance(fixture, Mapping):
        raise ValueError("Body-field GOLD fixture must be a JSON object")
    workbook = bound_path(fixture, "source_workbook", expected_sha256=str(fixture["source_workbook_sha256"]))
    pdf = bound_path(fixture, "source_pdf", expected_sha256=str(fixture["pdf_sha256"]))
    index_path = bound_path(fixture, "source_pdf_index", expected_sha256=str(fixture["pdf_index_sha256"]))
    compiled_path = bound_path(fixture, "compiled", expected_sha256=str(fixture["compiled_sha256"]))
    expected_files = (
        (workbook, str(fixture["source_workbook_sha256"])),
        (pdf, str(fixture["pdf_sha256"])),
        (index_path, str(fixture["pdf_index_sha256"])),
        (compiled_path, str(fixture["compiled_sha256"])),
    )
    for path, expected_sha in expected_files:
        if not path.is_file():
            raise FileNotFoundError(path)
        if _sha256(path) != expected_sha:
            raise ValueError(f"GOLD-bound source changed: {path.name}")

    pdf_index = _read(index_path)
    contexts = _validate_fixture(
        fixture, compiled=_read(compiled_path), pdf_index=pdf_index,
    )
    prompt = build_prompt(contexts)
    input_tokens = max(1, math.ceil(len(prompt.encode("utf-8")) / 3))
    output_tokens = max(1, int(output_tokens))
    job_id = "gold_body_" + hashlib.sha256(
        (str(fixture["map_id"]) + "|" + hashlib.sha256(prompt.encode("utf-8")).hexdigest())
        .encode("utf-8")
    ).hexdigest()[:20]
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    route_error: str | None = None
    accepted: Sequence[Mapping[str, Any]] = ()
    with tempfile.TemporaryDirectory(prefix="body_gold_", dir=output_path.parent) as temp:
        forced_path = Path(temp) / "routing.json"
        _atomic_json(forced_path, _forced_config(_read(routing_path.resolve()), provider, model))
        router = LLMRouter(config_path=forced_path)
        try:
            routed = router.execute(
                LLMRequest(
                    stage=STAGE,
                    logical_job_id=job_id,
                    prompt=prompt,
                    estimated_input_tokens=input_tokens,
                    reserved_output_tokens=output_tokens,
                    required_capabilities=("text", "json", "japanese", "long_context"),
                ),
                lambda response: validate_response(contexts, response),
            )
            accepted = tuple(
                row for row in routed.validation.accepted or [] if isinstance(row, Mapping)
            )
        except AllProvidersFailed:
            route_error = "provider_output_rejected"

    actual_by_unit: dict[str, set[str]] = {
        str(case["unit_id"]): set() for case in fixture["cases"]
    }
    accepted_fields_by_unit: dict[str, set[str]] = {
        str(case["unit_id"]): set() for case in fixture["cases"]
    }
    for row in accepted:
        unit_id = str(row.get("unit_id") or "")
        if unit_id in actual_by_unit:
            field = str(row.get("field") or "")
            actual_by_unit[unit_id].add(
                _field_item_id(unit_id, field, row.get("candidate"))
            )
            accepted_fields_by_unit[unit_id].add(field)

    cases = []
    for source_case in fixture["cases"]:
        unit_id = str(source_case["unit_id"])
        target_fields = set(source_case["expected_fields"])
        accepted_fields = accepted_fields_by_unit[unit_id]
        if not accepted_fields:
            decision = "reject"
        elif target_fields.issubset(accepted_fields):
            decision = "accept"
        else:
            decision = "partial"
        cases.append({
            "case_id": str(source_case["case_id"]),
            "validator_decision": decision,
            "expected_items": sorted(
                _field_item_id(unit_id, field, value)
                for field, value in source_case["expected_fields"].items()
            ),
            "actual_items": sorted(actual_by_unit[unit_id]),
            "critical_failures": [route_error] if route_error else [],
        })
    document = {
        "schema_version": "llm-gold-results/1.0",
        "generated_at": generated_at,
        "stage": STAGE,
        "provider": provider,
        "requested_model": model,
        "prompt_version": PROMPT_VERSION,
        "validator_version": SCHEMA_VERSION,
        "cases": cases,
    }
    _atomic_json(output_path, document)
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--routing", type=Path, default=DEFAULT_ROUTING)
    parser.add_argument("--output-tokens", type=int, default=4096)
    args = parser.parse_args(argv)
    result = run(
        workspace=args.workspace,
        fixture_path=args.fixture,
        provider=args.provider,
        model=args.model,
        output_path=args.output,
        routing_path=args.routing,
        output_tokens=args.output_tokens,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["_field_item_id", "_forced_config", "_validate_fixture", "run"]
