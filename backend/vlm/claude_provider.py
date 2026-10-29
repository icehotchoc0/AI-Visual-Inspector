"""
Claude provider (primary) - Anthropic Python SDK.

* Structured output via `output_config.format` (JSON schema) so the reply
  is guaranteed to be a single JSON object matching our schema.
* `fallbacks="default"` (beta `server-side-fallback-2026-07-01`) so a
  safety-classifier refusal is retried server-side on Anthropic's
  recommended substitute model; if the whole chain still refuses, we raise
  a retryable VLMError and the router moves on to the local Ollama model.
* Thinking is left at the model default (always on for Claude Opus 5.5);
  depth is controlled with `output_config.effort`.
"""
from __future__ import annotations

import base64
import json
import time

from backend.vlm.base import VLMError, VLMRequest, VLMResponse

_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeProvider:
    name = "claude"

    def __init__(
        self,
        model: str = "claude-opus-5-5",
        effort: str = "medium",
        api_key: str | None = None,
        timeout_s: float = 120.0,
        max_tokens: int = 8192,
        use_server_fallback: bool = True,
    ):
        import anthropic  # local import: the service still boots without the SDK when Claude is not configured

        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.use_server_fallback = use_server_fallback
        kwargs: dict = {"timeout": timeout_s, "max_retries": 2}
        if api_key:
            kwargs["api_key"] = api_key
        self._client = anthropic.Anthropic(**kwargs)

    # -- request building ---------------------------------------------------
    def _content_blocks(self, request: VLMRequest) -> list[dict]:
        blocks: list[dict] = []
        for img in request.images:
            if img.label:
                blocks.append({"type": "text", "text": img.label})
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img.media_type,
                        "data": base64.standard_b64encode(img.data).decode("ascii"),
                    },
                }
            )
        blocks.append({"type": "text", "text": request.prompt})
        return blocks

    def complete_json(self, request: VLMRequest) -> VLMResponse:
        import anthropic

        start = time.perf_counter()
        kwargs: dict = dict(
            model=self.model,
            max_tokens=min(self.max_tokens, request.max_tokens) if request.max_tokens else self.max_tokens,
            system=request.system,
            messages=[{"role": "user", "content": self._content_blocks(request)}],
            output_config={
                "format": {"type": "json_schema", "schema": request.schema},
                "effort": self.effort,
            },
        )
        if self.use_server_fallback:
            kwargs["betas"] = [_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        try:
            response = self._client.beta.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise VLMError(f"authentication failed: {exc.message}", retryable=True, provider=self.name) from exc
        except anthropic.BadRequestError as exc:
            # Our request is malformed (schema, image too large, ...) - the
            # same request will also fail next time, but a different
            # provider may still cope, so let the router try it.
            raise VLMError(f"bad request: {exc.message}", retryable=True, provider=self.name) from exc
        except anthropic.RateLimitError as exc:
            raise VLMError(f"rate limited: {exc.message}", retryable=True, provider=self.name) from exc
        except anthropic.APIStatusError as exc:
            raise VLMError(f"api error {exc.status_code}: {exc.message}", retryable=True, provider=self.name) from exc
        except anthropic.APIConnectionError as exc:
            raise VLMError(f"connection error: {exc}", retryable=True, provider=self.name) from exc
        except Exception as exc:  # noqa: BLE001
            # e.g. TypeError "Could not resolve authentication method" when no
            # key is configured - a provider problem, never a 500 for the caller.
            raise VLMError(f"{type(exc).__name__}: {exc}", retryable=True, provider=self.name) from exc

        latency_ms = (time.perf_counter() - start) * 1000.0

        if response.stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            raise VLMError(f"refused by safety classifier (category={category})", retryable=True, provider=self.name)
        if response.stop_reason == "max_tokens":
            raise VLMError("output truncated (max_tokens)", retryable=True, provider=self.name)

        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise VLMError(f"non-JSON output: {text[:200]!r}", retryable=True, provider=self.name) from exc
        if not isinstance(data, dict):
            raise VLMError("JSON output is not an object", retryable=True, provider=self.name)

        usage = {}
        if getattr(response, "usage", None) is not None:
            usage = {
                "input_tokens": getattr(response.usage, "input_tokens", None),
                "output_tokens": getattr(response.usage, "output_tokens", None),
                "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", None),
            }
        return VLMResponse(
            data=data,
            provider=self.name,
            model=getattr(response, "model", self.model) or self.model,
            latency_ms=latency_ms,
            raw_text=text,
            usage=usage,
        )
