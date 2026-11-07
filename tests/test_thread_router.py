"""thread_router — the slow path: one work request on an unowned thread, at most one
worker spawned.

WHY THESE CASES
---------------
Every guard in ``thread_router`` is a way this module could spend compute on somebody
else's behalf, so the refusals are the point of this suite: an owned thread stays with the
fast path, an unstamped stranger cannot spawn, a burst cannot become a fleet, a worker
this router started cannot start more, and a retrying producer cannot start two workers
for one request. The happy arms exist so "everything is refused" cannot pass as green --
the spawn is asserted, the subscription is read back from the real table, the mailbox
file is read from disk, and the receipt is read out of a real ``Room``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from adk.harnesses.rooms import Room
from adk.harnesses.steer_dispatch import SteerDispatcher
from adk.harnesses.thread_router import (
    ROUTED_EVENT_TYPE,
    SpawnRequest,
    ThreadRouter,
    is_work_request_event,
    register,
)
from adk.harnesses.thread_subs import ThreadSubscriptions

PR = {"surface": "github", "id": "Aitherium/AitherOS#128"}
PR2 = {"surface": "github", "id": "Aitherium/AitherOS#129"}

#: What ``Room.publish`` stamps when the ROOT bearer (the owner) was presented.
OWNER_AUTH = ("owner", "owner")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_THREAD_ROUTER_STATUS", str(tmp_path / "router.json"))
    return tmp_path


@pytest.fixture()
def subs(tmp_path):
    table = ThreadSubscriptions(tmp_path / "subs.db")
    yield table
    table.close()


class Spawner:
    """Stands in for the daemon's ``mgr.create(config)`` path."""

    def __init__(self) -> None:
        self.requests: list[SpawnRequest] = []
        self.outcome = "ok"  # "ok" | "raise" | "none"

    @property
    def worker_ids(self) -> list[str]:
        return [f"worker-{i + 1}" for i in range(len(self.requests))]

    def __call__(self, request: SpawnRequest):
        self.requests.append(request)
        if self.outcome == "raise":
            raise RuntimeError("harness could not start")
        if self.outcome == "none":
            return None
        return self.worker_ids[-1]


def _router(env, subs, spawner, **kw):
    kw.setdefault("status_file", env / "router.json")
    return ThreadRouter(thread_subscriptions=subs, spawn=spawner, **kw)


def _room(router, name="r"):
    """A real Room with the router registered as a listener.

    A test that published to a Room WITHOUT this would prove nothing: every refusal arm
    would pass because the router was never called.
    """
    room = Room(name)
    room.add_listener(router.handle)
    return room


def _ev(actor_id="maintainer", etype="github.comment", *, kind="human",
        text="please fix the failing gate", thread=PR, work_request=None,
        event_id=None, **extra):
    payload = {"text": text}
    if thread is not None:
        payload["thread"] = thread
    if work_request is not None:
        payload["work_request"] = work_request
    event = {
        "type": etype,
        "actor": {"kind": kind, "id": actor_id, "name": actor_id},
        "payload": payload,
    }
    if event_id:
        event["id"] = event_id
    event.update(extra)
    return event


def _publish(room, event, auth=OWNER_AUTH):
    return room.publish(event, auth=auth)


def _receipts(room):
    return [e for e in room.events_since(0) if e["type"] == ROUTED_EVENT_TYPE]


def _mailbox(env, worker_id):
    return sorted((env / "steer" / worker_id).glob("*.md"))


# -- the deterministic predicate -----------------------------------------------------


def test_the_default_predicate_reads_the_flag_and_the_mention():
    assert is_work_request_event(_ev(work_request=True))
    assert is_work_request_event(_ev(work_request="true"))
    assert is_work_request_event(_ev(text=" @Aither please take this"))
    assert not is_work_request_event(_ev(work_request=False, text="looks good to me"))
    assert not is_work_request_event(_ev(text="ping someone@aither.io about it"))
    assert not is_work_request_event(_ev(text="", work_request=None))
    assert not is_work_request_event({"payload": {"text": "hi"}})


# -- the happy arm -------------------------------------------------------------------


