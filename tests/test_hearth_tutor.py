"""Aither Learn inside Hearth: a parent asks the home agent about their kids.

"how did Alexander do this week?" and "have Athena practice short vowels" are
driven through the REAL :class:`adk.agent.AitherAgent` that ``adk home serve``
builds (:func:`adk.home.serve.build_serve_agent`), with only the model and the
network faked. A stub agent hid real-model failures before (the 2026-09-29 Bonsai
run), so these tests read the tool schema and system prompt the agent actually
hands the model, and let the agent's own approval gate pause ``tutor_assign``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

pytest.importorskip("cryptography")

from adk import approval, receipts  # noqa: E402
from adk.home import config as hc  # noqa: E402
from adk.home import connector_tools as ct  # noqa: E402
from adk.home import hearth, serve  # noqa: E402
from adk.home.life_tools import ALWAYS_ASK, FollowupStore  # noqa: E402
from adk.home.tutor_tools import (  # noqa: E402
    SKILL_ALIASES,
    build_tutor_tools,
    focus_skills,
    parent_report,
    resolve_skill,
)
from adk.llm.base import LLMResponse, ToolCall  # noqa: E402

TUTOR_TOOLS = {"tutor_learners", "tutor_report", "tutor_assign", "tutor_set_focus"}
OWNER = "david"
BEARER = "guardian-bearer-xyz"
ROSTER = [
    {"lid": "lrn_5493aa7d79d24e84", "alias": "Athena", "grade": 1, "age_band": "6-7",
     "claimed": True, "settings": {}},
    {"lid": "lrn_6537875f75a04d60", "alias": "Alexander", "grade": 2, "age_band": "8-9",
     "claimed": True, "settings": {}},
]
REPORT = {
    "lid": "lrn_6537875f75a04d60", "week": "2026-W40", "minutes": 42.5, "sessions": 4,
    "attempts": 60,
    "skills": {
        "math.add_within_20": {"state": "secure", "kid_title": "I can add within 20!",
                               "score": 0.93},
        "math.sub_within_20": {"state": "learning", "kid_title": "I can take away within 20!",
                               "score": 0.41},
    },
    "edge": ["math.sub_within_20"],
    "at_risk_reviews": [],
    "breaks_used": 1,
    "observations": ["Practiced on 3 days this week.", "Now secure: I can add within 20!."],
    "notes": [],
    # never reaches the model, even if the router ever sent it
    "items": [{"prompt": "7+8", "answer": "15", "correct": False}],
}
PLATFORM_ENV = ("AITHER_API_KEY", "AITHER_IDENTITY_BEARER", "AITHER_SESSION_BEARER")


# ── fakes ───────────────────────────────────────────────────────────────────────

class Genesis:
    """The family-tutor router over httpx.MockTransport; ``calls`` is every request."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append({"method": request.method, "path": request.url.path, "body": body,
                           "auth": request.headers.get("authorization")})
        path = request.url.path
        if path.endswith("/family/learners"):
            return httpx.Response(200, json=ROSTER)
        if path.endswith("/report"):
            return httpx.Response(200, json=REPORT)
        if path.endswith("/assign"):
            return httpx.Response(201, json={"assignment_id": "asg_1"})
        if path.endswith("/focus") and request.method == "PUT":
            return httpx.Response(200, json={"lid": path.split("/")[-2],
                                             "focus": {"active": True}})
        return httpx.Response(404, json={"detail": "Not Found"})

    def posts(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "POST"]


