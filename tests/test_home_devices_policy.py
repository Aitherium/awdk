"""The household device risk policy and the quorum store: pure, table-driven."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from adk.home.devices import approvals as ap
from adk.home.devices import policy as pol

NOON = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
NIGHT = datetime(2026, 10, 7, 23, 30, tzinfo=timezone.utc)
ROLES = ("owner", "co_guardian", "adult", "teen", "child", "guest")
#: class -> one representative action on one entity
CASES = {
    "free": ("light.kitchen", "turn_on", {}),
    "confirm": ("cover.blinds", "open_cover", {}),
    "guarded": ("lock.front_door", "unlock", {}),
}
DEVICE_CLASSES = {"cover.blinds": "blind"}


def expected(cls: str, role: str, tainted: bool) -> str:
    if role == "guest":
        return pol.REFUSE
    if role in ("teen", "child"):
        return pol.ACT if cls == "free" and not tainted else pol.ASK_PARENT
    if cls == "guarded":
        return pol.ASK_GUARDIANS
    if cls == "confirm" or tainted:
        return pol.ASK_ASKER
    return pol.ACT


@pytest.mark.parametrize("cls", list(CASES))
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("tainted", [False, True])
def test_every_class_role_and_taint(cls, role, tainted):
    eid, action, params = CASES[cls]
    person = {"pid": "p1", "name": "Ada", "role": role}
    d = pol.decide(cfg=pol.default_config(), person=person, entity_id=eid, action=action,
                   params=params, tainted=tainted, now=NOON, exposed=[eid],
                   device_class=DEVICE_CLASSES.get(eid))
    assert d.verdict == expected(cls, role, tainted), d
    if d.verdict != pol.REFUSE:
        assert d.cls == cls
    if d.verdict in (pol.ASK_GUARDIANS, pol.ASK_PARENT):
        assert d.approvals_required == 1 and d.approvers == pol.ANY_GUARDIAN
    if d.verdict == pol.ASK_ASKER:
        assert d.approvers == ["p1"]


@pytest.mark.parametrize("eid,dc,cls", [
    ("light.x", None, "free"), ("fan.x", None, "free"), ("scene.x", None, "free"),
    ("media_player.x", None, "free"), ("climate.x", None, "confirm"),
    ("cover.x", "blind", "confirm"), ("cover.x", "shade", "confirm"),
    ("cover.x", "garage", "guarded"), ("cover.x", "gate", "guarded"),
    ("cover.x", "door", "guarded"), ("vacuum.x", None, "confirm"),
    ("lock.x", None, "guarded"), ("alarm_control_panel.x", None, "guarded"),
    ("camera.x", None, "guarded"), ("valve.x", None, "guarded"),
    ("switch.x", None, "confirm"), ("something_new.x", None, "confirm"),
])
def test_entity_classes(eid, dc, cls):
    assert pol.entity_class(eid, dc) == cls


def test_override_table_and_dangerous():
    cfg = pol.validate_config({"devices": {"switch.oven": {"dangerous": True},
                                           "lock.shed": {"class": "confirm"},
                                           "light.lava": {"class": "guarded"}}})
    assert pol.entity_class("switch.oven", None, cfg) == "guarded"
    assert pol.entity_class("lock.shed", None, cfg) == "confirm"
    assert pol.action_class("light.lava", "turn_on", {}, cfg, NOON) == "guarded"


def test_climate_band():
    cfg = pol.default_config()
    assert pol.action_class("climate.hall", "set_temperature", {"temperature": 21}, cfg, NOON) == "free"
    assert pol.action_class("climate.hall", "set_temperature", {"temperature": 29}, cfg, NOON) == "confirm"
    assert pol.action_class("climate.hall", "set_temperature", {}, cfg, NOON) == "confirm"
    assert pol.action_class("climate.hall", "set_hvac_mode", {"hvac_mode": "off"}, cfg, NOON) == "confirm"


def test_speakers_in_quiet_hours():
    cfg = pol.default_config()          # 21:00-07:00 UTC
    assert pol.action_class("media_player.lr", "media_play", {}, cfg, NOON) == "free"
    assert pol.action_class("media_player.lr", "media_play", {}, cfg, NIGHT) == "confirm"
    assert pol.action_class("media_player.lr", "volume_set", {"volume_level": .9}, cfg, NIGHT) == "confirm"
    assert pol.action_class("media_player.lr", "media_pause", {}, cfg, NIGHT) == "free"
    with pytest.raises(ValueError):
        pol.in_quiet_hours(cfg, datetime(2026, 1, 1, 23, 0))   # naive


def test_camera_actions_are_modelled():
    cfg = pol.default_config()
    for act in ("turn_on", "turn_off", "record", "snapshot"):
        assert pol.action_class("camera.porch", act, {}, cfg, NOON) == "guarded"
    assert pol.action_class("camera.porch", "summarize", {}, cfg, NOON) == "confirm"
    assert ("camera", "summarize") in pol.TAINTING_ACTIONS


def test_exposure_by_role():
    ids = ["light.kitchen", "light.ada_room", "scene.bedtime", "lock.front_door",
           "camera.porch"]
    cfg = pol.validate_config({"hidden": ["camera.porch"]})
    adult = {"pid": "a", "name": "Sam", "role": "adult"}
    ada = {"pid": "k", "name": "Ada Smith", "role": "child"}
    guest = {"pid": "g", "name": "Visitor", "role": "guest"}
    assert pol.exposed_to(adult, cfg, ids) == sorted(set(ids) - {"camera.porch"})
    assert pol.exposed_to(ada, cfg, ids) == ["light.ada_room", "scene.bedtime"]
    assert pol.exposed_to(guest, cfg, ids) == []
    listed = pol.validate_config({"exposure": {"people": {"k": ["light.kitchen"]}}})
    assert pol.exposed_to(ada, listed, ids) == ["light.kitchen"]


def test_child_cannot_reach_unlisted_and_guarded_asks_a_parent():
    ada = {"pid": "k", "name": "Ada", "role": "child"}
    d = pol.decide(cfg=pol.default_config(), person=ada, entity_id="light.kitchen",
                   action="turn_on", params={}, tainted=False, now=NOON,
                   exposed=["light.ada_room"])
    assert d.verdict == pol.REFUSE and "ask a parent" in d.reason
    d = pol.decide(cfg=pol.default_config(), person=ada, entity_id="lock.front_door",
                   action="unlock", params={}, tainted=False, now=NOON,
                   exposed=["lock.front_door"])
    assert d.verdict == pol.ASK_PARENT and d.cls == "guarded"


def test_device_rule_over_class_rule():
    cfg = pol.validate_config({
        "classes": {"guarded": {"approvals_required": 1, "approvers": "any_guardian"}},
        "devices": {"lock.front_door": {"approvals_required": 2,
                                        "approvers": ["g1", "g2", "g3"],
                                        "expires_s": 120}}})
    d = pol.decide(cfg=cfg, person={"pid": "a", "role": "adult"},
                   entity_id="lock.front_door", action="unlock", params={}, tainted=False,
                   now=NOON, exposed=["lock.front_door"])
    assert (d.approvals_required, d.approvers, d.expires_s, d.rule_source) == \
        (2, ["g1", "g2", "g3"], 120, "device")


@pytest.mark.parametrize("bad", [
    {"devices": {"lock.x": {"approvals_required": 3, "approvers": ["a", "b"]}}},
    {"classes": {"guarded": {"approvals_required": 0}}},
    {"classes": {"nope": {}}},
    {"quiet_hours": {"start": "25:00", "end": "07:00"}},
    {"quiet_hours": {"start": "21:00", "end": "07:00", "timezone": "Mars/Base"}},
    {"climate_band": {"min": 25, "max": 18}},
    {"exposure": {"people": {"k": ["not an id"]}}},
])
def test_invalid_policies_are_refused(bad):
    with pytest.raises(pol.PolicyError):
        pol.validate_config(bad)


@pytest.mark.parametrize("domain,action,target,params", [
    ("light", "turn_on", "lock.front_door", {}),           # domain mismatch
    ("light", "turn_on", "light.kitchen", {"entity_id": "lock.front_door"}),
    ("light", "turn_on", "light.kitchen", {"target": {"entity_id": "lock.x"}}),
    ("light", "turn_on", "light.kitchen", {"area_id": "house"}),
    ("light", "turn_on", "light.kitchen", "not json"),
    ("Light", "turn on", "light.kitchen", {}),
])
def test_actions_that_would_retarget_are_refused(domain, action, target, params):
    with pytest.raises(pol.PolicyError):
        pol.normalize_action(domain, action, target, params)


def test_digest_binds_every_field():
    a = pol.normalize_action("climate", "set_temperature", "climate.hall", {"temperature": 21})
    b = pol.normalize_action("climate", "set_temperature", "climate.hall", '{"temperature": 22}')
    assert pol.action_digest(a, "hh") != pol.action_digest(b, "hh")
    assert pol.action_digest(a, "hh") != pol.action_digest(a, "other")
    assert pol.action_digest(a, "hh") == pol.action_digest(dict(a), "hh")


# ── quorum ────────────────────────────────────────────────────────────────────

def _row(store, required=2, approvers=("g1", "g2", "g3"), expires_s=600, now=1000.0):
    act = pol.normalize_action("lock", "unlock", "lock.front_door")
    return ap.create(store, action=act, household="hh", verdict="guarded",
                     required=required, approvers=list(approvers),
                     requested_by={"pid": "a"}, expires_s=expires_s, now=now,
                     summary="unlock", policy={"cls": "guarded"})


def test_two_of_three_quorum():
    store = {}
    row = _row(store)
    g = ["g1", "g2", "g3"]
    ap.vote(row, pid="g1", allow=True, now=1001, eligible=g)
    assert row["status"] == ap.PENDING and row["approvals"] == 1
    ap.vote(row, pid="g1", allow=True, now=1002, eligible=g)        # same person twice
    assert row["status"] == ap.PENDING and row["approvals"] == 1
    ap.vote(row, pid="g3", allow=True, now=1003, eligible=g)
    assert row["status"] == ap.APPROVED
    act = ap.claim_for_run(row, 1004)
    assert act["target"] == "lock.front_door" and row["status"] == ap.EXECUTING
    with pytest.raises(ap.ApprovalError):
        ap.claim_for_run(row, 1005)                                  # runs once


def test_denied_when_quorum_is_out_of_reach():
    store = {}
    row = _row(store)
    g = ["g1", "g2", "g3"]
    ap.vote(row, pid="g1", allow=False, now=1001, eligible=g)
    assert row["status"] == ap.PENDING                              # 2 yes still possible
    ap.vote(row, pid="g2", allow=False, now=1002, eligible=g)
    assert row["status"] == ap.DENIED


def test_expiry_and_strangers():
    store = {}
    row = _row(store, expires_s=60)
    with pytest.raises(ap.ApprovalError):
        ap.vote(row, pid="intruder", allow=True, now=1001, eligible=["g1", "g2", "g3"])
    with pytest.raises(ap.ApprovalError):
        ap.vote(row, pid="g1", allow=True, now=1061, eligible=["g1", "g2", "g3"])
    assert row["status"] == ap.EXPIRED


def test_removed_guardian_no_longer_counts():
    store = {}
    row = _row(store, required=1, approvers=("g1", "g2"))
    with pytest.raises(ap.ApprovalError):
        ap.vote(row, pid="g2", allow=True, now=1001, eligible=["g1"])
    assert row["status"] == ap.PENDING


def test_tampered_action_never_runs():
    store = {}
    row = _row(store, required=1)
    ap.vote(row, pid="g1", allow=True, now=1001, eligible=["g1", "g2", "g3"])
    row["action"]["target"] = "lock.back_door"
    with pytest.raises(ap.ApprovalError):
        ap.claim_for_run(row, 1002)
    assert row["status"] == ap.FAILED


def test_quorum_larger_than_approvers_is_refused():
    with pytest.raises(ap.ApprovalError):
        _row({}, required=3, approvers=("g1", "g2"))