def test_a_work_request_spawns_subscribes_delivers_and_publishes_a_receipt(env, subs):
    spawner = Spawner()
    router = _router(env, subs, spawner)
    room = _room(router, "collective")  # exercise the registered fn(room, event) path

    _publish(room, _ev(work_request=True, event_id="e1"))

    assert len(spawner.requests) == 1
    request = spawner.requests[0]
    assert isinstance(request, SpawnRequest)
    assert (request.surface, request.thread_id) == ("github", PR["id"])
    assert request.sender == "maintainer"
    assert "failing gate" in request.text

    worker = "worker-1"
    assert subs.subscribers("github", PR["id"]) == [worker]

    files = _mailbox(env, worker)
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert 'authority="peer"' in body.splitlines()[0]
    assert "failing gate" in body

    receipts = [e for e in _receipts(room) if e["payload"]["status"] == "routed"]
    assert len(receipts) == 1
    assert receipts[0]["payload"]["worker"] == worker
    assert receipts[0]["causation_id"] == "e1"
    # A receipt must never be re-dispatchable or re-routed: no `to`, no payload.thread.
    assert "to" not in receipts[0]
    assert "thread" not in receipts[0]["payload"]

    status = json.loads((env / "router.json").read_text(encoding="utf-8"))
    assert status["routed"] == 1 and status["workers"] == 1


def test_a_mention_alone_is_enough(env, subs):
    spawner = Spawner()
    router = _router(env, subs, spawner)
    room = _room(router)
    _publish(room, _ev(text="@aither can you take this one?"))
    assert len(spawner.requests) == 1


def test_a_plain_conversation_on_an_unowned_thread_is_not_routed(env, subs):
    spawner = Spawner()
    router = _router(env, subs, spawner)
    room = _room(router)
    _publish(room, _ev(text="nice work, thanks"))
    assert spawner.requests == []
    assert _receipts(room) == []
    assert not (env / "steer").exists()


# -- the refusals --------------------------------------------------------------------


def test_an_owned_thread_stays_with_the_fast_path(env, subs):
    spawner = Spawner()
    router = _router(env, subs, spawner)
    subs.subscribe("github", PR["id"], "worker-a")
    room = _room(router)
    _publish(room, _ev(work_request=True, event_id="owned"))
    assert spawner.requests == []
    assert subs.subscribers("github", PR["id"]) == ["worker-a"]
    assert _receipts(room) == []
    assert router.status()["recent"][-1]["channel"] == "fast-path"


def test_an_unstamped_work_request_is_refused(env, subs):
    spawner = Spawner()
    router = _router(env, subs, spawner)
    room = _room(router)
    _publish(room, _ev(work_request=True), auth=())
    assert spawner.requests == []
    assert subs.subscribers("github", PR["id"]) == []
    receipt = _receipts(room)[0]
    assert receipt["payload"]["status"] == "refused"
    assert "owner-plan" in receipt["payload"]["detail"]
    assert router.status()["refused"] == 1


def test_an_agent_plan_stamp_is_not_an_owner_stamp(env, subs):
    spawner = Spawner()
    router = _router(env, subs, spawner)
    room = _room(router)
    _publish(room, _ev(work_request=True), auth=("agent:peer-tab", "agent"))
    assert spawner.requests == []


def test_allow_unstamped_is_an_explicit_opt_in(env, subs):
    spawner = Spawner()
    router = _router(env, subs, spawner, allow_unstamped=True)
    room = _room(router)
    _publish(room, _ev(work_request=True), auth=())
    assert len(spawner.requests) == 1


def test_a_payload_supplied_auth_block_cannot_buy_a_spawn(env, subs):
    spawner = Spawner()
    router = _router(env, subs, spawner)
    room = _room(router)
    event = _ev(work_request=True)
    event["auth"] = {"plan": "owner", "principal": "owner"}  # a producer's own claim
    _publish(room, event, auth=())
    assert spawner.requests == []


def test_the_same_event_id_spawns_exactly_once(env, subs):
    spawner = Spawner()
    router = _router(env, subs, spawner)
    room = _room(router)
    event = _ev(work_request=True, event_id="dup-1")
    _publish(room, event)
    _publish(room, _ev(work_request=True, event_id="dup-1"))
    assert len(spawner.requests) == 1


