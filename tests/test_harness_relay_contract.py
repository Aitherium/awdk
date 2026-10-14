"""The relay parser reads what ``adk serve`` actually streams, and ``aither`` survives
an unreachable Genesis.

Two defects measured 2026-10-14 from awsh on the PC:

* an awdk-harness tool turn wrote ``{"kind": "tool.call", "tool": "", "data":
  {"input": {}}}`` and ``{"output": null}`` to events.jsonl: the server batches calls
  as ``{tools: [{name, args}]}`` / ``{results: [{tool, success, output}]}`` and the
  parser read only Genesis's flat ``{tool, args}`` / ``{result}``. The contract test
  below runs the REAL server generator and feeds its frames through the REAL parser,
  so the two cannot drift apart again with both sides' unit tests green.
* every ``aither`` turn died with WinError 10061: its default base URL is Genesis's
  in-network address, which no fleet publishes to the host.
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

from adk.harnesses import agents as agents_mod
from adk.harnesses.agents import _iter_sse, translate_genesis
from adk.harnesses.events import EventKind
from adk.harnesses.registry import get as get_harness
from adk.harnesses.session import SessionConfig


class _Tools:
    def list_tools(self):
        return ["file_list"]

    def get(self, _name):
        return None


class _ToolAgent:
    """Just enough of AitherAgent for ``_aitheros_stream``'s tool branch."""

    name = "atlas"
    llm = SimpleNamespace(model="m")
    _tools = _Tools()

    async def stream_react(self, message, on_event=None, **_kw):
        on_event({"type": "tool", "name": "file_list", "args": {"path": "/tmp"}})
        on_event({"type": "tool_result", "name": "file_list", "result": "a.txt\nb.txt",
                  "ok": True})
        on_event({"type": "tool", "name": "read_file", "args": {"path": "/nope"}})
        on_event({"type": "tool_result", "name": "read_file", "result": "not found",
                  "ok": False})
        on_event({"type": "token", "text": "2 files"})
        return SimpleNamespace(content="2 files", tokens_used=3, model="m", latency_ms=1)


def _server_frames(monkeypatch) -> list[str]:
    from adk import server

    async def _no_classify(*_a, **_k):
        return None

    async def _no_ingest(*_a, **_k):
        return None

    monkeypatch.setenv("AITHER_ADK_SUPERVISOR", "0")
    monkeypatch.setattr(server, "_classify_turn", _no_classify)
    monkeypatch.setattr(server, "_strata_ingest_bg", _no_ingest)

    async def _get_agent(_name):
        return _ToolAgent()

    async def _collect():
        return [f async for f in server._aitheros_stream(
            _get_agent, "list files", "sid", "atlas", False)]

    return asyncio.run(_collect())


class _LinesResponse:
    def __init__(self, frames):
        self._lines = "".join(frames).split("\n")

    def iter_lines(self):
        return iter(self._lines)


def test_server_tool_frames_parse_into_named_tool_events(monkeypatch):
    frames = _server_frames(monkeypatch)
    events = []
    for name, payload in _iter_sse(_LinesResponse(frames)):
        events.extend(translate_genesis(name, payload))

    calls = [e for e in events if e.kind == EventKind.TOOL_CALL]
    results = [e for e in events if e.kind == EventKind.TOOL_RESULT]
    assert [c.tool for c in calls] == ["file_list", "read_file"]
    assert calls[0].data["input"] == {"path": "/tmp"}
    assert [r.tool for r in results] == ["file_list", "read_file"]
    assert results[0].data["output"] == "a.txt\nb.txt"
    assert results[0].data["is_error"] is False
    assert results[1].data["is_error"] is True


def test_genesis_flat_tool_events_still_parse():
    (call,) = translate_genesis("tool_call", {"tool": "t", "id": "1", "args": {"a": 1}})
    assert (call.tool, call.tool_use_id, call.data["input"]) == ("t", "1", {"a": 1})
    (res,) = translate_genesis("tool_result", {"id": "1", "tool": "t", "result": "ok"})
    assert (res.tool, res.data["output"], res.data["is_error"]) == ("t", "ok", False)
    (bad,) = translate_genesis("tool_result", {"id": "2", "result": None, "error": "x"})
    assert bad.data["is_error"] is True


