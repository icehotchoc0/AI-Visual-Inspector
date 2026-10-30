"""
Shared fixtures. No GPU, no network, no Anthropic/Ollama calls: the VLM is a
scripted fake that returns whatever the test tells it to.
"""
from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.categories import CategoryRegistry  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.pipeline import InspectionPipeline  # noqa: E402
from backend.store import SQLiteStore  # noqa: E402
from backend.vlm.base import VLMError, VLMRequest, VLMResponse  # noqa: E402
from backend.vlm.router import VLMRouter  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------- images ----
def make_photo(seed: int, size=(800, 600), blur: float = 0.0, brightness: int | None = None) -> Image.Image:
    """A distinctive, sharp synthetic 'photo': gradient + random rectangles + texture."""
    rng = np.random.default_rng(seed)
    w, h = size
    base = np.zeros((h, w, 3), dtype=np.float32)
    base[..., 0] = np.linspace(40, 200, w)[None, :]
    base[..., 1] = np.linspace(60, 180, h)[:, None]
    base[..., 2] = 120
    img = Image.fromarray(base.astype(np.uint8))
    draw = ImageDraw.Draw(img)
    for _ in range(12):
        x0, y0 = rng.integers(0, w - 100), rng.integers(0, h - 100)
        x1, y1 = x0 + rng.integers(40, 300), y0 + rng.integers(40, 300)
        color = tuple(int(c) for c in rng.integers(0, 255, 3))
        draw.rectangle([x0, y0, min(x1, w - 1), min(y1, h - 1)], fill=color, outline=(0, 0, 0), width=3)
    noise = rng.normal(0, 12, (h, w, 3))
    arr = np.clip(np.asarray(img, dtype=np.float32) + noise, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    if brightness is not None:
        img = Image.fromarray(np.full((h, w, 3), brightness, dtype=np.uint8))
    return img


def to_bytes(img: Image.Image, fmt: str = "JPEG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, quality=92)
    return buf.getvalue()


def to_b64(img: Image.Image) -> str:
    return base64.b64encode(to_bytes(img)).decode("ascii")


# ------------------------------------------------------------ fake VLM ----
class FakeVLM:
    """Scripted provider. `script` maps a declared shot type (parsed from the
    prompt) to the analysis dict; `compare` is returned for before/after."""

    name = "fake"
    model = "fake-vlm"

    def __init__(self, script: dict | None = None, compare: dict | None = None, fail_with: str | None = None):
        self.script = script or {}
        self.compare = compare
        self.fail_with = fail_with
        self.calls: list[VLMRequest] = []

    def complete_json(self, request: VLMRequest) -> VLMResponse:
        self.calls.append(request)
        if self.fail_with:
            raise VLMError(self.fail_with, retryable=True, provider=self.name)
        if request.task == "compare_before_after":
            data = self.compare or {"same_location": True, "confidence": 0.9, "work_completed_visible": True, "changes_description": "설비가 설치되었습니다.", "concerns": []}
            return VLMResponse(data=dict(data), provider=self.name, model=self.model, latency_ms=1.0)
        declared = None
        for line in request.prompt.splitlines():
            if line.startswith("기사가 신고한 컷: ") and "없음" not in line:
                declared = line.split(":", 1)[1].strip().split(" ")[0]
        data = self.script.get(declared) or self.script.get("*") or {
            "shot_type": declared or "other",
            "shot_confidence": 0.9 if declared else 0.8,
            "work_area_visible": True,
            "scene_description": "작업 부위가 보입니다.",
            "visibility_note": "",
            "defects": [],
        }
        return VLMResponse(data=dict(data), provider=self.name, model=self.model, latency_ms=1.0, usage={"input_tokens": 100, "output_tokens": 20})


def analysis(shot_type: str, defects: list | None = None, conf: float = 0.9, visible: bool = True) -> dict:
    return {
        "shot_type": shot_type,
        "shot_confidence": conf,
        "work_area_visible": visible,
        "scene_description": f"{shot_type} 장면",
        "visibility_note": "",
        "defects": defects or [],
    }


def defect(code: str, severity: str = "high", conf: float = 0.9) -> dict:
    return {"code": code, "title": code, "description": f"{code} 관찰", "severity": severity, "confidence": conf, "location": "중앙", "evidence": "보임"}


# ------------------------------------------------------------ fixtures ----
@pytest.fixture(scope="session")
def registry() -> CategoryRegistry:
    return CategoryRegistry.from_path(ROOT / "config" / "categories")


@pytest.fixture
def test_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        INSPECTOR_API_KEYS="",
        INSPECTOR_DB_PATH=str(tmp_path / "test.db"),
        INSPECTOR_VLM_PROVIDERS="claude,ollama",
    )


@pytest.fixture
def store(tmp_path) -> SQLiteStore:
    s = SQLiteStore(tmp_path / "store.db")
    yield s
    s.close()


def make_pipeline(registry, settings, fake: FakeVLM | None, store=None) -> InspectionPipeline:
    vlm = VLMRouter([fake]) if fake is not None else None
    return InspectionPipeline(registry, settings, vlm=vlm, store=store, anomaly_enabled=False, max_workers=2)