def test_the_worker_cap_refuses_a_burst(env, subs):
    spawner = Spawner()
    router = _router(env, subs, spawner, max_workers=2)
    room = _room(router)
    _publish(room, _ev(work_request=True, event_id="a", thread=PR))
    _publish(room, _ev(work_request=True, event_id="b", thread=PR2))
    _publish(room, _ev(work_request=True, event_id="c",
                       thread={"surface": "discord", "id": "999"}))
    assert len(spawner.requests) == 2
    statuses = [e["payload"]["status"] for e in _receipts(room)]
    assert statuses.count("routed") == 2 and statuses.count("refused") == 1
    refusals = [e for e in _receipts(room) if e["payload"]["status"] == "refused"]
    assert "worker cap" in refusals[0]["payload"]["detail"]


def test_a_worker_this_router_spawned_cannot_spawn_more(env, subs):
    spawner = Spawner()
    router = _router(env, subs, spawner)
    room = _room(router)
    _publish(room, _ev(work_request=True, event_id="first", thread=PR))
    assert len(spawner.requests) == 1

    # The worker posts on a DIFFERENT, unowned thread: the fast path does not apply.
    _publish(room, _ev("worker-1", etype="github.comment", kind="adk_agent",
                       work_request=True, event_id="loop", thread=PR2))
    assert len(spawner.requests) == 1
    refusals = [e for e in _receipts(room) if e["payload"]["status"] == "refused"]
    assert "worker this router spawned" in refusals[-1]["payload"]["detail"]


def test_the_loop_guard_outlives_the_worker(env, subs):
    clock = [1000.0]
    spawner = Spawner()
    live: list[str] = []
    router = _router(
        env, subs, spawner, now=lambda: clock[0], reap_grace_s=10,
        list_sessions=lambda: [{"id": i, "state": "ready"} for i in live],
    )
    room = _room(router)
    _publish(room, _ev(work_request=True, event_id="first", thread=PR))
    clock[0] += 11
    assert router.reap() == 1  # the worker ended and was reaped
    assert subs.subscribers("github", PR["id"]) == []
    _publish(room, _ev("worker-1", etype="github.comment", kind="adk_agent",
                       work_request=True, event_id="after-reap", thread=PR2))
    assert len(spawner.requests) == 1


def test_a_raising_spawn_degrades_to_a_receipt(env, subs):
    spawner = Spawner()
    spawner.outcome = "raise"
    router = _router(env, subs, spawner)
    room = _room(router)
    _publish(room, _ev(work_request=True))
    assert subs.subscribers("github", PR["id"]) == []
    receipt = _receipts(room)[0]
    assert receipt["payload"]["status"] == "spawn_failed"
    assert "harness could not start" in receipt["payload"]["detail"]
    assert router.status()["spawn_failed"] == 1


def test_a_spawn_that_returns_nothing_degrades(env, subs):
    spawner = Spawner()
    spawner.outcome = "none"
    router = _router(env, subs, spawner)
    room = _room(router)
    _publish(room, _ev(work_request=True))
    assert _receipts(room)[0]["payload"]["status"] == "spawn_failed"


def test_an_unwired_spawn_callable_degrades(env, subs):
    router = _router(env, subs, None)
    room = _room(router)
    _publish(room, _ev(work_request=True))
    receipt = _receipts(room)[0]
    assert receipt["payload"]["status"] == "spawn_failed"
    assert "no spawn callable" in receipt["payload"]["detail"]


def test_an_addressed_event_is_never_the_slow_path(env, subs):
    spawner = Spawner()
    router = _router(env, subs, spawner)
    room = _room(router)
    _publish(room, _ev(work_request=True, to=["worker-a"]))
    assert spawner.requests == []


def test_thread_control_events_are_not_work(env, subs):
    spawner = Spawner()
    router = _router(env, subs, spawner)
    room = _room(router)
    _publish(room, _ev("s1", etype="thread.subscribe", kind="adk_agent",
                       work_request=True))
    assert spawner.requests == []


