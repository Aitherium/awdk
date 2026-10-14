"""awsh transcript 2026-10-11: discovery stopped one call short of the work.

"what is in the news today" -> search_tools("news") -> the model described the
newsletter tools and stopped; "do it" -> search_tools("do it") because the daemon's
stream path handed the model no history.
"""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

from adk.agent import AitherAgent, discovery_nudge
from adk.llm.base import StreamChunk


def _agent(replies, seen):
    it = iter(replies)

    async def _stream(messages, *a, **k):
        seen.append([m.content for m in messages])
        yield StreamChunk(content=next(it), done=True, model="t")

    llm = AsyncMock()
    llm.chat_stream = _stream
    llm.provider_name = "test"
    llm.chat = AsyncMock(return_value=MagicMock(content="x", tool_calls=[], finish_reason="stop"))
    return AitherAgent(name="t", llm=llm, builtin_tools=False, system_prompt="IDENTITY")


def test_a_search_tools_menu_answered_with_prose_is_sent_back_to_act():
    seen: list = []
    agent = _agent([
        'ACTION: search_tools\nINPUT: {"query": "news"}',
        "FINAL: The tools related to news are available. I can search the web.",
        'ACTION: web_search\nINPUT: {"query": "news today"}',
        "FINAL: Top story: X.",
    ], seen)
    calls = {"web": 0}

    @agent.tool
    def search_tools(query: str) -> str:
        """find"""
        return json.dumps({"results": [{"name": "newsletter_create_campaign",
                                        "parameters": {"required": ["list_cn"]}}]})

    @agent.tool
    def web_search(query: str) -> str:
        """web"""
        calls["web"] += 1
        return "X happened"

    resp = asyncio.run(agent.stream_react("what is in the news today",
                                          on_event=lambda e: None, max_steps=6))
    assert calls["web"] == 1, "the turn went on to do the work"
    assert resp.content == "Top story: X."
    assert any("web_search" in str(m) and "FIND a tool" in str(m) for m in seen[2])


def test_web_reach_line_names_web_search_for_news():
    seen: list = []
    agent = _agent(["FINAL: ok"], seen)

    @agent.tool
    def search_tools(query: str) -> str:
        """find"""
        return "{}"

    @agent.tool
    def web_search(query: str) -> str:
        """web"""
        return ""

    asyncio.run(agent.stream_react("hi", on_event=lambda e: None))
    assert "call web_search directly" in seen[0][0]


def test_a_question_about_tools_may_end_on_the_menu():
    seen: list = []
    agent = _agent([
        'ACTION: search_tools\nINPUT: {"query": "mail"}',
        "FINAL: mail_list and mail_read.",
    ], seen)

    @agent.tool
    def search_tools(query: str) -> str:
        """find"""
        return json.dumps({"results": [{"name": "mail_list"}]})

    resp = asyncio.run(agent.stream_react("which tools handle mail?",
                                          on_event=lambda e: None))
    assert resp.content == "mail_list and mail_read."
    assert len(seen) == 2


def test_the_stream_reads_its_own_session_history_when_none_is_passed():
    seen: list = []
    agent = _agent(["FINAL: first", "FINAL: second"], seen)
    _sid = f"s-hist-{uuid.uuid4().hex}"
    asyncio.run(agent.stream_react("what is in the news today", on_event=lambda e: None,
                                   session_id=_sid))
    asyncio.run(agent.stream_react("do it", on_event=lambda e: None, session_id=_sid))
    assert "what is in the news today" in seen[1], "the follow-up carries the prior turn"


def test_nudge_hands_back_a_copyable_call():
    obs = json.dumps({"results": [{"name": "web_search",
                                   "parameters": {"required": ["query"]}}]})
    text = discovery_nudge("search_tools", obs)
    assert 'INPUT: {"name": "web_search", "arguments": {"query": "<value>"}}' in text
    assert "call_tool" not in discovery_nudge("load_tools", "ok")


def test_daemon_stream_budget_follows_effort_and_fails_open():
    from adk import server

    class _Mem:
        async def get_history(self, sid, limit=50):
            return []

    def _agent_with(effort=None, boom=False):
        a = MagicMock()
        a.memory = _Mem()

        async def _classify(message, history=None):
            if boom:
                raise RuntimeError("router down")
            return MagicMock(effort=effort, intent="research", agentic=True)
        a.classify_intent = _classify
        return a

    events: list = []
    assert asyncio.run(server._effort_step_budget(_agent_with(8), "m", "s", events.append)) == 14
    assert events[0]["type"] == "effort" and events[0]["max_steps"] == 14
    assert asyncio.run(server._effort_step_budget(_agent_with(3), "m", "s", events.append)) == 6
    assert asyncio.run(server._effort_step_budget(_agent_with(boom=True), "m", "s",
                                                  events.append)) == 6


