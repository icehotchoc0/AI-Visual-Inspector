"""SQLAlchemy store (review columns, queue, technician stats, hash search) and photo retention."""
import json
import os
import time

import pytest

from backend.photostore import PhotoStore
from backend.schemas import InspectionOptions, InspectionRequest, JobContext, PhotoInput
from tests.conftest import make_store, FakeVLM, make_photo, make_pipeline, to_b64, to_bytes


def _run(pipe, job_id, tech="T-1", seed=10, cat="plumbing", run_vlm=False):
    shots = ["site_before", "pipe_connection", "leak_test", "site_after"]
    req = InspectionRequest(
        job=JobContext(job_id=job_id, category=cat, technician_id=tech),
        photos=[PhotoInput(photo_id=f"p{i}", image_base64=to_b64(make_photo(seed + i)), declared_shot_type=s) for i, s in enumerate(shots)],
        options=InspectionOptions(run_vlm=run_vlm),
    )
    return pipe.run(req)


def test_store_roundtrip_review_and_lists(registry, test_settings, tmp_path):
    store = make_store(tmp_path, "s")
    pipe = make_pipeline(registry, test_settings, None, store)
    res = _run(pipe, "J-1")
    assert store.get(res.inspection_id).inspection_id == res.inspection_id
    assert store.get_review(res.inspection_id)["review_verdict"] is None
    assert store.list(review_state="pending")[0].inspection_id == res.inspection_id
    assert store.set_review(res.inspection_id, "FAIL", "reviewer-a", "배관 지지 없음")
    rv = store.get_review(res.inspection_id)
    assert rv["review_verdict"] == "FAIL" and rv["reviewed_by"] == "reviewer-a" and rv["reviewed_at"] is not None
    assert store.list(review_state="pending") == []
    assert store.list(review_state="reviewed")[0].review_verdict == "FAIL"
    reviewed = store.list_reviewed()
    assert reviewed[0]["review_notes"] == "배관 지지 없음" and reviewed[0]["result"].job_id == "J-1"
    assert not store.set_review("insp_nope", "PASS", "x", None)
    store.close()


def test_technician_stats_and_filters(registry, test_settings, tmp_path):
    store = make_store(tmp_path, "t")
    pipe = make_pipeline(registry, test_settings, None, store)
    _run(pipe, "J-a", tech="T-1", seed=100)
    _run(pipe, "J-b", tech="T-1", seed=200)
    _run(pipe, "J-c", tech="T-2", seed=300)
    stats = {s["technician_id"]: s for s in store.technician_stats()}
    assert stats["T-1"]["total"] == 2 and stats["T-2"]["total"] == 1
    assert 0 <= stats["T-1"]["pass_rate"] <= 1
    assert [i.job_id for i in store.list(technician_id="T-2")] == ["J-c"]
    assert store.stats()["backend"] in ("sqlite", "postgresql") and store.stats()["review_pending"] >= 0
    store.close()


def test_hash_history_cross_job(registry, test_settings, tmp_path):
    store = make_store(tmp_path, "h")
    pipe = make_pipeline(registry, test_settings, None, store)
    first = _run(pipe, "J-x", seed=500)
    phash = first.photos[0].meta.perceptual_hash
    assert store.find_similar(phash, 6, exclude_job_id="J-x") == []  # same job excluded
    m = store.find_similar(phash, 6, exclude_job_id="J-other")
    assert m and m[0].job_id == "J-x" and m[0].distance == 0
    store.close()


def test_queue_lifecycle_and_idempotency(tmp_path):
    store = make_store(tmp_path, "q")
    k1, created = store.enqueue('{"a":1}', "https://cb.example/hook", "idem-1")
    assert created
    k2, created2 = store.enqueue('{"a":1}', None, "idem-1")
    assert k2 == k1 and not created2
    k3, _ = store.enqueue('{"b":2}', None, None)
    job = store.claim_next()
    assert job["job_key"] == k1 and job["status"] == "running" and job["attempts"] == 1
    assert store.get_job(k3)["status"] == "queued"
    store.finish_job(k1, "insp_1", None)
    assert store.get_job(k1)["status"] == "done" and store.get_job(k1)["inspection_id"] == "insp_1"
    job2 = store.claim_next()
    store.finish_job(job2["job_key"], None, "boom")
    assert store.get_job(k3)["status"] == "failed" and store.claim_next() is None
    assert store.stats()["queue_active"] == 0
    store.close()


def test_requeue_stale_running(tmp_path):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    from backend.store import queue_jobs

    store = make_store(tmp_path, "r")
    k, _ = store.enqueue("{}", None, None)
    store.claim_next()
    with store.engine.begin() as conn:
        conn.execute(update(queue_jobs).where(queue_jobs.c.job_key == k).values(started_at=datetime.now(timezone.utc) - timedelta(hours=2)))
    assert store.requeue_stale_running(30) == 1 and store.get_job(k)["status"] == "queued"
    store.close()


def test_photo_store_policy_save_find_purge(tmp_path):
    ps = PhotoStore(tmp_path / "photos", retention_days=30, policy="review_only")
    assert ps.should_store("REVIEW") and ps.should_store("FAIL") and not ps.should_store("PASS")
    assert PhotoStore(tmp_path / "p2", policy="all").should_store("PASS")
    assert not PhotoStore(tmp_path / "p3", policy="off").should_store("FAIL")
    path = ps.save("insp_abc", "p1", b"img", "jpg")
    assert path.exists() and ps.find("insp_abc", "p1") == path
    assert ps.find("insp_abc", "nope") is None
    # traversal-ish ids are sanitised
    weird = ps.save("../../etc", "../passwd", b"x", "jpg")
    assert str(ps.root) in str(weird.resolve())
    old = time.time() - 40 * 86400
    os.utime(path, (old, old))
    assert ps.purge_expired() >= 1 and ps.find("insp_abc", "p1") is None


def test_pipeline_retains_photos_for_review_only(registry, test_settings, tmp_path):
    store = make_store(tmp_path, "pr")
    ps = PhotoStore(tmp_path / "ph", policy="review_only")
    pipe = make_pipeline(registry, test_settings, FakeVLM(fail_with="down"), store)  # VLM down -> REVIEW
    pipe.photo_store = ps
    res = _run(pipe, "J-r", seed=700, run_vlm=True)
    assert res.verdict == "REVIEW" and all(p.stored for p in res.photos)
    assert ps.find(res.inspection_id, "p0") is not None
    assert store.get_review(res.inspection_id)["photos_stored"] is True
    pipe2 = make_pipeline(registry, test_settings, FakeVLM(), store)
    pipe2.photo_store = ps
    res2 = _run(pipe2, "J-p", seed=800, run_vlm=True)
    assert res2.verdict == "PASS" and not any(p.stored for p in res2.photos)
    assert ps.find(res2.inspection_id, "p0") is None
    store.close()
