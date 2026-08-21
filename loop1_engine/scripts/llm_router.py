# -*- coding: utf-8 -*-
"""Validated sequential failover for configured LLM providers."""

from __future__ import annotations

import argparse
import base64
import json
import math
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

try:
    from common import load_secret
    from llm_runtime import BudgetUnavailable, DEFAULT_DB_PATH, LLMRuntimeStore
except ImportError:  # pragma: no cover - package-style import
    from .common import load_secret
    from .llm_runtime import BudgetUnavailable, DEFAULT_DB_PATH, LLMRuntimeStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "llm_routing.json"


class RouterConfigurationError(RuntimeError):
    """The static route configuration is invalid or incomplete."""


class AllProvidersFailed(OSError):
    """No configured candidate produced a validated result."""

    def __init__(self, stage: str, attempts: Sequence[Mapping[str, Any]]) -> None:
        self.stage = stage
        self.attempts = [dict(row) for row in attempts]
        summary = "; ".join(
            f"{row.get('provider')}:{row.get('model')}={row.get('error_kind') or row.get('status')}"
            for row in self.attempts
        ) or "no eligible candidates"
        super().__init__(f"All LLM providers failed for {stage}: {summary}")


class ProviderCallError(OSError):
    """A single provider invocation failed with classified metadata."""

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        http_status: int | None = None,
        retry_after: float | None = None,
        reset_at: float | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(" ".join(message.split())[:500])
        self.kind = kind
        self.http_status = http_status
        self.retry_after = retry_after
        self.reset_at = reset_at
        self.retryable = retryable


@dataclass(frozen=True)
class ProviderResponse:
    payload: Mapping[str, Any]
    text: str
    requested_model: str
    actual_model: str
    http_status: int = 200
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class ValidationReport:
    decision: str
    accepted: Any = None
    dropped: Any = None
    unresolved: Any = None
    fatal_errors: tuple[str, ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.decision not in {"accept", "partial", "reject"}:
            raise ValueError(f"Unsupported validation decision: {self.decision}")


@dataclass(frozen=True)
class LLMImage:
    """Local image attachment whose bytes exist only during provider invocation."""

    path: str | Path
    mime_type: str


@dataclass(frozen=True)
class LLMRequest:
    stage: str
    logical_job_id: str
    prompt: str
    estimated_input_tokens: int
    reserved_output_tokens: int = 2048
    required_capabilities: tuple[str, ...] = ("text", "json")
    images: tuple[LLMImage, ...] = ()

    @property
    def estimated_total_tokens(self) -> int:
        return max(1, self.estimated_input_tokens + self.reserved_output_tokens)


@dataclass(frozen=True)
class RouterResult:
    response: Mapping[str, Any]
    provider: str
    requested_model: str
    actual_model: str
    attempt_id: str
    attempts: tuple[Mapping[str, Any], ...]
    validation: ValidationReport
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class Adapter(Protocol):
    def invoke(
        self,
        *,
        prompt: str,
        model: str,
        max_output_tokens: int,
        timeout: int,
        images: Sequence[LLMImage] = (),
    ) -> ProviderResponse: ...


def _balanced_objects(text: str) -> Iterator[str]:
    """Yield each top-level ``{...}`` span, honouring strings and escapes.

    The previous extraction took ``text[first "{" : last "}"]``.  When a model
    printed a valid object and then added a note that also contained braces,
    that span covered both and no longer parsed, so a usable answer was thrown
    away as ``json_parse``.  Walking the braces returns the object itself.

    This is transport-level only.  Whatever is recovered still has to pass the
    stage validator, so nothing a model invents becomes acceptable here.
    """

    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start:index + 1]
                    start = -1


