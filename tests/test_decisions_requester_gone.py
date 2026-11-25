"""A card is bound to its REQUESTER's life — the fix for the 58-card zombie inbox.

Measured 2026-11-20 (owner screenshot, Atlas task_9c3a28a43cce): the desk tray
showed the same ``Allow Claude to use: Bash(python ...)`` permission grant twice,
"+5 more waiting on you", "Inbox — 58 decisions waiting" and "Now — one card at a
time (58)", and a popup announced a grant "waiting 1 h" for a session that had
long since ended. Two defects, pinned here:

(a) the one-card inbox shows ONE card and the rest rot, so a live ask never
    reaches him;
(b) a permission grant raised by a session was not bound to that session's life,
    so a dead session's ask stayed open, counted and re-pushed forever.

The rules these tests hold:

* a ``claude-permission-grant`` card whose session (or recorded pid) is gone is
  closed AT ONCE with reason "requester gone", drops out of the counts, and is
  never pushed again;
* any other non-credential card is retired once it is older than its KIND'S TTL
  and its requester is gone (a credential ask never is);
* the badge/inbox counts only actionable cards, and the ONE-CARD order surfaces
  the NEWEST high-urgency ask first;
* a LIVE card still delivers, through the same receipt path PR #18131 added.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from adk.decisions import delivery, liveness
from adk.decisions.notify import NotifyResult
from adk.decisions.store import (
    STATUS_OPEN,
    DecisionCard,
    DecisionOption,
    DecisionSource,
    DecisionStore,
    kind_ttl_seconds,
)

DEAD_PID = 999_999


class Env:
    def __init__(self, store: DecisionStore, sessions, tmp):
        self.store = store
        self.sessions = sessions
        self.tmp = tmp


@pytest.fixture
def env(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("AITHER_CLAUDE_SESSIONS_DIR", str(sessions))
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AWASK_GROUPING", "0")
    import adk.decisions.store as store_mod

    monkeypatch.setattr(store_mod, "_STORE", None, raising=False)
    return Env(DecisionStore(tmp_path / "decisions"), sessions, tmp_path)


def _register(env: Env, session_id: str, pid: int, name: str = "", status: str = "idle") -> None:
    path = env.sessions / (name or f"{session_id}.json")
    path.write_text(json.dumps({"sessionId": session_id, "pid": pid, "status": status}),
                    encoding="utf-8")


def _options():
    return [DecisionOption(key="grant", label="Grant this one rule"),
            DecisionOption(key="deny", label="Deny")]


def _grant(env: Env, *, session_id: str = "", session_pid: int = 0,
           age_s: float = 0.0, agent: str = "claude-permission-grant") -> DecisionCard:
    """A permission-grant card, exactly the shape claude_permission_grant.py raises."""
    return env.store.create(DecisionCard(
        id="", title="Allow Claude to use: Bash(python scripts/org_onboard.py -*)",
        kind="blocked", urgency="high", default_key="deny", options=_options(),
        source=DecisionSource(session_id=session_id, session_pid=session_pid, agent=agent),
        created_at=time.time() - age_s,
    ))


def _decision(env: Env, *, kind: str = "decision", session_id: str = "",
              session_pid: int = 0, age_s: float = 0.0) -> DecisionCard:
    silent = kind in ("blocked", "blocker")     # a stopped session offers no choice
    return env.store.create(DecisionCard(
        id="", title="Approve the routine", kind=kind, urgency="high",
        default_key="" if silent else "grant",
        options=[] if silent else _options(),
        source=DecisionSource(session_id=session_id, session_pid=session_pid,
                              agent="ci"),
        created_at=time.time() - age_s,
    ))


def _quiet_notify(monkeypatch, delivered=()):
    def fake(card, store=None):
        return NotifyResult(delivered=list(delivered), skipped=["card window (off)"],
                            errors=[])

    monkeypatch.setattr("adk.decisions.notify.notify", fake)


def _open_ids(store: DecisionStore) -> list[str]:
    return [c.id for c in store.list(status=STATUS_OPEN)]


# ── (b) a permission grant dies with its session ──────────────────────────────

def test_dead_session_grant_closes_itself_with_requester_gone(env, monkeypatch):
    _register(env, "sess-dead", DEAD_PID)
    _quiet_notify(monkeypatch)
    card = _grant(env, session_id="sess-dead")

    retired = env.store.retire_gone()

    assert [c.id for c in retired] == [card.id]
    after = env.store.get(card.id)
    assert after.status == "expired"
    assert after.retire_reason == "requester gone"
    assert after.answered_via == "requester-gone"
    assert after.answer == "deny", "granting is never the default of a dead session"
    assert after.is_open is False
    assert _open_ids(env.store) == []


def test_a_dead_pid_with_no_registry_entry_still_closes_the_grant(env, monkeypatch):
    _quiet_notify(monkeypatch)
    card = _grant(env, session_id="never-listed", session_pid=DEAD_PID)

    assert [c.id for c in env.store.retire_gone()] == [card.id]
    assert env.store.get(card.id).retire_reason == "requester gone"


def test_store_list_closes_a_dead_grant_without_a_sweep(env, monkeypatch):
    """A surface that only READS the store must never show a grant for a dead
    session. Before this, `_expire_if_due` set the reason but still waited the
    24 h retire window, so `list()` disagreed with `retire_gone` and a consumer
    that never ran the sweep (a CLI, a relay sync) kept the zombie card."""
    _register(env, "sess-dead", DEAD_PID)
    _quiet_notify(monkeypatch)
    card = _grant(env, session_id="sess-dead")

    assert env.store.list(status=STATUS_OPEN) == []
    after = env.store.get(card.id)
    assert after.status == "expired"
    assert after.retire_reason == "requester gone"
    assert after.answered_via == "requester-gone"
    assert after.answer == "deny"


def test_an_unmeasurable_grant_is_kept(env, monkeypatch):
    # No registry record and no pid at all: "not measured", never "gone".
    _quiet_notify(monkeypatch)
    card = _grant(env, session_id="unknown-session")

    assert env.store.retire_gone() == []
    assert env.store.get(card.id).is_open


def test_a_live_sessions_grant_is_kept(env, monkeypatch):
    _register(env, "sess-live", os.getpid())
    _quiet_notify(monkeypatch)
    card = _grant(env, session_id="sess-live", age_s=200)

    assert env.store.retire_gone() == []
    assert env.store.get(card.id).is_open


# ── the sweep wires it in, and a dead card is never pushed again ─────────────

def test_sweep_retires_the_dead_grant_and_never_pushes_it(env, monkeypatch):
    _register(env, "sess-dead", DEAD_PID)
    card = _grant(env, session_id="sess-dead", age_s=200)
    pushed = []

    def fake_push(c, store=None):
        pushed.append(c.id)
        return delivery.DeliveryResult(["relay (forced)"], [], [], True)

    monkeypatch.setattr(delivery, "push", fake_push)

    assert delivery.sweep(env.store) == []
    assert pushed == [], "a card whose requester is gone must never be pushed again"
    assert env.store.get(card.id).is_open is False
    assert not [r for r in delivery._rows_for(card.id)
                if r.get("surface") == delivery.SURFACE_ESCALATE]


def test_a_live_grant_still_pushes_through_the_receipt_path(env, monkeypatch):
    """The #18131 path is unchanged: an undelivered live card is re-pushed."""
    _register(env, "sess-live", os.getpid())
    card = _grant(env, session_id="sess-live", age_s=200)
    _quiet_notify(monkeypatch, delivered=[])            # no real push landed
    monkeypatch.setattr(delivery, "_post_relay", lambda c: None)

    out = delivery.sweep(env.store)

    assert [r["card"] for r in out] == [card.id] and out[0]["delivered"] is True
    assert delivery.delivered(card.id)
    row = [r for r in delivery._rows_for(card.id)
           if r.get("surface") == delivery.SURFACE_ESCALATE][-1]
    assert row["ok"] is True and "relay (forced)" in row["channels"]


