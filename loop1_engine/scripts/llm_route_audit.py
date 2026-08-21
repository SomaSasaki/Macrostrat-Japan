# -*- coding: utf-8 -*-
"""Audit the secret-free, ordered LLM failover policy.

This command reads only public routing configuration and sanitized
qualification verdicts.  It never loads credentials or calls a provider.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from llm_qualification import DEFAULT_RECORD_DIR, status as qualification_status
from llm_router import DEFAULT_CONFIG_PATH


SCHEMA_VERSION = "llm-route-audit/1.0"
TARGET_CHAIN_SIZE = 3


def _read_json(path: str | Path) -> Mapping[str, Any]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise ValueError(f"Expected a JSON object: {path}")
    return document


def _candidate_identity(candidate: Mapping[str, Any]) -> dict[str, str]:
    return {
        "provider": str(candidate.get("provider") or ""),
        "model": str(candidate.get("model") or ""),
    }


def audit_routes(
    routing: Mapping[str, Any],
    qualifications: Sequence[Mapping[str, Any]] = (),
    *,
    target_chain_size: int = TARGET_CHAIN_SIZE,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic, secret-free routing audit document."""

    providers = routing.get("providers") or {}
    routes = routing.get("routes") or {}
    if not isinstance(providers, Mapping) or not isinstance(routes, Mapping):
        raise ValueError("Routing config must contain providers and routes objects")
    target = max(2, int(target_chain_size))
    qualification_by_key = {
        (
            str(row.get("stage") or ""),
            str(row.get("provider") or ""),
            str(row.get("requested_model") or ""),
        ): row
        for row in qualifications
        if isinstance(row, Mapping)
    }
    findings: list[dict[str, Any]] = []
    route_rows: list[dict[str, Any]] = []
    operational_providers: set[str] = set()

    for stage, raw_route in routes.items():
        if not isinstance(raw_route, Mapping):
            findings.append({
                "severity": "error", "code": "invalid_route", "stage": str(stage),
            })
            continue
        configured_enabled: list[Mapping[str, Any]] = []
        disabled: list[Mapping[str, Any]] = []
        for raw_candidate in raw_route.get("candidates") or []:
            if not isinstance(raw_candidate, Mapping):
                continue
            provider_name = str(raw_candidate.get("provider") or "")
            provider = providers.get(provider_name) or {}
            enabled = bool(
                isinstance(provider, Mapping)
                and provider.get("enabled", True)
                and raw_candidate.get("enabled", True)
            )
            (configured_enabled if enabled else disabled).append(raw_candidate)

        failovers = max(0, int(raw_route.get("max_failovers") or 0))
        chain = configured_enabled[:failovers + 1]
        standby = configured_enabled[failovers + 1:]
        operational_providers.update(str(row.get("provider") or "") for row in chain)
        route_rows.append({
            "stage": str(stage),
            "max_failovers": failovers,
            "operational_chain": [_candidate_identity(row) for row in chain],
            "standby": [_candidate_identity(row) for row in standby],
            "disabled": [
                {
                    **_candidate_identity(row),
                    "reason": str(row.get("disabled_reason") or "provider_disabled"),
                }
                for row in disabled
            ],
        })

        if len(chain) < 2:
            findings.append({
                "severity": "error",
                "code": "single_point_of_failure",
                "stage": str(stage),
                "operational_candidates": len(chain),
            })
        elif len(chain) < target:
            findings.append({
                "severity": "warning",
                "code": "thin_failover_chain",
                "stage": str(stage),
                "operational_candidates": len(chain),
                "target": target,
            })
        if len(chain) > target:
            findings.append({
                "severity": "warning",
                "code": "overdistributed_route",
                "stage": str(stage),
                "operational_candidates": len(chain),
                "target": target,
            })

        for candidate in chain:
            key = (
                str(stage),
                str(candidate.get("provider") or ""),
                str(candidate.get("model") or ""),
            )
            verdict = qualification_by_key.get(key)
            qualification_required = bool(candidate.get("qualification_required"))
            if qualification_required and verdict is None:
                findings.append({
                    "severity": "error",
                    "code": "required_qualification_missing",
                    "stage": str(stage),
                    **_candidate_identity(candidate),
                })
                continue
            if verdict is not None and not bool(verdict.get("currently_qualified")):
                findings.append({
                    "severity": "error",
                    "code": "blocked_candidate_is_operational",
                    "stage": str(stage),
                    **_candidate_identity(candidate),
                    "reasons": list(verdict.get("reasons") or []),
                })
            if verdict is not None and qualification_required:
                expected_prompt = str(candidate.get("qualification_prompt_version") or "")
                expected_validator = str(candidate.get("qualification_validator_version") or "")
                if (
                    str(verdict.get("prompt_version") or "") != expected_prompt
                    or str(verdict.get("validator_version") or "") != expected_validator
                ):
                    findings.append({
                        "severity": "error",
                        "code": "qualification_contract_mismatch",
                        "stage": str(stage),
                        **_candidate_identity(candidate),
                        "expected_prompt_version": expected_prompt,
                        "expected_validator_version": expected_validator,
                    })
            provider = providers.get(key[1]) or {}
            if isinstance(provider, Mapping) and provider.get("billing_mode") == "promotional_credit":
                findings.append({
                    "severity": "info",
                    "code": "credit_backed_candidate",
                    "stage": str(stage),
                    **_candidate_identity(candidate),
                })

    provider_rows: list[dict[str, Any]] = []
    for name, raw_provider in providers.items():
        if not isinstance(raw_provider, Mapping):
            continue
        enabled = bool(raw_provider.get("enabled", True))
        operational = str(name) in operational_providers
        state = "operational" if operational else "prepared_dormant" if enabled else "disabled"
        provider_rows.append({
            "provider": str(name),
            "state": state,
            "quota_group": str(raw_provider.get("quota_group") or name),
            "billing_mode": str(raw_provider.get("billing_mode") or "free_tier"),
        })
        if enabled and not operational:
            findings.append({
                "severity": "info",
                "code": "provider_prepared_but_dormant",
                "provider": str(name),
            })

    counts = {
        level: sum(1 for finding in findings if finding.get("severity") == level)
        for level in ("error", "warning", "info")
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "policy": {
            "mode": "ordered_failover",
            "target_operational_candidates": target,
            "parallel_hedging": False,
            "cross_provider_merging": False,
        },
        "summary": counts,
        "routes": route_rows,
        "providers": provider_rows,
        "findings": findings,
    }


