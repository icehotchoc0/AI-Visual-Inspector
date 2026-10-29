from backend.checks.quality import assess_quality
from tests.conftest import make_photo


def test_sharp_photo_passes(registry):
    q, findings = assess_quality(make_photo(1), "p", registry.get("aircon_install").quality)
    assert q.passed and findings == []


def test_blurry_photo_rejected(registry):
    q, findings = assess_quality(make_photo(1, blur=10), "p", registry.get("aircon_install").quality)
    assert not q.passed
    assert "blurry" in {f.code for f in findings}


def test_dark_photo_rejected_without_blur_noise(registry):
    q, findings = assess_quality(make_photo(1, brightness=8), "p", registry.get("aircon_install").quality)
    codes = {f.code for f in findings}
    assert not q.passed and "too_dark" in codes and "blurry" not in codes


def test_overexposed_photo_rejected(registry):
    q, findings = assess_quality(make_photo(1, brightness=250), "p", registry.get("aircon_install").quality)
    assert not q.passed and "overexposed" in {f.code for f in findings}


def test_low_resolution_is_medium_not_rejecting(registry):
    q, findings = assess_quality(make_photo(1, size=(320, 240)), "p", registry.get("aircon_install").quality)
    assert "low_resolution" in {f.code for f in findings}
    assert all(f.severity != "high" for f in findings if f.code == "low_resolution")
    assert q.passed  # low resolution alone flags, it does not reject