def test_a_receipt_cannot_be_routed_again(env, subs):
    """A receipt must not be re-routable, by shape AND by type.

    The old version of this case recorded the spawn count and asserted it unchanged
    with nothing published in between, so it could not fail for any router. This one
    RE-PUBLISHES the receipt (and a forged receipt that carries a thread) and asserts
    the worker count is still one: the shape guard (no ``to``, no ``payload.thread``)
    and the control-type guard are both exercised.
    """
    spawner = Spawner()
    router = _router(env, subs, spawner)
    room = _room(router)
    _publish(room, _ev(work_request=True, event_id="one"))
    receipt = _receipts(room)[0]

    # The receipt's SHAPE is the guard: a receipt is not addressable and is not a
    # thread event, so neither the pass-through nor this router can act on it.
    assert "to" not in receipt
    assert "thread" not in receipt["payload"]

    before = list(spawner.requests)
    replay = {k: v for k, v in receipt.items() if k not in ("seq", "room")}
    _publish(room, replay)
    # Even when a receipt DOES carry a thread, its type is control traffic, never work.
    forged = dict(replay)
    forged["payload"] = dict(replay["payload"], thread=PR)
    _publish(room, forged)

    assert spawner.requests == before
    assert subs.subscribers("github", PR["id"]) == ["worker-1"]


def test_a_raising_subscription_store_does_not_block_delivery(env, subs):
    spawner = Spawner()
    router = _router(env, subs, spawner)

    def _boom(*_a, **_kw):
        raise RuntimeError("store is down")

    subs.subscribe = _boom  # type: ignore[method-assign]
    room = _room(router)
    _publish(room, _ev(work_request=True))
    assert len(spawner.requests) == 1
    assert len(_mailbox(env, "worker-1")) == 1  # the worker still got its request
    receipt = _receipts(room)[0]
    assert "subscribed=False" in receipt["payload"]["detail"]


def test_a_failed_subscribe_does_not_spawn_another_worker(env, subs):
    """A spawn whose subscription write failed must not leave the thread re-spawnable.

    Without the orphan claim the table still looks empty, so every follow-up spawned a
    fresh worker until the cap. The claim keeps the worker the owner of the thread, names
    it in the refusal, and retries the subscription on the next event.
    """
    spawner = Spawner()
    router = _router(env, subs, spawner)
    attempts = {"n": 0}

    def _full(*_a, **_kw):
        attempts["n"] += 1
        raise RuntimeError("table is full")

    subs.subscribe = _full  # type: ignore[method-assign]
    room = _room(router)
    _publish(room, _ev(work_request=True, event_id="first", thread=PR))
    assert len(spawner.requests) == 1
    routed = _receipts(room)[0]
    assert routed["payload"]["status"] == "routed"
    assert "subscribed=False" in routed["payload"]["detail"]
    assert "holds this thread" in routed["payload"]["detail"]

    _publish(room, _ev(work_request=True, event_id="second", thread=PR))

    assert len(spawner.requests) == 1  # no second worker for the same thread
    assert attempts["n"] >= 2  # the subscription was retried, not abandoned
    refusals = [e for e in _receipts(room) if e["payload"]["status"] == "refused"]
    assert len(refusals) == 1
    assert "worker-1" in refusals[0]["payload"]["detail"]


# -- the injectable predicate --------------------------------------------------------


def test_the_predicate_is_injectable(env, subs):
    spawner = Spawner()
    router = _router(
        env, subs, spawner,
        work_request_predicate=lambda ev: ev["payload"].get("kind") == "task",
    )
    room = _room(router)
    _publish(room, _ev(text="no mention here"))
    assert spawner.requests == []
    event = _ev(text="no mention here")
    event["payload"]["kind"] = "task"
    _publish(room, event)
    assert len(spawner.requests) == 1


def test_an_injected_predicate_can_refuse_everything(env, subs):
    spawner = Spawner()
    router = _router(env, subs, spawner, work_request_predicate=lambda _ev: False)
    room = _room(router)
    _publish(room, _ev(work_request=True))
    assert spawner.requests == []


# -- session end ---------------------------------------------------------------------


