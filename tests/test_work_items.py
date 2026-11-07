"""work_items — the gate, the lifecycle and the attention stage.

WHY THESE CASES
---------------
The module exists to answer three questions that used to have no durable answer:
*who may start work here*, *what was granted*, and *does anything need a person now*.
Every case below is a way one of those could be answered wrongly:

* an unlisted sender, a bot, or a disabled rule must not mint authority;
* ``observe`` mode must record what it *would* accept and start nothing;
* a ``person`` stage must be the only stage ``needs_person`` returns;
* a person's stop must be final, and a resumed item must revalidate its Mandate;
* a retried event must not create a second work item.

The router arms prove the hook is real, not decoration: a refusal and an observation
spawn no worker, an accept spawns one and records the item.
"""

from __future__ import annotations

import json

import pytest

from adk.harnesses.rooms import Room
from adk.harnesses.thread_router import ROUTED_EVENT_TYPE, SpawnRequest, ThreadRouter
from adk.harnesses.thread_subs import ThreadSubscriptions
from adk.harnesses.work_items import (
    AdmissionPolicy,
    Rule,
    WorkItemError,
    WorkItems,
    needs_person,
    policy_from_env,
    stage,
)

PR = {"surface": "github", "id": "Aitherium/AitherOS#128"}
OWNER_AUTH = ("owner", "owner")


def _policy(**kw):
    base = dict(
        allowed={"github": frozenset({"maintainer"})},
        bots={"github": frozenset({"dependabot[bot]"})},
        accept_mode="observe",
        rules=(Rule(id="fix-ci", name="Fix failing CI"),),
        version="p1",
    )
    base.update(kw)
    return AdmissionPolicy(**base)


def _check(store, policy, *, sender="maintainer", human=True, addressed=True,
           event_id="e1", rules=()):
    return store.check(
        policy,
        surface="github",
        thread_id=PR["id"],
        event_id=event_id,
        sender=sender,
        sender_id=sender,
        human=human,
        addressed=addressed,
        matched_rules=rules,
    )


@pytest.fixture()
def store(tmp_path):
    table = WorkItems(tmp_path / "work-items.db")
    yield table
    table.close()


# -- the gate ----------------------------------------------------------------------


def test_a_listed_person_addressing_the_agent_is_an_explicit_request():
    gate = _policy().gate(
        surface="github", sender_id="maintainer", human=True, addressed=True)
    assert gate.admitted
    assert (gate.strength, gate.basis) == ("explicit", "addressed")


def test_a_listed_person_who_does_not_address_the_agent_is_inferred():
    gate = _policy().gate(
        surface="github", sender_id="maintainer", human=True, addressed=False)
    assert gate.admitted
    assert (gate.strength, gate.basis) == ("inferred", "listed")


def test_an_unlisted_person_is_ignored_by_default():
    gate = _policy().gate(
        surface="github", sender_id="stranger", human=True, addressed=True)
    assert not gate.admitted
    assert gate.reason == "sender_not_allowed"


def test_an_unlisted_person_may_be_admitted_by_an_explicit_cell():
    policy = _policy(explicit_for_unlisted=True)
    gate = policy.gate(
        surface="github", sender_id="stranger", human=True, addressed=True)
    assert gate.admitted and gate.strength == "explicit"


def test_a_listed_bot_is_only_ever_an_inferred_candidate():
    gate = _policy().gate(
        surface="github", sender_id="dependabot[bot]", human=False, addressed=True)
    assert gate.admitted
    assert (gate.strength, gate.basis) == ("inferred", "bot")


def test_an_unlisted_bot_is_not_admitted():
    gate = _policy().gate(
        surface="github", sender_id="random[bot]", human=False, addressed=True)
    assert not gate.admitted and gate.reason == "bot_not_listed"


# -- accept mode --------------------------------------------------------------------


