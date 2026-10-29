from backend.schemas import Finding, PhotoAnalysis, PhotoMeta, PhotoQuality, PhotoResult
from backend.verdict import decide, photo_status


def _photo(pid="p", findings=None, passed=True):
    return PhotoResult(
        photo_id=pid, status="OK", quality=PhotoQuality(width=800, height=600, blur_score=100, brightness=120, contrast=40, passed=passed),
        meta=PhotoMeta(perceptual_hash="0" * 16, sha256="x"), analysis=PhotoAnalysis(), findings=findings or [],
    )


def _f(code, kind, sev, conf=0.9, pid="p"):
    return Finding(code=code, kind=kind, source="vlm", severity=sev, confidence=conf, photo_id=pid, message=f"{code} msg")


def test_pass_when_clean(registry):
    cat = registry.get("plumbing")
    d = decide(cat, [_photo()], [], vlm_requested=True, vlm_available=True)
    assert d.verdict == "PASS" and d.summary.startswith("합격")


def test_high_defect_fails_only_when_confident(registry):
    cat = registry.get("plumbing")
    d = decide(cat, [_photo(findings=[_f("leak_visible", "defect", "high", 0.95)])], [], vlm_requested=True, vlm_available=True)
    assert d.verdict == "FAIL"
    d2 = decide(cat, [_photo(findings=[_f("leak_visible", "defect", "high", 0.45)])], [], vlm_requested=True, vlm_available=True)
    assert d2.verdict == "REVIEW"  # weak evidence -> human look, not an automatic fail
    d3 = decide(cat, [_photo(findings=[_f("leak_visible", "defect", "high", 0.2)])], [], vlm_requested=True, vlm_available=True)
    assert d3.verdict == "PASS"  # below the floor it is ignored


def test_medium_defect_reviews_low_defect_passes(registry):
    cat = registry.get("plumbing")
    assert decide(cat, [_photo(findings=[_f("corrosion", "defect", "medium")])], [], vlm_requested=True, vlm_available=True).verdict == "REVIEW"
    d = decide(cat, [_photo(findings=[_f("debris_left", "defect", "low")])], [], vlm_requested=True, vlm_available=True)
    assert d.verdict == "PASS" and any("경미" in r for r in d.reasons)


def test_confident_fraud_fails(registry):
    cat = registry.get("plumbing")
    f = Finding(code="duplicate_in_job", kind="fraud", source="fraud_check", severity="high", confidence=1.0, photo_id="p", message="dup")
    assert decide(cat, [_photo(findings=[f])], [], vlm_requested=True, vlm_available=True).verdict == "FAIL"


def test_missing_required_shot_policy(registry):
    cat = registry.get("plumbing")
    f = Finding(code="missing_required_shot", kind="checklist", source="checklist", severity="high", message="m", data={"shot_id": "leak_test"})
    assert decide(cat, [_photo()], [f], vlm_requested=True, vlm_available=True).verdict == "FAIL"
    review_cat = cat.model_copy(update={"verdict": cat.verdict.model_copy(update={"missing_required_shot": "review"})})
    assert decide(review_cat, [_photo()], [f], vlm_requested=True, vlm_available=True).verdict == "REVIEW"


def test_vlm_unavailable_forces_review(registry):
    cat = registry.get("plumbing")
    d = decide(cat, [_photo()], [], vlm_requested=True, vlm_available=False)
    assert d.verdict == "REVIEW" and any("VLM" in r for r in d.reasons)


def test_vlm_skipped_by_request_can_pass(registry):
    cat = registry.get("plumbing")
    d = decide(cat, [_photo()], [], vlm_requested=False, vlm_available=False)
    assert d.verdict == "PASS"


def test_different_location_fails(registry):
    cat = registry.get("plumbing")
    f = Finding(code="before_after_different_location", kind="consistency", source="vlm", severity="high", confidence=0.9, message="bg differs")
    assert decide(cat, [_photo()], [f], vlm_requested=True, vlm_available=True).verdict == "FAIL"
    f2 = f.model_copy(update={"confidence": 0.6})
    assert decide(cat, [_photo()], [f2], vlm_requested=True, vlm_available=True).verdict == "REVIEW"


def test_photo_status():
    assert photo_status(False, [], 0.6) == "REJECTED"
    assert photo_status(True, [], 0.6) == "OK"
    assert photo_status(True, [_f("x", "defect", "medium", 0.8)], 0.6) == "FLAGGED"
    assert photo_status(True, [_f("x", "defect", "low", 0.9)], 0.6) == "OK"