def test_an_unbound_grant_retires_on_its_own_ttl_not_at_once(env, monkeypatch):
    """Every card raised before 2026-11-20 names NO session (measured on the
    owner's store: four 20-22 h old grants, session_id="" and pid 0), so proof
    of death is impossible. A grant that names no caller can still never be
    answered into anything, so it retires on its own (longer) window -- not at
    once, because it is not provably dead either."""
    from adk.decisions.store import unbound_grant_ttl_seconds

    _quiet_notify(monkeypatch)
    ttl = unbound_grant_ttl_seconds()
    young = _grant(env)                       # no session, no pid, just raised
    old = _grant(env, age_s=ttl + 60)         # no session, past the window

    retired = env.store.retire_gone()

    assert [c.id for c in retired] == [old.id]
    assert "requester gone" in (env.store.get(old.id).retire_reason or "")
    assert "no session was recorded" in (env.store.get(old.id).retire_reason or "")
    assert env.store.get(young.id).is_open


def test_the_unbound_grant_ttl_is_configurable(env, monkeypatch):
    _quiet_notify(monkeypatch)
    monkeypatch.setenv("AITHER_DECISIONS_UNBOUND_GRANT_TTL_HOURS", "1")
    card = _grant(env, age_s=2 * 3600)

    assert [c.id for c in env.store.retire_gone()] == [card.id]


