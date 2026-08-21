# -*- coding: utf-8 -*-
"""Evaluate secret-safe provider GOLD summaries against routing policy.

This command never calls an LLM.  It consumes a connectivity-probe report and
a normalized GOLD result document containing only stable item IDs and validator
decisions.  Prompts, source text, images and raw model responses are rejected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = ROOT / "config" / "llm_qualification.json"
DEFAULT_ROUTING_PATH = ROOT / "config" / "llm_routing.json"
DEFAULT_RECORD_DIR = ROOT / "data" / "50k" / "00_management" / "llm_qualification"
RESULT_SCHEMA = "llm-gold-results/1.0"
VERDICT_SCHEMA = "llm-qualification-verdict/1.0"
FORBIDDEN_KEYS = {
    "api_key", "authorization", "image", "image_data", "images", "prompt",
    "raw", "raw_response", "response", "source", "source_text",
}
GOLD_KEYS = {
    "schema_version", "generated_at", "stage", "provider", "requested_model",
    "prompt_version", "validator_version", "cases",
    # Two-stage Column runs record which supplied Column IDs the provider said
    # were visible.  It is a boolean map over IDs the reviewer supplied, so it
    # carries no prompt, response or secret material.  It is validated below.
    "column_detection",
    # 画像の描画倍率。数値のみで、比較のときにどの条件の測定かを識別するために残す。
    "render_scale",
}
CASE_KEYS = {
    "case_id", "validator_decision", "expected_items", "actual_items",
    "critical_failures",
}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/|@+\-]{0,159}$")


class QualificationError(ValueError):
    """The supplied evidence cannot be evaluated safely or deterministically."""


def _read_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualificationError(f"Cannot read JSON: {path}") from exc


def _utc(value: Any, label: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise QualificationError(f"{label} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QualificationError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise QualificationError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _assert_secret_safe(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
            if normalized in FORBIDDEN_KEYS:
                raise QualificationError(f"Forbidden content key at {path}.{key}")
            _assert_secret_safe(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_secret_safe(child, f"{path}[{index}]")


def _policy_for(config: Mapping[str, Any], stage: str) -> dict[str, Any]:
    stages = config.get("stages") or {}
    override = stages.get(stage) if isinstance(stages, Mapping) else None
    if not isinstance(override, Mapping):
        raise QualificationError(f"No qualification policy for stage: {stage}")
    default = config.get("default_policy") or {}
    return {**(dict(default) if isinstance(default, Mapping) else {}), **dict(override)}


def _route_candidate(
    routing: Mapping[str, Any], stage: str, provider: str, model: str,
) -> Mapping[str, Any] | None:
    routes = routing.get("routes") or {}
    route = routes.get(stage) if isinstance(routes, Mapping) else None
    if not isinstance(route, Mapping):
        return None
    for candidate in route.get("candidates") or []:
        if (
            isinstance(candidate, Mapping)
            and str(candidate.get("provider") or "") == provider
            and str(candidate.get("model") or "") == model
        ):
            return candidate
    return None


def _probe_rows(document: Any) -> tuple[datetime, list[Mapping[str, Any]]]:
    if isinstance(document, list):
        raise QualificationError("Probe report must include generated_at metadata")
    if not isinstance(document, Mapping):
        raise QualificationError("Probe report must be a JSON object")
    if str(document.get("schema_version") or "") != "llm-probe-results/1.0":
        raise QualificationError("Unsupported probe report schema_version")
    generated_at = _utc(document.get("generated_at"), "probe.generated_at")
    rows = document.get("results") or []
    if not isinstance(rows, list):
        raise QualificationError("probe.results must be a list")
    return generated_at, [row for row in rows if isinstance(row, Mapping)]


def _safe_ids(value: Any, label: str) -> set[str]:
    if not isinstance(value, list):
        raise QualificationError(f"{label} must be a list")
    result: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not SAFE_ID.fullmatch(text):
            raise QualificationError(f"{label} contains an invalid item ID")
        result.add(text)
    return result


def evaluate(
    gold: Mapping[str, Any],
    probe: Mapping[str, Any],
    *,
    policy_config: Mapping[str, Any],
    routing_config: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a deterministic, persistence-safe qualification verdict."""
    _assert_secret_safe(gold)
    unknown_gold_keys = set(gold) - GOLD_KEYS
    if unknown_gold_keys:
        raise QualificationError(
            f"Unexpected gold keys: {', '.join(sorted(map(str, unknown_gold_keys)))}"
        )
    if str(gold.get("schema_version") or "") != RESULT_SCHEMA:
        raise QualificationError(f"gold.schema_version must be {RESULT_SCHEMA}")
    scale = gold.get("render_scale")
    if scale is not None and (isinstance(scale, bool) or not isinstance(scale, (int, float))):
        raise QualificationError("gold.render_scale must be a number")
    detection = gold.get("column_detection")
    if detection is not None:
        if not isinstance(detection, Mapping):
            raise QualificationError("gold.column_detection must be an object")
        for key, value in detection.items():
            if not SAFE_ID.fullmatch(str(key)) or not isinstance(value, bool):
                raise QualificationError(
                    "gold.column_detection must map supplied Column IDs to booleans"
                )
    stage = str(gold.get("stage") or "")
    provider = str(gold.get("provider") or "")
    model = str(gold.get("requested_model") or "")
    if not stage or not provider or not model:
        raise QualificationError("stage, provider and requested_model are required")
    policy = _policy_for(policy_config, stage)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    reasons: list[str] = []

    candidate = _route_candidate(routing_config, stage, provider, model)
    if candidate is None:
        reasons.append("not_configured_for_route")
    expected_actual_model = str(
        candidate.get("expected_actual_model") or model
    ) if candidate is not None else model

    if str(gold.get("prompt_version") or "") != str(policy.get("prompt_version") or ""):
        reasons.append("stale_prompt_version")
    if str(gold.get("validator_version") or "") != str(policy.get("validator_version") or ""):
        reasons.append("stale_validator_version")

    gold_at = _utc(gold.get("generated_at"), "gold.generated_at")
    gold_age = (current - gold_at).total_seconds() / 86400
    if gold_age < -1 / 24 or gold_age > float(policy_config.get("gold_max_age_days") or 90):
        reasons.append("gold_expired")

    probe_at, probe_rows = _probe_rows(probe)
    required_capability = str(policy.get("required_probe_capability") or "text")
    matching_probe = next((
        row for row in probe_rows
        if str(row.get("provider") or "") == provider
        and str(row.get("requested_model") or "") == model
        and required_capability in set(row.get("capabilities") or ["text"])
    ), None)
    if matching_probe is None:
        reasons.append("matching_probe_missing")
    else:
        if not bool(matching_probe.get("ok")):
            reasons.append("probe_failed")
        actual_model = str(matching_probe.get("actual_model") or model)
        if actual_model != expected_actual_model:
            reasons.append("actual_model_mismatch")
    probe_age = (current - probe_at).total_seconds() / 86400
    if probe_age < -1 / 24 or probe_age > float(policy_config.get("probe_max_age_days") or 14):
        reasons.append("probe_expired")

    cases = gold.get("cases") or []
    if not isinstance(cases, list):
        raise QualificationError("gold.cases must be a list")
    case_ids: set[str] = set()
    true_positive = false_positive = false_negative = 0
    validator_passes = critical_failures = expected_count = 0
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise QualificationError(f"gold.cases[{index}] must be an object")
        unknown_case_keys = set(case) - CASE_KEYS
        if unknown_case_keys:
            raise QualificationError(
                f"Unexpected case keys: {', '.join(sorted(map(str, unknown_case_keys)))}"
            )
        case_id = str(case.get("case_id") or "").strip()
        if not SAFE_ID.fullmatch(case_id) or case_id in case_ids:
            raise QualificationError("case_id values must be safe, non-empty and unique")
        case_ids.add(case_id)
        expected = _safe_ids(case.get("expected_items"), f"cases[{index}].expected_items")
        actual = _safe_ids(case.get("actual_items"), f"cases[{index}].actual_items")
        expected_count += len(expected)
        true_positive += len(expected & actual)
        false_positive += len(actual - expected)
        false_negative += len(expected - actual)
        if str(case.get("validator_decision") or "") in {"accept", "partial"}:
            validator_passes += 1
        failures = _safe_ids(
            case.get("critical_failures") or [],
            f"cases[{index}].critical_failures",
        )
        critical_failures += len(failures)

    case_count = len(cases)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 1.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 1.0
    validator_rate = validator_passes / case_count if case_count else 0.0
    thresholds = {
        "min_cases": int(policy.get("min_cases") or 0),
        "min_expected_items": int(policy.get("min_expected_items") or 0),
        "min_validator_pass_rate": float(policy.get("min_validator_pass_rate") or 0),
        "min_precision": float(policy.get("min_precision") or 0),
        "min_recall": float(policy.get("min_recall") or 0),
        "max_critical_failures": int(policy.get("max_critical_failures") or 0),
    }
    if case_count < thresholds["min_cases"]:
        reasons.append("insufficient_cases")
    if expected_count < thresholds["min_expected_items"]:
        reasons.append("insufficient_expected_items")
    if validator_rate < thresholds["min_validator_pass_rate"]:
        reasons.append("validator_pass_rate_below_threshold")
    if precision < thresholds["min_precision"]:
        reasons.append("precision_below_threshold")
    if recall < thresholds["min_recall"]:
        reasons.append("recall_below_threshold")
    if critical_failures > thresholds["max_critical_failures"]:
        reasons.append("critical_failures")

    reasons = list(dict.fromkeys(reasons))
    valid_until = min(
        gold_at + timedelta(days=float(policy_config.get("gold_max_age_days") or 90)),
        probe_at + timedelta(days=float(policy_config.get("probe_max_age_days") or 14)),
    )
    return {
        "schema_version": VERDICT_SCHEMA,
        "evaluated_at": current.isoformat().replace("+00:00", "Z"),
        "stage": stage,
        "provider": provider,
        "requested_model": model,
        "qualified": not reasons,
        "valid_until": valid_until.isoformat().replace("+00:00", "Z"),
        "route_enabled": bool(candidate is not None and candidate.get("enabled", True)),
        "required_probe_capability": required_capability,
        "prompt_version": gold.get("prompt_version"),
        "validator_version": gold.get("validator_version"),
        "metrics": {
            "cases": case_count,
            "expected_items": expected_count,
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "validator_pass_rate": round(validator_rate, 6),
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "critical_failures": critical_failures,
        },
        "thresholds": thresholds,
        "probe": {
            "generated_at": probe_at.isoformat().replace("+00:00", "Z"),
            "ok": bool(matching_probe and matching_probe.get("ok")),
            "actual_model": matching_probe.get("actual_model") if matching_probe else None,
        },
        "reasons": reasons,
    }


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _record_path(record_dir: Path, verdict: Mapping[str, Any]) -> Path:
    identity = "|".join(str(verdict.get(key) or "") for key in ("stage", "provider", "requested_model"))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return record_dir / f"qualification_{digest}.json"


