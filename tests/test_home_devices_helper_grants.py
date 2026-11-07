"""Helper grants: a guest's time-bound access to some devices, fail closed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from adk.home.devices import approvals as ap
from adk.home.devices import policy as pol
from adk.home.devices.gate import DeviceGate
from adk.home.devices.ha import HAClient
from adk.home.devices.testing import FakeHomeAssistant, household_states

NOON = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
SITTER = {"pid": "g1", "name": "Bea Sitter", "role": "guest"}
OTHER_GUEST = {"pid": "g2", "name": "Visitor", "role": "guest"}
OWNER = {"pid": "o1", "name": "Dana", "role": "owner"}
GUARDIANS = ["o1"]
IDS = ["light.kitchen", "light.porch", "cover.blinds", "lock.front_door", "camera.porch"]
H = timedelta(hours=1)


def _cfg(**extra):
    return pol.grant_helper(
        pol.validate_config({"exposure": {"guest": ["light.porch"]}, **extra}),
        pid="g1", name="Bea", granted_by="alice", now=NOON,
        not_before=NOON - H, expires_at=NOON + 4 * H,
        entities=["light.kitchen", "lock.front_door", "cover.blinds"])


def _grant(cfg=None, **kw):
    args = {"pid": "g1", "name": "Bea", "granted_by": "alice", "now": NOON,
            "not_before": NOON, "expires_at": NOON + H, "entities": ["light.kitchen"]}
    args.update(kw)
    return pol.grant_helper(cfg or pol.default_config(), **args)


def _decide(cfg, person, eid, action="turn_on", now=NOON, tainted=False):
    return pol.decide(cfg=cfg, person=person, entity_id=eid, action=action, params={},
                      tainted=tainted, now=now,
                      exposed=pol.exposed_to(person, cfg, IDS, now=now),
                      device_class="blind" if eid == "cover.blinds" else None)


# ── the policy ────────────────────────────────────────────────────────────────

def test_active_grant_allows_like_an_adult_on_its_entities_only():
    cfg, grant = _cfg()
    assert pol.grant_status(grant, NOON) == "active"
    assert pol.exposed_to(SITTER, cfg, IDS, now=NOON) == [
        "cover.blinds", "light.kitchen", "light.porch", "lock.front_door"]
    d = _decide(cfg, SITTER, "light.kitchen")
    assert d.verdict == pol.ACT and d.role == "guest" and "helper" in d.reason
    # confirm asks the helper's own yes; guarded still goes to the guardians' quorum
    assert _decide(cfg, SITTER, "cover.blinds", "open_cover").verdict == pol.ASK_ASKER
    d = _decide(cfg, SITTER, "lock.front_door", "unlock")
    assert d.verdict == pol.ASK_GUARDIANS and d.approvers == pol.ANY_GUARDIAN
    assert _decide(cfg, SITTER, "light.kitchen", tainted=True).verdict == pol.ASK_ASKER
    assert _decide(cfg, SITTER, "camera.porch", "summarize").verdict == pol.REFUSE


def test_exposure_guest_is_snapshotted_at_grant_time():
    base = pol.validate_config({"exposure": {"guest": ["light.porch"]}})
    cfg, grant = _grant(base, entities=[], exposure="guest")
    assert grant["entities"] == ["light.porch"] and grant["exposure"] == ""
    assert _decide(cfg, SITTER, "light.porch").verdict == pol.ACT
    # widening the guest list later does NOT widen the signed-off grant
    cfg["exposure"]["guest"] = ["light.porch", "light.kitchen"]
    assert _decide(cfg, SITTER, "light.kitchen").verdict == pol.REFUSE
    with pytest.raises(pol.PolicyError):          # an empty guest list shares nothing
        _grant(entities=[], exposure="guest")


def test_expired_grant_denies():
    cfg, grant = _cfg()
    later = NOON + 4 * H        # expires_at is exclusive
    assert pol.grant_status(grant, later) == "expired"
    d = _decide(cfg, SITTER, "light.kitchen", now=later)
    assert d.verdict == pol.REFUSE and "helper access" in d.reason
    assert pol.exposed_to(SITTER, cfg, IDS, now=later) == ["light.porch"]


def test_not_before_denies():
    cfg, grant = _cfg()
    early = NOON - 2 * H
    assert pol.grant_status(grant, early) == "pending"
    assert _decide(cfg, SITTER, "light.kitchen", now=early).verdict == pol.REFUSE


def test_revoked_grant_denies_and_stays_listed():
    cfg, grant = _cfg()
    cfg, gone = pol.revoke_helper(cfg, grant["grant_id"], revoked_by="alice", now=NOON)
    assert gone["revoked_at"] and gone["revoked_by"] == "alice"
    assert _decide(cfg, SITTER, "light.kitchen").verdict == pol.REFUSE
    assert [r["status"] for r in pol.list_helper_grants(cfg, NOON)] == ["revoked"]
    again = pol.validate_config(cfg)          # survives a save/load round trip
    assert pol.list_helper_grants(again, NOON)[0]["status"] == "revoked"
    with pytest.raises(pol.PolicyError):
        pol.revoke_helper(cfg, "hg_nosuchgrant", revoked_by="alice", now=NOON)


def test_guest_without_grant_is_unchanged():
    cfg, _ = _cfg()
    for c in (cfg, pol.default_config()):
        d = _decide(c, OTHER_GUEST, "light.kitchen")
        assert d.verdict == pol.REFUSE and d.reason == "guests cannot use the home's devices"
    assert pol.exposed_to(OTHER_GUEST, cfg, IDS, now=NOON) == ["light.porch"]
    assert _decide(cfg, OTHER_GUEST, "light.porch").verdict == pol.REFUSE


def test_grant_for_one_pid_never_covers_another_or_an_empty_pid():
    cfg, _ = _cfg()
    nobody = {"pid": "", "name": "", "role": "guest"}
    assert _decide(cfg, nobody, "light.kitchen").verdict == pol.REFUSE
    assert pol.granted_entities(cfg, "", NOON) == set()


def test_hidden_still_wins_over_a_grant():
    cfg, _ = _cfg(hidden=["light.kitchen"])
    assert "light.kitchen" not in pol.exposed_to(SITTER, cfg, IDS, now=NOON)
    assert _decide(cfg, SITTER, "light.kitchen").verdict == pol.REFUSE


def test_not_before_is_at_most_30_days_ahead():
    _grant(not_before=NOON + timedelta(days=30), expires_at=NOON + timedelta(days=31))
    with pytest.raises(pol.PolicyError, match="30 days"):
        _grant(not_before=NOON + timedelta(days=30, seconds=1),
               expires_at=NOON + timedelta(days=31))
    with pytest.raises(pol.PolicyError, match="already ended"):
        _grant(not_before=NOON - 3 * H, expires_at=NOON - H)


def test_overlapping_live_grant_for_the_same_pid_is_refused():
    cfg, first = _grant(expires_at=NOON + 4 * H)
    with pytest.raises(pol.PolicyError, match="revoke it"):
        _grant(cfg, not_before=NOON + 2 * H, expires_at=NOON + 6 * H)
    # back to back is fine; another pid is fine; revoke + grant extends
    cfg2, _ = _grant(cfg, not_before=NOON + 4 * H, expires_at=NOON + 6 * H)
    _grant(cfg, pid="g2", expires_at=NOON + 4 * H)
    cfg3, _ = pol.revoke_helper(cfg, first["grant_id"], revoked_by="alice", now=NOON)
    _grant(cfg3, expires_at=NOON + 8 * H)
    assert len(cfg2["grants"]) == 2


@pytest.mark.parametrize("bad", [
    {"not_before": "2026-10-07T12:00:00", "expires_at": "2026-10-07T13:00:00+00:00"},
    {"not_before": NOON, "expires_at": NOON},
    {"not_before": NOON, "expires_at": NOON + timedelta(days=91)},
    {"not_before": "soon", "expires_at": NOON + H},
    {"entities": []},
    {"exposure": "people"},
    {"entities": ["not an entity"]},
])
def test_malformed_grants_are_refused(bad):
    with pytest.raises(pol.PolicyError):
        _grant(**bad)


def test_unreadable_times_fail_closed():
    assert pol.grant_status({"not_before": "x", "expires_at": "y"}, NOON) == "invalid"
    cfg = pol.default_config()
    cfg["grants"] = [{"grant_id": "hg_abcdef", "pid": "g1", "entities": ["light.kitchen"],
                      "not_before": "garbage", "expires_at": "garbage"}]
    assert _decide(cfg, SITTER, "light.kitchen").verdict == pol.REFUSE


def test_iso_z_and_epoch_times_are_accepted():
    _, g = _grant(not_before="2026-10-07T11:00:00Z", expires_at=NOON.timestamp() + 3600)
    assert pol.grant_status(g, NOON) == "active"


def test_cancel_requested_by_withdraws_only_that_pids_pending_rows():
    store: Dict[str, Any] = {}
    act = {"domain": "lock", "action": "unlock", "target": "lock.front_door", "params": {}}
    kw = dict(action=act, household="h", verdict="guarded", required=1, approvers=["o1"],
              expires_s=900, now=100.0, summary="unlock", policy={})
    mine = ap.create(store, requested_by={"pid": "g1", "role": "guest"}, **kw)
    other = ap.create(store, requested_by={"pid": "a1", "role": "adult"}, **kw)
    gone = ap.cancel_requested_by(store, "g1", now=101.0, reason="revoked")
    assert [r["id"] for r in gone] == [mine["id"]]
    assert mine["status"] == ap.CANCELLED and other["status"] == ap.PENDING
    with pytest.raises(ap.ApprovalError, match="cancelled"):
        ap.vote(mine, pid="o1", allow=True, now=102.0, eligible=["o1"])


# ── the gate: a late yes never runs a request whose grant ended ───────────────

class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def ha():
    with FakeHomeAssistant(household_states()) as fake:
        yield fake


def _gate(ha, clock):
    log: List[Dict[str, Any]] = []
    gate = DeviceGate(HAClient(ha.url, lambda: ha.token), household="hh", clock=clock,
                      receipt=lambda kind, name, args, result, approval: log.append(
                          {"kind": kind, "approval": approval}))
    return gate, log


async def _ask_unlock(gate, cfg, store):
    out = await gate.request(SITTER, cfg, store, domain="lock", action="unlock",
                             target="lock.front_door", tainted=False, guardians=GUARDIANS)
    assert out["status"] == "waiting_for_guardians"
    return out["approval"]["id"]


def _unlocks(ha):
    return [c for c in ha.calls if c.get("service") == "unlock"]


async def test_vote_runs_while_the_grant_is_active(ha):
    gate, _ = _gate(ha, Clock(NOON.timestamp()))
    cfg, _ = _cfg()
    store: Dict[str, Any] = {}
    aid = await _ask_unlock(gate, cfg, store)
    ok = await gate.vote(OWNER, cfg, store, aid, True, guardians=GUARDIANS)
    assert ok["ran"] is True and _unlocks(ha)


async def test_vote_after_revoke_is_refused(ha):
    gate, log = _gate(ha, Clock(NOON.timestamp()))
    cfg, grant = _cfg()
    store: Dict[str, Any] = {}
    aid = await _ask_unlock(gate, cfg, store)
    cfg, _ = pol.revoke_helper(cfg, grant["grant_id"], revoked_by="o1", now=NOON)
    late = await gate.vote(OWNER, cfg, store, aid, True, guardians=GUARDIANS)
    assert "access to that device has ended" in late["error"] and not _unlocks(ha)
    assert ap.get(store, aid)["status"] == ap.CANCELLED
    assert log[-1]["approval"] == "refused:helper-grant-ended"


async def test_vote_after_expiry_is_refused(ha):
    clock = Clock(NOON.timestamp())
    gate, _ = _gate(ha, clock)
    cfg, _ = pol.grant_helper(pol.default_config(), pid="g1", name="Bea", granted_by="o1",
                              now=NOON, not_before=NOON - H, expires_at=NOON + timedelta(seconds=60),
                              entities=["lock.front_door"])
    store: Dict[str, Any] = {}
    aid = await _ask_unlock(gate, cfg, store)
    clock.t += 120          # the grant ended; the request itself has not expired
    late = await gate.vote(OWNER, cfg, store, aid, True, guardians=GUARDIANS)
    assert "error" in late and not _unlocks(ha)
