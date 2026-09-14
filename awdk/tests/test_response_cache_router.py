"""LLMRouter ↔ ResponseCache opt-in wiring (OQ16 lever 2).

The cache is OFF unless a ResponseCache is attached to the router AND the call
passes cacheable=True — so default behavior (every call hits the provider) is
unchanged. When both are true, an identical request is served from cache.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.llm import LLMRouter
from adk.llm.base import LLMResponse, Message, ProviderCapabilities
from adk.llm.cache import ResponseCache


class _CountingProvider:
    """Minimal provider that counts chat() calls and returns a unique response."""

    def __init__(self):
        self.calls = 0

    def capabilities(self):
        return ProviderCapabilities()

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return LLMResponse(content=f"resp-{self.calls}")


def _wire(router, provider, monkeypatch):
    async def _get_provider():
        return provider
    monkeypatch.setattr(router, "get_provider", _get_provider)


async def test_cache_hit_serves_second_identical_call(monkeypatch):
    prov = _CountingProvider()
    router = LLMRouter(response_cache=ResponseCache())
    _wire(router, prov, monkeypatch)
    msgs = [Message(role="user", content="hello")]

    r1 = await router.chat(msgs, cacheable=True)
    r2 = await router.chat(msgs, cacheable=True)

    assert prov.calls == 1                      # second served from cache
    assert r1.content == r2.content == "resp-1"
    assert r2.cache_status == "response_hit"


async def test_no_flag_means_no_caching(monkeypatch):
    prov = _CountingProvider()
    router = LLMRouter(response_cache=ResponseCache())
    _wire(router, prov, monkeypatch)
    msgs = [Message(role="user", content="hi")]

    await router.chat(msgs)                      # cacheable defaults False
    await router.chat(msgs)
    assert prov.calls == 2                       # provider hit both times


async def test_no_cache_attached_means_no_caching(monkeypatch):
    prov = _CountingProvider()
    router = LLMRouter()                          # no response_cache
    _wire(router, prov, monkeypatch)
    msgs = [Message(role="user", content="hi")]

    await router.chat(msgs, cacheable=True)       # flag ignored without a cache
    await router.chat(msgs, cacheable=True)
    assert prov.calls == 2


async def test_different_messages_miss(monkeypatch):
    prov = _CountingProvider()
    router = LLMRouter(response_cache=ResponseCache())
    _wire(router, prov, monkeypatch)

    await router.chat([Message(role="user", content="a")], cacheable=True)
    await router.chat([Message(role="user", content="b")], cacheable=True)
    assert prov.calls == 2                        # distinct inputs → distinct keys