def status(
    record_dir: Path, *, now: datetime | None = None,
) -> list[Mapping[str, Any]]:
    if not record_dir.is_dir():
        return []
    rows: list[Mapping[str, Any]] = []
    for path in sorted(record_dir.glob("qualification_*.json")):
        document = _read_json(path)
        if isinstance(document, Mapping) and document.get("schema_version") == VERDICT_SCHEMA:
            current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            valid_until = _utc(document.get("valid_until"), "record.valid_until")
            row = dict(document)
            row["currently_qualified"] = bool(document.get("qualified") and current <= valid_until)
            rows.append(row)
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gold", nargs="?", type=Path, help="Secret-safe GOLD result JSON")
    parser.add_argument("--probe-results", type=Path)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--routing", type=Path, default=DEFAULT_ROUTING_PATH)
    parser.add_argument("--record-dir", type=Path, default=DEFAULT_RECORD_DIR)
    parser.add_argument("--record", action="store_true", help="Persist only the sanitized verdict")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.status:
        rows = status(args.record_dir)
        print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else "\n".join(
            f"{row.get('stage')} {row.get('provider')}:{row.get('requested_model')} "
            f"{'QUALIFIED' if row.get('currently_qualified') else 'BLOCKED'}"
            for row in rows
        ) or "No qualification records.")
        return 0
    if args.gold is None or args.probe_results is None:
        parser.error("gold and --probe-results are required unless --status is used")
    try:
        verdict = evaluate(
            _read_json(args.gold),
            _read_json(args.probe_results),
            policy_config=_read_json(args.policy),
            routing_config=_read_json(args.routing),
        )
    except QualificationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.record:
        path = _record_path(args.record_dir, verdict)
        _atomic_json(path, verdict)
    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
    else:
        print(
            f"{verdict['stage']} {verdict['provider']}:{verdict['requested_model']} "
            f"{'QUALIFIED' if verdict['qualified'] else 'BLOCKED'}"
        )
        for reason in verdict["reasons"]:
            print(f"  - {reason}")
    return 0 if verdict["qualified"] else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["QualificationError", "evaluate", "status"]