class ScriptedModel:
    """A model that answers like a small local one would: one tool call for the
    parent's request, then a short reply. Records exactly what the agent sent."""

    provider_name = "mock"
    model = "mock-small"

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []

    async def chat(self, messages, tools=None, **kwargs):
        self.requests.append({"messages": list(messages), "tools": tools})
        last = messages[-1]
        text = ""
        for m in reversed(messages):
            if getattr(m, "role", "") == "user":
                text = str(m.content)
                break
        if "athena practice short vowels, quietly" in text.lower():
            # A small model that reads the roster, then claims an assignment it
            # never made (the READ_ONLY_TOOLS hole).
            if getattr(last, "role", "") == "tool":
                return LLMResponse(content="I've added short vowels for Athena.",
                                   model=self.model)
            call = ToolCall(id="tc_l", name="tutor_learners", arguments={})
            return LLMResponse(content="", model=self.model, tool_calls=[call])
        if getattr(last, "role", "") == "tool":
            return LLMResponse(content="Done -- here is what I found: "
                               + str(last.content)[:200], model=self.model)
        if "how did alexander" in text.lower():
            call = ToolCall(id="tc_r", name="tutor_report", arguments={"lid": "Alexander"})
        elif "have athena practice" in text.lower():
            call = ToolCall(id="tc_a", name="tutor_assign",
                            arguments={"lid": "Athena", "skill_id": "short vowels"})
        else:
            return LLMResponse(content="Hello!", model=self.model)
        return LLMResponse(content="", model=self.model, tool_calls=[call])


class FakeTransport:
    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: List[tuple] = []

    async def start(self, core):
        return None

    async def stop(self):
        return None

    async def send(self, user_id, text):
        self.sent.append((user_id, text))
        return True

    async def verify_identity(self, user_id):
        return user_id == OWNER


# ── fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "agent-home"
    monkeypatch.setenv(hc.HOME_ENV, str(root))
    monkeypatch.setenv("AITHER_DATA_DIR", str(tmp_path / "data"))
    for name in ("AITHER_RECEIPTS_PATH", serve.TUTOR_FLAG_ENV, *serve.TUTOR_URL_ENV,
                 *PLATFORM_ENV, "ADK_BUILTIN_TOOL_CATEGORIES", "AITHER_TOOL_PACKS",
                 "ADK_APP_PROXY_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(receipts.KEY_ENV, raising=False)
    monkeypatch.setattr(receipts, "_home_dir", lambda: root)
    monkeypatch.setattr(receipts, "_awseal_private_key", lambda: None)
    monkeypatch.setattr(approval, "_STORE", approval.ApprovalStore(tmp_path / "paused.json"))
    monkeypatch.setattr(ct, "_saved_bearer", lambda: "")
    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "")
    serve.apply_approval_policy()
    hc.init_home(name="hearth", root=root)
    return root


@pytest.fixture
def genesis(monkeypatch):
    g = Genesis()
    real_client = httpx.Client

    def _client(*a, **k):
        return real_client(transport=httpx.MockTransport(g))

    monkeypatch.setattr(httpx, "Client", _client)
    monkeypatch.setenv("AITHER_TUTOR_URL", "https://genesis.test")
    return g


def _signed_in(monkeypatch):
    monkeypatch.setattr(ct, "_saved_bearer", lambda: BEARER)


def _agent(home: Path, model: ScriptedModel):
    store = FollowupStore(home / "followups.json")
    return serve.build_serve_agent(hc.load_config(home), store, root=home, llm=model,
                                   receipts_file=home / "actions.jsonl"), store


def _schemas(tools: Any) -> Dict[str, Dict[str, Any]]:
    out = {}
    for t in tools or []:
        fn = t.get("function", t) if isinstance(t, dict) else {}
        if fn.get("name"):
            out[fn["name"]] = fn
    return out


# ── when Hearth gets the tutor ──────────────────────────────────────────────────

class _Bare:
    name = "bare"

    def __init__(self):
        from adk.tools import ToolRegistry

        self._tools = ToolRegistry()