def test_batched_tool_call_emits_one_event_per_entry():
    events = translate_genesis("tool_call", {"tools": [{"name": "a", "args": {}},
                                                       {"name": "b", "args": {"k": 2}},
                                                       "junk"]})
    assert [e.tool for e in events] == ["a", "b"]


# ── aither harness: Genesis refused -> the local awdk loop, with the persona ──

def _fake_httpx(monkeypatch, refuse_prefix: str, sent: list):
    fake = types.ModuleType("httpx")

    class ConnectError(Exception):  # noqa: N818 - mirrors the httpx name
        pass

    class ConnectTimeout(Exception):  # noqa: N818 - mirrors the httpx name
        pass

    sse = [b"event: token", b'data: {"type": "token", "t": "PONG"}', b"",
           b"event: complete", b'data: {"type": "complete"}', b""]

    class _Resp:
        status_code = 200
        text = ""

        def read(self):
            return None

        def iter_lines(self):
            return iter(sse)

    class _Stream:
        def __enter__(self):
            return _Resp()

        def __exit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url, json=None, headers=None):
            sent.append((url, json))
            if url.startswith(refuse_prefix):
                raise ConnectError("[WinError 10061] actively refused")
            return _Stream()

    fake.Client = _Client
    fake.Timeout = lambda *a, **k: None
    fake.ConnectError = ConnectError
    fake.ConnectTimeout = ConnectTimeout
    monkeypatch.setitem(sys.modules, "httpx", fake)


def _relay(tmp_path, monkeypatch, base_url=""):
    cfg = SessionConfig(harness="aither", agent="atlas")
    sess = agents_mod.AgentRelaySession(get_harness("aither"), cfg, root=tmp_path,
                                        agent="atlas", base_url=base_url)
    seen = []
    monkeypatch.setattr(sess, "_emit", lambda e, *a, **k: seen.append(e))
    monkeypatch.setattr(sess, "_observe", lambda *a, **k: None)
    return sess, seen


def test_aither_falls_back_to_local_loop_when_genesis_refuses(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_GENESIS_URL", raising=False)
    sent: list = []
    _fake_httpx(monkeypatch, agents_mod.GENESIS_URL.rstrip("/"), sent)
    sess, seen = _relay(tmp_path, monkeypatch)
    sess._run_turn("reply PONG", 1)

    assert [u for u, _ in sent] == [
        agents_mod.GENESIS_URL.rstrip("/") + "/chat/stream",
        agents_mod.ADK_URL.rstrip("/") + "/chat/stream",
    ]
    local_body = sent[1][1]
    assert local_body["system_additions"][0].startswith("You are Atlas")
    assert "".join(e.text for e in seen if e.kind == EventKind.TEXT_DELTA) == "PONG"
    assert not [e for e in seen if e.kind == EventKind.ERROR]
    assert any(e.kind == EventKind.NOTICE and e.data.get("stage") == "genesis_unreachable"
               for e in seen)
    # The next turn goes straight to the local loop.
    sess._run_turn("again", 2)
    assert sent[2][0].startswith(agents_mod.ADK_URL.rstrip("/"))
    assert sess.info()["local_fallback"] is True


@pytest.mark.parametrize("pin", ["env", "base_url"])
def test_named_genesis_route_never_falls_back(tmp_path, monkeypatch, pin):
    sent: list = []
    if pin == "env":
        monkeypatch.setenv("AITHER_GENESIS_URL", "http://genesis.example:8001")
        base_url = ""
    else:
        monkeypatch.delenv("AITHER_GENESIS_URL", raising=False)
        base_url = "http://genesis.example:8001"
    _fake_httpx(monkeypatch, "http://", sent)
    sess, seen = _relay(tmp_path, monkeypatch, base_url=base_url)
    sess._run_turn("reply PONG", 1)

    assert len(sent) == 1
    assert any(e.kind == EventKind.ERROR and "turn failed" in e.text for e in seen)
