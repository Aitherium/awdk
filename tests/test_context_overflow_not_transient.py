"""L3c: a context overflow is deterministic -- never retried as a transient; the loop
learns the request size from the body, compacts once and retries once.

Measured 2026-09-21 on L-bonsai-S1-full3: llama.cpp answered 400 "request (17415 tokens)
exceeds the available context size (16384 tokens)", the fleet scheduler relayed it as a
502, and awdk retried it twelve times at 20-30 s before giving the instance up.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import httpx
import pytest
from adk.llm import openai_compat as oc

OVERFLOW_BODY = ('{"error":{"code":400,"message":"request (17415 tokens) exceeds the '
                 'available context size (16384 tokens), try increasing it",'
                 '"type":"exceed_context_size_error"}}')


class _Resp:
    def __init__(self, status: int, text: str = ""):
        self.status_code = status
        self.headers: dict[str, str] = {}
        self.text = text or f"status {status}"
        self.reason_phrase = "x"
        self.request = httpx.Request("POST", "http://x")

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


class _Client:
    def __init__(self, resps: list[_Resp]):
        self.resps = list(resps)
        self.calls = 0

    async def post(self, url, json=None):
        self.calls += 1
        return self.resps.pop(0) if self.resps else _Resp(200)


def _provider():
    return oc.OpenAIProvider(base_url="http://x/v1", api_key="k", default_model="m")


def _run(client, sleeps, monkeypatch):
    async def fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(oc.asyncio, "sleep", fake_sleep)
    return asyncio.run(_provider()._post_with_retry(client, "http://x/v1/chat/completions", {}))


def test_overflow_body_is_parsed():
    ok, req, lim = oc._overflow_from_body(OVERFLOW_BODY)
    assert (ok, req, lim) == (True, 17415, 16384)
    ok, req, lim = oc._overflow_from_body("This model's maximum context length is 8192 tokens")
    assert ok and lim == 8192
    assert oc._overflow_from_body("status 502")[0] is False


def test_a_502_wrapping_an_overflow_is_not_retried(monkeypatch):
    monkeypatch.setenv("ADK_LLM_MAX_RETRIES", "12")
    sleeps: list[float] = []
    client = _Client([_Resp(502, OVERFLOW_BODY), _Resp(200)])
    with pytest.raises(oc.ContextOverflowError) as ei:
        _run(client, sleeps, monkeypatch)
    assert client.calls == 1, "the same prompt was re-sent to the same slot"
    assert sleeps == []
    assert ei.value.requested_tokens == 17415 and ei.value.limit_tokens == 16384
    assert isinstance(ei.value, httpx.HTTPStatusError), "existing handlers must still catch it"


def test_a_plain_400_overflow_is_the_same_class(monkeypatch):
    client = _Client([_Resp(400, OVERFLOW_BODY)])
    with pytest.raises(oc.ContextOverflowError):
        _run(client, [], monkeypatch)


def test_a_real_transient_502_is_still_retried(monkeypatch):
    monkeypatch.setenv("ADK_LLM_MAX_RETRIES", "3")
    monkeypatch.setenv("ADK_LLM_RETRY_WAIT_S", "0")
    sleeps: list[float] = []
    client = _Client([_Resp(502), _Resp(200)])
    resp = _run(client, sleeps, monkeypatch)
    assert resp.status_code == 200 and client.calls == 2 and len(sleeps) == 1


def test_the_loop_compacts_once_and_retries_once_on_overflow():
    """Source-level: the ReAct loop's chat call is guarded, learns the overhead from
    the error's requested_tokens, calls maybe_compact and re-sends exactly once."""
    src = (Path(__file__).resolve().parents[1] / "adk" / "agent.py").read_text(encoding="utf-8")
    assert "requested_tokens" in src and "ContextOverflowError" in src
    tree = ast.parse(src)
    handlers = [h for n in ast.walk(tree) if isinstance(n, ast.Try) for h in n.handlers
                if "requested_tokens" in ast.unparse(h)]
    assert handlers, "no handler learns the request size from the overflow"
    body = ast.unparse(handlers[0])
    assert "maybe_compact(" in body and body.count("self.llm.chat(") == 1, \
        "the overflow handler must compact and re-send exactly once"


@pytest.mark.asyncio
async def test_limit_override_beats_the_name_table():
    """The provider said 16,384; the name table says 32k for an unknown name. The
    refusal's number wins, so the loop compacts where the table would not."""
    from adk.context_budget import estimate_tokens, maybe_compact
    messages = [{"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 3000}
                for i in range(12)] + [{"role": "user", "content": "go"}]
    est = estimate_tokens(messages)
    assert est < int(32_768 * 0.7), "fixture must sit under the unknown-model threshold"
    _, c_table = await maybe_compact(list(messages), model=None, summarize=None)
    assert c_table is False
    _, c_said = await maybe_compact(list(messages), model=None, summarize=None,
                                    limit_override=8192)
    assert c_said is True


def test_the_loop_remembers_the_limit_the_provider_named():
    src = (Path(__file__).resolve().parents[1] / "adk" / "agent.py").read_text(encoding="utf-8")
    assert "_context_limit_observed" in src and "limit_tokens" in src
    assert src.count("limit_override=self._compaction_limit()") == 2, \
        "both compaction calls must budget through _compaction_limit"
    # ...and that helper still honours the limit a provider named in a refusal.
    from adk.agent import AitherAgent
    agent = object.__new__(AitherAgent)
    agent._context_limit_observed = 16384
    agent._context_limit_discovered = 131072
    import os
    saved = os.environ.pop("ADK_CONTEXT_LIMIT", None)
    try:
        assert agent._compaction_limit() == 16384
    finally:
        if saved is not None:
            os.environ["ADK_CONTEXT_LIMIT"] = saved
