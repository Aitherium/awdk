"""The vLLM port scan must not adopt a server that does not serve the requested model.

Measured 2026-09-10: with the real backend unreachable, ``LLMRouter._try_vllm`` scanned
[8120, 8200, ...], found localhost:8200 answering /health and /v1/models (Media Forge,
serving xybrid local models) and logged "Auto-detected vLLM at localhost:8200 (model:
the requested model)". Every chat turn then 503'd. A port that lists models is not
thereby the server we asked for.
"""
from __future__ import annotations

import pytest

from adk.llm import LLMRouter
from adk.llm import openai_compat


def _fake_models(port_models: dict[int, list[str]]):
    """Patch OpenAIProvider so /health answers on every listed port and /v1/models
    answers per port. Ports absent from the map are dead."""

    async def health_check(self):  # noqa: ANN001
        return _port(self) in port_models

    async def list_models(self):  # noqa: ANN001
        return list(port_models.get(_port(self), []))

    def _port(provider) -> int:  # noqa: ANN001
        base = str(getattr(provider, "base_url", "") or getattr(provider, "_base_url", ""))
        try:
            return int(base.rsplit(":", 1)[1].split("/", 1)[0])
        except (IndexError, ValueError):
            return -1

    return health_check, list_models


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("AITHER_VLLM_URL", "VLLM_URL", "AITHER_LLM_BASE_URL", "AITHER_VLLM_PORTS"):
        monkeypatch.delenv(k, raising=False)


async def test_scan_skips_a_port_that_serves_other_models(monkeypatch, clean_env):
    hc, lm = _fake_models({8200: ["other-model-a", "other-model-b"],
                           8000: ["wanted-model"]})
    monkeypatch.setattr(openai_compat.OpenAIProvider, "health_check", hc)
    monkeypatch.setattr(openai_compat.OpenAIProvider, "list_models", lm)
    router = LLMRouter(model="wanted-model")
    p = await router._try_vllm()
    assert p is not None
    assert ":8000/" in str(getattr(p, "base_url", "") or getattr(p, "_base_url", ""))


async def test_scan_returns_none_when_no_port_serves_the_model(monkeypatch, clean_env):
    hc, lm = _fake_models({8200: ["other-model-a"]})
    monkeypatch.setattr(openai_compat.OpenAIProvider, "health_check", hc)
    monkeypatch.setattr(openai_compat.OpenAIProvider, "list_models", lm)
    router = LLMRouter(model="wanted-model")
    assert await router._try_vllm() is None


async def test_scan_without_a_pinned_model_takes_the_first_server(monkeypatch, clean_env):
    hc, lm = _fake_models({8200: ["other-model-a"]})
    monkeypatch.setattr(openai_compat.OpenAIProvider, "health_check", hc)
    monkeypatch.setattr(openai_compat.OpenAIProvider, "list_models", lm)
    router = LLMRouter(model=None)
    p = await router._try_vllm()
    assert p is not None and p.default_model == "other-model-a"


async def test_llm_base_url_env_is_honoured_before_the_scan(monkeypatch, clean_env):
    monkeypatch.setenv("AITHER_LLM_BASE_URL", "http://127.0.0.1:8150/v1")
    hc, lm = _fake_models({8150: ["wanted-model"], 8200: ["other-model-a"]})
    monkeypatch.setattr(openai_compat.OpenAIProvider, "health_check", hc)
    monkeypatch.setattr(openai_compat.OpenAIProvider, "list_models", lm)
    router = LLMRouter(model="wanted-model")
    p = await router._try_vllm()
    assert p is not None
    assert ":8150/" in str(getattr(p, "base_url", "") or getattr(p, "_base_url", ""))