# ── requirement 2: any other non-credential card past its KIND TTL ───────────

def test_a_dead_decision_card_retires_after_its_kind_ttl(env, monkeypatch):
    _register(env, "sess-dead", DEAD_PID)
    _quiet_notify(monkeypatch)
    ttl = kind_ttl_seconds("decision")
    stale = _decision(env, session_id="sess-dead", age_s=ttl + 60)
    fresh = _decision(env, session_id="sess-dead", age_s=60, kind="blocker")

    retired = env.store.retire_gone()

    assert [c.id for c in retired] == [stale.id]
    assert "requester gone" in (env.store.get(stale.id).retire_reason or "")
    assert env.store.get(fresh.id).is_open


def test_a_dead_blocked_card_waits_for_the_blocked_ttl(env, monkeypatch):
    _register(env, "sess-dead", DEAD_PID)
    _quiet_notify(monkeypatch)
    ttl = kind_ttl_seconds("blocked")
    young = _decision(env, kind="blocked", session_id="sess-dead", age_s=ttl / 2)
    old = _decision(env, kind="blocked", session_id="sess-dead", age_s=ttl + 60)

    assert [c.id for c in env.store.retire_gone()] == [old.id]
    assert env.store.get(young.id).is_open


def test_the_kind_ttl_is_configurable(env, monkeypatch):
    _register(env, "sess-dead", DEAD_PID)
    _quiet_notify(monkeypatch)
    monkeypatch.setenv("AITHER_DECISIONS_BLOCKED_TTL_HOURS", "1")
    card = _decision(env, kind="blocked", session_id="sess-dead", age_s=2 * 3600)

    assert [c.id for c in env.store.retire_gone()] == [card.id]


def test_a_credential_ask_is_never_retired_on_its_raisers_death(env, monkeypatch):
    _register(env, "sess-dead", DEAD_PID)
    _quiet_notify(monkeypatch)
    monkeypatch.setenv("AITHER_DECISIONS_RETIRE_HOURS", "1")
    card = env.store.create(DecisionCard(
        id="", title="Need the Stripe key", kind="credential", urgency="high",
        secret_name="STRIPE_API_KEY", credential_format="api_key",
        credential_description="to test the checkout flow",
        source=DecisionSource(session_id="sess-dead", session_pid=DEAD_PID),
        created_at=time.time() - 30 * 3600,
    ))

    assert kind_ttl_seconds("credential") is None
    assert env.store.retire_gone() == []
    assert env.store.get(card.id).is_open


# ── counts and the one-card order (requirement 3) ────────────────────────────

def test_badge_count_drops_the_moment_the_requester_is_gone(env, monkeypatch):
    _register(env, "sess-dead", DEAD_PID)
    _register(env, "sess-live", os.getpid())
    _quiet_notify(monkeypatch)
    dead = _grant(env, session_id="sess-dead")
    live = _grant(env, session_id="sess-live")

    from adk.decisions.triage import decisions_waiting

    assert env.store.is_actionable(dead) is False
    assert env.store.is_actionable(live) is True
    counted = [c for c in env.store.list(status=STATUS_OPEN) if env.store.is_actionable(c)]
    assert [c.id for c in counted] == [live.id]
    assert len(decisions_waiting(counted)) == 1

    env.store.retire_gone()
    assert _open_ids(env.store) == [live.id]


