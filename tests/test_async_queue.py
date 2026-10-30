"""Async mode: 202 + job status, worker processing, signed callbacks, idempotency."""
import json

import pytest
from fastapi.testclient import TestClient

import backend.app as app_module
from backend.queue import QueueWorker, verify_signature
from tests.conftest import make_store, FakeVLM, make_photo, make_pipeline, to_b64, to_bytes


@pytest.fixture
def env(registry, test_settings, tmp_path, monkeypatch):
    store = make_store(tmp_path, "q")
    pipe = make_pipeline(registry, test_settings, FakeVLM(), store)
    monkeypatch.setattr(app_module, "get_pipeline", lambda: pipe)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    monkeypatch.setattr(app_module, "get_photo_store", lambda: None)
    monkeypatch.setattr(app_module, "get_registry", lambda: registry)
    monkeypatch.setattr(app_module, "get_vlm", lambda: None)
    monkeypatch.setattr(app_module.settings, "api_keys", "")
    monkeypatch.setattr(app_module.settings, "rate_limit_per_minute", 0)
    monkeypatch.setattr(app_module.settings, "queue_workers", 0)  # drive the worker by hand
    sent = []

    def fake_sender(url, body, headers):
        sent.append((url, body, headers))
        return 200

    worker = QueueWorker(store, pipe, workers=0, secret="topsecret", sender=fake_sender)
    with TestClient(app_module.app) as c:
        yield c, store, worker, sent
    store.close()


def _body(job_id, seed=1, callback=None, category="plumbing"):
    shots = ["site_before", "pipe_connection", "leak_test", "site_after"]
    b = {"job": {"job_id": job_id, "category": category}, "photos": [{"photo_id": f"p{i}", "image_base64": to_b64(make_photo(seed + i)), "declared_shot_type": s} for i, s in enumerate(shots)]}
    if callback:
        b["options"] = {"callback_url": callback}
    return b


def test_async_roundtrip_with_signed_callback(env):
    client, store, worker, sent = env
    r = client.post("/v1/inspections", params={"mode": "async"}, json=_body("A-1", callback="https://ops.example/hook"))
    assert r.status_code == 202
    job = r.json()
    assert job["status"] == "queued" and job["job_key"].startswith("q_")
    assert client.get(f"/v1/jobs/{job['job_key']}").json()["status"] == "queued"
    assert worker.process_one() is True
    done = client.get(f"/v1/jobs/{job['job_key']}").json()
    assert done["status"] == "done" and done["result"]["verdict"] == "PASS" and done["inspection_id"] == done["result"]["inspection_id"]
    assert client.get(f"/v1/jobs/{job['job_key']}", params={"include_result": "false"}).json()["result"] is None
    url, body, headers = sent[0]
    assert url == "https://ops.example/hook"
    payload = json.loads(body)
    assert payload["job_key"] == job["job_key"] and payload["verdict"] == "PASS" and payload["result"]["job_id"] == "A-1"
    assert verify_signature("topsecret", body, headers["X-Inspector-Signature"])
    assert not verify_signature("wrong", body, headers["X-Inspector-Signature"])
    assert worker.process_one() is False  # queue drained


def test_async_multipart_and_failed_job(env):
    client, store, worker, sent = env
    files = [("files", ("site_before.jpg", to_bytes(make_photo(50)), "image/jpeg"))]
    data = {"job": json.dumps({"job_id": "A-2", "category": "spaceship"}), "photos": json.dumps([{"photo_id": "site_before", "declared_shot_type": "site_before"}])}
    r = client.post("/v1/inspections/upload", params={"mode": "async"}, data=data, files=files)
    assert r.status_code == 202
    worker.process_one()
    job = client.get(f"/v1/jobs/{r.json()['job_key']}").json()
    assert job["status"] == "failed" and "Unknown category" in job["error"] and sent == []
    assert client.get("/v1/jobs/q_nope").status_code == 404


def test_idempotency_async_and_sync(env):
    client, store, worker, _ = env
    h = {"Idempotency-Key": "order-777"}
    r1 = client.post("/v1/inspections", params={"mode": "async"}, json=_body("I-1", seed=100), headers=h)
    r2 = client.post("/v1/inspections", params={"mode": "async"}, json=_body("I-1", seed=100), headers=h)
    assert r1.status_code == r2.status_code == 202 and r1.json()["job_key"] == r2.json()["job_key"]
    assert store.stats()["queue_active"] == 1
    worker.process_one()
    # sync call with the same key returns the stored result instead of running again
    r3 = client.post("/v1/inspections", json=_body("I-1", seed=100), headers=h)
    assert r3.status_code == 200 and r3.json()["inspection_id"] == client.get(f"/v1/jobs/{r1.json()['job_key']}").json()["inspection_id"]
    # sync-first idempotency
    s1 = client.post("/v1/inspections", json=_body("I-2", seed=200), headers={"Idempotency-Key": "sync-1"})
    s2 = client.post("/v1/inspections", json=_body("I-2", seed=200), headers={"Idempotency-Key": "sync-1"})
    assert s1.json()["inspection_id"] == s2.json()["inspection_id"]
    assert len(store.list(job_id="I-2")) == 1


def test_worker_threads_process_queue(registry, test_settings, tmp_path):
    import time

    from backend.schemas import InspectionOptions, InspectionRequest, JobContext, PhotoInput

    store = make_store(tmp_path, "w")
    pipe = make_pipeline(registry, test_settings, FakeVLM(), store)
    req = InspectionRequest(job=JobContext(job_id="W-1", category="plumbing"), photos=[PhotoInput(photo_id="p0", image_base64=to_b64(make_photo(9)), declared_shot_type="site_before")], options=InspectionOptions(run_vlm=False))
    key, _ = store.enqueue(req.model_dump_json(), None, None)
    w = QueueWorker(store, pipe, workers=1, poll_interval_s=0.05)
    w.start()
    for _ in range(100):
        if store.get_job(key)["status"] == "done":
            break
        time.sleep(0.05)
    w.stop()
    assert store.get_job(key)["status"] == "done"
    store.close()
