# -*- coding: utf-8 -*-
"""Run one provider through the production Column Vision validator and GOLD."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from column_geography import canonical_units
from llm_column_vision import (
    PROMPT_VERSION,
    STAGE,
    VALIDATOR_VERSION,
    build_job,
    run_column_vision,
)
from llm_router import AllProvidersFailed, LLMRouter
from pdf_image_extract import _render_pages
from pilot_llm import _read_pdf_index
from gold_snapshot import bound_path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE = ROOT / "config" / "llm_gold_column_vision.json"
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
    import re
    text = " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())
    return re.sub(r"\bflood\s+plain\b", "floodplain", text)


def _membership_id(name: Any, column_id: Any) -> str:
    identity = f"{_normalise_name(name)}|{str(column_id or '').strip()}"
    return "membership_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


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


def _route_failure_code(exc: AllProvidersFailed) -> str:
    last_attempt = exc.attempts[-1] if exc.attempts else {}
    error_kind = str(last_attempt.get("error_kind") or "output_rejected")
    return f"provider_{error_kind}"


def _source_context(workspace: Path, fixture_path: Path) -> tuple[
    dict[str, Any], Path, Path, Path, list[dict[str, Any]]
]:
    workspace = workspace.resolve()
    fixture = _read(fixture_path.resolve())
    if fixture.get("schema_version") != "column-vision-gold-fixture/1.0":
        raise ValueError("Unsupported Column Vision GOLD fixture")
    workbook = bound_path(
        fixture, "source_workbook",
        expected_sha256=str(fixture["source_workbook_sha256"]),
    )
    pdf = bound_path(fixture, "source_pdf")
    abstract = bound_path(fixture, "source_abstract")
    index_path = bound_path(fixture, "source_pdf_index")
    unit_inventory_path = bound_path(
        fixture, "raw_bundle",
        expected_sha256=str(fixture["unit_inventory_sha256"]),
    )
    for required in (pdf, abstract, index_path, unit_inventory_path):
        if not required.is_file():
            raise FileNotFoundError(required)
    if _sha256(unit_inventory_path) != fixture.get("unit_inventory_sha256"):
        raise ValueError("Bound GOLD unit inventory changed")
    units = canonical_units(_read(unit_inventory_path))
    if not units:
        raise ValueError("Bound GOLD unit inventory is empty")
    return fixture, pdf, abstract, index_path, units


def payload_summary(
    workspace: Path,
    fixture_path: Path,
    provider: str,
    model: str,
    routing_path: Path = DEFAULT_ROUTING,
) -> dict[str, Any]:
    fixture, pdf, abstract, _index_path, units = _source_context(workspace, fixture_path)
    forced = _forced_config(_read(routing_path.resolve()), provider, model)
    candidate = forced["routes"][STAGE]["candidates"][0]
    with tempfile.TemporaryDirectory(prefix="column_vision_gold_dry_") as temp:
        rendered = _render_pages(
            pdf, Path(temp) / "rendered", [int(fixture["pdf_page"])], scale=2.0,
        )[0]
        job = build_job(
            map_id=str(fixture["map_id"]),
            pdf_path=pdf,
            image_path=rendered,
            pdf_page=int(fixture["pdf_page"]),
            printed_page=int(fixture["printed_page"]),
            report_text=abstract.read_text(encoding="utf-8"),
            units=units,
            expected_columns=fixture.get("expected_columns") or [],
            model=model,
        )
        image_bytes = rendered.stat().st_size
    expected_memberships = sum(
        len(row.get("expected_items") or []) for row in fixture.get("cases") or []
    )
    return {
        "stage": STAGE,
        "provider": provider,
        "model": model,
        "failover": False,
        "max_attempts": 1,
        "images": [{"pdf_page": int(fixture["pdf_page"]), "bytes": image_bytes}],
        "canonical_units": len(units),
        "expected_columns": len(fixture.get("expected_columns") or []),
        "expected_memberships": expected_memberships,
        "prompt_characters": len(job.prompt),
        "estimated_input_tokens": job.estimated_input_tokens,
        "reserved_output_tokens": job.reserved_output_tokens,
        "candidate_max_output_tokens": candidate.get("max_output_tokens"),
        "output_capacity_ok": (
            candidate.get("max_output_tokens") is None
            or int(candidate["max_output_tokens"]) >= job.reserved_output_tokens
        ),
    }


def run(
    *,
    workspace: Path,
    fixture_path: Path,
    provider: str,
    model: str,
    output_path: Path,
    routing_path: Path = DEFAULT_ROUTING,
) -> dict[str, Any]:
    fixture, pdf, abstract, index_path, units = _source_context(workspace, fixture_path)
    expected_columns = fixture.get("expected_columns") or []
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="column_vision_gold_", dir=output_path.parent) as temp:
        scratch = Path(temp)
        rendered = _render_pages(
            pdf, scratch / "rendered", [int(fixture["pdf_page"])], scale=2.0,
        )[0]
        forced_path = scratch / "routing.json"
        _atomic_json(forced_path, _forced_config(_read(routing_path.resolve()), provider, model))
        router = LLMRouter(config_path=forced_path)
        proposal: Mapping[str, Any] = {"columns": [], "units": [], "unassigned_units": []}
        validator_decision = "reject"
        route_error = None
        try:
            result = run_column_vision(
                map_id=str(fixture["map_id"]),
                pdf_path=pdf,
                image_path=rendered,
                pdf_page=int(fixture["pdf_page"]),
                printed_page=int(fixture["printed_page"]),
                report_text=abstract.read_text(encoding="utf-8"),
                units=units,
                expected_columns=expected_columns,
                pdf_index=_read_pdf_index(index_path),
                cache_dir=scratch / "cache",
                output_dir=scratch / "output",
                router=router,
                generated_at=generated_at,
            )
            proposal = result.proposal
            validator_decision = "partial" if proposal.get("unassigned_units") else "accept"
            actual_model = str(result.manifest.get("actual_model") or model)
        except AllProvidersFailed as exc:
            route_error = _route_failure_code(exc)
            actual_model = model

        actual_by_column: dict[str, set[str]] = {
            str(case["column_id"]): set() for case in fixture.get("cases") or []
        }
        for unit in proposal.get("units") or []:
            if not isinstance(unit, Mapping):
                continue
            for membership in unit.get("memberships") or []:
                if not isinstance(membership, Mapping):
                    continue
                column_id = str(membership.get("column_id") or "")
                if column_id in actual_by_column:
                    actual_by_column[column_id].add(
                        _membership_id(unit.get("unit_name"), column_id)
                    )
        present_columns = {
            str(row.get("column_id") or "")
            for row in proposal.get("columns") or [] if isinstance(row, Mapping)
        }
        cases = []
        for source_case in fixture.get("cases") or []:
            column_id = str(source_case["column_id"])
            failures = []
            if route_error:
                failures.append(route_error)
            elif column_id not in present_columns:
                failures.append("required_column_missing")
            cases.append({
                "case_id": str(source_case["case_id"]),
                "validator_decision": validator_decision,
                "expected_items": list(source_case.get("expected_items") or []),
                "actual_items": sorted(actual_by_column[column_id]),
                "critical_failures": failures,
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
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--routing", type=Path, default=DEFAULT_ROUTING)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        result = payload_summary(
            args.workspace, args.fixture, args.provider, args.model, args.routing,
        )
    else:
        if args.output is None:
            parser.error("--output is required unless --dry-run is used")
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


__all__ = [
    "_forced_config", "_membership_id", "_route_failure_code", "payload_summary", "run",
]
