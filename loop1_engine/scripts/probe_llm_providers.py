# -*- coding: utf-8 -*-
"""Minimal, secret-safe connectivity probes for configured LLM providers.

Each probe sends only a fixed request asking for ``OK``.  No project source
material is transmitted and API keys are never printed.
"""

from __future__ import annotations

import argparse
import binascii
import json
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from common import load_secret
from llm_router import HTTPAdapter, LLMImage, _parse_json_block
from llm_runtime import BudgetUnavailable, LLMRuntimeStore


PROMPT = "Reply with exactly: OK"
TIMEOUT = 90
ROUTING_PATH = Path(__file__).resolve().parents[2] / "config" / "llm_routing.json"
RUNTIME_FACTORY = LLMRuntimeStore


@dataclass(frozen=True)
class ProbeResult:
    provider: str
    configured: bool
    ok: bool
    http_status: int | None = None
    requested_model: str | None = None
    actual_model: str | None = None
    response: str | None = None
    error: str | None = None
    latency_ms: int | None = None
    capabilities: tuple[str, ...] = ("text",)
    image_count: int = 0


def _request_json(
    url: str,
    *,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    timeout: int = TIMEOUT,
) -> tuple[int, dict[str, Any]]:
    request_headers = {
        "User-Agent": "MacroStrat-LLM-Probe/1.0",
        **dict(headers),
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("API response was not a JSON object")
        return int(response.status), payload


def _openai_text(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], Mapping):
        return ""
    message = choices[0].get("message") or {}
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, Mapping)
        )
    return ""


def _gemini_text(payload: Mapping[str, Any]) -> str:
    chunks: list[str] = []
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        content = candidate.get("content") or {}
        if not isinstance(content, Mapping):
            continue
        for part in content.get("parts") or []:
            if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "".join(chunks)


def _cohere_text(payload: Mapping[str, Any]) -> str:
    message = payload.get("message") or {}
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    chunks = []
    for part in content or []:
        if isinstance(part, Mapping) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "".join(chunks)


def _error_message(body: bytes, secrets: tuple[str, ...]) -> str:
    text = body.decode("utf-8", "replace")[:2000]
    try:
        payload = json.loads(text)
        error = payload.get("error") if isinstance(payload, Mapping) else None
        if isinstance(error, Mapping):
            text = str(error.get("message") or error.get("code") or "API error")
        elif error:
            text = str(error)
        elif isinstance(payload, Mapping):
            text = str(payload.get("message") or payload.get("detail") or "API error")
    except json.JSONDecodeError:
        pass
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<KEY>")
    return " ".join(text.split())[:500] or "API error"