def test_surface_order_surfaces_the_newest_high_urgency_card_first(env, monkeypatch):
    _register(env, "sess-live", os.getpid())
    _quiet_notify(monkeypatch)
    old_high = _grant(env, session_id="sess-live", age_s=4 * 3600)
    new_high = _grant(env, session_id="sess-live", age_s=30)
    old_normal = env.store.create(DecisionCard(
        id="", title="Choose the release note", urgency="normal", default_key="grant",
        options=_options(), source=DecisionSource(session_id="sess-live"),
        created_at=time.time() - 8 * 3600,
    ))

    order = [c.id for c in env.store.list(status=STATUS_OPEN, order="surface")]

    assert order == [new_high.id, old_high.id, old_normal.id]
    # the LIST order is deliberately different (longest-waiting first, unchanged),
    # and it is exactly what surfaced a stale ask over the live one.
    queue = [c.id for c in env.store.list(status=STATUS_OPEN, order="queue")]
    assert queue[0] == old_high.id and queue[0] != order[0]


def test_surface_order_is_the_beams_own_order(env, monkeypatch):
    from adk.decisions import beam
    from adk.decisions.store import surface_order

    _register(env, "sess-live", os.getpid())
    _quiet_notify(monkeypatch)
    cards = [_grant(env, session_id="sess-live", age_s=10),
             _grant(env, session_id="sess-live", age_s=20)]
    now = time.time()

    assert [c.id for c in surface_order(cards, now)] == [
        c.id for c in sorted(cards, key=lambda c: beam.ask_rank(c, now))]


# ── the daemon records the requester and serves only actionable cards ────────

def test_daemon_raise_records_the_session_and_pid(env, monkeypatch):
    from adk.harnesses import daemon

    root = "root-bearer-for-tests"
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", root)
    _quiet_notify(monkeypatch, delivered=["awdesk"])
    monkeypatch.setattr(delivery, "_post_relay", lambda c: "off: no relay in a test")
    client = TestClient(daemon.create_app(token=root))
    client.headers = {"Authorization": f"Bearer {root}"}

    resp = client.post("/decisions", json={
        "title": "Allow Claude to use: Bash(python x.py --reconcile -*)",
        "kind": "blocked", "urgency": "high", "default": "deny",
        "options": [{"key": "grant", "label": "Grant"}, {"key": "deny", "label": "Deny"}],
        "raised_by": "claude-permission-grant",
        "session_id": "sess-daemon", "session_pid": 4321, "pid": 99,
    })

    assert resp.status_code == 200, resp.text
    card = env.store.get(resp.json()["id"])
    assert card.source.session_id == "sess-daemon"
    assert card.source.session_pid == 4321 and card.source.pid == 99
    # NOT the daemon's own pid, which is what the old default recorded -- a
    # process that outlives every session and would make every card immortal.
    assert card.source.pid != os.getpid()


def test_daemon_list_and_count_hide_a_dead_grant_at_once(env, monkeypatch):
    from adk.harnesses import daemon

    root = "root-bearer-for-tests"
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", root)
    _register(env, "sess-dead", DEAD_PID)
    _register(env, "sess-live", os.getpid())
    _quiet_notify(monkeypatch, delivered=["awdesk"])
    monkeypatch.setattr(delivery, "_post_relay", lambda c: "off: no relay in a test")
    _grant(env, session_id="sess-dead")
    live = _grant(env, session_id="sess-live")

    client = TestClient(daemon.create_app(token=root))
    client.headers = {"Authorization": f"Bearer {root}"}

    listed = client.get("/decisions?status=open&order=surface")
    assert listed.status_code == 200, listed.text
    assert [c["id"] for c in listed.json()["decisions"]] == [live.id]

    counted = client.get("/decisions/count")
    assert counted.status_code == 200, counted.text
    assert counted.json()["open"] == 1


def test_the_registry_cache_never_serves_a_stale_liveness_verdict(env):
    """The cache is keyed on the files' own signature, so a fresh write is seen."""
    _register(env, "sess-x", os.getpid(), name="a.json")
    assert liveness.raiser_gone(_grant(env, session_id="sess-x", session_pid=os.getpid())) is False
    _register(env, "sess-x", DEAD_PID, name="a.json")
    assert liveness.raiser_gone(_grant(env, session_id="sess-x", session_pid=os.getpid())) is True
