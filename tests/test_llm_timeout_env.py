"""AITHER_LLM_TIMEOUT sets OpenAIProvider's read timeout from the unit file."""
from adk.llm.openai_compat import OpenAIProvider


def test_default_is_120(monkeypatch):
    monkeypatch.delenv("AITHER_LLM_TIMEOUT", raising=False)
    assert OpenAIProvider()._timeout == 120.0


def test_env_raises_the_default(monkeypatch):
    monkeypatch.setenv("AITHER_LLM_TIMEOUT", "900")
    assert OpenAIProvider()._timeout == 900.0


def test_explicit_timeout_beats_env(monkeypatch):
    monkeypatch.setenv("AITHER_LLM_TIMEOUT", "900")
    assert OpenAIProvider(timeout=5.0)._timeout == 5.0


def test_garbage_and_nonpositive_fall_back(monkeypatch):
    for raw in ("abc", "0", "-3"):
        monkeypatch.setenv("AITHER_LLM_TIMEOUT", raw)
        assert OpenAIProvider()._timeout == 120.0
