import json

import pytest
from fastapi.testclient import TestClient

import backend.app as app_module
from backend.store import SQLiteStore
from tests.conftest import FakeVLM, make_photo, make_pipeline, to_b64, to_bytes


@pytest.fixture
def client(registry, test_settings, tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "api.db")
    pipe = make_pipeline(registry, test_settings, FakeVLM(), store)
    monkeypatch.setattr(app_module, "get_pipeline", lambda: pipe)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    monkeypatch.setattr(app_module, "get_registry", lambda: registry)
    monkeypatch.setattr(app_module, "get_vlm", lambda: None)
    monkeypatch.setattr(app_module.settings, "api_keys", "")
    with TestClient(app_module.app) as c:
        yield c
    store.close()


def _body():
    shots = ["indoor_unit_before", "indoor_unit_after", "outdoor_unit_after", "pipe_finish"]
    return {
        "job": {"job_id": "J-100", "category": "aircon_install", "technician_id": "T-7"},
        "photos": [{"photo_id": f"p{i}", "image_base64": to_b64(make_photo(500 + i)), "declared_shot_type": s} for i, s in enumerate(shots)],
    }


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and "aircon_install" in r.json()["categories"]


def test_categories(client):
    r = client.get("/v1/categories")
    assert r.status_code == 200
    ids = {c["id"] for c in r.json()}
    assert "plumbing" in ids
    air = next(c for c in r.json() if c["id"] == "aircon_install")
    assert any(s["required"] for s in air["shots"]) and air["defect_codes"]


def test_json_inspection_roundtrip(client):
    r = client.post("/v1/inspections", json=_body())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["verdict"] == "PASS" and data["job_id"] == "J-100" and len(data["photos"]) == 4
    r2 = client.get(f"/v1/inspections/{data['inspection_id']}")
    assert r2.status_code == 200 and r2.json()["inspection_id"] == data["inspection_id"]
    r3 = client.get("/v1/inspections", params={"job_id": "J-100"})
    assert r3.status_code == 200 and r3.json()[0]["verdict"] == "PASS"


def test_multipart_upload(client):
    shots = ["site_before", "pipe_connection", "leak_test", "site_after"]
    files = [("files", (f"{s}.jpg", to_bytes(make_photo(600 + i)), "image/jpeg")) for i, s in enumerate(shots)]
    data = {
        "job": json.dumps({"job_id": "J-200", "category": "plumbing"}),
        "photos": json.dumps([{"photo_id": s, "declared_shot_type": s, "phase": "before" if s == "site_before" else "after"} for s in shots]),
    }
    r = client.post("/v1/inspections/upload", data=data, files=files)
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "PASS"
    assert {p["photo_id"] for p in r.json()["photos"]} == set(shots)


def test_unknown_category_400(client):
    body = _body()
    body["job"]["category"] = "spaceship_repair"
    r = client.post("/v1/inspections", json=body)
    assert r.status_code == 400


def test_validation_422(client):
    r = client.post("/v1/inspections", json={"job": {"job_id": "x"}, "photos": []})
    assert r.status_code == 422


def test_not_found_404(client):
    assert client.get("/v1/inspections/insp_nope").status_code == 404


def test_api_key_enforced(client, monkeypatch):
    monkeypatch.setattr(app_module.settings, "api_keys", "secret-1,secret-2")
    assert client.get("/v1/categories").status_code == 401
    assert client.get("/v1/categories", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/v1/categories", headers={"X-API-Key": "secret-2"}).status_code == 200
    assert client.get("/health").status_code == 200  # health stays open for probes
