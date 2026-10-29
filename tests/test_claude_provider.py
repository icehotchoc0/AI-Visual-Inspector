"""Claude provider request/response handling, with the SDK client mocked -
no network. Verifies the request shape the skill mandates (structured
output via output_config, server-side fallback beta) and the error paths
the router relies on."""
import json
from types import SimpleNamespace

import pytest

from backend.vlm.base import VLMError, VLMImage, VLMRequest
from backend.vlm.claude_provider import ClaudeProvider

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["a"], "properties": {"a": {"type": "string"}}}


def _resp(text='{"a": "x"}', stop_reason="end_turn", category=None):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category=category) if category else None,
        content=[SimpleNamespace(type="text", text=text)],
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
    )


@pytest.fixture
def provider():
    p = ClaudeProvider(model="claude-opus-5-5", effort="medium", api_key="test-key")
    p._captured = {}

    def fake_create(**kwargs):
        p._captured.update(kwargs)
        return p._next
    p._client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=fake_create)))
    return p


def _req(images=1):
    return VLMRequest(system="sys", prompt="질문", images=[VLMImage(data=b"\xff\xd8jpeg", media_type="image/jpeg", label="[사진 1]") for _ in range(images)], schema=SCHEMA, max_tokens=2048)


def test_request_shape(provider):
    provider._next = _resp()
    out = provider.complete_json(_req())
    k = provider._captured
    assert k["model"] == "claude-opus-5-5"
    assert k["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert k["output_config"]["effort"] == "medium"
    assert k["betas"] == ["server-side-fallback-2026-07-01"] and k["fallbacks"] == "default"
    assert "thinking" not in k  # model default (always on for Opus 5.5)
    assert k["max_tokens"] == 2048
    content = k["messages"][0]["content"]
    assert [b["type"] for b in content] == ["text", "image", "text"]  # label, image, prompt
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert out.data == {"a": "x"} and out.provider == "claude" and out.usage["input_tokens"] == 10


def test_refusal_is_retryable_error(provider):
    provider._next = _resp(text="", stop_reason="refusal", category="general_harms")
    with pytest.raises(VLMError) as exc:
        provider.complete_json(_req())
    assert exc.value.retryable and "refused" in str(exc.value)


def test_truncation_and_non_json(provider):
    provider._next = _resp(text='{"a": ', stop_reason="max_tokens")
    with pytest.raises(VLMError):
        provider.complete_json(_req())
    provider._next = _resp(text="not json")
    with pytest.raises(VLMError):
        provider.complete_json(_req())


def test_sdk_errors_are_wrapped(provider):
    import anthropic
    import httpx

    def boom(**kwargs):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    provider._client.beta.messages.create = boom
    with pytest.raises(VLMError) as exc:
        provider.complete_json(_req())
    assert exc.value.provider == "claude" and exc.value.retryable


def test_missing_credentials_wrapped(provider):
    def boom(**kwargs):
        raise TypeError("Could not resolve authentication method")
    provider._client.beta.messages.create = boom
    with pytest.raises(VLMError) as exc:
        provider.complete_json(_req())
    assert exc.value.retryable and "TypeError" in str(exc.value)
