"""
Deterministic anomaly-detection layer.

This is the ONLY component allowed to decide "defective / not defective".
Everything downstream (the VLM explainer) describes that decision in
words - it never overrides it. Same "don't trust the agent's self-report,
verify independently" principle used in AI-Dev-Crew's test verification.

Wraps Anomalib's TorchInferencer. Anomalib's public inference API has
shifted slightly across 1.x releases - if `from anomalib.deploy import
TorchInferencer` fails or its returned object's attribute names differ on
your installed version, check `SETUP_GUIDE.md` -> "Troubleshooting" and
adjust the two spots marked ANOMALIB-API below. This file was written
against the documented anomalib>=1.1 API but not run end-to-end against a
live install (no GPU in this environment) - the code path from image in
to (score, heatmap) out has been kept deliberately small and isolated in
this class specifically so a version mismatch is a one-file fix, not a
rewrite.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from backend.config import settings


class ModelNotTrainedError(RuntimeError):
    """Raised when no checkpoint exists yet for a given category/backend."""


@dataclass
class DetectionResult:
    is_defective: bool
    anomaly_score: float
    heatmap: np.ndarray | None   # float array, same H x W as input, 0..1
    latency_ms: float


def _checkpoint_path(category: str, backend: str) -> Path:
    # Expected layout produced by training/train_patchcore.py and
    # training/train_efficientad.py:
    #   backend/data/checkpoints/<category>/<backend>/model.ckpt
    return settings.checkpoint_dir_path / category / backend / "model.ckpt"


class AnomalyDetector:
    """Loads one trained Anomalib model for one (category, backend) pair."""

    def __init__(self, category: str, backend: str | None = None):
        self.category = category
        self.backend = backend or settings.ad_backend
        self._inferencer = None
        self._load()

    def _load(self) -> None:
        ckpt = _checkpoint_path(self.category, self.backend)
        if not ckpt.exists():
            raise ModelNotTrainedError(
                f"No trained checkpoint at {ckpt}. Run training/train_{self.backend}.py "
                f"for category='{self.category}' first (see SETUP_GUIDE.md)."
            )
        # ANOMALIB-API: import kept local so the whole module still imports
        # (and the rest of the app still runs) even before anomalib/torch
        # are installed, e.g. while just exploring the API endpoints.
        from anomalib.deploy import TorchInferencer

        self._inferencer = TorchInferencer(path=str(ckpt), device="auto")

    def predict(self, image: Image.Image) -> DetectionResult:
        start = time.perf_counter()
        img_array = np.array(image.convert("RGB"))

        # ANOMALIB-API: TorchInferencer.predict returns an ImageResult-like
        # object. Field names below (pred_score, pred_label, anomaly_map)
        # match anomalib>=1.1's documented `ImageResult` dataclass.
        result = self._inferencer.predict(img_array)

        score = float(result.pred_score)
        anomaly_map = getattr(result, "anomaly_map", None)
        if anomaly_map is not None:
            anomaly_map = np.asarray(anomaly_map, dtype=np.float32)
            m_min, m_max = float(anomaly_map.min()), float(anomaly_map.max())
            if m_max > m_min:
                anomaly_map = (anomaly_map - m_min) / (m_max - m_min)

        is_defective = score >= settings.anomaly_threshold

        latency_ms = (time.perf_counter() - start) * 1000.0
        return DetectionResult(
            is_defective=is_defective,
            anomaly_score=score,
            heatmap=anomaly_map,
            latency_ms=latency_ms,
        )


def make_heatmap_overlay(image: Image.Image, heatmap: np.ndarray, alpha: float = 0.45) -> Image.Image:
    """Blend a 0..1 anomaly heatmap onto the original image using a JET colormap."""
    import cv2

    base = np.array(image.convert("RGB"))
    h, w = base.shape[:2]

    heatmap_resized = cv2.resize(heatmap, (w, h))
    heatmap_uint8 = np.uint8(255 * np.clip(heatmap_resized, 0, 1))
    heatmap_color = cv2.applyColorMap(heatmap_uint8, cv2.COLORMAP_JET)
    heatmap_color = cv2.cvtColor(heatmap_color, cv2.COLOR_BGR2RGB)

    overlay = np.uint8(base * (1 - alpha) + heatmap_color * alpha)
    return Image.fromarray(overlay)


# Simple process-wide cache so the API doesn't reload a checkpoint from
# disk on every request.
_detector_cache: dict[tuple[str, str], AnomalyDetector] = {}


def get_detector(category: str, backend: str | None = None) -> AnomalyDetector:
    backend = backend or settings.ad_backend
    key = (category, backend)
    if key not in _detector_cache:
        _detector_cache[key] = AnomalyDetector(category, backend)
    return _detector_cache[key]