def test_explicit_requests_work_in_every_mode(store):
    for mode in ("off", "observe", "auto"):
        policy = _policy(accept_mode=mode)
        decision = _check(store, policy, event_id=f"e-{mode}")
        assert decision.kind == "accept", mode
        assert store.by_event("github", PR["id"], f"e-{mode}") is not None


def test_off_admits_no_inferred_candidate(store):
    policy = _policy(accept_mode="off")
    decision = _check(store, policy, addressed=False, rules=("fix-ci",))
    assert decision.kind == "ignore"
    assert decision.reason == "inferred_not_admitted"
    assert store.open_items() == []


def test_observe_records_what_it_would_accept_and_starts_nothing(store):
    policy = _policy(accept_mode="observe")
    decision = _check(store, policy, addressed=False, rules=("fix-ci",))
    assert decision.kind == "would_accept"
    assert store.open_items() == []
    observed = store.observations()
    assert len(observed) == 1
    assert observed[0]["reason"] == "observed_inferred"
    assert json.loads(observed[0]["rules"]) == ["fix-ci"]


def test_auto_accepts_inferred_only_with_a_matched_enabled_rule(store):
    policy = _policy(accept_mode="auto")
    no_rule = _check(store, policy, addressed=False, event_id="e-none")
    assert no_rule.kind == "ignore" and no_rule.reason == "no_matching_rule"
    accepted = _check(store, policy, addressed=False, event_id="e-rule",
                      rules=("fix-ci",))
    assert accepted.kind == "accept"
    assert accepted.mandate is not None
    assert accepted.mandate.rules == ("fix-ci",)
    assert store.by_event("github", PR["id"], "e-rule") is not None


def test_a_disabled_rule_cannot_carry_an_accept(store):
    policy = _policy(
        accept_mode="auto",
        rules=(Rule(id="fix-ci", name="Fix failing CI", enabled=False),),
    )
    decision = _check(store, policy, addressed=False, rules=("fix-ci",))
    assert decision.kind == "ignore" and decision.reason == "no_matching_rule"


def test_a_retried_event_mints_one_item(store):
    policy = _policy(accept_mode="observe")
    first = _check(store, policy, event_id="same")
    second = _check(store, policy, event_id="same")
    assert first.kind == second.kind == "accept"
    assert len(store.open_items()) == 1


# -- the stage table ----------------------------------------------------------------


def _item(store, **kw):
    policy = _policy(accept_mode="auto")
    decision = _check(store, policy, event_id=kw.pop("event_id", "e-stage"))
    assert decision.mandate is not None
    item = store.by_event("github", PR["id"], "e-stage")
    for key, value in kw.items():
        setattr(item, key, value)
    return item


def test_a_queued_item_is_working_and_needs_no_person(store):
    item = _item(store, assignment_state="pending")
    assert stage(item) == ("working", "queued")
    assert not needs_person(item)


def test_a_running_item_is_working(store):
    item = _item(store, assignment_state="running", report_state="progress")
    assert stage(item) == ("working", "")


def test_a_waiting_report_is_idle(store):
    item = _item(store, assignment_state="ended", report_state="waiting")
    assert stage(item) == ("idle", "")
    assert not needs_person(item)


def test_a_ready_report_awaits_review(store):
    item = _item(store, assignment_state="running", report_state="ready")
    assert stage(item) == ("person", "review")
    assert needs_person(item)


def test_an_attention_awaits_an_answer(store):
    item = _item(store, assignment_state="running", attention="Which branch?")
    assert stage(item) == ("person", "answer")


def test_a_blocked_report_waits_for_a_person_until_a_human_message(store):
    item = _item(store, assignment_state="running", report_state="blocked")
    assert stage(item) == ("person", "blocked")
    item.human_since_report = True
    assert stage(item) == ("working", "")


def test_a_failed_delivery_is_a_person_stage_before_anything_else(store):
    item = _item(store, assignment_state="running", report_state="ready",
                 delivery_error="mailbox unreachable")
    assert stage(item) == ("person", "delivery")