def _print_human(report: Mapping[str, Any]) -> None:
    for route in report.get("routes") or []:
        chain = " -> ".join(
            str(row.get("provider")) for row in route.get("operational_chain") or []
        ) or "(none)"
        standby = ", ".join(
            str(row.get("provider")) for row in route.get("standby") or []
        )
        print(f"{route.get('stage')}: {chain}")
        if standby:
            print(f"  standby: {standby}")
    summary = report.get("summary") or {}
    print(
        "findings: "
        f"{summary.get('error', 0)} error / "
        f"{summary.get('warning', 0)} warning / "
        f"{summary.get('info', 0)} info"
    )
    for finding in report.get("findings") or []:
        if finding.get("severity") in {"error", "warning"}:
            subject = finding.get("stage") or finding.get("provider") or "routing"
            print(f"  [{finding.get('severity')}] {subject}: {finding.get('code')}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routing", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--record-dir", type=Path, default=DEFAULT_RECORD_DIR)
    parser.add_argument(
        "--additional-record-dir", type=Path, action="append", default=[],
        help="Additional sanitized qualification directories (for exact constrained contracts).",
    )
    parser.add_argument("--target-chain-size", type=int, default=TARGET_CHAIN_SIZE)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    qualifications = list(qualification_status(args.record_dir))
    for record_dir in args.additional_record_dir:
        qualifications.extend(qualification_status(record_dir))
    report = audit_routes(
        _read_json(args.routing),
        qualifications,
        target_chain_size=args.target_chain_size,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 1 if args.strict and int(report["summary"]["error"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SCHEMA_VERSION", "TARGET_CHAIN_SIZE", "audit_routes"]
