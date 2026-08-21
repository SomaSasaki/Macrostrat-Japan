# -*- coding: utf-8 -*-
"""Run one closed-world Environment classification request per reviewed unit."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from llm_column_vision import _image_token_estimate
from llm_constrained_vision import (
    CONSTRAINED_VALIDATOR_VERSION,
    ENVIRONMENT_CLASSIFICATION_PROMPT_VERSION,
    build_environment_unit_prompt,
    environment_candidates,
    validate_environment_unit,
)
from llm_router import AllProvidersFailed, LLMImage, LLMRequest, LLMRouter, ValidationReport
from pdf_environment import STAGE, _mime_type
from run_environment_gold import (
    DEFAULT_FIXTURE,
    DEFAULT_ROUTING,
    _atomic_json,
    _item_id,
    _prepare,
)


def payload_summary(
    fixture_path: Path, provider: str, model: str,
    routing_path: Path = DEFAULT_ROUTING,
) -> dict[str, Any]:
    fixture, targets, figures, _job, _forced = _prepare(fixture_path, provider, model, routing_path)
    image_tokens = sum(_image_token_estimate(Path(row["path"])) for row in figures)
    rows = []
    total_input = 0
    for target in targets:
        candidates = environment_candidates(target)
        prompt = build_environment_unit_prompt(target, figures, candidates)
        estimate = math.ceil(len(prompt.encode("utf-8")) / 3) + image_tokens
        total_input += estimate
        rows.append({
            "unit_id": target["unit_id"], "unit_name": target["unit_name"],
            "candidate_count": len(candidates), "candidates": list(candidates),
            "estimated_input_tokens": estimate,
        })
    return {
        "stage": STAGE, "provider": provider, "model": model,
        "failover": False, "max_attempts_per_call": 1,
        "external_calls": len(targets), "targets": rows,
        "figures_per_call": len(figures), "estimated_input_tokens": total_input,
        "reserved_output_tokens": len(targets) * 768,
    }


def run(
    *, fixture_path: Path, provider: str, model: str, output_path: Path,
    routing_path: Path = DEFAULT_ROUTING,
) -> dict[str, Any]:
    fixture, targets, figures, _job, forced = _prepare(fixture_path, provider, model, routing_path)
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    forced_path = output_path.with_name(f".{output_path.stem}.constrained.routing.json")
    _atomic_json(forced_path, forced)
    accepted_by_unit: dict[str, list[Mapping[str, Any]]] = {}
    error_by_unit: dict[str, str] = {}
    try:
        router = LLMRouter(config_path=forced_path)
        image_tokens = sum(_image_token_estimate(Path(row["path"])) for row in figures)
        images = tuple(
            LLMImage(path=row["path"], mime_type=_mime_type(Path(row["path"]))) for row in figures
        )
        for target in targets:
            unit_id = str(target["unit_id"])
            candidates = environment_candidates(target)
            prompt = build_environment_unit_prompt(target, figures, candidates)

            def validate(response: Mapping[str, Any], target=target, candidates=candidates) -> ValidationReport:
                try:
                    accepted, dropped, unresolved = validate_environment_unit(
                        response, target, figures, candidates,
                    )
                except (ValueError, RuntimeError) as exc:
                    return ValidationReport(decision="reject", fatal_errors=(str(exc),))
                if accepted:
                    return ValidationReport(
                        decision="accept", accepted={"accepted": accepted},
                        dropped=dropped, unresolved=unresolved,
                    )
                # "unresolved" is an answer the closed-world prompt explicitly
                # allows.  Reporting it as a provider failure opened the stage
                # circuit breaker after two units and left the remaining units
                # unsent, so a cautious model produced no measurement at all.
                # A declared unresolve is now a scored miss; an omitted unit,
                # a hallucinated identifier, or unverifiable evidence stays a
                # provider failure.
                declared_unresolved = bool(unresolved) and not dropped and all(
                    str(row.get("reason") or "") != "model_omitted_target_unit"
                    for row in unresolved
                )
                if declared_unresolved:
                    return ValidationReport(
                        decision="accept", accepted={"accepted": []},
                        dropped=dropped, unresolved=unresolved,
                    )
                return ValidationReport(decision="reject", dropped=dropped, unresolved=unresolved)

            try:
                routed = router.execute(
                    LLMRequest(
                        stage=STAGE,
                        logical_job_id=f"gold_environment_unit_{fixture['map_id']}_{unit_id}",
                        prompt=prompt,
                        estimated_input_tokens=math.ceil(len(prompt.encode("utf-8")) / 3) + image_tokens,
                        reserved_output_tokens=768,
                        required_capabilities=("text", "json", "japanese", "vision"),
                        images=images,
                    ),
                    validate,
                )
                validated = routed.validation.accepted or {}
                accepted_by_unit[unit_id] = list(validated.get("accepted") or [])
            except AllProvidersFailed as exc:
                last = exc.attempts[-1] if exc.attempts else {}
                error_by_unit[unit_id] = "provider_" + str(last.get("error_kind") or "output_rejected")
    finally:
        forced_path.unlink(missing_ok=True)

    cases = []
    for source in fixture.get("cases") or []:
        unit_id = str(source["unit_id"])
        actual = []
        for row in accepted_by_unit.get(unit_id, []):
            value = "not_applicable" if row.get("field") == "environment_applicability" else row.get("candidate")
            actual.append(_item_id(unit_id, value))
        cases.append({
            "case_id": str(source["case_id"]),
            "validator_decision": "accept" if actual else "reject",
            "expected_items": [_item_id(unit_id, source["expected_environment"])],
            "actual_items": sorted(set(actual)),
            "critical_failures": [error_by_unit[unit_id]] if unit_id in error_by_unit else [],
        })
    document = {
        "schema_version": "llm-gold-results/1.0", "generated_at": generated_at,
        "stage": STAGE, "provider": provider, "requested_model": model,
        "prompt_version": ENVIRONMENT_CLASSIFICATION_PROMPT_VERSION,
        "validator_version": CONSTRAINED_VALIDATOR_VERSION, "cases": cases,
    }
    _atomic_json(output_path, document)
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--routing", type=Path, default=DEFAULT_ROUTING)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        result = payload_summary(args.fixture, args.provider, args.model, args.routing)
    else:
        if args.output is None:
            parser.error("--output is required unless --dry-run is used")
        result = run(
            fixture_path=args.fixture, provider=args.provider, model=args.model,
            output_path=args.output, routing_path=args.routing,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

