"""
Ordered provider chain: try the primary, fall through on failure.

Also validates every provider's JSON against the request schema with a
small built-in validator (type / enum / required / range) so a sloppy local
model cannot push malformed data into the pipeline.
"""
from __future__ import annotations

import logging
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


class VLMRouter:
    def __init__(self, providers: Sequence[VLMProvider]):
        if not providers:
            raise ValueError("VLMRouter needs at least one provider")
        self.providers = list(providers)

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.providers]

    def complete_json(self, request: VLMRequest) -> VLMResponse:
        errors: dict[str, str] = {}
        for provider in self.providers:
            try:
                response = provider.complete_json(request)
                validate_against_schema(response.data, request.schema)
                record_vlm_call(provider.name, request.task, "ok", response.latency_ms / 1000.0, response.usage)
                return response
            except SchemaViolation as exc:
                record_vlm_call(provider.name, request.task, "schema_violation")
                errors[provider.name] = f"schema violation: {exc}"
                log.warning("VLM provider %s returned invalid JSON for %s: %s", provider.name, request.task, exc)
            except VLMError as exc:
                record_vlm_call(provider.name, request.task, "error")
                errors[provider.name] = str(exc)
                log.warning("VLM provider %s failed for %s: %s", provider.name, request.task, exc)
                if not exc.retryable:
                    break
            except Exception as exc:  # noqa: BLE001 - a buggy provider must not take the request down
                record_vlm_call(provider.name, request.task, "crash")
                errors[provider.name] = f"unexpected {type(exc).__name__}: {exc}"
                log.exception("VLM provider %s crashed for %s", provider.name, request.task)
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
    return VLMRouter(providers)
