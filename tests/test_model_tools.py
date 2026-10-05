"""The orchestrator calls bigger models as TOOLS (owner, 2026-10-05): reasoning + vision."""

import asyncio
import json

import adk.builtin_tools as bt
import adk.model_tools as mt


class _Resp:
    def __init__(self, content):
        self.content = content


class _Router:
    def __init__(self, content="Get-PSDrive C | Select-Object Used,Free", exc=None):
        self.calls = []
        self.content = content
        self.exc = exc

    async def chat(self, messages, model=None, **kw):
        self.calls.append((messages, model))
        if self.exc:
            raise self.exc
        return _Resp(self.content)


def _run(coro):
    return asyncio.run(coro)


def test_ask_reasoner_routes_to_the_reasoning_model(monkeypatch):
    r = _Router()
    monkeypatch.setattr(mt, "_router", r)
    monkeypatch.delenv("AITHER_REASONING_MODEL", raising=False)
    out = json.loads(_run(mt.ask_reasoner("free space on C: in PowerShell?", "Windows 11")))
    assert out["answer"].startswith("Get-PSDrive")
    assert out["model"] == mt.REASONING_DEFAULT
    msgs, model = r.calls[0]
    assert model == mt.REASONING_DEFAULT
    assert "Windows 11" in msgs[-1].content


def test_reasoning_model_follows_config(monkeypatch):
    r = _Router()
    monkeypatch.setattr(mt, "_router", r)
    monkeypatch.setenv("AITHER_REASONING_MODEL", "my-reasoner")
    assert json.loads(_run(mt.ask_reasoner("q")))["model"] == "my-reasoner"


def test_failures_come_back_as_errors_not_exceptions(monkeypatch):
    monkeypatch.setattr(mt, "_router", _Router(exc=RuntimeError("lane down")))
    assert "lane down" in json.loads(_run(mt.ask_reasoner("q")))["error"]
    monkeypatch.setattr(mt, "_router", _Router(content=""))
    assert "empty" in json.loads(_run(mt.ask_reasoner("q")))["error"]
    assert "empty" in json.loads(_run(mt.ask_reasoner("  ")))["error"]