def _parse_json_block(text: str) -> Mapping[str, Any]:
    candidate = str(text or "").strip()
    if candidate.startswith("```"):
        first_newline = candidate.find("\n")
        candidate = candidate[first_newline + 1:] if first_newline >= 0 else candidate
        if candidate.rstrip().endswith("```"):
            candidate = candidate.rstrip()[:-3]
    try:
        parsed: Any = json.loads(candidate.strip())
    except json.JSONDecodeError as exc:
        parsed = None
        for span in _balanced_objects(candidate):
            try:
                loaded = json.loads(span)
            except json.JSONDecodeError:
                continue
            if isinstance(loaded, Mapping):
                parsed = loaded
                break
        if parsed is None:
            # 生responseは永続化しない。診断のために「形」だけを数える。
            # 文字数・括弧の数・囲みの有無だけで、本文・引用・IDは一切残さない。
            shape = (
                f"chars={len(candidate)} "
                f"open_braces={candidate.count('{')} close_braces={candidate.count('}')} "
                f"balanced_objects={sum(1 for _ in _balanced_objects(candidate))} "
                f"fenced={'```' in str(text or '')} "
                f"starts_with_brace={candidate.lstrip().startswith('{')}"
            )
            if "{" not in candidate:
                raise ProviderCallError(
                    f"Provider response did not contain a JSON object [{shape}]",
                    kind="json_parse",
                ) from exc
            raise ProviderCallError(
                f"Provider response contained invalid JSON [{shape}]",
                kind="json_parse",
            ) from exc
    if not isinstance(parsed, Mapping):
        raise ProviderCallError("Provider response JSON was not an object", kind="json_parse")
    return parsed


def _response_error(body: bytes, secret: str) -> str:
    text = body.decode("utf-8", "replace")[:4000]
    try:
        payload = json.loads(text)
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, Mapping):
                text = str(error.get("message") or error.get("code") or "API error")
            elif error:
                text = str(error)
            else:
                text = str(payload.get("message") or payload.get("detail") or "API error")
    except json.JSONDecodeError:
        pass
    if secret:
        text = text.replace(secret, "<KEY>")
    return " ".join(text.split())[:500] or "API error"


def _next_midnight(timezone_name: str) -> float:
    zone = ZoneInfo(timezone_name)
    now = datetime.now(tz=zone)
    tomorrow = (now + timedelta(days=1)).date()
    return datetime.combine(tomorrow, datetime.min.time(), tzinfo=zone).timestamp()


def _classify_http(
    status: int,
    message: str,
    retry_after: float | None,
    reset_timezone: str,
) -> ProviderCallError:
    lowered = message.lower()
    if status in {401, 403}:
        return ProviderCallError(message, kind="auth", http_status=status)
    if status in {404, 410}:
        return ProviderCallError(message, kind="model_unavailable", http_status=status)
    if status == 429:
        quota_markers = (
            "daily", "per day", "rpd", "monthly", "per month",
            "free_tier_requests", "requests per day",
        )
        if any(marker in lowered for marker in quota_markers):
            return ProviderCallError(
                message, kind="quota", http_status=status,
                reset_at=_next_midnight(reset_timezone),
            )
        return ProviderCallError(
            message, kind="rate_limit", http_status=status,
            retry_after=retry_after, retryable=retry_after is None or retry_after <= 120,
            reset_at=(time.time() + retry_after) if retry_after else None,
        )
    if status in {408, 500, 502, 503, 504}:
        return ProviderCallError(
            message, kind="transient", http_status=status,
            retry_after=retry_after, retryable=True,
        )
    if status == 400:
        kind = "context" if any(
            marker in lowered for marker in ("context length", "too many tokens", "maximum context")
        ) else "capability"
        return ProviderCallError(message, kind=kind, http_status=status)
    return ProviderCallError(message, kind="provider_error", http_status=status)


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
            str(row.get("text") or "") for row in content if isinstance(row, Mapping)
        )
    return ""


def _cohere_text(payload: Mapping[str, Any]) -> str:
    message = payload.get("message") or {}
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return content
    return "".join(
        str(row.get("text") or "") for row in (content or []) if isinstance(row, Mapping)
    )


