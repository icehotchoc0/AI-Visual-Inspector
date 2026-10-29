"""
Ollama provider (local fallback) - Qwen2.5-VL or any vision model served
by Ollama. Uses Ollama's `format=<json schema>` constrained decoding so the
output shape matches the Claude provider's. Smaller local models are less
reliable, so the pipeline records which provider produced each analysis and
the verdict rules can be tuned per provider if needed.
"""
from __future__ import annotations

import base64
import json
import re
import time

from backend.vlm.base import VLMError, VLMRequest, VLMResponse


class OllamaProvider:
    name = "ollama"

    def __init__(self, host: str = "http://localhost:11434", model: str = "qwen2.5vl:7b", timeout_s: float = 180.0):
        self.host = host
        self.model = model
        self.timeout_s = timeout_s

    def complete_json(self, request: VLMRequest) -> VLMResponse:
        try:
            import ollama
        except ImportError as exc:  # pragma: no cover
            raise VLMError("ollama package not installed", retryable=True, provider=self.name) from exc

        start = time.perf_counter()
        labels = [img.label for img in request.images if img.label]
        content = ("\n".join(labels) + "\n\n" if labels else "") + request.prompt
        images = [base64.standard_b64encode(img.data).decode("ascii") for img in request.images]
        try:
            client = ollama.Client(host=self.host, timeout=self.timeout_s)
            response = client.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": content, "images": images},
                ],
                format=request.schema,
                options={"temperature": 0},
            )
        except Exception as exc:  # noqa: BLE001 - ollama raises a mix of httpx/ResponseError types
            raise VLMError(f"ollama error: {type(exc).__name__}: {exc}", retryable=True, provider=self.name) from exc

        latency_ms = (time.perf_counter() - start) * 1000.0
        text = (response["message"]["content"] if isinstance(response, dict) else response.message.content) or ""
        data = _parse_json_object(text)
        if data is None:
            raise VLMError(f"non-JSON output: {text[:200]!r}", retryable=True, provider=self.name)
        return VLMResponse(data=data, provider=self.name, model=self.model, latency_ms=latency_ms, raw_text=text)


def _parse_json_object(text: str) -> dict | None:
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)  # tolerate ```json fences
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
