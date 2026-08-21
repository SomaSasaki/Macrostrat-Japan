# -*- coding: utf-8 -*-
"""Activate only exact-contract, currently-qualified image backup candidates.

The command is dry-run by default.  It consumes sanitized qualification
verdicts only; it never reads API keys, prompts, source text, images, or raw
responses.  Each image stage is decided independently.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from llm_qualification import status as qualification_status
from llm_router import DEFAULT_CONFIG_PATH


IMAGE_STAGES = ("column_geography_vision", "pdf_environment_multimodal")


def _read_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def activate_qualified_candidates(
    routing: Mapping[str, Any],
    qualifications: Sequence[Mapping[str, Any]],
    *,
    provider: str = "openrouter",
    model: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return an updated copy plus a secret-safe per-stage activation report."""

    document = copy.deepcopy(dict(routing))
    routes = document.get("routes")
    if not isinstance(routes, dict):
        raise ValueError("Routing config requires routes{}")
    verdicts = {
        (
            str(row.get("stage") or ""),
            str(row.get("provider") or ""),
            str(row.get("requested_model") or ""),
        ): row
        for row in qualifications
        if isinstance(row, Mapping)
    }
    results = []
    for stage in IMAGE_STAGES:
        route = routes.get(stage)
        if not isinstance(route, dict):
            raise ValueError(f"Missing image route: {stage}")
        candidates = route.get("candidates") or []
        matches = [
            row for row in candidates
            if isinstance(row, dict)
            and str(row.get("provider") or "") == provider
            and (model is None or str(row.get("model") or "") == model)
        ]
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one candidate for {stage}:{provider}:{model or '*'}")
        candidate = matches[0]
        candidate_model = str(candidate.get("model") or "")
        verdict = verdicts.get((stage, provider, candidate_model))
        expected_prompt = str(candidate.get("qualification_prompt_version") or "")
        expected_validator = str(candidate.get("qualification_validator_version") or "")
        reasons: list[str] = []
        if not candidate.get("qualification_required"):
            reasons.append("candidate_not_marked_qualification_required")
        if verdict is None:
            reasons.append("qualification_missing")
        else:
            if not bool(verdict.get("currently_qualified")):
                reasons.append("qualification_not_current")
            if str(verdict.get("prompt_version") or "") != expected_prompt:
                reasons.append("prompt_version_mismatch")
            if str(verdict.get("validator_version") or "") != expected_validator:
                reasons.append("validator_version_mismatch")
        activated = not reasons
        if activated:
            candidate["enabled"] = True
            candidate.pop("disabled_reason", None)
            route["max_failovers"] = 2
        results.append({
            "stage": stage,
            "provider": provider,
            "model": candidate_model,
            "activated": activated,
            "reasons": reasons,
            "prompt_version": expected_prompt,
            "validator_version": expected_validator,
        })
    return document, {
        "schema_version": "vision-backup-activation/1.0",
        "provider": provider,
        "model": model,
        "stages": results,
        "all_activated": all(row["activated"] for row in results),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routing", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--record-dir", type=Path, action="append", required=True)
    parser.add_argument("--provider", default="openrouter")
    parser.add_argument("--model")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    qualifications = []
    for record_dir in args.record_dir:
        qualifications.extend(qualification_status(record_dir))
    updated, report = activate_qualified_candidates(
        _read_json(args.routing), qualifications,
        provider=args.provider, model=args.model,
    )
    if args.apply:
        _atomic_json(args.routing, updated)
    print(json.dumps({**report, "applied": bool(args.apply)}, ensure_ascii=False, indent=2))
    return 0 if report["all_activated"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["IMAGE_STAGES", "activate_qualified_candidates"]
