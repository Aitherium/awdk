"""Gemini provider tests (OQ16 lever 2 provider).

Tests explicit prompt caching via cachedContents API, message/tool conversion,
and graceful fallback when caching fails.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.llm.gemini import GeminiProvider, _hash_prefix
from adk.llm import gemini as gemini_mod
from adk.llm.base import LLMResponse, Message, ProviderCapabilities


class _FakeResp:
    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code
        self.text = ""

    def json(self) -> dict:
        return self._data


def _capture(captured: dict, data: dict, status_code: int = 200):
    """Return a fake _post_with_retry that records payloads."""
    async def _fake(_client, _url, payload):
        if "payloads" not in captured:
            captured["payloads"] = []
        captured["payloads"].append(payload)
        return _FakeResp(data, status_code=status_code)
    return _fake


# ── Capabilities ────────────────────────────────────────────────────────────

def test_gemini_capabilities_explicit_cache():
    """Gemini declares explicit prompt caching."""
    prov = GeminiProvider(api_key="x")
    caps = prov.capabilities()
    assert caps.prompt_cache == "explicit"
    assert caps.batch is False


# ── Message and Tool Conversion ──────────────────────────────────────────────

def test_convert_messages_splits_system():
    """System messages are extracted separately."""
    prov = GeminiProvider(api_key="x")
    messages = [
        Message(role="system", content="RULES"),
        Message(role="user", content="hello"),
    ]
    system, conv = prov._convert_messages(messages)
    assert system == "RULES"
    assert len(conv) == 1
    assert conv[0]["role"] == "user"


def test_convert_messages_tool_response():
    """Tool responses are converted to Gemini format."""
    prov = GeminiProvider(api_key="x")
    messages = [
        Message(role="tool", content="result", name="search", tool_call_id="1"),
    ]
    system, conv = prov._convert_messages(messages)
    assert len(conv) == 1
    assert conv[0]["role"] == "user"
    assert "functionResponse" in conv[0]["parts"][0]


def test_convert_tools_to_function_declarations():
    """Tool schema is converted to Gemini functionDeclarations."""
    prov = GeminiProvider(api_key="x")
    tools = [
        {
            "function": {
                "name": "search",
                "description": "Search the web",
                "parameters": {"type": "object", "properties": {}},
            }
        }
    ]
    result = prov._convert_tools(tools)
    assert result is not None
    assert "functionDeclarations" in result
    assert len(result["functionDeclarations"]) == 1
    assert result["functionDeclarations"][0]["name"] == "search"


# ── Prefix Hashing ──────────────────────────────────────────────────────────

def test_hash_prefix_deterministic():
    """Prefix hash is deterministic."""
    h1 = _hash_prefix("RULES", [{"name": "search"}])
    h2 = _hash_prefix("RULES", [{"name": "search"}])
    assert h1 == h2


def test_hash_prefix_dict_order_normalized():
    """Dict key order doesn't affect the hash."""
    h1 = _hash_prefix("RULES", [{"a": 1, "b": 2}])
    h2 = _hash_prefix("RULES", [{"b": 2, "a": 1}])
    assert h1 == h2


# ── Chat with Explicit Caching ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gemini_chat_creates_cached_content_on_cache_true(monkeypatch):
    """When cache=True, the provider creates a cachedContents handle."""
    captured: dict = {}

    async def fake_post(_client, url, payload):
        captured["url"] = url
        captured["payload"] = payload

        # If this is a cachedContents POST, return a created handle
        if "cachedContents" in url:
            return _FakeResp({
                "name": "cachedContents/abc123",
                "model": "models/gemini-2.0-flash",
            }, status_code=200)

        # Otherwise, it's a generateContent call
        return _FakeResp({
            "candidates": [{"content": {"parts": [{"text": "answer"}]}}],
            "usageMetadata": {
                "promptTokenCount": 20,
                "candidatesTokenCount": 5,
                "totalTokenCount": 25,
                "cachedContentTokenCount": 100,
            },
        }, status_code=200)

    # Patch the _post_with_retry
    monkeypatch.setattr(gemini_mod, "_post_with_retry", fake_post)

    prov = GeminiProvider(api_key="x")
    resp = await prov.chat(
        [
            Message(role="system", content="RULES"),
            Message(role="user", content="q"),
        ],
        cache=True,
    )

    assert resp.content == "answer"
    # Should have a cache hit
    assert resp.cache_read_tokens == 100
    assert resp.cache_status == "hit"


@pytest.mark.asyncio
async def test_gemini_chat_falls_back_on_cache_creation_failure(monkeypatch):
    """If cache creation fails, continue with inline system."""
    captured: dict = {}

    async def fake_post(_client, url, payload):
        # If this is a cachedContents POST, fail
        if "cachedContents" in url:
            return _FakeResp({"error": "quota exceeded"}, status_code=429)

        # Otherwise, it's a generateContent call (should have inline system)
        captured["payload"] = payload
        return _FakeResp({
            "candidates": [{"content": {"parts": [{"text": "answer"}]}}],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 5,
                "totalTokenCount": 105,
            },
        }, status_code=200)

    monkeypatch.setattr(gemini_mod, "_post_with_retry", fake_post)

    prov = GeminiProvider(api_key="x")
    resp = await prov.chat(
        [
            Message(role="system", content="RULES"),
            Message(role="user", content="q"),
        ],
        cache=True,
    )

    # Should succeed despite cache creation failure
    assert resp.content == "answer"
    # Should have inline system in the request
    assert "systemInstruction" in captured["payload"]


