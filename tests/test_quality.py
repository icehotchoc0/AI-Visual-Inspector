from PIL import Image, ImageFilter

from backend.checks.quality import assess_quality, sharpness_score, _to_gray
from tests.conftest import make_photo


def _q(img, registry):
    return assess_quality(img, "p", registry.get("aircon_install").quality)


def test_sharp_photo_passes(registry):
    q, findings = _q(make_photo(1), registry)
    assert q.passed and findings == [] and q.sharpness > 30


def test_blurry_photo_rejected(registry):
    q, findings = _q(make_photo(1, blur=10), registry)
    assert not q.passed and "blurry" in {f.code for f in findings}


def test_slightly_soft_photo_only_warns(registry):
    q, findings = _q(make_photo(1, blur=3), registry)
    assert q.passed  # still analysed, counts as evidence
    assert {f.code for f in findings} == {"low_sharpness"} and findings[0].severity == "medium"


def test_dark_photo_rejected_without_blur_noise(registry):
    q, findings = _q(make_photo(1, brightness=8), registry)
    codes = {f.code for f in findings}
    assert not q.passed and "too_dark" in codes and "blurry" not in codes


def test_dim_but_sharp_room_passes_with_warning(registry):
    # a sharp photo in a dim room: brightness-normalised sharpness keeps it usable
    img = make_photo(2)
    dim = Image.eval(img, lambda v: int(v * 0.15))
    q, findings = _q(dim, registry)
    assert q.brightness < 35 and q.passed
    assert {f.code for f in findings} <= {"dim", "low_sharpness"} and "dim" in {f.code for f in findings}


def test_overexposed_photo_rejected(registry):
    q, findings = _q(make_photo(1, brightness=250), registry)
    assert not q.passed and "overexposed" in {f.code for f in findings}


def test_low_resolution_is_medium_not_rejecting(registry):
    q, findings = _q(make_photo(1, size=(320, 240)), registry)
    assert "low_resolution" in {f.code for f in findings}
    assert all(f.severity != "high" for f in findings if f.code == "low_resolution") and q.passed


def test_sharpness_is_brightness_invariant_and_noise_tolerant():
    img = make_photo(3)
    base = sharpness_score(_to_gray(img))
    dim = sharpness_score(_to_gray(Image.eval(img, lambda v: int(v * 0.3))))
    assert abs(base - dim) / base < 0.25
    blurred = sharpness_score(_to_gray(img.filter(ImageFilter.GaussianBlur(6))))
    assert blurred < base / 4


def test_legacy_threshold_keys_are_ignored():
    from backend.categories import QualityThresholds

    t = QualityThresholds(blur_threshold=60, sharpness_warn=40)
    assert t.sharpness_warn == 40 and t.sharpness_reject == 12.0
