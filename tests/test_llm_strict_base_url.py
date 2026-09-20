"""A named endpoint is not silently replaced by whatever answers a port scan.

Measured 2026-09-20 on the live #agents responder: the same process alternated
between real answers and `[agent error: 503 ... on-device inference failed
(model gemma3npc-1b)]`. `health_check` allows the named endpoint 5 s; on a host
at load 100+ MicroScheduler routinely takes longer, and the fallback scan then
finds :8200 in milliseconds -- a server that health-checks, lists models and
503s every chat. That 503 was posted into the channel as the agent's own reply,
which is worse than an outage: it is the agent saying something false in its own
voice.
"""
from __future__ import annotations

import asyncio

import pytest

from adk.llm import LLMRouter


class _Unreachable:
    """The named endpoint, too slow (or too down) to pass a 5 s health check."""

    def __init__(self, *a, **kw):
        self.base_url = kw.get("base_url", "")
        self.default_model = kw.get("default_model", "")

    async def health_check(self):
        return False

    async def list_models(self):
        return []


def _client(monkeypatch, strict: str | None, scanned: list[str]) -> LLMRouter:
    monkeypatch.setenv("AITHER_VLLM_URL", "https://127.0.0.1:8150/v1")
    if strict is None:
        monkeypatch.delenv("AITHER_LLM_STRICT_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("AITHER_LLM_STRICT_BASE_URL", strict)
    import adk.llm.openai_compat as oc

    class _Recording(_Unreachable):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            scanned.append(self.base_url)

    monkeypatch.setattr(oc, "OpenAIProvider", _Recording)
    return LLMRouter()


@pytest.mark.parametrize("flag", ["1", "true", "YES", "on"])
def test_strict_pins_the_named_endpoint_without_a_health_check(monkeypatch, flag):
    scanned: list[str] = []
    client = _client(monkeypatch, flag, scanned)
    provider = asyncio.run(client._try_vllm())
    assert provider is not None, "a named endpoint must be used even when it is slow"
    assert provider.base_url == "https://127.0.0.1:8150/v1"
    assert scanned == ["https://127.0.0.1:8150/v1"], \
        f"strict mode must not probe anything else, probed {scanned}"


def test_without_the_flag_the_old_fallback_still_scans(monkeypatch):
    """Customers whose endpoint really is absent keep the discovery behaviour --
    the strictness is opt-in, because this platform's policy is not everyone's."""
    scanned: list[str] = []
    client = _client(monkeypatch, None, scanned)
    asyncio.run(client._try_vllm())
    assert len(scanned) > 1, "the unflagged path must still fall back to the scan"