def test_an_ended_item_is_ended_whatever_else_it_carries(store):
    item = _item(store, report_state="ready", attention="ignored")
    store.end(item.id, by="executor", outcome="completed")
    item = store.get(item.id)
    assert stage(item) == ("ended", "")


def test_needs_person_is_oldest_first_and_only_person_stages(store):
    now = [100.0]

    def tick():
        now[0] += 1.0
        return now[0]

    store._now = tick
    policy = _policy(accept_mode="auto")
    first = _check(store, policy, event_id="a")
    second = _check(store, policy, event_id="b")
    third = _check(store, policy, event_id="c")
    assert first.kind == second.kind == third.kind == "accept"
    a = store.by_event("github", PR["id"], "a")
    b = store.by_event("github", PR["id"], "b")
    c = store.by_event("github", PR["id"], "c")
    store.report(a.id, "ready", "open for review")
    store.report(b.id, "progress", "working")
    store.report(c.id, "blocked", "missing credential")
    waiting = store.needs_person()
    assert [item.id for item in waiting] == [a.id, c.id]


# -- lifecycle ----------------------------------------------------------------------


def test_start_report_ask_answer_and_note_human_round_trip(store):
    policy = _policy(accept_mode="auto")
    _check(store, policy, event_id="life")
    item = store.by_event("github", PR["id"], "life")
    store.start(item.id, "worker-1")
    assert store.get(item.id).assignment_state == "running"
    store.report(item.id, "blocked", "needs a decision")
    assert needs_person(store.get(item.id))
    store.note_human_message(item.id)
    assert not needs_person(store.get(item.id))
    store.ask(item.id, "Merge or close?")
    assert needs_person(store.get(item.id))
    store.answer(item.id)
    assert store.get(item.id).attention == ""


def test_a_bad_report_state_is_refused(store):
    policy = _policy(accept_mode="auto")
    _check(store, policy, event_id="bad")
    item = store.by_event("github", PR["id"], "bad")
    with pytest.raises(WorkItemError):
        store.report(item.id, "finished")


def test_a_person_stop_is_final(store):
    policy = _policy(accept_mode="auto")
    _check(store, policy, event_id="stop")
    item = store.by_event("github", PR["id"], "stop")
    store.stop(item.id, "no longer needed")
    ended = store.get(item.id)
    assert (ended.status, ended.ended_by, ended.outcome) == ("ended", "human", "stopped")
    assert store.resume(item.id, policy) is False


def test_an_ended_item_resumes_under_its_unchanged_mandate(store):
    policy = _policy(accept_mode="auto")
    _check(store, policy, event_id="resume")
    item = store.by_event("github", PR["id"], "resume")
    store.end(item.id, by="executor", outcome="completed")
    assert store.resume(item.id, policy) is True
    resumed = store.get(item.id)
    assert resumed.status == "open"
    assert resumed.mandate == item.mandate
    assert resumed.human_since_report is True


def test_a_mandate_the_policy_no_longer_holds_does_not_resume(store):
    policy = _policy(accept_mode="auto")
    _check(store, policy, event_id="revoked", addressed=False, rules=("fix-ci",))
    item = store.by_event("github", PR["id"], "revoked")
    store.end(item.id, by="executor", outcome="completed")
    # Leaving auto mode revokes inferred work.
    assert store.resume(item.id, _policy(accept_mode="observe")) is False
    # Disabling the matched rule revokes it too.
    disabled = _policy(
        accept_mode="auto",
        rules=(Rule(id="fix-ci", name="Fix failing CI", enabled=False),),
    )
    assert store.resume(item.id, disabled) is False
    # Removing the sender from the list revokes it as well.
    removed = _policy(accept_mode="auto", allowed={"github": frozenset()})
    assert store.resume(item.id, removed) is False


def test_an_explicit_mandate_survives_changes_that_revoke_inferred_work(store):
    policy = _policy(accept_mode="auto")
    _check(store, policy, event_id="explicit")
    item = store.by_event("github", PR["id"], "explicit")
    store.end(item.id, by="executor", outcome="completed")
    assert store.resume(item.id, _policy(accept_mode="off")) is True


