"""
Ordered provider chain: try the primary, fall through on failure.

Also validates every provider's JSON against the request schema with a
small built-in validator (type / enum / required / range) so a sloppy local
model cannot push malformed data into the pipeline.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Sequence

from backend.observability import record_vlm_call
from backend.vlm.base import VLMError, VLMProvider, VLMRequest, VLMResponse, VLMUnavailable

log = logging.getLogger(__name__)


class SchemaViolation(ValueError):
    pass


def validate_against_schema(data, schema: dict, path: str = "$") -> None:
    """Minimal JSON-schema subset validator (object/array/string/number/boolean, enum, required, min/max)."""
    t = schema.get("type")
    if t == "object":
        if not isinstance(data, dict):
            raise SchemaViolation(f"{path}: expected object")
        for key in schema.get("required", []):
            if key not in data:
                raise SchemaViolation(f"{path}.{key}: missing")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(data) - set(props)
            if extra:
                # Extra keys are tolerated but dropped - local models add commentary fields.
                for k in extra:
                    data.pop(k, None)
        for key, sub in props.items():
            if key in data:
                validate_against_schema(data[key], sub, f"{path}.{key}")
    elif t == "array":
        if not isinstance(data, list):
            raise SchemaViolation(f"{path}: expected array")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(data):
                validate_against_schema(item, item_schema, f"{path}[{i}]")
    elif t == "string":
        if not isinstance(data, str):
            raise SchemaViolation(f"{path}: expected string")
        if "enum" in schema and data not in schema["enum"]:
            raise SchemaViolation(f"{path}: {data!r} not in enum")
    elif t == "number" or t == "integer":
        if isinstance(data, bool) or not isinstance(data, (int, float)):
            raise SchemaViolation(f"{path}: expected number")
        if "minimum" in schema and data < schema["minimum"]:
            raise SchemaViolation(f"{path}: below minimum")
        if "maximum" in schema and data > schema["maximum"]:
            raise SchemaViolation(f"{path}: above maximum")
    elif t == "boolean":
        if not isinstance(data, bool):
            raise SchemaViolation(f"{path}: expected boolean")


class CircuitBreaker:
    """Per-provider breaker: after `failures` consecutive failures the
    provider is skipped for `cooldown_s`, then one trial call is allowed."""

    def __init__(self, failures: int = 5, cooldown_s: float = 60.0):
        self.failures, self.cooldown_s = failures, cooldown_s
        self._count: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, name: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        with self._lock:
            until = self._open_until.get(name, 0.0)
            if until and now < until:
                return False
            if until:  # cooldown elapsed -> half-open: let one call through
                self._open_until.pop(name, None)
            return True

    def record(self, name: str, ok: bool, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        with self._lock:
            if ok:
                self._count[name] = 0
                return
            self._count[name] = self._count.get(name, 0) + 1
            if self._count[name] >= self.failures:
                self._open_until[name] = now + self.cooldown_s
                log.warning("VLM provider %s circuit OPEN for %.0fs after %d consecutive failures", name, self.cooldown_s, self._count[name])

    def state(self, name: str, now: float | None = None) -> str:
        now = now if now is not None else time.monotonic()
        until = self._open_until.get(name, 0.0)
        return "open" if until and now < until else "closed"


class VLMRouter:
    def __init__(self, providers: Sequence[VLMProvider], *, max_concurrency: int | None = None, breaker: CircuitBreaker | None = None):
        if not providers:
            raise ValueError("VLMRouter needs at least one provider")
        self.providers = list(providers)
        self.breaker = breaker
        self._sem = threading.BoundedSemaphore(max_concurrency) if max_concurrency and max_concurrency > 0 else None
        self.max_concurrency = max_concurrency

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.providers]

    def has_local(self) -> bool:
        return any(getattr(p, "local", False) for p in self.providers)

    def complete_json(self, request: VLMRequest) -> VLMResponse:
        errors: dict[str, str] = {}
        candidates = [p for p in self.providers if not request.local_only or getattr(p, "local", False)]
        if not candidates:
            raise VLMUnavailable({"router": "local_only requested but no local provider is configured"})
        for provider in candidates:
            if self.breaker and not self.breaker.allow(provider.name):
                errors[provider.name] = "circuit open"
                record_vlm_call(provider.name, request.task, "circuit_open")
                continue
            try:
                if self._sem:
                    self._sem.acquire()
                try:
                    response = provider.complete_json(request)
                finally:
                    if self._sem:
                        self._sem.release()
                validate_against_schema(response.data, request.schema)
                record_vlm_call(provider.name, request.task, "ok", response.latency_ms / 1000.0, response.usage)
                if self.breaker:
                    self.breaker.record(provider.name, True)
                return response
            except SchemaViolation as exc:
                record_vlm_call(provider.name, request.task, "schema_violation")
                errors[provider.name] = f"schema violation: {exc}"
                log.warning("VLM provider %s returned invalid JSON for %s: %s", provider.name, request.task, exc)
                # a schema miss is a model quality problem, not an outage - don't trip the breaker
            except VLMError as exc:
                record_vlm_call(provider.name, request.task, "error")
                errors[provider.name] = str(exc)
                log.warning("VLM provider %s failed for %s: %s", provider.name, request.task, exc)
                if self.breaker:
                    self.breaker.record(provider.name, False)
                if not exc.retryable:
                    break
            except Exception as exc:  # noqa: BLE001 - a buggy provider must not take the request down
                record_vlm_call(provider.name, request.task, "crash")
                errors[provider.name] = f"unexpected {type(exc).__name__}: {exc}"
                log.exception("VLM provider %s crashed for %s", provider.name, request.task)
                if self.breaker:
                    self.breaker.record(provider.name, False)
        raise VLMUnavailable(errors)


def build_router(settings) -> VLMRouter | None:
    """Build the provider chain from Settings. Returns None when the VLM layer is disabled."""
    if not settings.vlm_enabled:
        return None
    providers: list[VLMProvider] = []
    for name in settings.provider_order:
        if name == "claude":
            try:
                from backend.vlm.claude_provider import ClaudeProvider

                providers.append(
                    ClaudeProvider(
                        model=settings.claude_model,
                        effort=settings.claude_effort,
                        api_key=settings.anthropic_api_key or None,
                        timeout_s=settings.claude_timeout_s,
                        max_tokens=settings.claude_max_tokens,
                        prompt_cache=settings.claude_prompt_cache,
                    )
                )
            except ImportError:
                log.warning("anthropic SDK not installed; skipping Claude provider")
        elif name == "ollama":
            from backend.vlm.ollama_provider import OllamaProvider

            providers.append(OllamaProvider(host=settings.ollama_host, model=settings.ollama_model, timeout_s=settings.ollama_timeout_s))
        else:
            log.warning("unknown VLM provider '%s' in INSPECTOR_VLM_PROVIDERS - ignored", name)
    if not providers:
        return None
    breaker = CircuitBreaker(settings.vlm_breaker_failures, settings.vlm_breaker_cooldown_s) if settings.vlm_breaker_failures > 0 else None
    return VLMRouter(providers, max_concurrency=settings.vlm_max_concurrency, breaker=breaker)