def test_the_flag_turns_the_tutor_on_without_a_sign_in(home, monkeypatch):
    store = FollowupStore(home / "f.json")
    assert not TUTOR_TOOLS & set(serve.register_serve_tools(_Bare(), store))
    monkeypatch.setenv(serve.TUTOR_FLAG_ENV, "on")
    built = []
    real = serve.build_home_tools
    monkeypatch.setattr(serve, "build_home_tools",
                        lambda planner=None, **kw: built.append(kw) or real(planner, **kw))
    names = set(serve.register_serve_tools(_Bare(), store))
    assert TUTOR_TOOLS <= names
    # The built-in calendar tools are always there; the OAuth accounts stay off.
    assert built[-1]["remote"] is False


def test_a_platform_token_alone_turns_the_tutor_on(home, monkeypatch):
    monkeypatch.setenv("AITHER_API_KEY", "platform-token")
    assert serve.tutor_enabled()
    assert TUTOR_TOOLS <= set(serve.register_serve_tools(_Bare(), FollowupStore(home / "f")))


def test_the_flag_turns_the_tutor_off_even_when_signed_in(home, monkeypatch):
    _signed_in(monkeypatch)
    monkeypatch.setenv(serve.TUTOR_FLAG_ENV, "0")
    assert not serve.tutor_enabled()
    agent, _ = _agent(home, ScriptedModel())
    assert not TUTOR_TOOLS & set(serve.tool_names(agent))
    assert "calendar_add" in serve.tool_names(agent)          # connectors unaffected


def test_assign_is_always_asked_and_reading_is_not(home):
    assert "tutor_assign" in ALWAYS_ASK
    assert not {"tutor_learners", "tutor_report"} & set(ALWAYS_ASK)
    names = [n.strip().lower() for n in
             __import__("os").environ["AITHER_TOOL_APPROVAL"].split(",")]
    assert "tutor_assign" in names


# ── a REAL agent turn: what the model is actually handed ────────────────────────

@pytest.mark.asyncio
async def test_a_real_turn_hands_the_model_the_tutor_schemas_and_prompt(home, genesis,
                                                                        monkeypatch):
    _signed_in(monkeypatch)
    model = ScriptedModel()
    agent, _ = _agent(home, model)
    assert agent.tool_selection == "all"
    resp = await agent.chat("how did Alexander do this week?", session_id="t-report")

    first = model.requests[0]
    schemas = _schemas(first["tools"])
    assert TUTOR_TOOLS <= set(schemas), sorted(schemas)
    report = schemas["tutor_report"]["parameters"]
    assert report["properties"]["lid"]["type"] == "string"
    assert "lid" in report.get("required", [])
    assign = schemas["tutor_assign"]["parameters"]
    assert {"lid", "skill_id"} <= set(assign.get("required", []))
    assert "note" not in assign.get("required", [])
    assert "name" in schemas["tutor_report"]["description"].lower() or \
        "child" in schemas["tutor_report"]["description"].lower()
    system = next(m for m in first["messages"] if m.role == "system")
    assert "tutor_report(lid=\"Alexander\")" in str(system.content)
    assert "tutor_assign" in str(system.content)

    # The tool really ran against the router, by name, with the guardian bearer.
    assert "tutor_report" in resp.tool_calls_made
    paths = [c["path"] for c in genesis.calls]
    assert paths == ["/api/v1/tutor/family/learners",
                     "/api/v1/tutor/family/learners/lrn_6537875f75a04d60/report"]
    assert {c["auth"] for c in genesis.calls} == {f"Bearer {BEARER}"}
    # ...and what came back to the model is parent text, with no answer data.
    tool_msg = [m for m in model.requests[-1]["messages"] if m.role == "tool"][-1]
    payload = json.loads(tool_msg.content)
    assert payload["summary"].startswith("Alexander did 4 learning sessions")
    assert "items" not in payload and "answer" not in tool_msg.content
    assert "score" not in tool_msg.content


# ── assign needs the owner's yes (the agent's own gate, over Hearth) ────────────

