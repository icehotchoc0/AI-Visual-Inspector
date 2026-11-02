"""
Deterministic photo-quality check, tuned for real field photos.

Field photos are low-texture scenes (smooth walls, appliance panels) taken
under room lighting, so the classic "variance of Laplacian" sharpness score
is misleading: it drops with brightness and rises with sensor noise. We
therefore measure sharpness as the strength of the *strongest edges*
(99.5th percentile of the Sobel gradient) on a brightness-normalised image.
A sharp photo of a plain wall still has crisp edges somewhere (appliance
outline, outlet, pipe); a blurred one does not, whatever the lighting.

Two tiers per issue:
  * warn   (severity medium) - the photo is still analysed and counts as
           evidence; the finding is informational ("조금 흐림").
  * reject (severity high)   - unusable; the photo is REJECTED, not sent
           to the VLM and does not satisfy a required shot.
Thresholds live in config/categories/_defaults.yaml (quality:) and can be
overridden per category. Use scripts/quality_calibrate.py on a folder of
your own photos to pick values.
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
    """Classic texture-based score. Reported for reference; not used for the decision."""
    try:
        import cv2

        return float(cv2.Laplacian(gray, cv2.CV_32F).var())
    except ImportError:  # pragma: no cover
        lap = -4 * gray[1:-1, 1:-1] + gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:]
        return float(lap.var())


def sharpness_score(gray: np.ndarray) -> float:
    """Strongest-edge strength (0..~255) on a percentile-stretched image.

    Brightness-invariant (a dim but sharp room scores like a bright one) and
    noise-tolerant (a percentile of the gradient ignores grain)."""
    lo, hi = np.percentile(gray, 1), np.percentile(gray, 99)
    norm = np.clip((gray - lo) / max(hi - lo, 1.0) * 255.0, 0, 255)
    try:
        import cv2

        gx = cv2.Sobel(norm, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(norm, cv2.CV_32F, 0, 1, ksize=3)
    except ImportError:  # pragma: no cover
        gx = np.zeros_like(norm); gy = np.zeros_like(norm)
        gx[:, 1:-1] = (norm[:, 2:] - norm[:, :-2]) * 4
        gy[1:-1, :] = (norm[2:, :] - norm[:-2, :]) * 4
    mag = np.sqrt(gx * gx + gy * gy) / 8.0
    return float(np.percentile(mag, 99.5))


def assess_quality(image: Image.Image, photo_id: str, t: QualityThresholds) -> tuple[PhotoQuality, list[Finding]]:
    width, height = image.size
    gray = _to_gray(image)
    blur = laplacian_variance(gray)
    brightness = float(gray.mean())
    contrast = float(gray.std())
    sharpness = sharpness_score(gray)
    findings: list[Finding] = []

    def add(code, severity, message, **data):
        findings.append(Finding(code=code, kind="quality", source="quality_check", severity=severity, photo_id=photo_id, message=message, data=data))

    if width < t.min_width or height < t.min_height:
        add("low_resolution", "medium", f"해상도가 낮습니다 ({width}x{height}). 가능하면 {t.min_width}x{t.min_height} 이상으로 촬영해 주세요.", width=width, height=height)

    # ---- exposure: severe first, then mild ----
    exposure_severe = False
    if brightness < t.dark_reject:
        exposure_severe = True
        add("too_dark", "high", "사진이 너무 어두워 작업 부위를 알아볼 수 없습니다. 조명을 켜거나 플래시를 사용해 다시 촬영해 주세요.", brightness=round(brightness, 1), threshold=t.dark_reject)
    elif brightness > t.bright_reject:
        exposure_severe = True
        add("overexposed", "high", "사진이 심하게 과노출되어 세부가 보이지 않습니다. 다시 촬영해 주세요.", brightness=round(brightness, 1), threshold=t.bright_reject)
    elif brightness < t.dark_warn:
        add("dim", "medium", "사진이 다소 어둡습니다. 판정은 진행하지만 다음에는 조명을 켜고 촬영해 주세요.", brightness=round(brightness, 1), threshold=t.dark_warn)
    elif brightness > t.bright_warn:
        add("bright", "medium", "사진이 다소 밝습니다. 판정은 진행하지만 역광·플래시 반사를 피해 주세요.", brightness=round(brightness, 1), threshold=t.bright_warn)

    # ---- sharpness (brightness-normalised, so it still applies to dim photos) ----
    if not exposure_severe:
        if sharpness < t.sharpness_reject:
            add("blurry", "high", "사진이 흐려 작업 부위를 식별할 수 없습니다. 초점을 맞춰 다시 촬영해 주세요.", sharpness=round(sharpness, 1), threshold=t.sharpness_reject)
        elif sharpness < t.sharpness_warn:
            add("low_sharpness", "medium", "사진이 조금 흐립니다. 판정은 진행하지만 세부 하자는 놓칠 수 있습니다.", sharpness=round(sharpness, 1), threshold=t.sharpness_warn)
        elif contrast < t.low_contrast_threshold and t.dark_warn <= brightness <= t.bright_warn:
            # absolute contrast is meaningless on a dim/bright photo (already warned above)
            add("low_contrast", "medium", "사진의 대비가 낮아 작업 부위 식별이 어려울 수 있습니다.", contrast=round(contrast, 1), threshold=t.low_contrast_threshold)

    passed = not any(f.severity == "high" for f in findings)
    quality = PhotoQuality(width=width, height=height, blur_score=round(blur, 2), sharpness=round(sharpness, 1),
                           brightness=round(brightness, 1), contrast=round(contrast, 1), passed=passed)
    return quality, findings
