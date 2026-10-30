"""
Deterministic photo-quality check.

A photo that is blurry, too dark/bright, too small, or low-contrast cannot
serve as evidence of the work, so it is REJECTED (the technician should
retake it) before any model is asked to interpret it. All thresholds come
from the category config (config/categories/<group>/<id>.yaml -> quality).
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from backend.categories import QualityThresholds
from backend.schemas import Finding, PhotoQuality


def _to_gray(image: Image.Image, max_side: int = 1024) -> np.ndarray:
    img = image.convert("L")
    w, h = img.size
    scale = max(w, h) / max_side
    if scale > 1:
        img = img.resize((max(1, int(w / scale)), max(1, int(h / scale))), Image.BILINEAR)
    return np.asarray(img, dtype=np.float32)


def laplacian_variance(gray: np.ndarray) -> float:
    """Variance of the Laplacian - the standard cheap sharpness measure."""
    try:
        import cv2

        return float(cv2.Laplacian(gray, cv2.CV_32F).var())
    except ImportError:  # pragma: no cover - opencv is a hard dependency in prod
        lap = (
            -4 * gray[1:-1, 1:-1]
            + gray[:-2, 1:-1]
            + gray[2:, 1:-1]
            + gray[1:-1, :-2]
            + gray[1:-1, 2:]
        )
        return float(lap.var())


def assess_quality(image: Image.Image, photo_id: str, thresholds: QualityThresholds) -> tuple[PhotoQuality, list[Finding]]:
    width, height = image.size
    gray = _to_gray(image)
    blur = laplacian_variance(gray)
    brightness = float(gray.mean())
    contrast = float(gray.std())

    findings: list[Finding] = []

    if width < thresholds.min_width or height < thresholds.min_height:
        findings.append(
            Finding(
                code="low_resolution",
                kind="quality",
                source="quality_check",
                severity="medium",
                photo_id=photo_id,
                message=f"해상도가 너무 낮습니다 ({width}x{height}). 최소 {thresholds.min_width}x{thresholds.min_height} 이상으로 다시 촬영해 주세요.",
                data={"width": width, "height": height},
            )
        )
    exposure_ok = thresholds.dark_threshold <= brightness <= thresholds.bright_threshold
    # Only report blur when exposure is sane: a black or white frame has zero
    # detail by construction, and "too dark" is the actionable message there.
    if exposure_ok and blur < thresholds.blur_threshold:
        findings.append(
            Finding(
                code="blurry",
                kind="quality",
                source="quality_check",
                severity="high",
                photo_id=photo_id,
                message="사진이 흐립니다. 초점을 맞춰 다시 촬영해 주세요.",
                data={"blur_score": round(blur, 2), "threshold": thresholds.blur_threshold},
            )
        )
    if brightness < thresholds.dark_threshold:
        findings.append(
            Finding(
                code="too_dark",
                kind="quality",
                source="quality_check",
                severity="high",
                photo_id=photo_id,
                message="사진이 너무 어둡습니다. 조명을 켜거나 플래시를 사용해 다시 촬영해 주세요.",
                data={"brightness": round(brightness, 1), "threshold": thresholds.dark_threshold},
            )
        )
    elif brightness > thresholds.bright_threshold:
        findings.append(
            Finding(
                code="overexposed",
                kind="quality",
                source="quality_check",
                severity="high",
                photo_id=photo_id,
                message="사진이 과노출되어 세부가 보이지 않습니다. 다시 촬영해 주세요.",
                data={"brightness": round(brightness, 1), "threshold": thresholds.bright_threshold},
            )
        )
    elif contrast < thresholds.low_contrast_threshold:
        findings.append(
            Finding(
                code="low_contrast",
                kind="quality",
                source="quality_check",
                severity="medium",
                photo_id=photo_id,
                message="사진의 대비가 낮아 작업 부위를 식별하기 어렵습니다.",
                data={"contrast": round(contrast, 1), "threshold": thresholds.low_contrast_threshold},
            )
        )

    passed = not any(f.severity == "high" for f in findings)
    quality = PhotoQuality(
        width=width,
        height=height,
        blur_score=round(blur, 2),
        brightness=round(brightness, 1),
        contrast=round(contrast, 1),
        passed=passed,
    )
    return quality, findings
