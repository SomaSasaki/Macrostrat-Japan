# -*- coding: utf-8 -*-
"""Run one provider through the production PDF-alias validator and GOLD set.

Only opaque mapping IDs and validator decisions are persisted.  English/Japanese
names, TOC text, prompts and accepted provider rows stay in memory for the run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from llm_router import AllProvidersFailed, LLMRequest, LLMRouter
from pdf_alias_mapping import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    STAGE,
    build_prompt,
    validate_alias_response,
)
from pdf_locate import normalize
from gold_snapshot import bound_path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE = ROOT / "config" / "llm_gold_alias_mapping.json"
DEFAULT_ROUTING = ROOT / "config" / "llm_routing.json"


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise_english(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode().casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _mapping_id(unit_id: Any, japanese_alias: Any) -> str:
    alias = unicodedata.normalize("NFKC", str(japanese_alias or "")).strip()
    identity = f"{str(unit_id or '').strip()}|{alias}"
    return "alias_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    os.replace(temporary, path)


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


def _mappings(fixture: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        mapping
        for case in fixture.get("cases") or []
        for mapping in case.get("mappings") or []
        if isinstance(mapping, Mapping)
    ]


def _alias_table(fixture: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "map_id": str(fixture["map_id"]),
        "units": [{
            "unit_id": str(mapping["unit_id"]),
            "unit_name": str(mapping["unit_name"]),
            "aliases": [str(mapping["unit_name"])],
            "japanese_aliases": [],
            "status": "alias_mapping_required",
        } for mapping in _mappings(fixture)],
    }


def _compiled_names(compiled: Mapping[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for unit in compiled.get("units") or []:
        if not isinstance(unit, Mapping):
            continue
        unit_id = str(unit.get("unit_id") or "")
        for container_name in ("review_values", "values"):
            container = unit.get(container_name)
            if isinstance(container, Mapping) and container.get("unit_name"):
                result.setdefault(unit_id, set()).add(
                    _normalise_english(container["unit_name"])
                )
    return result


def _validate_fixture(
    fixture: Mapping[str, Any],
    *,
    compiled: Mapping[str, Any],
    pdf_index: Mapping[str, Any],
) -> None:
    if fixture.get("schema_version") != "alias-mapping-gold-fixture/1.0":
        raise ValueError("Unsupported alias-mapping GOLD fixture")
    cases = fixture.get("cases") or []
    if not isinstance(cases, list) or not cases:
        raise ValueError("Alias-mapping GOLD fixture has no cases")
    pages = pdf_index.get("pages") if isinstance(pdf_index.get("pages"), list) else []
    known_names = _compiled_names(compiled)
    case_ids: set[str] = set()
    unit_ids: set[str] = set()
    mapping_ids: set[str] = set()
    for case in cases:
        if not isinstance(case, Mapping):
            raise ValueError("Alias-mapping GOLD cases must be objects")
        case_id = str(case.get("case_id") or "").strip()
        mappings = case.get("mappings") or []
        if not case_id or case_id in case_ids or not isinstance(mappings, list) or not mappings:
            raise ValueError("Alias-mapping GOLD cases require unique IDs and mappings")
        case_ids.add(case_id)
        for mapping in mappings:
            if not isinstance(mapping, Mapping):
                raise ValueError("Alias-mapping GOLD mappings must be objects")
            unit_id = str(mapping.get("unit_id") or "")
            unit_name = str(mapping.get("unit_name") or "")
            alias = str(mapping.get("japanese_alias") or "").strip()
            quote = str(mapping.get("toc_quote") or "").strip()
            try:
                page_number = int(mapping.get("pdf_page"))
            except (TypeError, ValueError) as exc:
                raise ValueError("Alias-mapping GOLD page must be an integer") from exc
            if not unit_id or unit_id in unit_ids or not unit_name or not alias or not quote:
                raise ValueError("Alias-mapping GOLD mappings must be complete and unique")
            if _normalise_english(unit_name) not in known_names.get(unit_id, set()):
                raise ValueError(f"GOLD unit does not match compiled source: {unit_id}")
            page = str(pages[page_number - 1] or "") if 1 <= page_number <= len(pages) else ""
            if (
                not page
                or normalize(alias) not in normalize(page)
                or normalize(quote) not in normalize(page)
                or normalize(alias) not in normalize(quote)
            ):
                raise ValueError(f"GOLD alias/quote is not on cited page: {unit_id}")
            identity = _mapping_id(unit_id, alias)
            if identity in mapping_ids:
                raise ValueError("Alias-mapping GOLD contains duplicate identities")
            unit_ids.add(unit_id)
            mapping_ids.add(identity)


def run(
    *,
    workspace: Path,
    fixture_path: Path,
    provider: str,
    model: str,
    output_path: Path,
    routing_path: Path = DEFAULT_ROUTING,
    output_tokens: int = 2048,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    fixture = _read(fixture_path.resolve())
    if not isinstance(fixture, Mapping):
        raise ValueError("Alias-mapping GOLD fixture must be a JSON object")
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
    compiled = _read(compiled_path)
    _validate_fixture(fixture, compiled=compiled, pdf_index=pdf_index)
    alias_table = _alias_table(fixture)
    prompt = build_prompt(alias_table, pdf_index)
    input_tokens = max(1, math.ceil(len(prompt.encode("utf-8")) / 3))
    output_tokens = max(1, int(output_tokens))
    job_id = "gold_alias_" + hashlib.sha256(
        (str(fixture["map_id"]) + "|" + hashlib.sha256(prompt.encode("utf-8")).hexdigest())
        .encode("utf-8")
    ).hexdigest()[:20]
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    validator_decision = "reject"
    route_error: str | None = None
    accepted: Sequence[Mapping[str, Any]] = ()
    with tempfile.TemporaryDirectory(prefix="alias_gold_", dir=output_path.parent) as temp:
        forced_path = Path(temp) / "routing.json"
        _atomic_json(
            forced_path,
            _forced_config(_read(routing_path.resolve()), provider, model),
        )
        router = LLMRouter(config_path=forced_path)
        try:
            routed = router.execute(
                LLMRequest(
                    stage=STAGE,
                    logical_job_id=job_id,
                    prompt=prompt,
                    estimated_input_tokens=input_tokens,
                    reserved_output_tokens=output_tokens,
                    required_capabilities=("text", "json", "japanese"),
                ),
                lambda response: validate_alias_response(alias_table, pdf_index, response),
            )
            validator_decision = routed.validation.decision
            accepted = tuple(
                row for row in routed.validation.accepted or []
                if isinstance(row, Mapping)
            )
        except AllProvidersFailed:
            route_error = "provider_output_rejected"

    actual_by_case: dict[str, set[str]] = {
        str(case["case_id"]): set() for case in fixture["cases"]
    }
    case_by_unit = {
        str(mapping["unit_id"]): str(case["case_id"])
        for case in fixture["cases"]
        for mapping in case["mappings"]
    }
    for row in accepted:
        unit_id = str(row.get("unit_id") or "")
        case_id = case_by_unit.get(unit_id)
        if case_id:
            actual_by_case[case_id].add(
                _mapping_id(unit_id, row.get("japanese_alias"))
            )

    cases = []
    for source_case in fixture["cases"]:
        cases.append({
            "case_id": str(source_case["case_id"]),
            "validator_decision": validator_decision,
            "expected_items": sorted(
                _mapping_id(row["unit_id"], row["japanese_alias"])
                for row in source_case["mappings"]
            ),
            "actual_items": sorted(actual_by_case[str(source_case["case_id"])]),
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
    parser.add_argument(
        "--output-tokens", type=int, default=2048,
        help="Reserved provider output cap; default: 2048.",
    )
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