@pytest.mark.asyncio
async def test_assign_is_a_card_first_and_runs_only_on_the_owners_yes(home, genesis,
                                                                      monkeypatch):
    _signed_in(monkeypatch)
    model = ScriptedModel()
    agent, store = _agent(home, model)
    relay = FakeTransport("relay")
    hearth.OwnerRegistry(hearth.owner_path(home)).bind("relay", OWNER)
    core = hearth.HearthCore(agent, store, home / "actions.jsonl", relay, root=home)

    await core.on_message("relay", OWNER, "have Athena practice short vowels")
    assert genesis.posts() == []                               # nothing queued yet
    card = relay.sent[-1][1]
    assert "tutor_assign" in card and "Athena" in card
    nonce = core.awaiting["nonce"]
    assert re.fullmatch(r"[0-9a-f]{8}", nonce)

    await core.on_message("relay", OWNER, f"yes {nonce}")
    posts = genesis.posts()
    assert len(posts) == 1
    assert posts[0]["path"] == "/api/v1/tutor/family/learners/lrn_5493aa7d79d24e84/assign"
    assert posts[0]["body"] == {"skill_id": "read.phx.cvc"}
    rows = receipts.tail(40, path=home / "actions.jsonl")
    tool_rows = [r for r in rows if r["kind"] == "tool" and r["name"] == "tutor_assign"]
    assert tool_rows and tool_rows[-1]["approval"] == f"owner:allow:{nonce}"


@pytest.mark.asyncio
async def test_no_to_the_assign_card_queues_nothing(home, genesis, monkeypatch):
    _signed_in(monkeypatch)
    agent, store = _agent(home, ScriptedModel())
    relay = FakeTransport("relay")
    hearth.OwnerRegistry(hearth.owner_path(home)).bind("relay", OWNER)
    core = hearth.HearthCore(agent, store, home / "actions.jsonl", relay, root=home)
    await core.on_message("relay", OWNER, "have Athena practice short vowels")
    await core.on_message("relay", OWNER, f"no {core.awaiting['nonce']}")
    assert genesis.posts() == []


# ── a tutor READ is not an action a claim can rest on ───────────────────────────

def test_every_tutor_tool_either_asks_or_is_read_only():
    """A tutor tool the owner is not asked about must be a reader, or a claim reply
    after it ("I've added ...") would pass HearthCore._honest unchecked."""
    names = {fn.__name__ for fn in build_tutor_tools("https://genesis.test", BEARER)}
    assert names == TUTOR_TOOLS
    for name in names:
        assert (name in ALWAYS_ASK) != (name in hearth.READ_ONLY_TOOLS), name


@pytest.mark.asyncio
async def test_a_claim_after_only_reading_the_roster_is_replaced(home, genesis, monkeypatch):
    _signed_in(monkeypatch)
    agent, store = _agent(home, ScriptedModel())
    relay = FakeTransport("relay")
    hearth.OwnerRegistry(hearth.owner_path(home)).bind("relay", OWNER)
    core = hearth.HearthCore(agent, store, home / "actions.jsonl", relay, root=home)
    await core.on_message("relay", OWNER, "have Athena practice short vowels, quietly")
    assert [c["path"] for c in genesis.calls] == ["/api/v1/tutor/family/learners"]
    assert genesis.posts() == []
    reply = relay.sent[-1][1]
    assert reply.startswith("I did not do that"), reply
    assert "added" not in reply


# ── set_focus resolves the child's name and the parent's phrases ────────────────

def test_set_focus_by_name_puts_to_the_learner_id(genesis):
    out = json.loads(_tools(genesis)["tutor_set_focus"](
        "Athena", "rhymes, short vowels", "loves dinosaurs"))
    assert out["focus"]["active"] is True
    puts = [c for c in genesis.calls if c["method"] == "PUT"]
    assert len(puts) == 1
    assert puts[0]["path"] == "/api/v1/tutor/family/learners/lrn_5493aa7d79d24e84/focus"
    assert puts[0]["body"] == {"skills": ["read.pa.rhyme", "read.phx.cvc"],
                               "note": "loves dinosaurs"}


