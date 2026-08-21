# -*- coding: utf-8 -*-
"""List fixed, zero-price OpenRouter models that accept image input."""

from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


MODELS_URL = "https://openrouter.ai/api/v1/models"


def _zero(value: Any) -> bool:
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def filter_free_vision_models(
    payload: Mapping[str, Any], *, min_context: int = 65536,
    min_output: int = 8192,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in payload.get("data") or []:
        if not isinstance(raw, Mapping):
            continue
        model_id = str(raw.get("id") or "")
        architecture = raw.get("architecture") or {}
        pricing = raw.get("pricing") or {}
        provider = raw.get("top_provider") or {}
        modalities = set(architecture.get("input_modalities") or [])
        context = int(raw.get("context_length") or provider.get("context_length") or 0)
        output = int(provider.get("max_completion_tokens") or 0)
        if (
            not model_id.endswith(":free")
            or "image" not in modalities
            or not _zero(pricing.get("prompt"))
            or not _zero(pricing.get("completion"))
            or context < min_context
            or output < min_output
        ):
            continue
        parameters = set(raw.get("supported_parameters") or [])
        rows.append({
            "model": model_id,
            "name": str(raw.get("name") or model_id),
            "context_window": context,
            "max_output_tokens": output,
            "input_modalities": sorted(modalities),
            "structured_outputs": bool(
                {"response_format", "structured_outputs"} & parameters
            ),
            "created": int(raw.get("created") or 0),
        })
    rows.sort(key=lambda row: (
        not row["structured_outputs"],
        -int(row["max_output_tokens"]),
        -int(row["context_window"]),
        -int(row["created"]),
        str(row["model"]),
    ))
    return rows


def fetch_models(url: str = MODELS_URL) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "MacroStrat-OpenRouter-Model-Audit/1.0"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("OpenRouter models endpoint returned a non-object")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-context", type=int, default=65536)
    parser.add_argument("--min-output", type=int, default=8192)
    args = parser.parse_args(argv)
    rows = filter_free_vision_models(
        fetch_models(), min_context=args.min_context, min_output=args.min_output,
    )
    document = {
        "schema_version": "openrouter-vision-candidates/1.0",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": MODELS_URL,
        "filters": {
            "fixed_model_suffix": ":free", "zero_prompt_price": True,
            "zero_completion_price": True, "requires_image_input": True,
            "min_context": args.min_context, "min_output": args.min_output,
        },
        "candidates": rows,
    }
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["filter_free_vision_models"]
