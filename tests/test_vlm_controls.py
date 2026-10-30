"""Circuit breaker, concurrency cap, local-only routing, prompt-cache blocks, per-job token budget."""
import threading
import time

import pytest

from backend.schemas import InspectionOptions, InspectionRequest, JobContext, PhotoInput
from backend.vlm.base import VLMError, VLMRequest, VLMResponse, VLMUnavailable
from backend.vlm.router import CircuitBreaker, VLMRouter
from tests.conftest import FakeVLM, make_photo, make_pipeline, to_b64

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["a"], "properties": {"a": {"type": "string"}}}


class P:
    def __init__(self, name, *, fail=False, local=False, delay=0.0):
        self.name, self.fail, self.local, self.delay = name, fail, local, delay
        self.calls = 0
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def complete_json(self, request):
        with self._lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.fail:
                raise VLMError("down", retryable=True, provider=self.name)
            return VLMResponse(data={"a": "x"}, provider=self.name, model="m", latency_ms=1)
        finally:
            with self._lock:
                self.active -= 1


def _req(local_only=False):
    return VLMRequest(system="s", prompt="p", images=[], schema=SCHEMA, local_only=local_only)


def test_breaker_opens_and_half_opens():
    b = CircuitBreaker(failures=2, cooldown_s=100)
    assert b.allow("x", now=0)
    b.record("x", False, now=0)
    assert b.allow("x", now=1)
    b.record("x", False, now=1)
    assert not b.allow("x", now=2) and b.state("x", now=2) == "open"
    assert b.allow("x", now=200)  # cooldown over -> one trial
    b.record("x", True, now=200)
    assert b.allow("x", now=201)


def test_router_skips_open_provider_and_falls_back():
    primary, backup = P("claude", fail=True), P("ollama", local=True)
    r = VLMRouter([primary, backup], breaker=CircuitBreaker(failures=2, cooldown_s=60))
    for _ in range(4):
        assert r.complete_json(_req()).provider == "ollama"
    assert primary.calls == 2  # after two failures the breaker stops calling it
    assert r.breaker.state("claude") == "open"


def test_concurrency_cap_limits_parallel_calls():
    slow = P("claude", delay=0.05)
    r = VLMRouter([slow], max_concurrency=2)
    threads = [threading.Thread(target=lambda: r.complete_json(_req())) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert slow.calls == 6 and slow.peak <= 2


def test_local_only_routes_past_cloud_providers():
    cloud, local = P("claude"), P("ollama", local=True)
    r = VLMRouter([cloud, local])
    assert r.complete_json(_req(local_only=True)).provider == "ollama" and cloud.calls == 0
    with pytest.raises(VLMUnavailable) as exc:
        VLMRouter([cloud]).complete_json(_req(local_only=True))
    assert "local_only" in exc.value.errors["router"]
    assert VLMRouter([cloud, local]).has_local() and not VLMRouter([cloud]).has_local()


def test_claude_request_uses_cacheable_system_blocks():
    from types import SimpleNamespace

    from backend.vlm.claude_provider import ClaudeProvider

    p = ClaudeProvider(api_key="k")
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(stop_reason="end_turn", stop_details=None, content=[SimpleNamespace(type="text", text='{"a": "x"}')], model="m",
                               usage=SimpleNamespace(input_tokens=5, output_tokens=2, cache_read_input_tokens=900, cache_creation_input_tokens=0))
    p._client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=fake_create)))
    req = VLMRequest(system="base", cacheable_context="카테고리 설명 ...", prompt="per-photo", images=[], schema=SCHEMA)
    out = p.complete_json(req)
    system = captured["system"]
    assert isinstance(system, list) and system[0]["text"] == "base"
    assert system[1]["text"].startswith("카테고리") and system[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system[0]
    assert captured["messages"][0]["content"][-1]["text"] == "per-photo"
    assert out.usage["cache_read_input_tokens"] == 900
    p.prompt_cache = False
    p.complete_json(req)
    assert "cache_control" not in captured["system"][1]
    assert p.complete_json(VLMRequest(system="only", prompt="x", images=[], schema=SCHEMA)) and captured["system"] == "only"


def _four(job_id, seed=1, **opts):
    shots = ["site_before", "pipe_connection", "leak_test", "site_after"]
    return InspectionRequest(job=JobContext(job_id=job_id, category="plumbing"),
                             photos=[PhotoInput(photo_id=f"p{i}", image_base64=to_b64(make_photo(seed + i)), declared_shot_type=s) for i, s in enumerate(shots)],
                             options=InspectionOptions(**opts))


def test_token_budget_skips_photos_and_forces_review(registry, test_settings, store):
    fake = FakeVLM(usage={"input_tokens": 100, "output_tokens": 20})
    pipe = make_pipeline(registry, test_settings, fake, store)
    pipe.max_workers = 1  # deterministic ordering for the budget check
    test_settings.vlm_max_tokens_per_job = 250  # the check runs before each call: 3 calls (120/240/360), the 4th photo is skipped
    res = pipe.run(_four("B-1"))
    assert res.verdict == "REVIEW"
    assert res.stats["vlm_budget_skipped"] == 1 and res.stats["vlm_calls"] == 3
    skipped = [p.photo_id for p in res.photos if any(f.code == "vlm_budget_exhausted" for f in p.findings)]
    assert len(skipped) == 1
    assert any("토큰 상한" in r for r in res.verdict_reasons)
    assert res.stats["prompt_version"]


def test_local_only_request_with_cloud_only_router_reviews(registry, test_settings, store):
    cloud = FakeVLM(local=False)
    pipe = make_pipeline(registry, test_settings, cloud, store)
    res = pipe.run(_four("L-1", local_only=True))
    assert res.verdict == "REVIEW" and cloud.calls == [] and res.stats["vlm_local_only"] is True
    local = FakeVLM(local=True)
    res2 = make_pipeline(registry, test_settings, local, store).run(_four("L-2", seed=300, local_only=True))
    assert res2.verdict == "PASS" and all(c.local_only for c in local.calls)


def test_category_privacy_policy_forces_local(registry, test_settings, store):
    cat = registry.get("plumbing")
    private = cat.model_copy(update={"privacy": cat.privacy.model_copy(update={"local_only": True})})
    cloud = FakeVLM(local=False)
    pipe = make_pipeline(registry, test_settings, cloud, store)
    pipe.registry = type("R", (), {"__contains__": lambda s, k: k == "plumbing", "get": lambda s, k: private, "ids": ["plumbing"]})()
    res = pipe.run(_four("C-1", seed=400))
    assert res.stats["vlm_local_only"] is True and cloud.calls == []


def test_cacheable_context_reaches_fake_provider(registry, test_settings, store):
    fake = FakeVLM()
    make_pipeline(registry, test_settings, fake, store).run(_four("K-1", seed=500))
    photo_calls = [c for c in fake.calls if c.task == "analyze_photo"]
    assert photo_calls and all(c.cacheable_context and "에어컨" not in c.cacheable_context and "배관" in c.cacheable_context for c in photo_calls)
    assert all(c.prompt.startswith("기사가 신고한 컷") for c in photo_calls)