def test_reap_drops_a_finished_workers_subscriptions(env, subs):
    clock = [1000.0]
    spawner = Spawner()
    live: list[str] = []
    router = _router(
        env, subs, spawner, now=lambda: clock[0], reap_grace_s=10,
        list_sessions=lambda: [{"id": i, "state": "ready"} for i in live],
    )
    room = _room(router)
    _publish(room, _ev(work_request=True))
    live.append("worker-1")
    clock[0] += 11
    assert router.reap() == 0  # listed as live: nothing to drop
    assert subs.subscribers("github", PR["id"]) == ["worker-1"]

    live.clear()
    assert router.reap() == 1
    assert subs.subscribers("github", PR["id"]) == []
    assert router.status()["workers"] == 0
    assert router.status()["recent"][-1]["channel"] == "reap"


def test_a_failed_session_is_reaped_even_if_still_listed(env, subs):
    clock = [1000.0]
    spawner = Spawner()
    router = _router(
        env, subs, spawner, now=lambda: clock[0], reap_grace_s=10,
        list_sessions=lambda: [{"id": "worker-1", "state": "exited"}],
    )
    room = _room(router)
    _publish(room, _ev(work_request=True))
    clock[0] += 11
    assert router.reap() == 1
    assert subs.subscribers("github", PR["id"]) == []


def test_reap_respects_the_grace_period(env, subs):
    clock = [1000.0]
    spawner = Spawner()
    router = _router(
        env, subs, spawner, now=lambda: clock[0], reap_grace_s=10,
        list_sessions=lambda: [],  # a brand-new session is not listed yet
    )
    room = _room(router)
    _publish(room, _ev(work_request=True))
    clock[0] += 5
    assert router.reap() == 0
    assert subs.subscribers("github", PR["id"]) == ["worker-1"]


def test_reap_does_not_judge_when_the_lister_raises(env, subs):
    clock = [1000.0]
    spawner = Spawner()

    def _boom():
        raise RuntimeError("listing is down")

    router = _router(
        env, subs, spawner, now=lambda: clock[0], reap_grace_s=10,
        list_sessions=_boom,
    )
    room = _room(router)
    _publish(room, _ev(work_request=True))
    clock[0] += 11
    assert router.reap() == 0  # cannot judge is not "all are dead"
    assert subs.subscribers("github", PR["id"]) == ["worker-1"]
    assert "listing failed" in router.status()["last_error"]


def test_session_ended_unsubscribes_only_this_routers_workers(env, subs):
    spawner = Spawner()
    router = _router(env, subs, spawner)
    subs.subscribe("github", PR["id"], "stranger")
    room = _room(router)
    _publish(room, _ev(work_request=True, thread=PR2))
    assert router.session_ended("stranger") == 0
    assert subs.subscribers("github", PR["id"]) == ["stranger"]
    assert router.session_ended("worker-1") == 1
    assert subs.subscribers("github", PR2["id"]) == []


# -- registration --------------------------------------------------------------------


class _Registry:
    def __init__(self) -> None:
        self.listeners = []

    def add_listener(self, fn) -> None:
        self.listeners.append(fn)


def test_register_puts_the_handler_on_the_registry(env, subs):
    registry = _Registry()
    router = register(
        registry, thread_subscriptions=subs, spawn=Spawner(),
        status_file=env / "router.json",
    )
    assert registry.listeners == [router.handle]


def test_register_refuses_to_wire_the_router_before_the_dispatcher(env, subs):
    """Ordering is load-bearing: reversed, a first work request is delivered twice.

    Before this guard the module documented no requirement and asserted none, so a
    future reorder in ``daemon.py`` would silently double every routing.
    """

    class _Dispatcher:
        def handle(self, room, event):
            pass

    dispatcher = _Dispatcher()
    registry = _Registry()
    with pytest.raises(ValueError, match="AFTER the SteerDispatcher"):
        register(
            registry, must_follow=dispatcher, thread_subscriptions=subs,
            spawn=Spawner(), status_file=env / "router.json",
        )
    assert registry.listeners == []

    registry.add_listener(dispatcher.handle)  # the fast path is in place first
    router = register(
        registry, must_follow=dispatcher, thread_subscriptions=subs,
        spawn=Spawner(), status_file=env / "router.json",
    )
    assert registry.listeners == [dispatcher.handle, router.handle]


