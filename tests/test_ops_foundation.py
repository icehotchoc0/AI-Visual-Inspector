"""HEIC decoding, rate limiting, request IDs, metrics and CORS defaults."""
import io

import pytest
from fastapi.testclient import TestClient

import backend.app as app_module
from backend.ratelimit import TokenBucket, get_bucket
from tests.conftest import make_store, FakeVLM, make_photo, make_pipeline, to_b64


@pytest.fixture
def client(registry, test_settings, tmp_path, monkeypatch):
    store = make_store(tmp_path, "ops")
    pipe = make_pipeline(registry, test_settings, FakeVLM(), store)
    monkeypatch.setattr(app_module, "get_pipeline", lambda: pipe)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    monkeypatch.setattr(app_module, "get_registry", lambda: registry)
    monkeypatch.setattr(app_module, "get_vlm", lambda: None)
    monkeypatch.setattr(app_module.settings, "api_keys", "")
    monkeypatch.setattr(app_module.settings, "rate_limit_per_minute", 0)
    with TestClient(app_module.app) as c:
        yield c
    store.close()


def test_heic_photo_is_decoded(registry, test_settings, tmp_path):
    pillow_heif = pytest.importorskip("pillow_heif")
    from backend.pipeline import HEIC_SUPPORTED
    from backend.schemas import InspectionOptions, InspectionRequest, JobContext, PhotoInput

    assert HEIC_SUPPORTED
    buf = io.BytesIO()
    pillow_heif.from_pillow(make_photo(41)).save(buf, quality=90)
    heic = buf.getvalue()
    pipe = make_pipeline(registry, test_settings, FakeVLM())
    req = InspectionRequest(job=JobContext(job_id="h", category="plumbing"), photos=[PhotoInput(photo_id="p1", declared_shot_type="site_before")], options=InspectionOptions(run_vlm=False))
    res = pipe.run(req, {"p1": heic})
    assert res.photos[0].quality.width == 800 and res.photos[0].status == "OK"


def test_token_bucket_blocks_then_refills():
    b = TokenBucket(rate_per_minute=60, burst=2)
    assert b.try_acquire("k")[0] and b.try_acquire("k")[0]
    allowed, wait = b.try_acquire("k")
    assert not allowed and 0 < wait <= 1.0
    assert b.try_acquire("other")[0]  # independent key


def test_rate_limit_returns_429(client, monkeypatch):
    monkeypatch.setattr(app_module.settings, "rate_limit_per_minute", 2)
    get_bucket().reset()
    assert client.get("/v1/categories").status_code == 200
    assert client.get("/v1/categories").status_code == 200
    r = client.get("/v1/categories")
    assert r.status_code == 429 and "Retry-After" in r.headers
    assert client.get("/health").status_code == 200  # health is never rate limited
    monkeypatch.setattr(app_module.settings, "rate_limit_per_minute", 0)


def test_request_id_is_echoed_or_generated(client):
    r = client.get("/health", headers={"X-Request-ID": "trace-123"})
    assert r.headers["X-Request-ID"] == "trace-123"
    r2 = client.get("/health")
    assert len(r2.headers["X-Request-ID"]) >= 8


def test_metrics_endpoint_counts_inspections(client):
    body = {"job": {"job_id": "M-1", "category": "plumbing"}, "photos": [{"photo_id": "p1", "image_base64": to_b64(make_photo(77)), "declared_shot_type": "site_before"}], "options": {"run_vlm": False}}
    assert client.post("/v1/inspections", json=body).status_code == 200
    m = client.get("/metrics")
    assert m.status_code == 200
    assert "inspector_inspections_total" in m.text and 'category="plumbing"' in m.text
    assert "inspector_http_requests_total" in m.text


def test_cors_off_by_default(client):
    r = client.options("/v1/categories", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