def _probe(
    provider: str,
    secret_name: str,
    env_var: str,
    requested_model: str,
    execute: Callable[[str], tuple[int, Mapping[str, Any], str, str | None]],
    *,
    capabilities: tuple[str, ...] = ("text",),
    image_count: int = 0,
) -> ProbeResult:
    key = load_secret(secret_name, env_var)
    if not key:
        return ProbeResult(
            provider=provider,
            configured=False,
            ok=False,
            requested_model=requested_model,
            error="API key is not configured",
            capabilities=capabilities,
            image_count=image_count,
        )
    routing = json.loads(ROUTING_PATH.read_text(encoding="utf-8"))
    provider_config = (routing.get("providers") or {}).get(provider) or {}
    runtime = RUNTIME_FACTORY()
    try:
        reservation = runtime.reserve(
            provider=provider,
            model=requested_model,
            quota_group=str(provider_config.get("quota_group") or provider),
            stage=("provider_vision_probe" if image_count else "provider_text_probe"),
            logical_job_id=f"probe:{provider}:{requested_model}:{uuid.uuid4().hex}",
            estimated_tokens=128,
            limits=(provider_config.get("limits") if isinstance(provider_config, Mapping) else {}),
            reset_timezone=str(provider_config.get("reset_timezone") or "UTC"),
            ttl_seconds=TIMEOUT + 60,
        )
    except BudgetUnavailable as exc:
        return ProbeResult(
            provider=provider,
            configured=True,
            ok=False,
            requested_model=requested_model,
            error=f"Local probe budget unavailable: {exc}",
            capabilities=capabilities,
            image_count=image_count,
        )
    started = time.monotonic()
    try:
        status, payload, text, actual_model = execute(key)
        latency_ms = round((time.monotonic() - started) * 1000)
        clean = " ".join(text.split())[:80]
        result = ProbeResult(
            provider=provider,
            configured=True,
            ok=clean.upper() == "OK",
            http_status=status,
            requested_model=requested_model,
            actual_model=actual_model or requested_model,
            response=clean or None,
            error=(
                None
                if clean.upper() == "OK"
                else f"Unexpected response: {clean}" if clean else "Response contained no text"
            ),
            latency_ms=latency_ms,
            capabilities=capabilities,
            image_count=image_count,
        )
        runtime.finalize(
            reservation,
            status="accepted" if result.ok else "rejected",
            actual_model=result.actual_model,
            http_status=status,
            validation_decision="accept" if result.ok else "reject",
            error_kind=None if result.ok else "probe_validation",
            error_message=result.error,
        )
        return result
    except urllib.error.HTTPError as exc:
        latency_ms = round((time.monotonic() - started) * 1000)
        body = exc.read(4096)
        result = ProbeResult(
            provider=provider,
            configured=True,
            ok=False,
            http_status=int(exc.code),
            requested_model=requested_model,
            error=_error_message(body, (key,)),
            latency_ms=latency_ms,
            capabilities=capabilities,
            image_count=image_count,
        )
        runtime.finalize(
            reservation,
            status="error",
            actual_model=requested_model,
            http_status=int(exc.code),
            error_kind="probe_http",
            error_message=result.error,
        )
        return result
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        latency_ms = round((time.monotonic() - started) * 1000)
        message = str(getattr(exc, "reason", exc))
        message = message.replace(key, "<KEY>")
        result = ProbeResult(
            provider=provider,
            configured=True,
            ok=False,
            requested_model=requested_model,
            error=" ".join(message.split())[:500],
            latency_ms=latency_ms,
            capabilities=capabilities,
            image_count=image_count,
        )
        runtime.finalize(
            reservation,
            status="error",
            actual_model=requested_model,
            error_kind="probe_transport",
            error_message=result.error,
        )
        return result