def test_dispatcher_registered_first_delivers_the_first_request_exactly_once(env, subs):
    """The end-to-end shape of the ordering rule.

    A router registered after the dispatcher delivers the triggering text once; if the
    dispatcher ran after the router it would see the worker's brand-new subscription on
    the same event and write the text a second time.
    """
    spawner = Spawner()
    writes = []

    def _write(session_id, lines, **_kwargs):
        writes.append((session_id, list(lines)))
        return env / "steer" / session_id / "x.md"

    dispatcher = SteerDispatcher(
        thread_subscriptions=subs, write_steer_fn=_write,
        status_file=env / "dispatch.json",
    )
    router = _router(env, subs, spawner, write_steer_fn=_write)
    room = Room("collective")
    room.add_listener(dispatcher.handle)  # fast path first, as daemon.py wires it
    room.add_listener(router.handle)

    _publish(room, _ev(work_request=True, event_id="e1"))

    assert len(spawner.requests) == 1
    worker_writes = [w for w in writes if w[0] == "worker-1"]
    assert len(worker_writes) == 1
    assert "failing gate" in worker_writes[0][1][0]


def test_a_router_without_a_table_is_a_programming_error(subs):
    with pytest.raises(ValueError):
        ThreadRouter(thread_subscriptions=None)


# -- the daemon wiring: default OFF --------------------------------------------------


@pytest.fixture()
def build_daemon(tmp_path, monkeypatch):
    """Build the real daemon app with every heavyweight side effect stubbed out.

    Only the SESSION PROCESS and the room/steer roots are stubbed; the wiring under test
    (``create_app`` deciding whether to register the router) is the real code.
    """
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("AITHER_THREAD_SUBS_DB", str(tmp_path / "subs.db"))
    monkeypatch.setenv("AITHER_THREAD_ROUTER_STATUS", str(tmp_path / "router.json"))
    monkeypatch.setenv("AITHER_AUTOPILOT_WATCH", "0")
    monkeypatch.setenv("AITHER_HOME_OVERRIDE", str(tmp_path / "nohome"))

    import adk.harnesses.daemon as daemon
    from adk.harnesses import rooms as rooms_mod
    from adk.harnesses import session as session_mod
    from adk.harnesses import session_directory as directory_mod
    from adk.harnesses.manager import SessionManager

    monkeypatch.setattr(rooms_mod, "_registry", None)
    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", tmp_path / "harness_tokens.json")
    monkeypatch.setattr(
        directory_mod, "_directory",
        directory_mod.SessionDirectory(discover_fn=lambda: []),
    )
    monkeypatch.setattr(session_mod.HarnessSession, "start", lambda self: None)
    mgr = SessionManager(root=tmp_path / "sessions")

    def _build():
        return daemon.create_app(manager=mgr, token="router-wiring-token")

    return _build


def test_the_router_is_off_by_default(build_daemon, monkeypatch):
    monkeypatch.delenv("AITHER_THREAD_ROUTER", raising=False)
    app = build_daemon()
    assert app.state.thread_router is None


def test_the_env_flag_registers_the_router(build_daemon, monkeypatch):
    monkeypatch.setenv("AITHER_THREAD_ROUTER", "1")
    app = build_daemon()
    router = app.state.thread_router
    assert isinstance(router, ThreadRouter)
    assert router.status()["max_workers"] == 4

    # The daemon must register the router AFTER the steer dispatcher. Reversed, the
    # dispatcher's pass-through would deliver a first work request to its brand-new
    # worker a second time.
    from adk.harnesses.rooms import default_registry

    listeners = list(default_registry()._listeners)
    index = listeners.index(router.handle)
    assert any(
        isinstance(getattr(fn, "__self__", None), SteerDispatcher)
        for fn in listeners[:index]
    )