def test_look_at_sends_the_image_to_the_vision_model(monkeypatch, tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"))
    r = _Router(content="a single pixel")
    monkeypatch.setattr(mt, "_router", r)
    monkeypatch.delenv("AITHER_PERCEPTION_MODEL", raising=False)
    out = json.loads(_run(mt.look_at(str(img), "what is it?")))
    assert out == {"model": mt.VISION_DEFAULT, "answer": "a single pixel"}
    parts = r.calls[0][0][0].content
    assert any(p.get("type") == "image_url" for p in parts)


def test_look_at_missing_file_is_an_error(monkeypatch):
    monkeypatch.setattr(mt, "_router", _Router())
    assert "not found" in json.loads(_run(mt.look_at("C:/no/such.png")))["error"]


def test_the_orchestrator_agents_get_the_models_category():
    assert bt.TOOL_CATEGORIES["models"] == [bt.ask_reasoner, bt.look_at]
    for ident in ("aither", "adk-daemon"):
        assert "models" in bt.IDENTITY_DEFAULTS[ident], ident


# ── The loop consults the reasoner after two failed / empty tool results ──────

def test_tool_result_failed_shapes():
    from adk.agent import tool_result_failed
    header_only = json.dumps({"exit_code": 0, "stdout": "\nFreeSpace\n---------\n\n", "stderr": ""})
    assert tool_result_failed(header_only)
    assert tool_result_failed(json.dumps({"exit_code": 1, "stdout": "x"}))
    assert tool_result_failed(json.dumps({"error": "missing arg"}))
    assert tool_result_failed("(tool error: KeyError: x)")
    assert tool_result_failed("")
    table = {"exit_code": 0, "stdout": "Used Free\n---- ----\n1 2"}
    assert not tool_result_failed(json.dumps(table))
    assert not tool_result_failed("plain text answer")


def test_stream_react_consults_the_reasoner_after_two_failures():
    from unittest.mock import AsyncMock, MagicMock

    from adk.agent import AitherAgent
    from adk.llm.base import StreamChunk

    replies = iter([
        'ACTION: shell_exec\nINPUT: {"command": "Get-PSDrive C | Select-Object FreeSpace"}',
        'ACTION: shell_exec\nINPUT: {"command": "Get-PSDrive C | Select-Object FreeSpace"}',
        'ACTION: shell_exec\nINPUT: {"command": "Get-PSDrive C | Select-Object Used,Free"}',
        "FINAL: 79.8 GB free",
    ])
    seen: list = []

    async def _stream(messages, *a, **k):
        seen.append([m.content for m in messages])
        yield StreamChunk(content=next(replies), done=True, model="t")

    llm = AsyncMock()
    llm.chat_stream = _stream
    llm.provider_name = "test"
    llm.chat = AsyncMock(return_value=MagicMock(content="x", tool_calls=[], finish_reason="stop"))
    agent = AitherAgent(name="t", llm=llm, builtin_tools=False, system_prompt="IDENTITY")
    calls = {"shell": 0, "reasoner": 0}

    @agent.tool
    def shell_exec(command: str) -> str:
        """run"""
        calls["shell"] += 1
        if "Used,Free" in command:
            return json.dumps({"exit_code": 0, "stdout": "Used Free\n---- ----\n1 85700000000"})
        return json.dumps({"exit_code": 0, "stdout": "\nFreeSpace\n---------\n\n"})

    @agent.tool
    def ask_reasoner(question: str, context: str = "") -> str:
        """consult"""
        calls["reasoner"] += 1
        return json.dumps({"answer": "Get-PSDrive C | Select-Object Used,Free"})

    events: list = []
    resp = asyncio.run(agent.stream_react("how much free space is on my C drive?",
                                          on_event=events.append, max_steps=6))
    assert calls["reasoner"] == 1, "consulted exactly once, after the second empty result"
    assert resp.content == "79.8 GB free"
    advised = any("REASONER" in str(m) and "Used,Free" in str(m) for m in seen[2])
    assert advised, "the advice reached the model"


def test_image_reference_finds_paths_and_urls():
    from adk.agent import image_reference
    bs = chr(92)
    win = "D:" + bs + "Aither-Avatar" + bs + "avatarsample_o_idle_front.png"
    assert image_reference("what is in the image " + win + " ?") == win
    assert image_reference("look at https://x.example/a/b.JPG please") == "https://x.example/a/b.JPG"
    assert image_reference("see ./shots/one.webp") == "./shots/one.webp"
    assert image_reference("how much free space is on my C drive?") == ""
    assert image_reference("read notes.txt") == ""


def test_stream_react_points_an_image_question_at_look_at():
    from unittest.mock import AsyncMock, MagicMock

    from adk.agent import AitherAgent
    from adk.llm.base import StreamChunk

    seen: list = []

    async def _stream(messages, *a, **k):
        seen.append(messages[0].content)
        yield StreamChunk(content="FINAL: ok", done=True, model="t")

    llm = AsyncMock()
    llm.chat_stream = _stream
    llm.provider_name = "test"
    llm.chat = AsyncMock(return_value=MagicMock(content="x", tool_calls=[], finish_reason="stop"))
    agent = AitherAgent(name="t", llm=llm, builtin_tools=False, system_prompt="IDENTITY")

    @agent.tool
    def look_at(image: str, question: str = "") -> str:
        """see"""
        return "{}"

    asyncio.run(agent.stream_react("what is in C:/pics/cat.png ?", on_event=lambda e: None))
    assert "call look_at" in seen[0] and "C:/pics/cat.png" in seen[0]
    seen.clear()
    asyncio.run(agent.stream_react("how much free space?", on_event=lambda e: None))
    assert "call look_at" not in seen[0]


def test_an_image_question_is_always_agentic():
    from unittest.mock import AsyncMock, MagicMock

    from adk.agent import AitherAgent

    llm = AsyncMock()
    llm.provider_name = "test"
    # the router model says "plain chat, no grounding" -- the measured 8B answer
    llm.chat = AsyncMock(return_value=MagicMock(
        content='{"intent":"question","effort":2,"agentic":false,"requires_grounding":false}'))
    agent = AitherAgent(name="t", llm=llm, builtin_tools=False, system_prompt="I")

    @agent.tool
    def look_at(image: str, question: str = "") -> str:
        """see"""
        return "{}"

    d = asyncio.run(agent.classify_intent("what is in the image C:/pics/cat.png ?"))
    assert d.agentic and d.requires_grounding
    d2 = asyncio.run(agent.classify_intent("tell me a joke"))
    assert not d2.agentic



def test_stream_react_looks_at_a_named_image_before_the_model_answers():
    from unittest.mock import AsyncMock, MagicMock

    from adk.agent import AitherAgent
    from adk.llm.base import StreamChunk

    seen: list = []

    async def _stream(messages, *a, **k):
        seen.append([str(m.content) for m in messages])
        yield StreamChunk(content="FINAL: a pink-haired avatar", done=True, model="t")

    llm = AsyncMock()
    llm.chat_stream = _stream
    llm.provider_name = "test"
    llm.chat = AsyncMock(return_value=MagicMock(content="x", tool_calls=[], finish_reason="stop"))
    agent = AitherAgent(name="t", llm=llm, builtin_tools=False, system_prompt="I")
    looked: list = []

    @agent.tool
    def look_at(image: str, question: str = "") -> str:
        """see"""
        looked.append(image)
        return json.dumps({"answer": "an anime avatar with pink hair"})

    resp = asyncio.run(agent.stream_react("what is in C:/pics/a.png ?", on_event=lambda e: None))
    assert looked == ["C:/pics/a.png"], "looked before the model's first reply"
    assert any("pink hair" in m for m in seen[0]), "the model answered from what was seen"
    assert "look_at" in resp.tool_calls_made



def test_native_chat_loop_also_looks_at_a_named_image():
    from unittest.mock import AsyncMock, MagicMock

    from adk.agent import AitherAgent

    sent: list = []

    async def _chat(messages, *a, **k):
        sent.append([str(m.content) for m in messages])
        return MagicMock(content="a pink-haired avatar", tool_calls=[], finish_reason="stop",
                         model="t", tokens_used=1, latency_ms=1.0, reasoning="")

    llm = AsyncMock()
    llm.provider_name = "test"
    llm.chat = _chat
    agent = AitherAgent(name="t", llm=llm, builtin_tools=False, system_prompt="I")
    looked: list = []

    @agent.tool
    def look_at(image: str, question: str = "") -> str:
        """see"""
        looked.append(image)
        return json.dumps({"answer": "an anime avatar with pink hair"})

    asyncio.run(agent.chat("what is in C:/pics/a.png ?", history=[]))
    assert looked == ["C:/pics/a.png"]
    assert any("pink hair" in m for call in sent for m in call)