def probe_gemini() -> ProbeResult:
    model = "gemini-3.5-flash-lite"

    def execute(key: str) -> tuple[int, Mapping[str, Any], str, str | None]:
        status, payload = _request_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
            headers={"Content-Type": "application/json"},
            body={
                "contents": [{"parts": [{"text": PROMPT}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 64},
            },
        )
        return status, payload, _gemini_text(payload), model

    return _probe("gemini", "gemini_api_key", "GEMINI_API_KEY", model, execute)


def probe_cohere() -> ProbeResult:
    model = "command-a-plus-05-2026"

    def execute(key: str) -> tuple[int, Mapping[str, Any], str, str | None]:
        status, payload = _request_json(
            "https://api.cohere.com/v2/chat",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            body={
                "model": model,
                "messages": [{"role": "user", "content": PROMPT}],
                "temperature": 0,
                "max_tokens": 256,
            },
        )
        return status, payload, _cohere_text(payload), model

    return _probe("cohere", "cohere_api_key", "COHERE_API_KEY", model, execute)


def _openai_probe(
    provider: str,
    secret_name: str,
    env_var: str,
    url: str,
    model: str,
) -> ProbeResult:
    def execute(key: str) -> tuple[int, Mapping[str, Any], str, str | None]:
        status, payload = _request_json(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            body={
                "model": model,
                "messages": [{"role": "user", "content": PROMPT}],
                "temperature": 0,
                "max_tokens": 64,
            },
        )
        actual = payload.get("model")
        return status, payload, _openai_text(payload), str(actual) if actual else model

    return _probe(provider, secret_name, env_var, model, execute)


def probe_nvidia() -> ProbeResult:
    return _openai_probe(
        "nvidia",
        "nvidia_api_key",
        "NVIDIA_API_KEY",
        "https://integrate.api.nvidia.com/v1/chat/completions",
        "nvidia/nemotron-3-nano-30b-a3b",
    )


def probe_groq() -> ProbeResult:
    return _openai_probe(
        "groq",
        "groq_api_key",
        "GROQ_API_KEY",
        "https://api.groq.com/openai/v1/chat/completions",
        "llama-3.3-70b-versatile",
    )


def probe_openrouter() -> ProbeResult:
    return _openai_probe(
        "openrouter",
        "openrouter_api_key",
        "OPENROUTER_API_KEY",
        "https://openrouter.ai/api/v1/chat/completions",
        "openrouter/free",
    )


def probe_mistral() -> ProbeResult:
    return _openai_probe(
        "mistral",
        "mistral_api_key",
        "MISTRAL_API_KEY",
        "https://api.mistral.ai/v1/chat/completions",
        "mistral-small-latest",
    )


def probe_bedrock() -> ProbeResult:
    model = "mistral.mistral-large-3-675b-instruct"
    routing = json.loads(ROUTING_PATH.read_text(encoding="utf-8"))
    provider_config = (routing.get("providers") or {}).get("bedrock") or {}

    def execute(key: str) -> tuple[int, Mapping[str, Any], str, str | None]:
        response = HTTPAdapter("bedrock", provider_config, key).invoke(
            prompt=PROMPT, model=model, max_output_tokens=64, timeout=TIMEOUT,
        )
        return (
            response.http_status, response.payload, response.text,
            response.actual_model,
        )

    return _probe(
        "bedrock", "bedrock_api_key", "AWS_BEARER_TOKEN_BEDROCK",
        model, execute,
    )


def probe_azure() -> ProbeResult:
    endpoint = load_secret("azure_ai_endpoint", "AZURE_OPENAI_ENDPOINT")
    model = load_secret("azure_ai_model", "AZURE_OPENAI_MODEL") or "gpt-5-mini"
    if not endpoint:
        return ProbeResult(
            provider="azure", configured=False, ok=False,
            requested_model=model, error="Azure endpoint is not configured",
        )
    routing = json.loads(ROUTING_PATH.read_text(encoding="utf-8"))
    provider_config = dict((routing.get("providers") or {}).get("azure") or {})
    provider_config["endpoint"] = endpoint

    def execute(key: str) -> tuple[int, Mapping[str, Any], str, str | None]:
        response = HTTPAdapter("azure", provider_config, key).invoke(
            prompt='Return only this JSON object: {"ok":"OK"}',
            # GPT-5 completion limits include hidden reasoning tokens.  A
            # 64-token cap can finish before any visible JSON is emitted.
            model=model, max_output_tokens=1024, timeout=TIMEOUT,
        )
        parsed = _parse_json_block(response.text)
        return (
            response.http_status, response.payload,
            str(parsed.get("ok") or ""), response.actual_model,
        )

    return _probe(
        "azure", "foundry_api_key", "AZURE_OPENAI_API_KEY", model, execute,
    )


PROBES: dict[str, Callable[[], ProbeResult]] = {
    "gemini": probe_gemini,
    "cohere": probe_cohere,
    "nvidia": probe_nvidia,
    "groq": probe_groq,
    "openrouter": probe_openrouter,
    "mistral": probe_mistral,
    "bedrock": probe_bedrock,
    "azure": probe_azure,
}


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _fixed_probe_png() -> bytes:
    """Build a deterministic 32x32 RGB checkerboard with valid PNG CRCs."""
    width = height = 32
    scanlines = bytearray()
    for y in range(height):
        scanlines.append(0)  # PNG filter: None
        for x in range(width):
            scanlines.extend((40, 90, 180) if (x // 8 + y // 8) % 2 else (240, 210, 60))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(bytes(scanlines), level=9))
        + _png_chunk(b"IEND", b"")
    )


# Vision probes never send project material.
_PROBE_PNG = _fixed_probe_png()
VISION_MODELS = {
    "gemini": "gemini-3.5-flash-lite",
    "cohere": "command-a-vision-07-2025",
    "mistral": "mistral-small-latest",
    "bedrock": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "openrouter": "google/gemma-4-26b-a4b-it:free",
}


def probe_vision(
    provider: str, *, image_count: int = 1, model: str | None = None,
) -> ProbeResult:
    """Authenticate the configured production image payload with fixed pixels."""
    if provider not in VISION_MODELS:
        raise ValueError(f"Vision probe is not configured for {provider}")
    if image_count not in {1, 2}:
        raise ValueError("image_count must be 1 or 2")
    routing = json.loads(ROUTING_PATH.read_text(encoding="utf-8"))
    provider_config = (routing.get("providers") or {}).get(provider) or {}
    model = model or VISION_MODELS[provider]
    secret_name = str(provider_config.get("secret_name") or f"{provider}_api_key")
    env_var = str(provider_config.get("env_var") or f"{provider.upper()}_API_KEY")

    with tempfile.TemporaryDirectory(prefix="macrostrat_llm_probe_") as temp:
        images: list[LLMImage] = []
        for index in range(image_count):
            image_path = Path(temp) / f"probe_{index + 1}.png"
            image_path.write_bytes(_PROBE_PNG)
            images.append(LLMImage(path=image_path, mime_type="image/png"))

        def execute(key: str) -> tuple[int, Mapping[str, Any], str, str | None]:
            response = HTTPAdapter(provider, provider_config, key).invoke(
                prompt='Return only this JSON object: {"ok":"OK"}',
                model=model,
                max_output_tokens=64,
                timeout=TIMEOUT,
                images=images,
            )
            parsed = _parse_json_block(response.text)
            return (
                response.http_status,
                response.payload,
                str(parsed.get("ok") or ""),
                response.actual_model,
            )

        capabilities = ("text", "vision", *(('multi_image',) if image_count > 1 else ()))
        return _probe(
            provider,
            secret_name,
            env_var,
            model,
            execute,
            capabilities=capabilities,
            image_count=image_count,
        )


def _get_model_ids(url: str, headers: Mapping[str, str]) -> list[str]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "MacroStrat-LLM-Probe/1.0", **dict(headers)},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload.get("models") or payload.get("data") or []
    ids = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        model_id = str(row.get("id") or row.get("name") or "").removeprefix("models/")
        if model_id:
            ids.append(model_id)
    return sorted(set(ids))


def list_provider_models(provider: str) -> list[str]:
    if provider == "gemini":
        key = load_secret("gemini_api_key", "GEMINI_API_KEY")
        if not key:
            raise RuntimeError("Gemini API key is not configured")
        return _get_model_ids(
            "https://generativelanguage.googleapis.com/v1beta/models",
            {"x-goog-api-key": key},
        )
    if provider == "nvidia":
        key = load_secret("nvidia_api_key", "NVIDIA_API_KEY")
        if not key:
            raise RuntimeError("NVIDIA API key is not configured")
        return _get_model_ids(
            "https://integrate.api.nvidia.com/v1/models",
            {"Authorization": f"Bearer {key}"},
        )
    raise ValueError(f"Model listing is not implemented for {provider}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        action="append",
        choices=tuple(PROBES),
        help="Probe only this provider; repeat to select multiple providers.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument(
        "--vision",
        action="store_true",
        help="Use the production image payload with a fixed 32x32 PNG.",
    )
    parser.add_argument(
        "--images",
        type=int,
        choices=(1, 2),
        default=1,
        help="Number of fixed images for --vision; use 2 to prove multi-image support.",
    )
    parser.add_argument(
        "--model",
        help="Fixed model ID for --vision; requires exactly one --provider.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write a secret-safe timestamped report for qualification.",
    )
    parser.add_argument(
        "--list-models",
        choices=("gemini", "nvidia"),
        help="List model IDs without sending a generation request.",
    )
    args = parser.parse_args(argv)

    if args.list_models:
        print("\n".join(list_provider_models(args.list_models)))
        return 0

    selected = args.provider or (list(VISION_MODELS) if args.vision else list(PROBES))
    if args.vision:
        unsupported = sorted(set(selected) - set(VISION_MODELS))
        if unsupported:
            parser.error(f"--vision is not configured for: {', '.join(unsupported)}")
        if args.model and len(selected) != 1:
            parser.error("--model with --vision requires exactly one --provider")
        results = [
            probe_vision(name, image_count=args.images, model=args.model)
            for name in selected
        ]
    else:
        results = [PROBES[name]() for name in selected]
    report = {
        "schema_version": "llm-probe-results/1.0",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "results": [asdict(result) for result in results],
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for result in results:
            status = str(result.http_status) if result.http_status is not None else "-"
            actual = result.actual_model or "-"
            detail = result.response if result.ok else result.error
            print(
                f"{result.provider:<11} "
                f"{'PASS' if result.ok else 'FAIL':<4} "
                f"HTTP={status:<3} model={actual} latency={result.latency_ms or 0}ms"
            )
            if detail:
                print(f"  {detail}")
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
