# -*- coding: utf-8 -*-
"""Run one provider through the production PDF-unit-bootstrap GOLD set.

Only opaque unit IDs and validator decisions are persisted.  Workbook values,
Abstract excerpts, prompts, and provider responses remain in memory.
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
from pdf_unit_bootstrap import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    STAGE,
    VALIDATOR_VERSION,
    build_prompt,
    validate_inventory_response,
)
from pilot_llm import SourceDocument
from gold_snapshot import bound_path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE = ROOT / "config" / "llm_gold_bootstrap.json"
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
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _normal_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("–", "-").replace("—", "-").replace("−", "-")
    return " ".join(text.casefold().split())


def _unit_item_id(value: Any) -> str:
    return "unit_" + hashlib.sha256(
        _normal_name(value).encode("utf-8")
    ).hexdigest()[:24]


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


def _compiled_names(compiled: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for unit in compiled.get("units") or []:
        if not isinstance(unit, Mapping):
            continue
        for container_name in ("review_values", "values"):
            container = unit.get(container_name)
            if isinstance(container, Mapping) and container.get("unit_name"):
                result.add(_normal_name(container["unit_name"]))
    return result


def _validate_fixture(
    fixture: Mapping[str, Any], *, abstract: str, compiled: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if fixture.get("schema_version") != "bootstrap-gold-fixture/1.0":
        raise ValueError("Unsupported bootstrap GOLD fixture")
    cases = fixture.get("cases") or []
    if not isinstance(cases, list) or not cases:
        raise ValueError("Bootstrap GOLD fixture has no cases")
    known_names = _compiled_names(compiled)
    case_ids: set[str] = set()
    contexts: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, Mapping):
            raise ValueError("Bootstrap GOLD cases must be objects")
        case_id = str(case.get("case_id") or "").strip()
        segments = case.get("source_segments")
        expected = case.get("expected_units")
        if (
            not case_id or case_id in case_ids
            or not isinstance(segments, list) or not segments
            or not isinstance(expected, list) or not expected
        ):
            raise ValueError("Bootstrap GOLD cases require unique IDs, source and targets")
        verified_segments: list[str] = []
        for segment in segments:
            segment_text = str(segment or "")
            if not segment_text or segment_text not in abstract:
                raise ValueError(f"GOLD Abstract segment was not found: {case_id}")
            verified_segments.append(segment_text)
        names: list[str] = []
        seen: set[str] = set()
        for item in expected:
            if not isinstance(item, Mapping):
                raise ValueError(f"GOLD expected_units must be objects: {case_id}")
            name = str(item.get("unit_name") or "").strip()
            workbook_range = str(item.get("workbook_range") or "").strip()
            key = _normal_name(name)
            if not name or not workbook_range.startswith("units!I") or key in seen:
                raise ValueError(f"Invalid GOLD expected unit: {case_id}")
            if key not in known_names:
                raise ValueError(f"GOLD unit is absent from compiled review: {name}")
            seen.add(key)
            names.append(name)
        text = "\n".join(verified_segments)
        contexts.append({"case_id": case_id, "text": text, "expected_names": names})
        case_ids.add(case_id)
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
        raise ValueError("Bootstrap GOLD fixture must be a JSON object")
    workbook = bound_path(fixture, "source_workbook", expected_sha256=str(fixture["source_workbook_sha256"]))
    abstract_path = bound_path(fixture, "source_abstract", expected_sha256=str(fixture["abstract_sha256"]))
    compiled_path = bound_path(fixture, "compiled", expected_sha256=str(fixture["compiled_sha256"]))
    for path, expected_sha in (
        (workbook, str(fixture["source_workbook_sha256"])),
        (abstract_path, str(fixture["abstract_sha256"])),
        (compiled_path, str(fixture["compiled_sha256"])),
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
        if _sha256(path) != expected_sha:
            raise ValueError(f"GOLD-bound source changed: {path.name}")
    contexts = _validate_fixture(
        fixture,
        abstract=abstract_path.read_text(encoding="utf-8"),
        compiled=_read(compiled_path),
    )
    output_tokens = max(1, int(output_tokens))
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    route_config = _forced_config(_read(routing_path.resolve()), provider, model)
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="bootstrap_gold_", dir=output_path.parent) as temp:
        forced_path = Path(temp) / "routing.json"
        _atomic_json(forced_path, route_config)
        router = LLMRouter(config_path=forced_path)
        for context in contexts:
            source = SourceDocument(context["text"], abstract_path)
            prompt = build_prompt(source)
            input_tokens = max(1, math.ceil(len(prompt.encode("utf-8")) / 3))
            prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            job_id = "gold_bootstrap_" + hashlib.sha256(
                (str(fixture["map_id"]) + "|" + context["case_id"] + "|" + prompt_sha)
                .encode("utf-8")
            ).hexdigest()[:20]
            route_error: str | None = None
            accepted: Sequence[Mapping[str, Any]] = ()
            validator_decision = "reject"
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
                    lambda response, source=source: validate_inventory_response(response, source),
                )
                validator_decision = str(routed.validation.decision)
                accepted = tuple(
                    row for row in routed.validation.accepted or []
                    if isinstance(row, Mapping)
                )
            except AllProvidersFailed:
                route_error = "provider_output_rejected"
            expected_items = sorted(_unit_item_id(name) for name in context["expected_names"])
            actual_items = sorted({
                _unit_item_id(row.get("unit_name"))
                for row in accepted if str(row.get("unit_name") or "").strip()
            })
            results.append({
                "case_id": context["case_id"],
                "validator_decision": validator_decision,
                "expected_items": expected_items,
                "actual_items": actual_items,
                "critical_failures": [route_error] if route_error else [],
            })
    document = {
        "schema_version": "llm-gold-results/1.0",
        "generated_at": generated_at,
        "stage": STAGE,
        "provider": provider,
        "requested_model": model,
        "prompt_version": PROMPT_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "cases": results,
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


__all__ = ["_forced_config", "_unit_item_id", "_validate_fixture", "run"]