def test_an_offer_to_look_it_up_is_answered_by_looking_it_up():
    seen: list = []
    agent = _agent([
        "FINAL: I'm not currently aware of any news about Tanium. Would you like me "
        "to search for more information?",
        'ACTION: web_search\nINPUT: {"query": "Tanium news"}',
        "FINAL: Tanium relaunched Security Operations.",
    ], seen)
    calls = {"web": 0}

    @agent.tool
    def web_search(query: str) -> str:
        """web"""
        calls["web"] += 1
        return "Tanium relaunches Security Operations"

    resp = asyncio.run(agent.stream_react("anything exciting about tanium?",
                                          on_event=lambda e: None, max_steps=6))
    assert calls["web"] == 1
    assert resp.content == "Tanium relaunched Security Operations."


def test_offers_lookup_leaves_real_questions_alone():
    from adk.agent import offers_lookup
    assert offers_lookup("Would you like me to search for more information?")
    assert offers_lookup("Should I look it up for you?")
    assert not offers_lookup("Would you like me to delete the branch?")
    assert not offers_lookup("Do you want me to send the email?")
    assert not offers_lookup("Here are the results. Anything else?")
    assert offers_lookup("Let me try a different approach. I'll perform a web search to find "
                         "the most recent information about Tanium. Would you like me to do "
                         "that?")
    assert not offers_lookup("I can delete the old branch for you. Would you like me to do "
                             "that?")


def test_companion_identity_scopes_honesty_to_shared_history(monkeypatch):
    import adk.private_companion as pc

    class _Vault:
        def get_safety_level(self):
            return "professional"

        def get_system_prompt_for_level(self, level):
            return "You are Mira."

    monkeypatch.setattr(pc, "get_companion_vault", lambda: _Vault())
    agent = _agent([], [])
    agent._system_prompt = None
    sp = agent.system_prompt
    assert "You are Mira." in sp
    assert "LOOK UP with your tools" in sp
    assert "operating system" not in sp, "the persona may talk about the user's platform"


def test_a_claimed_lookup_with_no_tool_call_is_sent_back_to_look():
    seen: list = []
    agent = _agent([
        "I've taken a look into what's happening with Tanium: lots of AI momentum.",
        'ACTION: web_search\nINPUT: {"query": "Tanium news"}',
        "FINAL: Tanium shipped Autonomous IT.",
    ], seen)
    calls = {"web": 0}

    @agent.tool
    def web_search(query: str) -> str:
        """web"""
        calls["web"] += 1
        return "Tanium ships Autonomous IT"

    resp = asyncio.run(agent.stream_react("anything exciting about tanium?",
                                          on_event=lambda e: None, max_steps=6))
    assert calls["web"] == 1, "the claimed lookup was made for real"
    assert resp.content == "Tanium shipped Autonomous IT."


def test_a_tool_result_is_framed_as_the_agents_own_call():
    seen: list = []
    agent = _agent([
        'ACTION: web_search\nINPUT: {"query": "news today"}',
        "FINAL: Top story: X.",
    ], seen)

    @agent.tool
    def web_search(query: str) -> str:
        """web"""
        return "X happened"

    asyncio.run(agent.stream_react("what is in the news today",
                                   on_event=lambda e: None, max_steps=4))
    obs = [c for c in seen[1] if "X happened" in str(c)]
    assert obs and "YOUR web_search call" in obs[-1]
    assert "the user did not write" in obs[-1]


def test_gemma_channel_markers_never_reach_the_answer():
    from adk.agent import normalize_channel_markers

    seen: list = []
    events: list = []
    it = iter(["<|chan", "nel>thought planning<chan", "nel|>FINAL: It is sunny."])

    async def _stream(messages, *a, **k):
        seen.append(1)
        for part in it:
            yield StreamChunk(content=part, done=False, model="t")

    agent = _agent([], [])
    agent.llm.chat_stream = _stream
    resp = asyncio.run(agent.stream_react("weather?", on_event=events.append, max_steps=3))
    streamed = "".join(e.get("text", "") for e in events if e.get("type") == "token")
    assert resp.content == "It is sunny."
    assert "channel" not in streamed and "channel" not in resp.content
    assert normalize_channel_markers("a < b") == ("a < b", "")


