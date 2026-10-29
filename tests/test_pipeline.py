from backend.pipeline import PipelineError
from backend.schemas import InspectionOptions, InspectionRequest, JobContext, PhotoInput
from tests.conftest import FakeVLM, analysis, defect, make_photo, make_pipeline, to_b64

import pytest


def _req(cat_id, photos, job_id="job-1", **opts):
    return InspectionRequest(
        job=JobContext(job_id=job_id, category=cat_id, notes="테스트"),
        photos=[PhotoInput(photo_id=pid, image_base64=to_b64(img), declared_shot_type=shot, phase=phase) for pid, img, shot, phase in photos],
        options=InspectionOptions(**opts),
    )


def _plumbing_set(seed=100, blur=None):
    shots = ["site_before", "pipe_connection", "leak_test", "site_after"]
    out = []
    for i, s in enumerate(shots):
        img = make_photo(seed + i, blur=blur if (blur and s == "leak_test") else 0)
        out.append((f"p{i+1}", img, s, "before" if s == "site_before" else "after"))
    return out


def test_clean_job_passes(registry, test_settings, store):
    fake = FakeVLM()
    pipe = make_pipeline(registry, test_settings, fake, store)
    res = pipe.run(_req("plumbing", _plumbing_set()))
    assert res.verdict == "PASS", res.verdict_reasons
    assert all(p.status == "OK" for p in res.photos)
    assert all(i.satisfied for i in res.checklist if i.required)
    assert res.vlm_provider_used == "fake"
    assert res.stats["vlm_calls"] == 5  # 4 photos + 1 before/after compare
    assert store.get(res.inspection_id) is not None
    assert store.list()[0].job_id == "job-1"


def test_high_defect_fails_and_flags_photo(registry, test_settings, store):
    fake = FakeVLM(script={"pipe_connection": analysis("pipe_connection", [defect("leak_visible", "high", 0.92)])})
    res = make_pipeline(registry, test_settings, fake, store).run(_req("plumbing", _plumbing_set()))
    assert res.verdict == "FAIL"
    p2 = next(p for p in res.photos if p.photo_id == "p2")
    assert p2.status == "FLAGGED" and any(f.code == "leak_visible" and f.source == "vlm" for f in p2.findings)
    assert any("누수" in r or "leak_visible" in r for r in res.verdict_reasons)


def test_blurry_required_shot_causes_missing_and_fail(registry, test_settings, store):
    res = make_pipeline(registry, test_settings, FakeVLM(), store).run(_req("plumbing", _plumbing_set(blur=12)))
    assert res.verdict == "FAIL"
    p3 = next(p for p in res.photos if p.photo_id == "p3")
    assert p3.status == "REJECTED" and p3.analysis.provider == "none"  # VLM never sees rejected photos
    assert any(f.code == "missing_required_shot" and f.data["shot_id"] == "leak_test" for f in res.job_findings)


def test_duplicate_photo_fails(registry, test_settings, store):
    photos = _plumbing_set()
    photos[3] = ("p4", photos[1][1].resize((640, 480)), "site_after", "after")  # same image as p2
    res = make_pipeline(registry, test_settings, FakeVLM(), store).run(_req("plumbing", photos))
    assert res.verdict == "FAIL"
    assert any(f.code == "duplicate_in_job" for p in res.photos for f in p.findings)


def test_cross_job_reuse_detected_via_store(registry, test_settings, store):
    pipe = make_pipeline(registry, test_settings, FakeVLM(), store)
    first = pipe.run(_req("plumbing", _plumbing_set(seed=200), job_id="job-A"))
    assert first.verdict == "PASS"
    photos = _plumbing_set(seed=300)
    photos[0] = ("p1", make_photo(200), "site_before", "before")  # reused from job-A
    second = pipe.run(_req("plumbing", photos, job_id="job-B"))
    assert second.verdict == "FAIL"
    f = next(f for p in second.photos for f in p.findings if f.code == "reused_from_other_job")
    assert f.data["matched_job_id"] == "job-A"


def test_shot_mismatch_reclassifies_and_reviews(registry, test_settings, store):
    # Technician says p2 is pipe_connection, VLM is confident it's actually site_after.
    fake = FakeVLM(script={"pipe_connection": analysis("site_after", conf=0.95)})
    res = make_pipeline(registry, test_settings, fake, store).run(_req("plumbing", _plumbing_set()))
    p2 = next(p for p in res.photos if p.photo_id == "p2")
    assert p2.resolved_shot_type == "site_after"
    assert any(f.code == "shot_type_mismatch" for f in p2.findings)
    # pipe_connection is now missing -> FAIL under the default policy
    assert res.verdict == "FAIL"
    assert any(f.code == "missing_required_shot" and f.data["shot_id"] == "pipe_connection" for f in res.job_findings)


def test_vlm_down_forces_review_not_crash(registry, test_settings, store):
    fake = FakeVLM(fail_with="connection refused")
    res = make_pipeline(registry, test_settings, fake, store).run(_req("plumbing", _plumbing_set()))
    assert res.verdict == "REVIEW"
    assert res.stats["vlm_errors"] == res.stats["vlm_calls"] == 4
    assert all(p.resolved_shot_type == p.declared_shot_type for p in res.photos)  # falls back to declared shots


def test_before_after_different_location_fails(registry, test_settings, store):
    fake = FakeVLM(compare={"same_location": False, "confidence": 0.9, "work_completed_visible": True, "changes_description": "벽지가 다릅니다", "concerns": ["다른 현장으로 보임"]})
    res = make_pipeline(registry, test_settings, fake, store).run(_req("plumbing", _plumbing_set()))
    assert res.verdict == "FAIL"
    assert any(f.code == "before_after_different_location" for f in res.job_findings)


def test_run_vlm_false_skips_vlm(registry, test_settings, store):
    fake = FakeVLM()
    res = make_pipeline(registry, test_settings, fake, store).run(_req("plumbing", _plumbing_set(), run_vlm=False))
    assert fake.calls == [] and res.verdict == "PASS" and res.stats["vlm_calls"] == 0


def test_thumbnails_returned_on_request(registry, test_settings, store):
    res = make_pipeline(registry, test_settings, FakeVLM(), store).run(_req("plumbing", _plumbing_set(), return_thumbnails=True))
    assert all(p.thumbnail_png_base64 for p in res.photos)


def test_bad_inputs_raise_pipeline_error(registry, test_settings, store):
    pipe = make_pipeline(registry, test_settings, FakeVLM(), store)
    with pytest.raises(PipelineError):
        pipe.run(_req("not_a_category", _plumbing_set()))
    req = _req("plumbing", _plumbing_set())
    req.photos[0].image_base64 = "!!!notbase64"
    with pytest.raises(PipelineError):
        pipe.run(req)
    req2 = _req("plumbing", _plumbing_set())
    req2.photos[1].photo_id = req2.photos[0].photo_id
    with pytest.raises(PipelineError):
        pipe.run(req2)


def test_unknown_declared_shot_is_informational(registry, test_settings, store):
    photos = _plumbing_set()
    photos.append(("p5", make_photo(999), "made_up_shot", None))
    res = make_pipeline(registry, test_settings, FakeVLM(), store).run(_req("plumbing", photos))
    p5 = next(p for p in res.photos if p.photo_id == "p5")
    assert any(f.code == "unknown_declared_shot" for f in p5.findings)
