"""Subscription pass-through: follow-ups on a subscribed thread reach the owning session.

The arms that matter most are the refusals: a session cannot subscribe somebody else
(unless the owner-plan router does it), a stranger's text never takes tier 1, and the
sender never gets its own event back. The happy arms exist so "nothing is ever delivered"
cannot pass.
"""

from __future__ import annotations

import pytest

from adk.harnesses.rooms import Room
from adk.harnesses.steer_dispatch import SteerDispatcher
from adk.harnesses.thread_subs import (
    MAX_SUBSCRIBERS_PER_THREAD,
    ThreadSubError,
    ThreadSubscriptions,
    thread_key_from_event,
)

PR = {"surface": "github", "id": "Aitherium/AitherOS#128"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_STEER_DISPATCH_STATUS", str(tmp_path / "status.json"))
    return tmp_path


@pytest.fixture()
def subs(tmp_path):
    table = ThreadSubscriptions(tmp_path / "subs.db")
    yield table
    table.close()


def _mailbox(env, session):
    return sorted((env / "steer" / session).glob("*.md"))


def _ev(actor_id, etype="github.comment", *, kind="human", text="please add a test",
        thread=PR, **extra):
    payload = {"text": text}
    if thread is not None:
        payload["thread"] = thread
    payload.update(extra.pop("payload_extra", {}))
    event = {"type": etype, "actor": {"kind": kind, "id": actor_id}, "payload": payload}
    event.update(extra)
    return event


# -- the table ---------------------------------------------------------------------


def test_table_is_many_to_many_and_unsubscribe_works(subs):
    assert subs.subscribe("github", "r#1", "worker-a") is True
    assert subs.subscribe("github", "r#1", "worker-a") is False  # refresh, not a dupe
    subs.subscribe("github", "r#1", "worker-b")
    subs.subscribe("discord", "999", "worker-a")
    assert subs.subscribers("github", "r#1") == ["worker-a", "worker-b"]
    assert {t["surface"] for t in subs.threads_of("worker-a")} == {"github", "discord"}
    assert subs.unsubscribe("github", "r#1", "worker-a") is True
    assert subs.subscribers("github", "r#1") == ["worker-b"]
    assert subs.unsubscribe_all("worker-a") == 1


def test_idle_rows_expire(tmp_path):
    clock = [1000.0]
    table = ThreadSubscriptions(tmp_path / "s.db", ttl_s=60, now=lambda: clock[0])
    table.subscribe("mail", "<root@x>", "worker-a")
    clock[0] += 61
    assert table.subscribers("mail", "<root@x>") == []
    table.close()


def test_bad_keys_are_refused_and_lookups_never_raise(subs):
    with pytest.raises(ThreadSubError):
        subs.subscribe("GitHub PR", "x", "w")
    with pytest.raises(ThreadSubError):
        subs.subscribe("github", "x", "bad id with spaces")
    assert subs.subscribers("", "") == []
    assert thread_key_from_event({"payload": {"thread": "github#1"}}) is None


def test_subscriber_cap(subs):
    for i in range(MAX_SUBSCRIBERS_PER_THREAD):
        subs.subscribe("github", "r#2", f"w{i}")
    with pytest.raises(ThreadSubError):
        subs.subscribe("github", "r#2", "one-too-many")


# -- the dispatcher ------------------------------------------------------------------


def test_follow_up_passes_through_to_the_subscriber_mailbox(env, subs):
    d = SteerDispatcher(thread_subscriptions=subs)
    room = Room("collective")
    d.handle(room, _ev("worker-a", "thread.subscribe", kind="adk_agent", id="s1"))
    d.handle(room, _ev("maintainer", id="c1", text="move this behind a collapsible"))
    files = _mailbox(env, "worker-a")
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert 'authority="peer"' in body.splitlines()[0]
    assert "collapsible" in body
    assert d.status()["by_channel"] == {"mailbox": 1}


def test_unsubscribed_thread_is_left_to_the_router(env, subs):
    d = SteerDispatcher(thread_subscriptions=subs)
    d.handle(Room("r"), _ev("maintainer", id="c2"))
    assert not (env / "steer").exists()
    assert d.status()["delivered"] == 0 and d.status()["refused"] == 0


def test_feature_off_without_a_table(env):
    d = SteerDispatcher()
    d.handle(Room("r"), _ev("worker-a", "thread.subscribe", kind="adk_agent"))
    d.handle(Room("r"), _ev("maintainer", id="c3"))
    assert not (env / "steer").exists()


def test_sender_never_gets_its_own_event_back(env, subs):
    d = SteerDispatcher(thread_subscriptions=subs)
    subs.subscribe("github", PR["id"], "worker-a")
    subs.subscribe("github", PR["id"], "worker-b")
    d.handle(Room("r"), _ev("worker-a", kind="adk_agent", id="c4", text="pushed the fix"))
    assert _mailbox(env, "worker-a") == []
    assert len(_mailbox(env, "worker-b")) == 1  # peers on the same PR see each other


def test_a_session_cannot_subscribe_another(env, subs):
    d = SteerDispatcher(thread_subscriptions=subs)
    d.handle(Room("r"), _ev("stranger", "thread.subscribe", kind="adk_agent",
                            payload_extra={"subscriber": "victim"}))
    assert subs.subscribers("github", PR["id"]) == []
    assert "owner-plan router" in d.status()["recent"][-1]["detail"]


def test_owner_plan_router_may_subscribe_a_worker(env, subs):
    d = SteerDispatcher(thread_subscriptions=subs)
    room = Room("r")
    stamped = room.publish(
        _ev("router", "thread.subscribe", kind="adk_agent",
            payload_extra={"subscriber": "worker-z"}),
        auth=("owner", "owner"))
    d.handle(room, stamped)
    assert subs.subscribers("github", PR["id"]) == ["worker-z"]


def test_pass_through_never_takes_tier_one(env, subs):
    typed = []
    d = SteerDispatcher(
        thread_subscriptions=subs,
        send_managed_input=lambda sid, text: typed.append((sid, text)) or True,
        tier1_opt_in=lambda _sid: True,
    )
    subs.subscribe("github", PR["id"], "worker-a")
    room = Room("r")
    stamped = room.publish(_ev("maintainer", id="c5"), auth=("owner", "owner"))
    d.handle(room, stamped)
    assert typed == []
    assert len(_mailbox(env, "worker-a")) == 1


def test_retry_does_not_double_deliver(env, subs):
    d = SteerDispatcher(thread_subscriptions=subs)
    subs.subscribe("github", PR["id"], "worker-a")
    d.handle(Room("r"), _ev("maintainer", id="dup"))
    d.handle(Room("r"), _ev("maintainer", id="dup"))
    assert len(_mailbox(env, "worker-a")) == 1


def test_addressed_events_keep_their_old_path(env, subs):
    """A ``to`` wins over the thread: pass-through only handles UNADDRESSED events."""
    d = SteerDispatcher(thread_subscriptions=subs)
    subs.subscribe("github", PR["id"], "worker-a")
    d.handle(Room("r"), _ev("maintainer", id="c6", to=["nobody-known"]))
    assert _mailbox(env, "worker-a") == []
    assert "unknown actor" in d.status()["recent"][-1]["detail"]