def test_a_gemma_native_tool_call_runs_the_tool():
    seen: list = []
    agent = _agent([
        '<|tool_call>call:web_search{query:<|"|>Tanium news, today<|"|>,limit:5}<tool_call|>',
        "FINAL: Tanium shipped Autonomous IT.",
    ], seen)
    got: dict = {}

    @agent.tool
    def web_search(query: str, limit: int = 5) -> str:
        """web"""
        got.update(query=query, limit=limit)
        return "Tanium ships Autonomous IT"

    resp = asyncio.run(agent.stream_react("anything exciting about tanium?",
                                          on_event=lambda e: None, max_steps=4))
    assert got == {"query": "Tanium news, today", "limit": 5}
    assert resp.content == "Tanium shipped Autonomous IT."
    assert "tool_call" not in resp.content


def test_an_outward_tool_asks_before_it_runs(tmp_path, monkeypatch):
    """Live 2026-10-14: the owner typed "hi" and the agent posted "Hi!" to twitter and
    linkedin with no question asked. Posting, sending and paying ask first by default."""
    monkeypatch.delenv("AITHER_TOOL_APPROVAL", raising=False)
    monkeypatch.setenv("AITHER_ADK_STATE_DIR", str(tmp_path))
    seen: list = []
    agent = _agent([
        'ACTION: post_to_social\nINPUT: {"text": "Hi!"}',
        "FINAL: done",
    ], seen)
    posted: list = []

    @agent.tool
    def post_to_social(text: str) -> str:
        """post"""
        posted.append(text)
        return "posted"

    events: list = []
    resp = asyncio.run(agent.stream_react("hi", on_event=events.append, max_steps=4,
                                          session_id="s-outward"))
    assert posted == [], "a public post ran without the owner's yes"
    assert getattr(resp, "requires_action", False) or any(
        e.get("type") == "approval_required" for e in events)


def test_a_live_state_question_answered_from_memory_is_sent_back_to_look():
    """Live 2026-10-14: "how is the platform looking" -> "The platform is looking good!"
    with zero tool calls."""
    seen: list = []
    agent = _agent([
        "FINAL: The platform is looking good! Most services are running smoothly.",
        'ACTION: get_platform_health\nINPUT: {}',
        "FINAL: 2 services are down: mail and cert.",
    ], seen)
    calls = {"n": 0}

    @agent.tool
    def get_platform_health() -> str:
        """health"""
        calls["n"] += 1
        return "down: mail, cert"

    resp = asyncio.run(agent.stream_react("how is the platform looking",
                                          on_event=lambda e: None, max_steps=6))
    assert calls["n"] == 1
    assert resp.content == "2 services are down: mail and cert."


def test_search_tools_points_to_the_web_when_no_platform_tool_matches():
    """Live 2026-10-14: search_tools("Tanium") ranked a generic text tool first and the
    turn never reached the web."""
    from adk.server import _point_to_web_when_nothing_matches

    agent = _agent([], [])

    @agent.tool
    def web_search(query: str) -> str:
        """web"""
        return ""

    menu = json.dumps({"results": [{"name": "awdesk_command",
                                    "description": "Run a desk command from text"}]})
    out = json.loads(_point_to_web_when_nothing_matches(menu, "Tanium", agent))
    assert out["results"][0]["name"] == "web_search" and "hint" in out
    hit = json.dumps({"results": [{"name": "get_platform_health",
                                   "description": "platform health summary"}]})
    same = json.loads(_point_to_web_when_nothing_matches(hit, "platform health", agent))
    assert same["results"][0]["name"] == "get_platform_health" and "hint" not in same


def test_a_question_gets_room_for_discovery_even_at_low_effort(monkeypatch):
    """Live 2026-10-14: effort 2 -> 4 steps; the turn spent them on search_tools + a
    wrong call_tool and ended on "I'll perform a web search ... one moment"."""
    from types import SimpleNamespace

    from adk import server

    async def classify(agent, message, session_id):
        return SimpleNamespace(effort=2, intent=intent["v"], agentic=True)

    monkeypatch.setattr(server, "_classify_turn", classify)
    intent = {"v": "question"}
    r = asyncio.run(server._turn_routing(None, "anything exciting about tanium?", "s",
                                         lambda e: None))
    assert r.max_steps >= server._STREAM_MIN_STEPS
    intent["v"] = "conversation"
    r = asyncio.run(server._turn_routing(None, "hi", "s", lambda e: None))
    assert r.max_steps < server._STREAM_MIN_STEPS