def _bedrock_text(payload: Mapping[str, Any]) -> str:
    output = payload.get("output") or {}
    message = output.get("message") if isinstance(output, Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    return "".join(
        str(row.get("text") or "") for row in (content or []) if isinstance(row, Mapping)
    )


def _gemini_text(payload: Mapping[str, Any]) -> str:
    chunks: list[str] = []
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        content = candidate.get("content") or {}
        for part in content.get("parts") or [] if isinstance(content, Mapping) else []:
            if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "".join(chunks)


def _usage(payload: Mapping[str, Any], protocol: str) -> tuple[int | None, int | None, int | None]:
    if protocol == "gemini":
        usage = payload.get("usageMetadata") or {}
        input_tokens = usage.get("promptTokenCount")
        output_tokens = usage.get("candidatesTokenCount")
        total_tokens = usage.get("totalTokenCount")
    elif protocol == "cohere":
        usage = payload.get("usage") or {}
        tokens = usage.get("tokens") or usage.get("billed_units") or {}
        input_tokens = tokens.get("input_tokens") if isinstance(tokens, Mapping) else None
        output_tokens = tokens.get("output_tokens") if isinstance(tokens, Mapping) else None
        total_tokens = (
            int(input_tokens or 0) + int(output_tokens or 0)
            if input_tokens is not None or output_tokens is not None else None
        )
    elif protocol == "bedrock":
        usage = payload.get("usage") or {}
        input_tokens = usage.get("inputTokens")
        output_tokens = usage.get("outputTokens")
        total_tokens = usage.get("totalTokens")
        if total_tokens is None and (input_tokens is not None or output_tokens is not None):
            total_tokens = int(input_tokens or 0) + int(output_tokens or 0)
    else:
        usage = payload.get("usage") or {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
    return (
        int(input_tokens) if input_tokens is not None else None,
        int(output_tokens) if output_tokens is not None else None,
        int(total_tokens) if total_tokens is not None else None,
    )


class HTTPAdapter:
    """Provider adapter for all centrally routed HTTP LLM protocols."""

    def __init__(self, provider: str, config: Mapping[str, Any], secret: str) -> None:
        self.provider = provider
        self.config = dict(config)
        self.secret = secret

    def invoke(
        self,
        *,
        prompt: str,
        model: str,
        max_output_tokens: int,
        timeout: int,
        images: Sequence[LLMImage] = (),
    ) -> ProviderResponse:
        protocol = str(self.config.get("protocol") or "openai")
        endpoint = str(self.config.get("endpoint") or "")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "MacroStrat-LLM-Router/1.0",
        }
        image_parts: list[dict[str, Any]] = []
        image_urls: list[dict[str, Any]] = []
        mistral_image_urls: list[dict[str, Any]] = []
        bedrock_image_blocks: list[dict[str, Any]] = []
        max_image_bytes = max(1, int(self.config.get("max_image_bytes") or 20 * 1024 * 1024))
        max_total_image_bytes = max(
            1, int(self.config.get("max_total_image_bytes") or 20 * 1024 * 1024),
        )
        max_images = max(1, int(self.config.get("max_images_per_request") or 4))
        total_image_bytes = 0
        if len(images) > max_images:
            raise ProviderCallError(
                f"Request contains {len(images)} images; provider limit is {max_images}",
                kind="attachment",
            )
        for attachment in images:
            path = Path(attachment.path).expanduser().resolve()
            mime_type = str(attachment.mime_type or "").casefold()
            if mime_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                raise ProviderCallError(
                    f"Unsupported image MIME type: {mime_type or '(blank)'}",
                    kind="attachment",
                )
            try:
                size = path.stat().st_size
                if size > max_image_bytes:
                    raise ProviderCallError(
                        f"Image attachment exceeds {max_image_bytes} bytes",
                        kind="attachment",
                    )
                total_image_bytes += size
                if total_image_bytes > max_total_image_bytes:
                    raise ProviderCallError(
                        f"Image attachments exceed {max_total_image_bytes} bytes in total",
                        kind="attachment",
                    )
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            except ProviderCallError:
                raise
            except OSError as exc:
                raise ProviderCallError(
                    f"Cannot read image attachment: {path.name}",
                    kind="attachment",
                ) from exc
            image_parts.append({
                "inline_data": {"mime_type": mime_type, "data": encoded},
            })
            image_urls.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
            })
            mistral_image_urls.append({
                "type": "image_url",
                "image_url": f"data:{mime_type};base64,{encoded}",
            })
            image_format = mime_type.split("/", 1)[1]
            bedrock_image_blocks.append({
                "image": {
                    "format": "jpeg" if image_format == "jpg" else image_format,
                    "source": {"bytes": encoded},
                },
            })

        if protocol == "bedrock":
            region = str(self.config.get("region") or "us-east-1")
            url = endpoint.format(region=region, model=model)
            headers["Authorization"] = f"Bearer {self.secret}"
            body = {
                "messages": [{
                    "role": "user",
                    "content": [{"text": prompt}, *bedrock_image_blocks],
                }],
                "inferenceConfig": {
                    "maxTokens": max_output_tokens,
                    "temperature": 0,
                },
            }
        elif protocol == "azure_openai":
            base = endpoint.rstrip("/")
            url = (
                base + "/chat/completions"
                if base.endswith("/openai/v1")
                else base + "/openai/v1/chat/completions"
            )
            headers["api-key"] = self.secret
            body = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_completion_tokens": max_output_tokens,
                "response_format": {"type": "json_object"},
            }
            if images:
                body["messages"][0]["content"] = [
                    {"type": "text", "text": prompt},
                    *image_urls,
                ]
            if bool(self.config.get("supports_temperature", False)):
                body["temperature"] = 0
        elif protocol == "gemini":
            url = endpoint.format(model=model)
            headers["x-goog-api-key"] = self.secret
            body = {
                "contents": [{"parts": [{"text": prompt}, *image_parts]}],
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": max_output_tokens,
                    "responseMimeType": "application/json",
                },
            }
        elif protocol == "cohere":
            url = endpoint
            headers["Authorization"] = f"Bearer {self.secret}"
            body = {
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": (
                        [{"type": "text", "text": prompt}, *image_urls]
                        if images else prompt
                    ),
                }],
                "temperature": 0,
                "max_tokens": max_output_tokens,
            }
        else:
            url = endpoint
            headers["Authorization"] = f"Bearer {self.secret}"
            body = {
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": (
                        [
                            {"type": "text", "text": prompt},
                            *(mistral_image_urls if self.provider == "mistral" else image_urls),
                        ]
                        if images else prompt
                    ),
                }],
                "temperature": 0,
                "max_tokens": max_output_tokens,
                "response_format": {"type": "json_object"},
            }
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            detail = _response_error(exc.read(4096), self.secret)
            retry_after = None
            try:
                retry_after = float(exc.headers.get("Retry-After")) if exc.headers else None
            except (TypeError, ValueError):
                pass
            raise _classify_http(
                int(exc.code), detail, retry_after,
                str(self.config.get("reset_timezone") or "UTC"),
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            transient = isinstance(
                reason,
                (TimeoutError, socket.timeout, ConnectionResetError, ConnectionAbortedError),
            )
            raise ProviderCallError(
                str(reason).replace(self.secret, "<KEY>"),
                kind="transient" if transient else "network",
                retryable=transient,
            ) from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderCallError("Provider returned a non-JSON HTTP response", kind="provider_error") from exc

        if not isinstance(payload, Mapping):
            raise ProviderCallError("Provider returned a non-object response", kind="provider_error")
        if protocol == "gemini":
            text = _gemini_text(payload)
        elif protocol == "cohere":
            text = _cohere_text(payload)
        elif protocol == "bedrock":
            text = _bedrock_text(payload)
        else:
            text = _openai_text(payload)
        if not text.strip():
            raise ProviderCallError("Provider response contained no text", kind="empty_response")
        input_tokens, output_tokens, total_tokens = _usage(payload, protocol)
        actual_model = str(payload.get("model") or model)
        return ProviderResponse(
            payload=payload,
            text=text,
            requested_model=model,
            actual_model=actual_model,
            http_status=status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )


SecretLoader = Callable[[str, str | None], str | None]
AdapterFactory = Callable[[str, Mapping[str, Any], str], Adapter]
Validator = Callable[[Mapping[str, Any]], ValidationReport]


class LLMRouter:
    """Select candidates sequentially and accept only validator-approved output."""

    def __init__(
        self,
        *,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        config: Mapping[str, Any] | None = None,
        runtime: LLMRuntimeStore | None = None,
        runtime_path: str | Path = DEFAULT_DB_PATH,
        secret_loader: SecretLoader = load_secret,
        adapter_factory: AdapterFactory = HTTPAdapter,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config_path = Path(config_path).expanduser().resolve()
        if config is None:
            try:
                loaded_config = json.loads(self.config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RouterConfigurationError(
                    f"Cannot read LLM routing config: {self.config_path}"
                ) from exc
        else:
            # Deep-copy the public document so a compatibility caller cannot
            # mutate another router.  Secrets are supplied only by the loader.
            loaded_config = json.loads(json.dumps(config))
        if not isinstance(loaded_config, Mapping):
            raise RouterConfigurationError("LLM routing config must be a JSON object")
        self.config = dict(loaded_config)
        self.runtime = runtime or LLMRuntimeStore(runtime_path)
        self.secret_loader = secret_loader
        self.adapter_factory = adapter_factory
        self.sleep = sleep

    def _eligible(
        self,
        request: LLMRequest,
        *,
        skipped: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        routes = self.config.get("routes") or {}
        route = routes.get(request.stage) if isinstance(routes, Mapping) else None
        if not isinstance(route, Mapping):
            raise RouterConfigurationError(f"No LLM route is configured for {request.stage}")
        providers = self.config.get("providers") or {}
        policy = self.config.get("policy") or {}
        free_only = bool(policy.get("free_only", False)) if isinstance(policy, Mapping) else False
        candidates: list[dict[str, Any]] = []
        max_failovers = max(0, int(route.get("max_failovers", 3)))
        for item in route.get("candidates") or []:
            if not isinstance(item, Mapping):
                continue
            if not item.get("enabled", True):
                continue
            provider_name = str(item.get("provider") or "")
            provider = providers.get(provider_name) if isinstance(providers, Mapping) else None
            if not isinstance(provider, Mapping) or not provider.get("enabled", True):
                continue
            if free_only and not bool(item.get("free_tier", provider.get("free_tier", False))):
                continue
            capabilities = set(item.get("capabilities") or ())
            if not set(request.required_capabilities).issubset(capabilities):
                continue
            effective_output_tokens = max(
                request.reserved_output_tokens,
                max(0, int(item.get("min_output_tokens") or 0)),
            )
            configured_output_limit = item.get("max_output_tokens")
            if configured_output_limit is not None:
                output_limit = max(1, int(configured_output_limit))
                if effective_output_tokens > output_limit:
                    if skipped is not None:
                        skipped.append({
                            "provider": provider_name,
                            "model": str(item.get("model") or ""),
                            "status": "skipped",
                            "error_kind": "output_capacity",
                            "required_output_tokens": effective_output_tokens,
                            "max_output_tokens": output_limit,
                        })
                    continue
            context_window = int(item.get("context_window") or 0)
            headroom = float(item.get("context_headroom") or 0.8)
            candidate_total_tokens = max(
                1, request.estimated_input_tokens + effective_output_tokens,
            )
            if context_window and candidate_total_tokens > math.floor(context_window * headroom):
                continue
            candidates.append({
                **dict(item),
                "provider_config": dict(provider),
                "_effective_output_tokens": effective_output_tokens,
            })
            if len(candidates) >= max_failovers + 1:
                break
        return candidates

    def execute(self, request: LLMRequest, validator: Validator) -> RouterResult:
        attempt_summaries: list[dict[str, Any]] = []
        for candidate in self._eligible(request, skipped=attempt_summaries):
            provider = str(candidate["provider"])
            model = str(candidate["model"])
            provider_config = dict(candidate["provider_config"])
            model_secret_name = str(
                candidate.get("model_secret_name")
                or provider_config.get("model_secret_name")
                or ""
            )
            if model_secret_name:
                configured_model = self.secret_loader(
                    model_secret_name,
                    str(provider_config.get("model_env_var") or "") or None,
                )
                if not configured_model:
                    attempt_summaries.append({
                        "provider": provider, "model": model,
                        "status": "skipped", "error_kind": "unconfigured",
                    })
                    continue
                model = str(configured_model)
            endpoint_secret_name = str(provider_config.get("endpoint_secret_name") or "")
            if endpoint_secret_name:
                configured_endpoint = self.secret_loader(
                    endpoint_secret_name,
                    str(provider_config.get("endpoint_env_var") or "") or None,
                )
                if not configured_endpoint:
                    attempt_summaries.append({
                        "provider": provider, "model": model,
                        "status": "skipped", "error_kind": "unconfigured",
                    })
                    continue
                provider_config["endpoint"] = str(configured_endpoint)
            quota_group = str(provider_config.get("quota_group") or provider)
            if not self.runtime.claim(provider, model, request.stage):
                attempt_summaries.append({
                    "provider": provider, "model": model,
                    "status": "skipped", "error_kind": "circuit_open",
                })
                continue
            secret_name = str(provider_config.get("secret_name") or f"{provider}_api_key")
            env_var = provider_config.get("env_var")
            secret = self.secret_loader(secret_name, str(env_var) if env_var else None)
            if not secret:
                attempt_summaries.append({
                    "provider": provider, "model": model,
                    "status": "skipped", "error_kind": "unconfigured",
                })
                continue
            adapter = self.adapter_factory(provider, provider_config, secret)
            max_attempts = max(1, int(candidate.get("max_attempts", 2)))
            timeout = max(1, int(candidate.get("timeout_seconds") or provider_config.get("timeout_seconds") or 180))
            effective_output_tokens = max(
                1,
                int(candidate.get("_effective_output_tokens") or request.reserved_output_tokens),
            )
            for retry_index in range(max_attempts):
                response: ProviderResponse | None = None
                try:
                    reservation = self.runtime.reserve(
                        provider=provider,
                        model=model,
                        quota_group=quota_group,
                        stage=request.stage,
                        logical_job_id=request.logical_job_id,
                        estimated_tokens=max(
                            1, request.estimated_input_tokens + effective_output_tokens,
                        ),
                        limits=provider_config.get("limits") if isinstance(provider_config.get("limits"), Mapping) else {},
                        reset_timezone=str(provider_config.get("reset_timezone") or "UTC"),
                        ttl_seconds=timeout + 300,
                    )
                except BudgetUnavailable as exc:
                    attempt_summaries.append({
                        "provider": provider, "model": model,
                        "status": "skipped", "error_kind": "budget",
                        "message": str(exc),
                    })
                    break
                try:
                    response = adapter.invoke(
                        prompt=request.prompt,
                        model=model,
                        max_output_tokens=effective_output_tokens,
                        timeout=timeout,
                        images=request.images,
                    )
                    parsed = _parse_json_block(response.text)
                    report = validator(parsed)
                    if report.decision not in {"accept", "partial"}:
                        raise ProviderCallError(
                            "Stage validator rejected provider output",
                            kind="validation",
                        )
                    attempt_id = self.runtime.finalize(
                        reservation,
                        status="accepted",
                        actual_model=response.actual_model,
                        http_status=response.http_status,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        total_tokens=response.total_tokens,
                        validation_decision=report.decision,
                    )
                    self.runtime.record_success(provider, model, request.stage)
                    attempt_summaries.append({
                        "attempt_id": attempt_id,
                        "provider": provider,
                        "model": model,
                        "actual_model": response.actual_model,
                        "status": "accepted",
                        "validation": report.decision,
                    })
                    return RouterResult(
                        response=parsed,
                        provider=provider,
                        requested_model=model,
                        actual_model=response.actual_model,
                        attempt_id=attempt_id,
                        attempts=tuple(attempt_summaries),
                        validation=report,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        total_tokens=response.total_tokens,
                    )
                except ProviderCallError as exc:
                    response_obj = response
                    actual_model = response_obj.actual_model if response_obj is not None else model
                    attempt_id = self.runtime.finalize(
                        reservation,
                        status="rejected" if exc.kind in {"validation", "json_parse", "empty_response"} else "error",
                        actual_model=actual_model,
                        error_kind=exc.kind,
                        http_status=exc.http_status,
                        input_tokens=(response_obj.input_tokens if response_obj is not None else None),
                        output_tokens=(response_obj.output_tokens if response_obj is not None else None),
                        total_tokens=(response_obj.total_tokens if response_obj is not None else None),
                        validation_decision="reject" if exc.kind == "validation" else None,
                        error_message=str(exc),
                    )
                    self.runtime.record_failure(
                        provider, model, request.stage, exc.kind, reset_at=exc.reset_at,
                    )
                    attempt_summaries.append({
                        "attempt_id": attempt_id, "provider": provider,
                        "model": model, "status": "error",
                        "error_kind": exc.kind, "http_status": exc.http_status,
                    })
                    if exc.retryable and retry_index + 1 < max_attempts:
                        delay = exc.retry_after if exc.retry_after is not None else 2 ** retry_index
                        self.sleep(min(120.0, max(0.0, float(delay))))
                        continue
                    break
                except Exception as exc:
                    # Validator bugs are not provider faults.  Record the HTTP call,
                    # then surface the programming error instead of hiding it via failover.
                    self.runtime.finalize(
                        reservation,
                        status="validator_error",
                        error_kind="validator_error",
                        error_message=f"{type(exc).__name__}: {exc}",
                    )
                    raise
        raise AllProvidersFailed(request.stage, attempt_summaries)


def single_provider_router(
    *,
    stage: str,
    provider: str,
    model: str,
    secret: str,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    runtime: LLMRuntimeStore | None = None,
    runtime_path: str | Path = DEFAULT_DB_PATH,
    adapter_factory: AdapterFactory = HTTPAdapter,
    sleep: Callable[[float], None] = time.sleep,
) -> LLMRouter:
    """Build an in-memory one-candidate router for an explicit credential.

    This preserves the old ``api_key=`` calling convention without restoring
    a second HTTP/retry/accounting path.  The credential is held only by the
    secret-loader closure and is never inserted into the cloned config.
    """

    public_path = Path(config_path).expanduser().resolve()
    try:
        document = json.loads(public_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RouterConfigurationError(
            f"Cannot read LLM routing config: {public_path}"
        ) from exc
    routes = document.get("routes") if isinstance(document, Mapping) else None
    route = routes.get(stage) if isinstance(routes, Mapping) else None
    if not isinstance(route, Mapping):
        raise RouterConfigurationError(f"No LLM route is configured for {stage}")
    selected = next((
        dict(row) for row in route.get("candidates") or []
        if isinstance(row, Mapping) and str(row.get("provider") or "") == provider
    ), None)
    if selected is None:
        raise RouterConfigurationError(
            f"Provider {provider} is not configured for route {stage}"
        )
    selected.update({"provider": provider, "model": model, "enabled": True})
    selected.pop("disabled_reason", None)
    cloned_route = dict(route)
    cloned_route["max_failovers"] = 0
    cloned_route["candidates"] = [selected]
    document = json.loads(json.dumps(document))
    document["routes"][stage] = cloned_route
    provider_config = (document.get("providers") or {}).get(provider) or {}
    expected_secret_name = str(
        provider_config.get("secret_name") or f"{provider}_api_key"
    )

    def explicit_secret_loader(name: str, _env: str | None) -> str | None:
        return secret if name == expected_secret_name else None

    return LLMRouter(
        config_path=public_path,
        config=document,
        runtime=runtime,
        runtime_path=runtime_path,
        secret_loader=explicit_secret_loader,
        adapter_factory=adapter_factory,
        sleep=sleep,
    )


def _parse_reset_target(
    value: str, routing: Mapping[str, Any], explicit_scope: str | None = None,
) -> tuple[str, str, str | None]:
    """Parse a reset target without splitting colons inside model IDs."""

    provider, separator, remainder = value.partition(":")
    if not separator or not provider or not remainder:
        raise ValueError("--reset-circuit must be PROVIDER:MODEL[:SCOPE]")
    if explicit_scope is not None:
        return provider, remainder, explicit_scope or None
    configured_models = {
        str(candidate.get("model") or "")
        for route in (routing.get("routes") or {}).values()
        if isinstance(route, Mapping)
        for candidate in route.get("candidates") or []
        if isinstance(candidate, Mapping)
        and str(candidate.get("provider") or "") == provider
    }
    matches = [
        model for model in configured_models
        if model and (remainder == model or remainder.startswith(model + ":"))
    ]
    if not matches:
        return provider, remainder, None
    model = max(matches, key=len)
    scope = remainder[len(model):].removeprefix(":") or None
    return provider, model, scope


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--runtime-db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--status", action="store_true", help="Print provider usage and circuits.")
    parser.add_argument(
        "--reset-circuit",
        metavar="PROVIDER:MODEL[:SCOPE]",
        help="Reset one model's circuits after a key/configuration change.",
    )
    parser.add_argument(
        "--reset-scope",
        help="Optional explicit scope for model IDs that contain colons.",
    )
    args = parser.parse_args(argv)
    # Reading status does not require constructing adapters or loading secrets.
    store = LLMRuntimeStore(args.runtime_db)
    if args.reset_circuit:
        try:
            routing = json.loads(args.config.read_text(encoding="utf-8"))
            provider, model, scope = _parse_reset_target(
                args.reset_circuit, routing, args.reset_scope,
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            parser.error(str(exc))
        removed = store.reset_circuit(
            provider, model, scope,
        )
        print(json.dumps({"circuits_reset": removed}, ensure_ascii=False))
        return 0
    if args.status:
        print(store.status_json())
        return 0
    parser.error("Specify --status or --reset-circuit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AllProvidersFailed",
    "HTTPAdapter",
    "LLMImage",
    "LLMRequest",
    "LLMRouter",
    "ProviderCallError",
    "ProviderResponse",
    "RouterConfigurationError",
    "RouterResult",
    "ValidationReport",
    "_parse_reset_target",
    "single_provider_router",
]