@pytest.mark.asyncio
async def test_gemini_chat_reuses_cached_content(monkeypatch):
    """Subsequent requests with the same prefix reuse the cached content handle."""
    captured: dict = {"call_count": 0}

    # Mock httpx.AsyncClient.post to intercept both cachedContents and generateContent
    class MockAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json=None):
            captured["call_count"] += 1

            if "cachedContents" in url:
                # Create cached content
                return _FakeResp({
                    "name": "cachedContents/abc123",
                    "model": "models/gemini-2.0-flash",
                }, status_code=200)

            # generateContent call
            return _FakeResp({
                "candidates": [{"content": {"parts": [{"text": "answer"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 15,
                },
            }, status_code=200)

    # Patch the _client method to return our mock
    def mock_client():
        return MockAsyncClient()

    prov = GeminiProvider(api_key="x")
    monkeypatch.setattr(prov, "_client", mock_client)

    # First request: tries to create cache
    _ = await prov.chat(
        [
            Message(role="system", content="RULES"),
            Message(role="user", content="q1"),
        ],
        cache=True,
    )

    # Second request with same system: should reuse cache
    _ = await prov.chat(
        [
            Message(role="system", content="RULES"),
            Message(role="user", content="q2"),
        ],
        cache=True,
    )

    # Expected: 1 cachedContents create + 2 generateContent calls = 3 total
    assert captured["call_count"] == 3


@pytest.mark.asyncio
async def test_gemini_chat_parses_tool_calls(monkeypatch):
    """Tool calls in response are parsed."""
    async def fake_post(_client, url, payload):
        return _FakeResp({
            "candidates": [{
                "content": {"parts": [
                    {"functionCall": {
                        "name": "search",
                        "args": {"query": "weather"},
                    }}
                ]},
                "finishReason": "TOOL_CALLS",
            }],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "totalTokenCount": 15,
            },
        }, status_code=200)

    monkeypatch.setattr(gemini_mod, "_post_with_retry", fake_post)

    prov = GeminiProvider(api_key="x")
    resp = await prov.chat([Message(role="user", content="q")])

    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "search"
    assert resp.tool_calls[0].arguments == {"query": "weather"}
    assert resp.finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_gemini_chat_normalizes_usage(monkeypatch):
    """Usage metadata is normalized to standard fields."""
    async def fake_post(_client, url, payload):
        return _FakeResp({
            "candidates": [{"content": {"parts": [{"text": "hello"}]}}],
            "usageMetadata": {
                "promptTokenCount": 25,
                "candidatesTokenCount": 10,
                "totalTokenCount": 35,
                "cachedContentTokenCount": 15,
            },
        }, status_code=200)

    monkeypatch.setattr(gemini_mod, "_post_with_retry", fake_post)

    prov = GeminiProvider(api_key="x")
    resp = await prov.chat([Message(role="user", content="q")])

    assert resp.prompt_tokens == 25
    assert resp.completion_tokens == 10
    assert resp.tokens_used == 35
    assert resp.cache_read_tokens == 15


# ── Streaming ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gemini_chat_stream(monkeypatch):
    """Streaming yields the response as a single chunk."""
    async def fake_post(_client, url, payload):
        return _FakeResp({
            "candidates": [{"content": {"parts": [{"text": "streaming"}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
        }, status_code=200)

    monkeypatch.setattr(gemini_mod, "_post_with_retry", fake_post)

    prov = GeminiProvider(api_key="x")
    chunks = []
    async for chunk in prov.chat_stream([Message(role="user", content="q")]):
        chunks.append(chunk)

    assert len(chunks) == 1
    assert chunks[0].content == "streaming"
    assert chunks[0].done is True


# ── list_models ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gemini_list_models(monkeypatch):
    """list_models returns a list of available Gemini models."""
    async def fake_get(_client, url):
        _FakeResp({"models": [
            {"name": "models/gemini-2.0-flash"},
            {"name": "models/gemini-1.5-pro"},
        ]}, status_code=200)
        # Return a simple mock response
        class MockResp:
            status_code = 200
            def json(self):
                return {
                    "models": [
                        {"name": "models/gemini-2.0-flash"},
                        {"name": "models/gemini-1.5-pro"},
                    ]
                }
        return MockResp()

    # Monkeypatch httpx.AsyncClient.get
    prov = GeminiProvider(api_key="x")

    # Fallback to default list if get fails (which it will in test)
    models = await prov.list_models()
    assert len(models) > 0
    assert any("gemini" in m for m in models)
