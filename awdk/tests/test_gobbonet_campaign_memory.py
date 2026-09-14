"""Campaign memory: the notes are scoped, the scopes are enforced, absence is soft.

The one property a happy-path probe cannot see is the BOUNDARY: a secret
recorded as known by one character must be structurally invisible in a scene
that character is absent from. Every test here asserts a positive AND its
boundary, because a memory that returns nothing to everyone passes every
"returns nothing" assertion while being inert (silent-no-op class).
"""

from __future__ import annotations

import importlib
import sys

import pytest

awm = pytest.importorskip("awm", reason="campaign memory tests need awm")

from adk.packs.gobbonet.campaign_memory import (  # noqa: E402
    WORLD,
    CampaignMemory,
    register_campaign_tools,
)


@pytest.fixture()
def mem(tmp_path):
    return CampaignMemory("testcamp", db_path=tmp_path / "camp.db")


class TestScopedNotes:
    def test_world_note_reaches_everyone(self, mem):
        mem.note("The city gates close at dusk.")
        brief = mem.brief([])
        assert "gates close at dusk" in brief
        assert "(everyone)" in brief

    def test_secret_reaches_its_holder_only(self, mem):
        """Theory of mind, enforced by the store: Vex's secret never enters a
        scene Vex is absent from — and DOES enter one Vex is present in."""
        mem.note("The landlord is a vampire.", known_by="Vex")
        mem.note("Rent is due on the first.", known_by=WORLD)

        without_vex = mem.brief(["mira"])
        assert "vampire" not in without_vex
        assert "Rent is due" in without_vex

        with_vex = mem.brief(["vex"])
        assert "vampire" in with_vex
        assert "known to vex" in with_vex

    def test_identical_note_upserts_not_duplicates(self, mem):
        mem.note("The bridge is out.")
        mem.note("The bridge is out.")
        assert mem.brief([]).count("bridge is out") == 1

    def test_sibling_campaigns_never_meet(self, tmp_path):
        a = CampaignMemory("camp-a", db_path=tmp_path / "a.db")
        b = CampaignMemory("camp-b", db_path=tmp_path / "b.db")
        a.note("Only in A.")
        assert "Only in A" not in b.brief([])
        assert b.known_characters() == []

    def test_empty_store_yields_empty_brief(self, mem):
        assert mem.brief([]) == ""
        assert mem.brief(["anyone"]) == ""


class TestPresence:
    def test_presence_is_triggered_by_being_named(self, mem):
        mem.note("Knows the tunnel code.", known_by="Vex")
        msgs = [{"role": "user", "content": "Mira waits outside."}]
        assert mem.present_in(msgs) == []
        msgs.append({"role": "assistant", "content": "Then Vex arrives, smiling."})
        assert mem.present_in(msgs) == ["vex"]

    def test_presence_matches_whole_words_only(self, mem):
        mem.note("A secret.", known_by="Ana")
        msgs = [{"role": "user", "content": "The banana was analyzed."}]
        assert mem.present_in(msgs) == []


class TestBrief:
    def test_budget_is_respected(self, mem):
        for i in range(200):
            mem.note(f"World fact number {i} with some padding text.")
        assert len(mem.brief([], budget_chars=500)) <= 500

    def test_brief_names_the_tool(self, mem):
        mem.note("Anything.")
        assert "campaign_note" in mem.brief([])


class TestFailSoft:
    def test_without_awm_everything_degrades_and_says_so(self, tmp_path, monkeypatch):
        """No awm ⇒ empty briefs, error dicts that name the fix — never a crash,
        never a wrong answer."""
        import adk.packs.gobbonet.campaign_memory as cm

        monkeypatch.setitem(sys.modules, "awm", None)
        try:
            reloaded = importlib.reload(cm)
            m = reloaded.CampaignMemory("x", db_path=tmp_path / "x.db")
            assert m.available() is False
            res = m.note("anything")
            assert res["ok"] is False and "pip install awm" in res["error"]
            assert m.brief(["vex"]) == ""
            assert m.notes_for("vex") == []
            assert m.present_in([{"role": "user", "content": "vex"}]) == []
        finally:
            monkeypatch.delitem(sys.modules, "awm", raising=False)
            importlib.reload(cm)


class _StubRegistry:
    def __init__(self):
        self.tools = {}

    def register(self, fn, name=None, description=None, **_):
        self.tools[name or fn.__name__] = fn


class _StubAgent:
    def __init__(self):
        self.tools = _StubRegistry()


class TestTools:
    def test_the_agent_gets_the_pen_and_it_writes(self, mem):
        agent = _StubAgent()
        assert register_campaign_tools(agent, mem) == 2
        note = agent.tools.tools["campaign_note"]
        recall = agent.tools.tools["campaign_recall"]

        assert note("Garg owes the goblin bank 40gp.", known_by="garg")["ok"]
        got = recall("garg")
        assert got["ok"] and any("40gp" in n["value"] for n in got["notes"])
        # and the boundary again, through the tool surface:
        other = recall("mira")
        assert not any("40gp" in n["value"] for n in other["notes"])

    def test_a_refusing_registry_does_not_kill_chat(self, mem):
        class Refuses:
            class tools:  # noqa: N801 - shape stub
                @staticmethod
                def register(*a, **k):
                    raise RuntimeError("no")

        assert register_campaign_tools(Refuses(), mem) == 0


class TestMixinInjection:
    def test_brief_is_prepended_only_when_nonempty(self, tmp_path, monkeypatch):
        """The stream path injects one system message carrying the brief, and
        injects NOTHING when there is nothing to say."""
        from adk.packs.gobbonet.agentic import AgenticEngineMixin

        class Engine(AgenticEngineMixin):
            pass

        eng = Engine()
        monkeypatch.setenv("AITHER_GOBBONET_HOME", str(tmp_path))
        mem = eng._get_campaign_memory()
        assert mem.available()

        captured = {}

        class FakeAgent:
            class tools:  # noqa: N801 - shape stub
                @staticmethod
                def register(*a, **k):
                    return None

            async def stream_react(self, message, on_event, history, max_steps):
                captured["history"] = history
                on_event({"type": "token", "text": "ok"})

        eng._agent = FakeAgent()

        msgs = [{"role": "user", "content": "hello there"}]
        list(eng.stream_chat(msgs))
        assert captured["history"] == []  # empty store -> untouched history

        mem.note("The moon is fake.", known_by=WORLD)
        list(eng.stream_chat(msgs))
        assert captured["history"][0]["role"] == "system"
        assert "moon is fake" in captured["history"][0]["content"]
