"""Reviewer verdict API, review queue, technician stats, eval export."""
import json

import pytest
from fastapi.testclient import TestClient

import backend.app as app_module
from backend.photostore import PhotoStore
from backend.store import Store
from tests.conftest import FakeVLM, make_photo, make_pipeline, to_b64
from training.export_eval_set import export


@pytest.fixture
def env(registry, test_settings, tmp_path, monkeypatch):
    store = Store(f"sqlite:///{tmp_path}/rv.db")
    ps = PhotoStore(tmp_path / "photos", policy="review_only")
    pipe = make_pipeline(registry, test_settings, FakeVLM(fail_with="down"), store)  # VLM down -> every job REVIEW
    pipe.photo_store = ps
    monkeypatch.setattr(app_module, "get_pipeline", lambda: pipe)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    monkeypatch.setattr(app_module, "get_photo_store", lambda: ps)
    monkeypatch.setattr(app_module, "get_registry", lambda: registry)
    monkeypatch.setattr(app_module, "get_vlm", lambda: None)
    monkeypatch.setattr(app_module.settings, "api_keys", "")
    monkeypatch.setattr(app_module.settings, "rate_limit_per_minute", 0)
    with TestClient(app_module.app) as c:
        yield c, store, ps
    store.close()


def _submit(client, job_id, tech="T-9", seed=1):
    shots = ["site_before", "pipe_connection", "leak_test", "site_after"]
    body = {"job": {"job_id": job_id, "category": "plumbing", "technician_id": tech},
            "photos": [{"photo_id": f"p{i}", "image_base64": to_b64(make_photo(seed + i)), "declared_shot_type": s} for i, s in enumerate(shots)]}
    r = client.post("/v1/inspections", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_review_queue_and_decision(env):
    client, store, ps = env
    res = _submit(client, "R-1")
    assert res["verdict"] == "REVIEW" and all(p["stored"] for p in res["photos"])
    queue = client.get("/v1/reviews/queue").json()
    assert [q["inspection_id"] for q in queue] == [res["inspection_id"]]
    assert client.get(f"/v1/inspections/{res['inspection_id']}/review").json()["review_verdict"] is None
    img = client.get(f"/v1/inspections/{res['inspection_id']}/photos/p0")
    assert img.status_code == 200 and img.headers["content-type"].startswith("image/")
    assert client.get(f"/v1/inspections/{res['inspection_id']}/photos/zzz").status_code == 404
    r = client.post(f"/v1/inspections/{res['inspection_id']}/review", json={"verdict": "FAIL", "reviewer": "김검수", "notes": "#leak_visible 이음부 누수"})
    assert r.status_code == 200
    info = r.json()
    assert info["review_verdict"] == "FAIL" and info["auto_verdict"] == "REVIEW" and info["reviewed_by"] == "김검수"
    assert client.get("/v1/reviews/queue").json() == []
    assert client.get("/v1/inspections", params={"review_state": "reviewed"}).json()[0]["review_verdict"] == "FAIL"
    assert client.post("/v1/inspections/insp_nope/review", json={"verdict": "PASS", "reviewer": "x"}).status_code == 404
    assert client.post(f"/v1/inspections/{res['inspection_id']}/review", json={"verdict": "MAYBE", "reviewer": "x"}).status_code == 422


def test_technician_stats_endpoint(env):
    client, store, _ = env
    _submit(client, "S-1", tech="T-a", seed=100)
    _submit(client, "S-2", tech="T-a", seed=200)
    _submit(client, "S-3", tech="T-b", seed=300)
    rows = {r["technician_id"]: r for r in client.get("/v1/stats/technicians").json()}
    assert rows["T-a"]["total"] == 2 and rows["T-a"]["REVIEW"] == 2 and rows["T-b"]["total"] == 1


def test_export_eval_set_uses_reviewer_labels(env, tmp_path):
    client, store, ps = env
    res = _submit(client, "E-1", seed=400)
    store.set_review(res["inspection_id"], "PASS", "rev", "문제 없음")
    res2 = _submit(client, "E-2", seed=500)
    store.set_review(res2["inspection_id"], "FAIL", "rev", "#blurry 아님, 실제로는 #leak_visible")
    report = export(store, ps, "plumbing", tmp_path / "eval")
    assert report["exported"] == 2 and report["skipped"] == []
    lines = [json.loads(l) for l in (tmp_path / "eval" / "plumbing" / "jobs.jsonl").read_text(encoding="utf-8").splitlines()]
    by_job = {l["job_id"]: l for l in lines}
    assert by_job["E-1"]["expected_verdict"] == "PASS" and by_job["E-1"]["auto_verdict"] == "REVIEW"
    assert by_job["E-2"]["expected_verdict"] == "FAIL"
    assert all(p["shot_type"] in ("site_before", "pipe_connection", "leak_test", "site_after") for p in by_job["E-1"]["photos"])
    assert (tmp_path / "eval" / "plumbing" / "photos" / "E-1" / "p0.jpg").exists()
    # a tag only becomes a defect label when that code was actually a finding on the photo
    assert all(d["code"] != "blurry" for p in by_job["E-2"]["photos"] for d in p["defects"])