# -- persistence and env ------------------------------------------------------------


def test_items_survive_a_reopen(tmp_path):
    path = tmp_path / "work-items.db"
    first = WorkItems(path)
    policy = _policy(accept_mode="auto")
    _check(first, policy, event_id="persist")
    item = first.by_event("github", PR["id"], "persist")
    first.report(item.id, "ready", "done")
    first.close()

    second = WorkItems(path)
    again = second.get(item.id)
    assert again is not None
    assert again.report_state == "ready"
    assert again.mandate.rules == ()
    second.close()


def test_policy_from_env_parses_lists_and_refuses_a_bad_mode():
    policy = policy_from_env({
        "AITHER_WORK_ITEMS_ALLOWED": "github:alice, discord:123",
        "AITHER_WORK_ITEMS_BOTS": "github:dependabot[bot]",
        "AITHER_WORK_ITEMS_ACCEPT_MODE": "auto",
        "AITHER_WORK_ITEMS_RULES": "fix-ci,triage",
        "AITHER_WORK_ITEMS_EXPLICIT_FOR_UNLISTED": "1",
    })
    assert policy.allowed["github"] == frozenset({"alice"})
    assert policy.allowed["discord"] == frozenset({"123"})
    assert policy.bots["github"] == frozenset({"dependabot[bot]"})
    assert policy.accept_mode == "auto"
    assert [rule.id for rule in policy.rules] == ["fix-ci", "triage"]
    assert policy.explicit_for_unlisted is True

    with pytest.raises(WorkItemError):
        policy_from_env({"AITHER_WORK_ITEMS_ACCEPT_MODE": "sometimes"})
    with pytest.raises(WorkItemError):
        policy_from_env({"AITHER_WORK_ITEMS_ALLOWED": "nonsense"})


# -- the router hook ----------------------------------------------------------------