def test_set_focus_with_an_unknown_name_puts_nothing(genesis):
    out = json.loads(_tools(genesis)["tutor_set_focus"]("Zed", "rhymes"))
    assert "no child by that name" in out["error"]
    assert not [c for c in genesis.calls if c["method"] == "PUT"]


@pytest.mark.parametrize("value,ids", [
    ("rhymes, short vowels", ["read.pa.rhyme", "read.phx.cvc"]),
    ("rhymes blending", ["read.pa.rhyme", "read.pa.blend_cvc"]),
    ("math.make10 math.add10, math.make10", ["math.make10", "math.add10"]),
    (["magic e", "coins"], ["read.phx.vce", "math.money"]),
    ("", []),
])
def test_focus_skills_accepts_ids_and_phrases(value, ids):
    assert focus_skills(value) == ids


def test_the_prompt_names_both_plan_changing_tools():
    assert "tutor_assign" in serve.TUTOR_PROMPT and "tutor_set_focus" in serve.TUTOR_PROMPT


# ── the tools themselves ────────────────────────────────────────────────────────

def _tools(genesis: Genesis) -> Dict[str, Any]:
    client = httpx.Client(transport=httpx.MockTransport(genesis))
    return {fn.__name__: fn for fn in build_tutor_tools("https://genesis.test", BEARER,
                                                        client=client)}


def test_report_is_parent_safe_text():
    out = parent_report(REPORT, "Alexander")
    text = out["summary"]
    assert text.startswith("Alexander did 4 learning sessions this week (42.5 minutes).")
    assert "Up next: I can take away within 20!" in text
    assert out["secure_skills"] == ["I can add within 20!"]
    low = json.dumps(out).lower()
    for word in ("wrong", "fail", "behind", "streak", "score", "answer", "0.41"):
        assert word not in low, word


def test_a_quiet_week_is_not_a_guilt_trip():
    text = parent_report({"sessions": 0, "minutes": 0}, "Athena")["summary"]
    assert "Athena has no sessions yet this week" in text
    for word in ("missed", "behind", "lost", "streak", "only"):
        assert word not in text.lower(), word


def test_a_name_that_matches_no_child_names_the_children(genesis):
    out = json.loads(_tools(genesis)["tutor_report"]("Zed"))
    assert "no child by that name" in out["error"] and "Athena, Alexander" in out["error"]
    assert [c["path"] for c in genesis.calls] == ["/api/v1/tutor/family/learners"]


def test_a_learner_id_skips_the_roster_lookup(genesis):
    json.loads(_tools(genesis)["tutor_report"]("lrn_6537875f75a04d60"))
    assert [c["path"] for c in genesis.calls] == [
        "/api/v1/tutor/family/learners/lrn_6537875f75a04d60/report"]


@pytest.mark.parametrize("phrase,sid", [
    ("short vowels", "read.phx.cvc"), ("Short Vowels", "read.phx.cvc"),
    ("practice short vowel", "read.phx.cvc"), ("sight words", "read.sw.dolch_primer"),
    ("magic e", "read.phx.vce"), ("coins", "math.money"),
    ("math.add_within_20", "math.add_within_20"), ("something new", "something new"),
])
def test_parent_phrases_map_to_skill_ids(phrase, sid):
    assert resolve_skill(phrase) == sid


def test_every_alias_targets_a_real_skill():
    """Drift guard: each alias must name a node in the monorepo's skill graph."""
    graph = Path(__file__).resolve().parents[2] / "AitherOS" / "config" / "tutor" / \
        "skill_graph.yaml"
    if not graph.is_file():
        pytest.skip("skill graph not in this checkout (standalone awdk)")
    ids = set(re.findall(r"\{id:\s*([a-z0-9_.]+)", graph.read_text(encoding="utf-8")))
    assert ids, "could not read any skill id from the graph"
    missing = sorted({sid for sid in SKILL_ALIASES.values() if sid not in ids})
    assert not missing, missing
