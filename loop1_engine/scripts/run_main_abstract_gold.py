# -*- coding: utf-8 -*-
"""Run one provider through the production Abstract validator and GOLD set.

The persisted result contains only stable field IDs and validator decisions.
The prompt, Abstract text, reviewed workbook values and raw provider response
exist only for the duration of the run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from compiled_layer import is_blank
from llm_router import AllProvidersFailed, LLMRequest, LLMRouter
from pilot_llm import (
    PROMPT_VERSION,
    STAGE_NAME,
    VALIDATOR_VERSION,
    _read_pdf_index,
    _validate_router_response,
    build_queue,
    load_source,
)
from gold_snapshot import bound_path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE = ROOT / "config" / "llm_gold_main_abstract.json"
DEFAULT_ROUTING = ROOT / "config" / "llm_routing.json"


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode().casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _field_id(unit_name: Any, field: Any) -> str:
    identity = f"{_normalise_name(unit_name)}|{str(field or '').strip()}"
    return "field_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _forced_config(
    routing: Mapping[str, Any], provider: str, model: str,
) -> dict[str, Any]:
    document = json.loads(json.dumps(routing))
    route = (document.get("routes") or {}).get(STAGE_NAME)
    if not isinstance(route, dict):
        raise ValueError(f"Route is not configured: {STAGE_NAME}")
    selected = next((
        dict(row) for row in route.get("candidates") or []
        if isinstance(row, Mapping)
        and str(row.get("provider") or "") == provider
        and str(row.get("model") or "") == model
    ), None)
    if selected is None:
        raise ValueError(
            f"Candidate is not registered for {STAGE_NAME}: {provider}:{model}"
        )
    selected["enabled"] = True
    selected.pop("disabled_reason", None)
    selected["max_attempts"] = 1
    route["max_failovers"] = 0
    route["candidates"] = [selected]
    return document


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _validate_fixture(fixture: Mapping[str, Any]) -> None:
    if fixture.get("schema_version") != "main-abstract-gold-fixture/1.0":
        raise ValueError("Unsupported main Abstract GOLD fixture")
    cases = fixture.get("cases") or []
    if not isinstance(cases, list) or not cases:
        raise ValueError("Main Abstract GOLD fixture has no cases")
    case_ids: set[str] = set()
    expected_ids: set[str] = set()
    for case in cases:
        if not isinstance(case, Mapping):
            raise ValueError("Main Abstract GOLD cases must be objects")
        case_id = str(case.get("case_id") or "").strip()
        fields = case.get("fields") or []
        expected = case.get("expected_items") or []
        if not case_id or case_id in case_ids or not isinstance(fields, list):
            raise ValueError("Main Abstract GOLD case IDs must be unique")
        if not isinstance(expected, list) or any(
            not re.fullmatch(r"field_[0-9a-f]{24}", str(item))
            for item in expected
        ):
            raise ValueError("Main Abstract GOLD expected IDs are invalid")
        if len(expected) != len(set(expected)):
            raise ValueError("Main Abstract GOLD case contains duplicate IDs")
        overlap = expected_ids.intersection(map(str, expected))
        if overlap:
            raise ValueError("Main Abstract GOLD IDs overlap between cases")
        case_ids.add(case_id)
        expected_ids.update(map(str, expected))


def run(
    *,
    workspace: Path,
    fixture_path: Path,
    provider: str,
    model: str,
    output_path: Path,
    routing_path: Path = DEFAULT_ROUTING,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    fixture = _read(fixture_path.resolve())
    if not isinstance(fixture, Mapping):
        raise ValueError("Main Abstract GOLD fixture must be a JSON object")
    _validate_fixture(fixture)

    workbook = bound_path(
        fixture, "source_workbook",
        expected_sha256=str(fixture["source_workbook_sha256"]),
    )
    pdf = bound_path(fixture, "source_pdf")
    abstract = bound_path(fixture, "source_abstract")
    index_path = bound_path(fixture, "source_pdf_index")
    compiled_path = bound_path(fixture, "compiled")
    for required in (pdf, abstract, index_path, compiled_path):
        if not required.is_file():
            raise FileNotFoundError(required)

    pdf_index = _read_pdf_index(index_path)
    source = load_source(pdf, abstract, pdf_index=pdf_index)
    compiled = _read(compiled_path)
    jobs = build_queue(compiled, source, map_id=str(fixture["map_id"]))
    if len(jobs) != 1:
        raise ValueError(f"Expected exactly one Abstract job, found {len(jobs)}")
    job = jobs[0]
    target_by_id = {target.unit_id: target for target in job.targets}
    targetable_ids = {
        _field_id(target.unit_name, field)
        for target in job.targets
        for field in target.fields
    }
    expected_ids = {
        str(item)
        for case in fixture["cases"]
        for item in case.get("expected_items") or []
    }
    if not expected_ids.issubset(targetable_ids):
        raise ValueError(
            "GOLD fixture includes fields that the production job does not request"
        )
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    validator_decision = "reject"
    route_error: str | None = None
    candidates: Sequence[Mapping[str, Any]] = ()
    with tempfile.TemporaryDirectory(
        prefix="main_abstract_gold_", dir=output_path.parent,
    ) as temp:
        forced_path = Path(temp) / "routing.json"
        _atomic_json(
            forced_path,
            _forced_config(_read(routing_path.resolve()), provider, model),
        )
        router = LLMRouter(config_path=forced_path)
        try:
            routed = router.execute(
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
            validator_decision = routed.validation.decision
            accepted = routed.validation.accepted
            if isinstance(accepted, Mapping):
                candidates = tuple(
                    row for row in accepted.get("candidates") or []
                    if isinstance(row, Mapping)
                )
        except AllProvidersFailed:
            route_error = "provider_output_rejected"

    actual_by_case: dict[str, set[str]] = {
        str(case["case_id"]): set() for case in fixture["cases"]
    }
    fields_by_case = {
        str(case["case_id"]): set(map(str, case.get("fields") or []))
        for case in fixture["cases"]
    }
    for candidate in candidates:
        for matched in candidate.get("_matched_targets") or []:
            if not isinstance(matched, Mapping):
                continue
            unit_id = str(matched.get("unit_id") or "")
            target = target_by_id.get(unit_id)
            if target is None:
                continue
            requested_fields = set(map(str, matched.get("fields") or []))
            for case_id, case_fields in fields_by_case.items():
                for field in sorted(requested_fields.intersection(case_fields)):
                    if not is_blank(candidate.get(field)):
                        actual_by_case[case_id].add(_field_id(target.unit_name, field))

    cases = []
    for source_case in fixture["cases"]:
        failures = [route_error] if route_error else []
        cases.append({
            "case_id": str(source_case["case_id"]),
            "validator_decision": validator_decision,
            "expected_items": list(source_case.get("expected_items") or []),
            "actual_items": sorted(actual_by_case[str(source_case["case_id"])]),
            "critical_failures": failures,
        })

    document = {
        "schema_version": "llm-gold-results/1.0",
        "generated_at": generated_at,
        "stage": STAGE_NAME,
        "provider": provider,
        "requested_model": model,
        "prompt_version": PROMPT_VERSION,
        "validator_version": VALIDATOR_VERSION,
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
    args = parser.parse_args(argv)
    result = run(
        workspace=args.workspace,
        fixture_path=args.fixture,
        provider=args.provider,
        model=args.model,
        output_path=args.output,
        routing_path=args.routing,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