@pytest.fixture()
def routed(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_THREAD_ROUTER_STATUS", str(tmp_path / "router.json"))
    table = ThreadSubscriptions(tmp_path / "subs.db")
    store = WorkItems(tmp_path / "work-items.db")
    yield tmp_path, table, store
    table.close()
    store.close()


class _Spawner:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return f"worker-{len(self.requests)}"


def _event(event_id="e1", *, kind="human", actor="maintainer", text="please fix it"):
    return {
        "type": "github.comment",
        "actor": {"kind": kind, "id": actor, "name": actor},
        "payload": {"text": text, "thread": PR, "work_request": True},
        "id": event_id,
    }


def test_a_refused_candidate_spawns_nothing(routed):
    _, subs, store = routed
    spawner = _Spawner()
    policy = _policy(allowed={"github": frozenset({"someone-else"})})

    def admission(request):
        decision = store.check(
            policy, surface=request.surface, thread_id=request.thread_id,
            event_id=request.event_id, sender=request.sender,
            sender_id=request.sender_id, human=request.human,
            addressed=request.addressed)
        return {"accepted": decision.kind == "accept",
                "observed": decision.kind == "would_accept",
                "reason": decision.reason}

    router = ThreadRouter(
        thread_subscriptions=subs, spawn=spawner, admission=admission,
        status_file=routed[0] / "router.json")
    room = Room("r")
    room.add_listener(router.handle)
    room.publish(_event(), auth=OWNER_AUTH)
    assert spawner.requests == []
    assert store.open_items() == []


def test_an_observed_candidate_spawns_nothing_but_is_recorded(routed):
    _, subs, store = routed
    spawner = _Spawner()
    policy = _policy(accept_mode="observe")

    def admission(request):
        decision = store.check(
            policy, surface=request.surface, thread_id=request.thread_id,
            event_id=request.event_id, sender=request.sender,
            sender_id=request.sender_id, human=request.human,
            addressed=request.addressed)
        return {"accepted": decision.kind == "accept",
                "observed": decision.kind == "would_accept",
                "reason": decision.reason}

    router = ThreadRouter(
        thread_subscriptions=subs, spawn=spawner, admission=admission,
        status_file=routed[0] / "router.json")
    room = Room("r")
    room.add_listener(router.handle)
    room.publish(_event(), auth=OWNER_AUTH)
    assert spawner.requests == []
    assert len(store.observations()) == 1


def test_an_accepted_candidate_spawns_and_the_recorder_mints_the_item(routed):
    _, subs, store = routed
    spawner = _Spawner()
    policy = _policy(accept_mode="auto")

    def admission(request):
        decision = store.check(
            policy, surface=request.surface, thread_id=request.thread_id,
            event_id=request.event_id, sender=request.sender,
            sender_id=request.sender_id, human=request.human,
            addressed=request.addressed, matched_rules=("fix-ci",))
        return {"accepted": decision.kind == "accept",
                "observed": decision.kind == "would_accept",
                "reason": decision.reason}

    def on_routed(request, worker_id):
        item = store.by_event(request.surface, request.thread_id, request.event_id)
        assert item is not None
        store.start(item.id, worker_id)

    router = ThreadRouter(
        thread_subscriptions=subs, spawn=spawner, admission=admission,
        on_routed=on_routed, status_file=routed[0] / "router.json")
    room = Room("r")
    room.add_listener(router.handle)
    room.publish(_event(), auth=OWNER_AUTH)
    assert len(spawner.requests) == 1
    item = store.by_event("github", PR["id"], "e1")
    assert item is not None
    assert item.worker_id == "worker-1"
    assert item.assignment_state == "running"
    receipts = [e for e in room.events_since(0) if e["type"] == ROUTED_EVENT_TYPE]
    assert receipts and receipts[0]["payload"]["status"] == "routed"


def test_a_raising_admission_gate_fails_closed(routed):
    _, subs, store = routed
    spawner = _Spawner()

    def admission(request):
        raise RuntimeError("policy store is down")

    router = ThreadRouter(
        thread_subscriptions=subs, spawn=spawner, admission=admission,
        status_file=routed[0] / "router.json")
    room = Room("r")
    room.add_listener(router.handle)
    room.publish(_event(), auth=OWNER_AUTH)
    assert spawner.requests == []
    assert "admission gate failed" in router.status()["last_error"]


# -- the daemon glue ----------------------------------------------------------------


def test_daemon_hooks_admit_and_record_one_item(tmp_path):
    from adk.harnesses import daemon

    store = WorkItems(tmp_path / "wi.db")
    policy = _policy(accept_mode="auto")
    admission, on_routed = daemon._work_item_hooks(store, policy)
    request = SpawnRequest(
        surface="github", thread_id=PR["id"], title="thread",
        text="@aither fix the gate", sender="maintainer",
        sender_id="maintainer", event_id="e9", event_type="github.comment",
        human=True, addressed=True,
    )
    assert admission(request)["accepted"] is True
    on_routed(request, "worker-9")
    item = store.by_event("github", PR["id"], "e9")
    assert item is not None
    assert item.worker_id == "worker-9"
    assert item.assignment_state == "running"
    view = daemon._work_item_view(item)
    assert (view["stage"], view["action"]) == ("working", "")
    store.close()


def test_daemon_hooks_refuse_an_unlisted_sender(tmp_path):
    from adk.harnesses import daemon

    store = WorkItems(tmp_path / "wi.db")
    policy = _policy()
    admission, _ = daemon._work_item_hooks(store, policy)
    request = SpawnRequest(
        surface="github", thread_id=PR["id"], title="thread",
        text="@aither fix the gate", sender="stranger",
        sender_id="stranger", event_id="e10", event_type="github.comment",
        human=True, addressed=True,
    )
    decision = admission(request)
    assert decision["accepted"] is False
    assert decision["reason"] == "sender_not_allowed"
    assert store.open_items() == []
    store.close()
