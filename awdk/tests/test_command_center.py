"""Tests for adk.shell.command_center — pure logic only, no live fleet."""

from datetime import datetime, timedelta, timezone

import pytest

from adk.shell.command_center.fleet_client import FleetClient, SourceState
from adk.shell.command_center.inbox import InboxItem, gather_inbox
from adk.shell.command_center.palette import fuzzy_score


# ─── fuzzy matching ──────────────────────────────────────────────────────────

def test_fuzzy_score():
    assert fuzzy_score("", "anything") == 0
    assert fuzzy_score("sbr", "sessions browser") >= 0
    assert fuzzy_score("xyz", "sessions browser") == -1
    # Tighter match scores lower (better).
    assert fuzzy_score("hq", "hq dashboard") < fuzzy_score("hd", "hq dashboard")
    assert fuzzy_score("watch", "watchtower") == 0


# ─── inbox aggregation ───────────────────────────────────────────────────────

class _FakeFleet(FleetClient):
    """FleetClient with canned responses; no network."""

    def __init__(self, mail=None, relay=None, alerts=None, nick="david"):
        super().__init__()
        self._mail = mail if mail is not None else SourceState.fail("unreachable")
        self._relay = relay if relay is not None else SourceState.fail("unreachable")
        self._alerts = alerts if alerts is not None else SourceState.fail("unreachable")
        self._nick = nick

    def default_nick(self):
        return self._nick

    async def mail_inbox(self, **kw):
        return self._mail

    async def relay_notifications(self, nick, **kw):
        return self._relay

    async def alerts(self, **kw):
        return self._alerts


def _iso(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


async def test_gather_inbox_merges_and_sorts():
    fc = _FakeFleet(
        mail=SourceState(True, data={"messages": [
            {"id": "m1", "subject": "old mail", "created_at": _iso(90), "sender": "a@x"},
        ]}),
        relay=SourceState(True, data={"notifications": [
            {"id": "r1", "title": "mention", "message": "ping", "from_nick": "atlas",
             "created_at": _iso(5)},
        ]}),
        alerts=SourceState(True, data={"alerts": [
            {"id": "a1", "title": "disk", "message": "low", "severity": 0.9,
             "created_at": _iso(30)},
        ]}),
    )
    items, down = await gather_inbox(fc)
    assert down == []
    assert [i.source for i in items] == ["relay", "alert", "mail"]  # newest first
    assert items[1].severity == pytest.approx(0.9)


async def test_gather_inbox_reports_down_sources():
    fc = _FakeFleet(
        mail=SourceState.fail("timeout >3s"),
        relay=SourceState(True, data={"notifications": []}),
        alerts=SourceState(True, data={"alerts": []}),
    )
    items, down = await gather_inbox(fc)
    assert items == []
    assert down == ["mail: timeout >3s"]


async def test_gather_inbox_no_nick():
    fc = _FakeFleet(
        mail=SourceState(True, data={"messages": []}),
        alerts=SourceState(True, data={"alerts": []}),
        nick="",
    )
    items, down = await gather_inbox(fc)
    assert any(d.startswith("relay: no nick") for d in down)


# ─── hq renderers ────────────────────────────────────────────────────────────

def _assert_fragments(frags):
    assert isinstance(frags, list) and frags
    assert all(isinstance(t, tuple) and len(t) == 2 for t in frags)


def test_hq_renderers_smoke(monkeypatch, tmp_path):
    from adk.shell import claude_sessions as cs
    monkeypatch.setattr(cs, "CRASH_SNAPSHOT", tmp_path / "crash.json")
    from adk.shell.command_center import hq

    state = hq.HQState()
    state.services = {"genesis": SourceState(True, data={"status": "healthy"}),
                      "pulse": SourceState.fail("unreachable")}
    state.queue = SourceState(True, data={
        "queued": 2, "processing": 1, "failed_total": 0,
        "models_loaded": ["qwen3.6"], "vram_used_mb": 100.0,
        "vram_available_mb": 900.0})
    state.alerts = SourceState(True, data={"alerts": [
        {"severity": 0.8, "title": "hot"}]})
    state.mail = SourceState(True, data={"unread": 3})
    state.relay = SourceState.fail("no nick")
    state.sessions_live = 4
    state.sessions_total = 9
    state.crash_pending = True

    for render in (hq.render_fleet, hq.render_llm, hq.render_alerts,
                   hq.render_sessions, hq.render_inbox, hq._render_header):
        _assert_fragments(render(state))
    text = "".join(f for _, f in hq.render_llm(state))
    assert "qwen3.6" in text
    text = "".join(f for _, f in hq.render_inbox(state))
    assert "3 unread" in text


def test_source_state_fail():
    st = SourceState.fail("boom")
    assert not st.ok and st.error == "boom" and st.data is None


# ─── inbox item age formatting ───────────────────────────────────────────────

def test_inbox_age():
    from adk.shell.command_center.inbox import _age
    assert _age(None) == "?"
    assert _age(datetime.now(timezone.utc)) == "now"
    assert _age(datetime.now(timezone.utc) - timedelta(hours=2)) == "2h"
