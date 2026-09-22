"""Two schema/roundtrip defects measured on the first honest SWE-bench runs (2026-09-21).

1. ``ToolRegistry`` derived a tool's JSON schema by ``inspect.signature`` and advertised
   ``*args``/``**kwargs`` as parameters. A wrapper ``translated(*a, **kw)`` advertised ``a``
   and ``kw``; the builtin file tools' ``**_ignored`` advertised ``_ignored``; the model
   passed exactly what it was shown and every call died in a TypeError. It read as a model
   vocabulary defect for a month.
2. awdk dropped the response's ``reasoning_content``. DeepSeek v4-pro in thinking mode
   refuses the next tool round without it ("The `reasoning_content` in the thinking mode
   must be passed back to the API"), so the frontier arm could not run a single tool loop.
"""

from __future__ import annotations

import json

from adk.llm.base import LLMResponse, Message, messages_to_dicts
from adk.tools import ToolRegistry


def test_registry_never_advertises_var_args():
    reg = ToolRegistry()

    def file_read(path: str, start_line: int = 0, **_ignored) -> str:
        """read"""
        return path

    def wrapper(*a, **kw):
        """w"""
        return "x"

    reg.register(file_read, name="file_read", description="d")
    reg.register(wrapper, name="wrapper", description="d")
    assert set(reg._tools["file_read"].parameters["properties"]) == {"path", "start_line"}
    assert reg._tools["file_read"].parameters.get("required") == ["path"]
    assert reg._tools["wrapper"].parameters.get("properties", {}) == {}


def test_messages_to_dicts_emits_reasoning_only_when_set():
    calls = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    with_reasoning = Message(role="assistant", content="", tool_calls=calls, reasoning="think")
    without = Message(role="assistant", content="", tool_calls=calls)
    d1, d2 = messages_to_dicts([with_reasoning, without])
    assert d1["reasoning_content"] == "think" and d1["tool_calls"] == calls
    assert "reasoning_content" not in d2
    # still plain JSON for the OpenAI-compatible body
    json.dumps([d1, d2])


def test_llm_response_carries_reasoning_and_defaults_empty():
    assert LLMResponse(content="x").reasoning == ""
    assert LLMResponse(content="x", reasoning="r").reasoning == "r"


def test_provider_parses_reasoning_content(monkeypatch):
    """The non-streaming OpenAI-compatible path returns the reasoning channel on the
    response, and does not double it when the content fallback already promoted it."""
    import asyncio

    import httpx
    from adk.llm.openai_compat import OpenAIProvider

    body = {
        "id": "x", "model": "deepseek-v4-pro",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "", "reasoning_content": "let me look",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "file_read", "arguments": "{\"path\": \"a\"}"}}],
        }}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    provider = OpenAIProvider(base_url="https://example.invalid/v1", api_key="k",
                              default_model="deepseek-v4-pro")
    transport = httpx.MockTransport(handler)
    # _client() is the provider's client factory; hand it the mock transport
    monkeypatch.setattr(provider, "_client",
                        lambda: httpx.AsyncClient(transport=transport,
                                                  base_url="https://example.invalid/v1"))

    resp = asyncio.run(provider.chat([Message(role="user", content="hi")]))
    assert resp.tool_calls and resp.tool_calls[0].name == "file_read"
    assert resp.reasoning == "let me look"
    # a tool round keeps content empty: the reasoning travels back as reasoning_content,
    # not as content (the empty-answer promotion is for final answers only)
    assert resp.content == ""


def test_agent_tool_turn_keeps_reasoning_in_source():
    """The assistant turn the loop appends for a tool round carries the reasoning; a
    source assertion because driving the full ReAct loop needs a backend."""
    from pathlib import Path

    import adk.agent as agent_mod

    src = Path(agent_mod.__file__).read_text(encoding="utf-8", errors="replace")
    assert 'reasoning=(getattr(resp, "reasoning", "") or None)' in src


def test_every_assistant_message_the_loop_appends_carries_reasoning():
    """DeepSeek thinking mode 400s ("reasoning_content ... must be passed back") on the
    call AFTER any assistant message that lost its reasoning. Measured 2026-09-21 on the
    S2 harness: the tool-call site carried it, the four TEXT continuations (steering /
    nudge / advisor) did not, and a run died at step 4 with HTTPStatusError."""
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "adk" / "agent.py").read_text(encoding="utf-8")
    sites = [m.start() for m in re.finditer(r'Message\(\s*role="assistant"', src)]
    assert sites, "the loop must construct assistant messages somewhere"
    bare = []
    for pos in sites:
        # the call's own argument list: scan to the paren that closes `Message(`
        start = src.index("(", pos)
        depth, end = 0, start
        for end in range(start, min(len(src), start + 4000)):
            depth += src[end] == "(" and 1 or (src[end] == ")" and -1 or 0)
            if depth == 0:
                break
        window = src[start:end + 1]
        # an explicit, reasoned exemption on the lines just above the call
        exempt = "# reasoning-n/a:" in src[max(0, pos - 300):pos]
        if 'reasoning=' not in window and not exempt:
            line = src[:pos].count("\n") + 1
            bare.append(line)
    assert not bare, f"assistant Message(...) without reasoning= at agent.py lines {bare}"
