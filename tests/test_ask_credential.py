"""ask_credential: one tool to get a secret, and the asking agent resumes by itself.

Pinned properties (owner, 2026-10-14: "theres no fucking card in awdesk"):

* a secret already in the vault raises NO card -- the agent is told it is present;
* two agents asking for the same secret at once produce exactly ONE card;
* a close returns ``stored=True`` and a receipt digest, and the returned JSON
  never carries the value;
* a waiter whose session has ended is RESUMED once, not left a mailbox nobody reads.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import adk.decisions.store as store_module
from adk.decisions import agent_tools, secure_prompt, steerback
from adk.decisions.store import DecisionCard, DecisionStore

STUB_VALUE = "stub-Value-9f3c-NEVER-RETURNED"
#: A credential card closes only with a vault receipt (the one-door rule).
_RECEIPT = {"version": 1, "signed": False, "payload": {}, "reason": "test"}


@pytest.fixture()
def store(tmp_path: Path, monkeypatch) -> DecisionStore:
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "cards"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_DECISIONS_POPUP", "0")
    monkeypatch.setenv("AITHER_NO_GUI_PROMPT", "1")
    # Nothing listens here: no test may reach a real harness daemon.
    monkeypatch.setenv("AITHER_HARNESS_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("AITHER_DECISIONS_STEER_URL", raising=False)
    monkeypatch.setattr(store_module, "_STORE", None)
    monkeypatch.setattr(agent_tools, "_find_recipe", lambda name: ("", {}))
    yield store_module.get_store()
    store_module._STORE = None  # noqa: SLF001


def _credential_cards(store: DecisionStore) -> list[DecisionCard]:
    return [c for c in store.list(status=None) if c.kind == "credential"]


def test_present_secret_raises_no_card(store, monkeypatch):
    monkeypatch.setattr(agent_tools, "vault_presence", lambda name, scope: True)
    out = agent_tools.ask_credential_sync("OWNER_ASK_PROBE", "platform",
                                          why="probe the ask path", wait_seconds=5)
    assert out["ok"] and out["present"] is True
    assert out["card_id"] is None
    assert _credential_cards(store) == []


def test_two_agents_at_once_make_one_card(store, monkeypatch):
    monkeypatch.setattr(agent_tools, "vault_presence", lambda name, scope: False)
    barrier = threading.Barrier(2)
    results: list[dict] = []

    def ask(sid: str) -> None:
        barrier.wait()
        results.append(agent_tools.start_credential_ask(
            "OWNER_ASK_PROBE", "platform", why=f"agent {sid} needs it",
            session_id=sid, agent=sid))

    threads = [threading.Thread(target=ask, args=(f"sess-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    cards = _credential_cards(store)
    assert len(cards) == 1, [c.id for c in cards]
    assert {r["card_id"] for r in results} == {cards[0].id}
    assert sorted(r["joined"] for r in results) == [False, True]
    assert len(steerback.load_waiters(store, cards[0].id)) == 2


def test_close_returns_stored_and_digest_never_the_value(store, monkeypatch):
    monkeypatch.setattr(agent_tools, "vault_presence", lambda name, scope: False)
    monkeypatch.setattr(secure_prompt, "_store_for_scope",
                        lambda scope, key, value, **_kw: (True, "stub vault"))
    monkeypatch.setattr(secure_prompt, "_sign_receipt",
                        lambda payload: {"version": 1, "payload": payload, "signed": False,
                                         "reason": "test"})
    monkeypatch.setattr(agent_tools, "POLL_SECONDS", 0.05)
    box: dict = {}

    def owner_types_it() -> None:
        for _ in range(200):
            cards = [c for c in _credential_cards(store) if c.is_open]
            if cards:
                box["code"] = secure_prompt.vault_credential(cards[0].id, store, STUB_VALUE,
                                                             via="test")
                return
            time.sleep(0.05)

    owner = threading.Thread(target=owner_types_it)
    owner.start()
    out = agent_tools.ask_credential_sync("OWNER_ASK_PROBE", "platform",
                                          why="probe the ask path", wait_seconds=20,
                                          session_id="sess-close")
    owner.join(30)
    assert box.get("code", (None,))[0] == 0, box
    assert out["stored"] is True and out["scope"] == "platform"
    assert len(out["receipt_digest"]) == 64
    text = json.dumps(out)
    assert STUB_VALUE not in text
    import hashlib

    assert hashlib.sha256(STUB_VALUE.encode()).hexdigest() not in text
    # The blocking call returned it, so steerback must not ALSO resume or steer.
    states = {w["key"]: w["state"] for w in steerback.load_waiters(store, out["card_id"])}
    assert states == {"sess-close": "returned"}


def test_dead_session_is_resumed_once(store, monkeypatch):
    calls: list[tuple[dict, str]] = []
    posts: list[str] = []
    monkeypatch.setattr(steerback, "RESUME_HOOK",
                        lambda waiter, text: (calls.append((waiter, text)) or (True, "stub")))
    monkeypatch.setattr(steerback, "POST_HOOK",
                        lambda store_, card, line: posts.append(line))
    monkeypatch.setattr(agent_tools, "vault_presence", lambda name, scope: False)
    started = agent_tools.start_credential_ask(
        "OWNER_ASK_PROBE", "platform", why="probe the ask path",
        session_id="sess-dead-0001", task="finish the probe")
    # Its session ended: no daemon PTY, no /chat/steer, no live process.
    monkeypatch.setattr(steerback, "pid_alive", lambda pid: False)
    store.answer(started["card_id"], DecisionCard.CREDENTIAL_ANSWER, receipt=_RECEIPT,
                 note="stored", via="test")
    assert len(calls) == 1, calls
    waiter, text = calls[0]
    assert waiter["session_id"] == "sess-dead-0001"
    assert "stored=True" in text
    assert len(posts) == 1 and "sess-dead-00" in posts[0]
    mailbox = Path(store_module.steer_dir()) / "sess-dead-0001"
    assert not list(mailbox.glob("*.md")), "a dead session must not get a mailbox file"


def test_joining_a_card_raised_elsewhere_keeps_its_raiser(store, monkeypatch):
    """An agent joining a CLI-raised card must not take the raiser's delivery away."""
    from adk.decisions.store import DecisionSource

    raised = store.create(DecisionCard(
        id="", title="Enter OWNER_ASK_PROBE", kind="credential",
        secret_name="OWNER_ASK_PROBE", credential_format="api_key",
        credential_scope="platform", credential_description="probe",
        source=DecisionSource(session_id="sess-cli")))
    monkeypatch.setattr(agent_tools, "vault_presence", lambda name, scope: None)
    joined = agent_tools.start_credential_ask("OWNER_ASK_PROBE", "platform",
                                              why="agent needs it too",
                                              session_id="sess-agent", task="t")
    assert joined["card_id"] == raised.id and joined["joined"] is True
    resumed: list[str] = []
    monkeypatch.setattr(steerback, "RESUME_HOOK",
                        lambda waiter, text: (resumed.append(waiter["session_id"])
                                              or (True, "stub")))
    monkeypatch.setattr(steerback, "POST_HOOK", lambda *a: None)
    monkeypatch.setattr(steerback, "session_live",
                        lambda waiter: waiter.get("session_id") == "sess-cli")
    monkeypatch.setattr(steerback, "deliver", lambda card, text: (False, "no live tier"))
    store.answer(raised.id, DecisionCard.CREDENTIAL_ANSWER, via="test", receipt=_RECEIPT)
    assert resumed == ["sess-agent"]
    box = Path(store_module.steer_dir()) / "sess-cli"
    assert len(list(box.glob("*.md"))) == 1, "the live raiser gets its mailbox answer"
