import pytest

from backend.vlm.base import VLMError, VLMRequest, VLMResponse, VLMUnavailable
from backend.vlm.router import SchemaViolation, VLMRouter, validate_against_schema

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["a", "b"], "properties": {"a": {"type": "string", "enum": ["x", "y"]}, "b": {"type": "number", "minimum": 0, "maximum": 1}}}


class P:
    def __init__(self, name, data=None, err=None, retryable=True):
        self.name, self.data, self.err, self.retryable = name, data, err, retryable
        self.calls = 0

    def complete_json(self, request):
        self.calls += 1
        if self.err:
            raise VLMError(self.err, retryable=self.retryable, provider=self.name)
        return VLMResponse(data=dict(self.data), provider=self.name, model="m", latency_ms=1)


def _req():
    return VLMRequest(system="s", prompt="p", images=[], schema=SCHEMA)


def test_primary_success_skips_fallback():
    a, b = P("claude", {"a": "x", "b": 0.5}), P("ollama", {"a": "y", "b": 0.1})
    r = VLMRouter([a, b]).complete_json(_req())
    assert r.provider == "claude" and b.calls == 0


def test_fallback_on_error():
    a, b = P("claude", err="refused"), P("ollama", {"a": "y", "b": 0.1})
    r = VLMRouter([a, b]).complete_json(_req())
    assert r.provider == "ollama"


def test_fallback_on_schema_violation():
    a, b = P("claude", {"a": "zzz", "b": 0.5}), P("ollama", {"a": "y", "b": 0.1})
    assert VLMRouter([a, b]).complete_json(_req()).provider == "ollama"


def test_all_fail_raises_with_errors():
    with pytest.raises(VLMUnavailable) as exc:
        VLMRouter([P("claude", err="e1"), P("ollama", err="e2")]).complete_json(_req())
    assert set(exc.value.errors) == {"claude", "ollama"}


def test_non_retryable_stops_chain():
    a, b = P("claude", err="fatal", retryable=False), P("ollama", {"a": "y", "b": 0.1})
    with pytest.raises(VLMUnavailable):
        VLMRouter([a, b]).complete_json(_req())
    assert b.calls == 0


def test_validator_drops_extra_keys_and_checks_types():
    d = {"a": "x", "b": 0.5, "extra": 1}
    validate_against_schema(d, SCHEMA)
    assert "extra" not in d
    with pytest.raises(SchemaViolation):
        validate_against_schema({"a": "x", "b": "no"}, SCHEMA)
    with pytest.raises(SchemaViolation):
        validate_against_schema({"a": "x", "b": 2}, SCHEMA)


def test_unexpected_provider_exception_falls_through():
    class Crashy:
        name = "crashy"

        def complete_json(self, request):
            raise TypeError("Could not resolve authentication method")

    r = VLMRouter([Crashy(), P("ollama", {"a": "y", "b": 0.1})]).complete_json(_req())
    assert r.provider == "ollama"
    with pytest.raises(VLMUnavailable) as exc:
        VLMRouter([Crashy()]).complete_json(_req())
    assert "TypeError" in exc.value.errors["crashy"]