def test_the_router_spawn_applies_the_entitlement_preflight(
    build_daemon, monkeypatch, capsys
):
    """_spawn_thread_worker must judge the harness entitlement, as POST /sessions does.

    A router-spawned worker is created outside the HTTP route, so without this check a
    harness the box is not licensed for would start anyway.
    """
    import adk.harnesses.daemon as daemon

    monkeypatch.setenv("AITHER_THREAD_ROUTER", "1")
    app = build_daemon()
    router = app.state.thread_router
    request = SpawnRequest(
        surface="github", thread_id="Aitherium/AitherOS#128", title="t", text="do it",
        sender="owner", sender_id="owner", event_id="e1", event_type="github.comment",
    )

    class _Spec:
        requires_entitlement = "premium"

    monkeypatch.setattr(daemon, "get_harness", lambda _name: _Spec())

    # A principal WITHOUT the entitlement is refused before mgr.create is reached.
    monkeypatch.setattr(
        daemon, "OWNER_PRINCIPAL",
        daemon.Principal(id="owner", plan="owner", entitlements=frozenset()),
    )
    assert router._spawn(request) is None
    assert "lacks entitlement" in capsys.readouterr().err

    # Positive control: a principal HOLDING it is not refused by this branch (the
    # spawn may still fail for an unrelated manager reason, but not on entitlement).
    monkeypatch.setattr(
        daemon, "OWNER_PRINCIPAL",
        daemon.Principal(id="owner", plan="owner", entitlements=frozenset({"premium"})),
    )
    router._spawn(request)
    assert "lacks entitlement" not in capsys.readouterr().err


WORK_REQUEST = SpawnRequest(
    surface="github", thread_id="Aitherium/AitherOS#128", title="t", text="do it",
    sender="owner", sender_id="owner", event_id="e1", event_type="github.comment",
)


def _capture_create(monkeypatch, captured):
    """Record the SessionConfig the spawn closure hands the manager."""

    def _create(self, config, **kwargs):
        captured["config"] = config
        return SimpleNamespace(id="worker-1")

    from adk.harnesses.manager import SessionManager

    class _Spec:
        requires_entitlement = ""

    import adk.harnesses.daemon as daemon

    monkeypatch.setattr(daemon, "get_harness", lambda _name: _Spec())
    monkeypatch.setattr(SessionManager, "create", _create)


def test_a_router_spawned_worker_carries_the_validated_permission_mode(
    build_daemon, monkeypatch
):
    """The env's mode must REACH SessionConfig, not only the two helper functions.

    Deleting ``permission_mode`` from the spawn closure leaves every helper unit test
    green, so this drives the real router spawn and reads what ``mgr.create`` received.
    """
    captured: dict = {}
    _capture_create(monkeypatch, captured)
    monkeypatch.setenv("AITHER_THREAD_ROUTER", "1")
    monkeypatch.setenv("AITHER_THREAD_ROUTER_PERMISSION_MODE", "acceptEdits")
    app = build_daemon()

    assert app.state.thread_router._spawn(WORK_REQUEST) == "worker-1"
    assert captured["config"].permission_mode == "acceptEdits"


def test_an_unset_permission_mode_spawns_with_the_harness_default(
    build_daemon, monkeypatch
):
    captured: dict = {}
    _capture_create(monkeypatch, captured)
    monkeypatch.setenv("AITHER_THREAD_ROUTER", "1")
    monkeypatch.delenv("AITHER_THREAD_ROUTER_PERMISSION_MODE", raising=False)
    app = build_daemon()

    assert app.state.thread_router._spawn(WORK_REQUEST) == "worker-1"
    assert captured["config"].permission_mode == ""


def test_an_invalid_mode_is_not_judged_while_the_router_is_off(
    build_daemon, monkeypatch, capsys
):
    """A disabled feature must not log a refusal for an operator's stale shell value."""
    monkeypatch.delenv("AITHER_THREAD_ROUTER", raising=False)
    monkeypatch.setenv("AITHER_THREAD_ROUTER_PERMISSION_MODE", "bypassPermissions")
    app = build_daemon()

    assert app.state.thread_router is None
    assert "thread router refused" not in capsys.readouterr().err


def test_an_invalid_mode_refuses_to_start_the_enabled_router(
    build_daemon, monkeypatch, capsys
):
    monkeypatch.setenv("AITHER_THREAD_ROUTER", "1")
    monkeypatch.setenv("AITHER_THREAD_ROUTER_PERMISSION_MODE", "bypassPermissions")
    app = build_daemon()

    assert app.state.thread_router is None
    err = capsys.readouterr().err
    assert "thread router refused" in err
    assert "bypassPermissions" in err

