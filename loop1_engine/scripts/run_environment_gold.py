# -*- coding: utf-8 -*-
"""Run one provider through the production PDF Environment prompt and validator."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from llm_router import AllProvidersFailed, LLMImage, LLMRequest, LLMRouter, ValidationReport
from pdf_environment import (
    PROMPT_VERSION,
    STAGE,
    VALIDATOR_VERSION,
    _figure_metadata,
    _mime_type,
    build_job,
    build_targets,
    verify_response,
)
from gold_snapshot import bound_path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE = ROOT / "config" / "llm_gold_environment.json"
DEFAULT_ROUTING = ROOT / "config" / "llm_routing.json"


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _item_id(unit_id: Any, environment: Any) -> str:
    identity = f"{str(unit_id or '').strip()}|{str(environment or '').strip().casefold()}"
    return "environment_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


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


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _prepare(fixture_path: Path, provider: str, model: str, routing_path: Path):
    fixture = _read(fixture_path.resolve())
    if fixture.get("schema_version") != "environment-gold-fixture/1.0":
        raise ValueError("Unsupported PDF Environment GOLD fixture")
    workbook = bound_path(
        fixture, "source_workbook",
        expected_sha256=str(fixture["source_workbook_sha256"]),
    )
    compiled_path = bound_path(
        fixture, "compiled", expected_sha256=str(fixture["compiled_sha256"]),
    )
    routed_path = bound_path(
        fixture, "routed_contexts",
        expected_sha256=str(fixture["routed_contexts_sha256"]),
    )
    figure_manifest_path = bound_path(
        fixture, "environment_figure_manifest",
        expected_sha256=str(fixture["figure_manifest_sha256"]),
    )
    compiled = _read(compiled_path)
    routed = _read(routed_path)
    figure_manifest = _read(figure_manifest_path)
    pdf = bound_path(
        fixture, "source_pdf", expected_sha256=str(fixture["pdf_sha256"]),
    )

    cases = fixture.get("cases") or []
    case_by_unit = {str(row.get("unit_id") or ""): row for row in cases}
    if len(case_by_unit) != len(cases) or len(cases) < 3:
        raise ValueError("Environment GOLD requires at least three unique cases")
    selected_units = [
        row for row in compiled.get("units") or []
        if str(row.get("unit_id") or "") in case_by_unit
    ]
    selected_contexts = [
        row for row in routed.get("contexts") or []
        if str(row.get("unit_id") or "") in case_by_unit
    ]
    if len(selected_units) != len(cases) or len(selected_contexts) != len(cases):
        raise ValueError("Every GOLD case must have one canonical unit and routed context")
    for unit in selected_units:
        case = case_by_unit[str(unit.get("unit_id") or "")]
        if str((unit.get("values") or {}).get("unit_name") or "") != str(case.get("unit_name") or ""):
            raise ValueError(f"Canonical unit name changed: {case.get('case_id')}")

    selected_figure_rows = []
    expected_figures = {
        (int(row["pdf_page"]), str(row["image_sha256"]))
        for row in fixture.get("figures") or []
    }
    for row in figure_manifest.get("candidates") or []:
        identity = (int(row.get("pdf_page") or 0), str(row.get("image_sha256") or ""))
        if identity in expected_figures:
            image_path = bound_path(
                fixture, f"environment_figure_p{identity[0]}",
                expected_sha256=identity[1],
            )
            selected_figure_rows.append({**row, "image_file": str(image_path)})
    if len(selected_figure_rows) != len(expected_figures):
        raise ValueError("A bound GOLD figure is missing")
    image_paths = [str(row["image_file"]) for row in selected_figure_rows]
    figures = _figure_metadata(image_paths, {"candidates": selected_figure_rows})
    targets, unresolved = build_targets(
        {**compiled, "units": selected_units},
        {**routed, "contexts": selected_contexts},
    )
    if unresolved or len(targets) != len(cases):
        raise ValueError("Every GOLD case must remain an unresolved production target")
    job = build_job(
        map_id=str(fixture["map_id"]),
        model=model,
        source_sha256=str(fixture["pdf_sha256"]),
        targets=targets,
        figures=figures,
    )
    forced = _forced_config(_read(routing_path.resolve()), provider, model)
    return fixture, targets, figures, job, forced


def payload_summary(
    fixture_path: Path, provider: str, model: str,
    routing_path: Path = DEFAULT_ROUTING,
) -> dict[str, Any]:
    fixture, targets, figures, job, _forced = _prepare(
        fixture_path, provider, model, routing_path,
    )
    return {
        "stage": STAGE,
        "provider": provider,
        "model": model,
        "failover": False,
        "max_attempts": 1,
        "target_units": [
            {"unit_id": row["unit_id"], "unit_name": row["unit_name"], "source_characters": len(str(row.get("source_text") or ""))}
            for row in targets
        ],
        "figures": [
            {"figure_id": row["figure_id"], "pdf_page": row.get("pdf_page"), "bytes": Path(row["path"]).stat().st_size}
            for row in figures
        ],
        "prompt_characters": len(job.prompt),
        "estimated_input_tokens": job.estimated_input_tokens,
        "reserved_output_tokens": job.reserved_output_tokens,
        "expected_cases": len(fixture.get("cases") or []),
    }


def run(
    *,
    fixture_path: Path,
    provider: str,
    model: str,
    output_path: Path,
    routing_path: Path = DEFAULT_ROUTING,
) -> dict[str, Any]:
    fixture, targets, figures, job, forced = _prepare(
        fixture_path, provider, model, routing_path,
    )
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    forced_path = output_path.with_name(f".{output_path.stem}.routing.json")
    _atomic_json(forced_path, forced)
    accepted: list[Mapping[str, Any]] = []
    route_error = None
    try:
        router = LLMRouter(config_path=forced_path)

        def validate(response: Mapping[str, Any]) -> ValidationReport:
            candidate, dropped, unresolved = verify_response(targets, figures, response)
            if not candidate:
                return ValidationReport(
                    decision="reject", dropped=dropped, unresolved=unresolved,
                )
            accepted_units = {str(row.get("unit_id") or "") for row in candidate}
            return ValidationReport(
                decision="accept" if len(accepted_units) == len(targets) else "partial",
                accepted={"accepted": candidate, "dropped": dropped, "unresolved": unresolved},
                dropped=dropped,
                unresolved=unresolved,
            )

        try:
            routed = router.execute(
                LLMRequest(
                    stage=STAGE,
                    logical_job_id="gold_environment_" + job.job_id,
                    prompt=job.prompt,
                    estimated_input_tokens=job.estimated_input_tokens,
                    reserved_output_tokens=job.reserved_output_tokens,
                    required_capabilities=("text", "json", "japanese", "vision"),
                    images=tuple(
                        LLMImage(path=row["path"], mime_type=_mime_type(Path(row["path"])))
                        for row in figures
                    ),
                ),
                validate,
            )
            validated = routed.validation.accepted
            if isinstance(validated, Mapping):
                accepted = list(validated.get("accepted") or [])
        except AllProvidersFailed:
            route_error = "provider_output_rejected"
    finally:
        forced_path.unlink(missing_ok=True)

    actual_by_unit: dict[str, set[str]] = {
        str(row["unit_id"]): set() for row in fixture.get("cases") or []
    }
    for row in accepted:
        unit_id = str(row.get("unit_id") or "")
        value = (
            "not_applicable"
            if row.get("field") == "environment_applicability"
            else row.get("candidate")
        )
        if unit_id in actual_by_unit:
            actual_by_unit[unit_id].add(_item_id(unit_id, value))
    cases = []
    for source_case in fixture.get("cases") or []:
        unit_id = str(source_case["unit_id"])
        expected = _item_id(unit_id, source_case["expected_environment"])
        actual = sorted(actual_by_unit[unit_id])
        cases.append({
            "case_id": str(source_case["case_id"]),
            "validator_decision": "accept" if actual else "reject",
            "expected_items": [expected],
            "actual_items": actual,
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
        "cases": cases,
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


__all__ = ["_forced_config", "_item_id", "payload_summary", "run"]
