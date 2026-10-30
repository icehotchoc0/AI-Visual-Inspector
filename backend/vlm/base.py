"""
Provider-agnostic VLM interface.

A provider receives one or more images plus a prompt and MUST return a JSON
object conforming to the given JSON schema. Everything above this layer
(pipeline, verdict) only ever sees validated dicts - never free text - so a
provider swap (Claude <-> local Qwen2.5-VL) changes nothing downstream.

Design rule carried over from the original project: the VLM is a
*descriptive* layer. It classifies what is in a photo and describes
visible defects with confidences; it never emits PASS/FAIL. The verdict is
computed by deterministic rules in backend/verdict.py.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from PIL import Image


class VLMError(RuntimeError):
    """A single provider failed. `retryable` tells the router whether to try the next provider."""

    def __init__(self, message: str, *, retryable: bool = True, provider: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.provider = provider


class VLMUnavailable(RuntimeError):
    """Every configured provider failed. Carries the per-provider errors."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("all VLM providers failed: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


@dataclass
class VLMImage:
    data: bytes
    media_type: str = "image/jpeg"
    label: str | None = None  # e.g. "사진 1 (작업 전)" - inserted as text before the image


@dataclass
class VLMRequest:
    system: str
    prompt: str
    images: list[VLMImage]
    schema: dict
    max_tokens: int = 4096
    task: str = "generic"  # for logging/metrics: analyze_photo | compare_before_after
    # Extra system text that is identical across many requests (e.g. the
    # category catalogue). Providers that support prompt caching mark it
    # cacheable; others just append it to the system prompt.
    cacheable_context: str | None = None
    local_only: bool = False  # only providers with .local == True may serve this request

    def full_system(self) -> str:
        return self.system if not self.cacheable_context else f"{self.system}\n\n{self.cacheable_context}"


@dataclass
class VLMResponse:
    data: dict
    provider: str
    model: str
    latency_ms: float
    raw_text: str = ""
    usage: dict = field(default_factory=dict)


@runtime_checkable
class VLMProvider(Protocol):
    name: str
    local: bool  # True when inference never leaves our infrastructure

    def complete_json(self, request: VLMRequest) -> VLMResponse: ...


def prepare_image(image: Image.Image, max_side: int = 1568, quality: int = 85) -> VLMImage:
    """Downscale + JPEG-encode for transport. Keeps aspect ratio; strips EXIF."""
    img = image.convert("RGB")
    w, h = img.size
    scale = max(w, h) / max_side
    if scale > 1:
        img = img.resize((max(1, round(w / scale)), max(1, round(h / scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return VLMImage(data=buf.getvalue(), media_type="image/jpeg")
