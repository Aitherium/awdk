"""`openrouter` is a first-class LLMRouter provider, on the same footing as deepseek.

Construction only — no client is built and no request is made, so the key comes
from the environment and nothing touches the network.
"""
from __future__ import annotations

import pytest

from adk.llm import LLMRouter


def test_constructs_with_openrouter_defaults(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    router = LLMRouter(provider="openrouter")

    assert router.provider_name == "openrouter"
    assert router._provider.base_url == "https://openrouter.ai/api/v1"
    assert router._provider.default_model == "deepseek/deepseek-v4.1-flash"


def test_key_comes_from_openrouter_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    router = LLMRouter(provider="openrouter")

    assert router._provider.api_key == "sk-or-test"


def test_explicit_key_wins_over_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    router = LLMRouter(provider="openrouter", api_key="sk-or-explicit")

    assert router._provider.api_key == "sk-or-explicit"


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        (2, "deepseek/deepseek-v4-flash"),
        (5, "deepseek/deepseek-v4-flash"),
        (8, "deepseek/deepseek-v4.1-flash"),
        (9, "deepseek/deepseek-v4-pro"),
    ],
)
def test_model_for_effort_tiers(monkeypatch, effort, expected):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    router = LLMRouter(provider="openrouter")

    assert router.model_for_effort(effort) == expected


def test_openrouter_sends_attribution_headers(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    headers = LLMRouter(provider="openrouter")._provider._headers()

    assert headers["HTTP-Referer"] == "https://aitherium.com"
    assert headers["X-OpenRouter-Title"] == "AitherOS"
    assert headers["Authorization"] == "Bearer sk-or-test"


def test_other_providers_get_no_attribution_headers(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
    headers = LLMRouter(provider="deepseek")._provider._headers()

    assert "HTTP-Referer" not in headers
    assert "X-OpenRouter-Title" not in headers


def test_unknown_provider_still_raises(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    with pytest.raises(ValueError, match="Unknown provider: no-such-provider"):
        LLMRouter(provider="no-such-provider")


def test_deepseek_is_unchanged(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
    router = LLMRouter(provider="deepseek")

    assert router.provider_name == "deepseek"
    assert router._provider.base_url == "https://api.deepseek.com/v1"
    assert router._provider.default_model == "deepseek-chat"
    assert router.model_for_effort(2) == "deepseek-chat"
    assert router.model_for_effort(9) == "deepseek-reasoner"
