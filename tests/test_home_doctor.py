"""``adk home doctor``: pure checks, one household notice per finding per 24 h, diagnostics
asked of the operator at once, state-changing fixes held on an approval card until a
person approves, and a receipt for every run and action. The notifier, the operator and
the facts are faked; nothing touches the network or the disks."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from adk import receipts
from adk.approval import ApprovalStore
from adk.home import cli
from adk.home import doctor as dr

NOW = 1_800_000_000.0
GIB = 1024 ** 3


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat()


def healthy() -> Dict[str, Any]:
    return {"disks": [{"path": "/", "total": 100 * GIB, "free": 50 * GIB}],
            "smart": [{"device": "/dev/sda", "passed": True}],
            "home_status": {"initialized": True,
                            "harness": {"kind": "aither", "available": True},
                            "model": {"ok": True, "detail": "200"}},
            # long before NOW and before the wall clock (the CLI test runs on real time)
            "lan_inventory": {"aa:bb:cc:dd:ee:01": {"first_seen": "2020-01-01T00:00:00+00:00"}},
            "lan_alerts": {"known": {}, "queue": []},
            "backup": {"configured": True, "overdue": False, "last_status": "ok"},
            "adk_installed": "3.9.0", "adk_latest": "3.9.0"}


class FakeNotifier:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.offline = False

    def __call__(self, notice: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(dict(notice))
        if self.offline:
            return {"error": "could not reach the tool gateway"}
        return {"notice_id": f"n{len(self.calls)}"}


class FakeOperator:
    def __init__(self, status: str = "executed") -> None:
        self.calls: List[tuple] = []
        self.status = status

    def __call__(self, kind: str, node: str, approved: bool,
                 node_id: str = "") -> Dict[str, Any]:
        self.calls.append((kind, node, approved, node_id))
        return {"decisions": [{"status": self.status, "action": kind, "target": node}]}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    monkeypatch.setattr(receipts, "_signing_key", lambda key_path=None, create=True: None)
    return {"tmp": tmp_path, "notifier": FakeNotifier(), "op": FakeOperator(),
            "store": ApprovalStore(tmp_path / "paused.json"),
            "receipts": tmp_path / "actions.jsonl", "state": tmp_path / "doctor.json"}


def _run(env: Dict[str, Any], facts: Dict[str, Any], now: float = NOW,
         **kw: Any) -> Dict[str, Any]:
    return dr.run_once(facts=facts, notifier=env["notifier"], fixer=env["op"],
                       store=env["store"], node="den-pc", path=env["state"],
                       receipts_path=env["receipts"], now=now, **kw)


def _receipts(env: Dict[str, Any]) -> List[Dict[str, Any]]:
    return receipts.tail(100, path=env["receipts"])


# ── pure checks ──────────────────────────────────────────────────────────────

def test_every_record_has_the_contract_shape() -> None:
    for r in dr.run_checks(healthy(), NOW):
        assert set(r) == {"id", "ok", "severity", "finding", "proposed_action",
                          "operator_action_kind", "skipped"}
        assert r["ok"] and r["severity"] == "ok" and not r["skipped"], r
    assert [r["id"] for r in dr.run_checks(healthy(), NOW)] == list(dr.CHECK_IDS)


def test_disk_space_thresholds() -> None:
    low = dr.check_disk_space([{"path": "C:\\", "total": 100 * GIB, "free": 8 * GIB}])
    assert not low["ok"] and low["severity"] == "warn" and "C:\\" in low["finding"]
    crit = dr.check_disk_space([{"path": "/", "total": 100 * GIB, "free": 50 * GIB},
                                {"path": "/data", "total": 100 * GIB, "free": 2 * GIB}])
    assert crit["severity"] == "critical" and "/data" in crit["finding"]
    assert crit["operator_action_kind"] == ""  # nothing the operator can do: advice only
    assert dr.check_disk_space(None)["skipped"]


def test_smart_missing_is_skipped_and_a_failure_is_critical() -> None:
    assert dr.check_disk_health(None)["skipped"]
    assert dr.check_disk_health([{"device": "/dev/sda", "passed": None}])["skipped"]
    bad = dr.check_disk_health([{"device": "/dev/sda", "passed": True},
                                {"device": "/dev/sdb", "passed": False}])
    assert bad["severity"] == "critical" and "/dev/sdb" in bad["finding"]
    assert bad["operator_action_kind"] == "device.diagnostics"


def test_smart_parsers() -> None:
    scan = "/dev/sda -d sat # /dev/sda [SAT], ATA device\n/dev/nvme0 -d nvme # nvme\n"
    assert dr.parse_smart_scan(scan) == [["/dev/sda", "-d", "sat"], ["/dev/nvme0", "-d",
                                                                       "nvme"]]
    assert dr.parse_smart_health(
        "SMART overall-health self-assessment test result: PASSED") is True
    assert dr.parse_smart_health("SMART Health Status: OK") is True
    assert dr.parse_smart_health(
        "SMART overall-health self-assessment test result: FAILED!") is False
    assert dr.parse_smart_health("Unable to detect device type") is None


def test_home_services() -> None:
    assert dr.check_home_services({"initialized": False})["proposed_action"] == \
        "Run: adk home init"
    down = dr.check_home_services({"initialized": True,
                                   "harness": {"kind": "claude", "available": False,
                                               "detail": "not installed"},
                                   "model": {"ok": False, "detail": "unreachable"}})
    assert not down["ok"] and "claude" in down["finding"] and "unreachable" in \
        down["finding"]
    assert down["operator_action_kind"] == "device.diagnostics"
    assert dr.check_home_services(None)["skipped"]


def test_lan_devices_counts_recent_unknown_and_queued() -> None:
    inv = {"aa:01": {"first_seen": _iso(NOW - 3600)},
           "aa:02": {"first_seen": _iso(NOW - 3600)},
           "aa:03": {"first_seen": _iso(NOW - 30 * 86400)}}
    r = dr.check_lan_devices(inv, {"known": {"aa:02": {}}, "queue": [{"key": "x"}]}, NOW)
    assert not r["ok"] and r["finding"].startswith("1 device(s) joined")
    assert "1 new-device notice" in r["finding"]
    assert dr.check_lan_devices(inv, {"known": {"aa:01": {}, "aa:02": {}}}, NOW)["ok"]
    assert dr.check_lan_devices(None, None, NOW)["skipped"]


def test_backup() -> None:
    assert dr.check_backup(None)["skipped"]
    assert not dr.check_backup({"configured": False})["ok"]
    late = dr.check_backup({"configured": True, "overdue": True, "last_ok": None})
    assert not late["ok"] and "never" in late["finding"]
    assert late["operator_action_kind"] == ""


def test_adk_version_is_read_only_and_needs_a_card() -> None:
    behind = dr.check_adk_version("3.8.2", "3.10.0", "pipx upgrade awdk")
    assert not behind["ok"] and behind["operator_action_kind"] == "device.update"
    assert "pipx upgrade awdk" in behind["proposed_action"]
    assert dr.check_adk_version("3.10.0", "3.9.9")["ok"]  # a dev build ahead is current
    assert dr.check_adk_version("3.9.0", None)["skipped"]
    assert dr.check_adk_version("", "3.9.0")["skipped"]


def test_a_check_that_raises_is_skipped_not_fatal() -> None:
    facts = healthy()
    facts["disks"] = [{"path": "/", "total": 10, "free": "lots"}]
    rows = {r["id"]: r for r in dr.run_checks(facts, NOW)}
    assert rows["disk_space"]["skipped"] and rows["adk_version"]["ok"]


def test_unknown_values_are_refused() -> None:
    with pytest.raises(ValueError):
        dr.result("disk_space", True, "fine", "x")
    with pytest.raises(ValueError):
        dr.result("disk_space", False, "warn", "x", operator_action_kind="host.restart")


# ── the loop ─────────────────────────────────────────────────────────────────

def test_healthy_run_tells_nobody_and_writes_a_run_receipt(env: Dict[str, Any]) -> None:
    res = _run(env, healthy())
    assert res["failing"] == [] and env["notifier"].calls == [] and env["op"].calls == []
    (row,) = _receipts(env)
    assert row["name"] == "home.doctor.run" and row["kind"] == "doctor"


def test_one_notice_per_finding_per_day(env: Dict[str, Any]) -> None:
    facts = healthy()
    facts["disks"] = [{"path": "/", "total": 100 * GIB, "free": 2 * GIB}]
    _run(env, facts)
    _run(env, facts, now=NOW + 3600)
    assert env["notifier"].calls == [{"check": "disk_space", "severity": "critical",
                                      "host": "den-pc"}]
    _run(env, facts, now=NOW + dr.RENOTIFY_S + 1)
    assert len(env["notifier"].calls) == 2
    names = [r["name"] for r in _receipts(env)]
    assert names.count("home.doctor.notice") == 2 and names.count("home.doctor.run") == 3


def test_an_offline_notice_is_retried_next_run(env: Dict[str, Any]) -> None:
    facts = healthy()
    facts["backup"] = {"configured": True, "overdue": True, "last_ok": NOW - 9 * 86400}
    env["notifier"].offline = True
    res = _run(env, facts)
    assert res["notices"][0]["sent"] is False
    env["notifier"].offline = False
    res = _run(env, facts, now=NOW + 60)
    assert res["notices"][0]["sent"] is True and len(env["notifier"].calls) == 2


def test_diagnostics_run_at_once_but_only_once_a_day(env: Dict[str, Any]) -> None:
    facts = healthy()
    facts["smart"] = [{"device": "/dev/sdb", "passed": False}]
    res = _run(env, facts)
    assert env["op"].calls == [("device.diagnostics", "den-pc", False, "")]
    assert res["cards"] == []  # a diagnostic changes nothing: no card
    _run(env, facts, now=NOW + 600)
    assert len(env["op"].calls) == 1
    act = [r for r in _receipts(env) if r["name"] == "home.doctor.device.diagnostics"]
    assert len(act) == 1 and act[0]["approval"] == "auto:diagnostics"


def test_a_state_change_waits_on_a_card_and_runs_only_on_approval(env: Dict[str, Any]
                                                                  ) -> None:
    facts = healthy()
    facts["adk_latest"] = "3.10.0"
    res = _run(env, facts)
    assert env["op"].calls == []  # NOTHING ran: an update changes state
    (card,) = res["cards"]
    assert card["kind"] == "device.update"
    _run(env, facts, now=NOW + 60)  # a second run does not stack a second card
    cards = dr.list_cards(env["store"])
    assert [c["card"] for c in cards] == [card["card"]] and cards[0]["node"] == "den-pc"
    got = dr.decide_card(card["card"], True, fixer=env["op"], store=env["store"],
                         receipts_path=env["receipts"], node_id="nd_den")
    assert env["op"].calls == [("device.update", "den-pc", True, "nd_den")]
    assert got["ran"]["decisions"][0]["status"] == "executed"
    assert dr.list_cards(env["store"]) == []
    with pytest.raises(KeyError):  # an answered card cannot run twice
        dr.decide_card(card["card"], True, fixer=env["op"], store=env["store"],
                       receipts_path=env["receipts"])
    assert len(env["op"].calls) == 1
    approvals = [r["approval"] for r in _receipts(env) if r["kind"] == "action"]
    assert approvals == [f"owner:yes:{card['card']}"]


def test_a_denied_card_runs_nothing(env: Dict[str, Any]) -> None:
    facts = healthy()
    facts["adk_latest"] = "3.10.0"
    (card,) = _run(env, facts)["cards"]
    got = dr.decide_card(card["card"], False, fixer=env["op"], store=env["store"],
                         receipts_path=env["receipts"])
    assert got["ran"] is None and env["op"].calls == []
    assert [r["approval"] for r in _receipts(env) if r["name"] == "home.doctor.card"][-1] \
        == f"owner:no:{card['card']}"


def test_a_card_closes_when_its_check_passes(env: Dict[str, Any]) -> None:
    facts = healthy()
    facts["adk_latest"] = "3.10.0"
    (card,) = _run(env, facts)["cards"]
    res = _run(env, healthy(), now=NOW + 60)
    assert res["closed_cards"] == [card["card"]] and dr.list_cards(env["store"]) == []


def test_check_only_tells_and_asks_nobody(env: Dict[str, Any]) -> None:
    facts = healthy()
    facts["smart"] = [{"device": "/dev/sdb", "passed": False}]
    res = _run(env, facts, notify=False)
    assert res["failing"] == ["disk_health"]
    assert env["notifier"].calls == [] and env["op"].calls == []


def test_watch_loops_with_the_interval_floor(env: Dict[str, Any]) -> None:
    slept: List[float] = []
    seen: List[Dict[str, Any]] = []
    n = dr.watch(interval=5, rounds=3, sleep=slept.append, on_round=seen.append,
                 facts=healthy(), notifier=env["notifier"], fixer=env["op"],
                 store=env["store"], node="den-pc", path=env["state"],
                 receipts_path=env["receipts"])
    assert n == 3 and slept == [dr.MIN_INTERVAL_S] * 2 and len(seen) == 3


def test_node_name_is_reduced() -> None:
    assert dr.node_name("Den PC/../evil") == "denpc..evil"


def _gateway(monkeypatch: pytest.MonkeyPatch, decisions: List[Dict[str, Any]]
             ) -> List[tuple]:
    from adk.home import gateway_hearth

    calls: List[tuple] = []

    def gw(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        calls.append((name, dict(args)))
        assert name == dr.OPERATOR_REQUEST_TOOL  # the gateway can only ASK
        return {"decisions": [dict(d) for d in decisions]}

    monkeypatch.setattr(gateway_hearth, "call_gateway", gw)
    return calls


OURS = {"status": "pending_approval", "approval_id": "ap1", "action": "device.update",
        "target": "nd_den", "audit_id": "au1"}


def test_gateway_fixer_answers_only_our_card_with_the_persons_approver(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _gateway(monkeypatch, [OURS])
    answered: List[str] = []

    def approver(aid: str) -> Dict[str, Any]:
        answered.append(aid)
        return {"decision": {"status": "executed"}}

    dr.gateway_fixer("device.update", "den-pc", False, "nd_den", approver=approver)
    assert answered == []  # not approved by a person: the card waits
    got = dr.gateway_fixer("device.update", "den-pc", True, "nd_den", approver=approver)
    assert answered == ["ap1"] and got["decisions"][0]["answered"] == {"status": "executed"}
    assert [c[0] for c in calls] == [dr.OPERATOR_REQUEST_TOOL] * 2


@pytest.mark.parametrize("change,node_id", [
    ({"action": "device.lend_on"}, "nd_den"),     # another action
    ({"target": "nd_other"}, "nd_den"),           # another device
    ({"audit_id": ""}, "nd_den"),                 # not raised by this request
    ({}, ""),                                     # this device's id is unknown
])
def test_gateway_fixer_leaves_any_other_card_for_veil(monkeypatch: pytest.MonkeyPatch,
                                                      change: Dict[str, Any],
                                                      node_id: str) -> None:
    _gateway(monkeypatch, [{**OURS, **change}])
    answered: List[str] = []
    got = dr.gateway_fixer("device.update", "den-pc", True, node_id,
                           approver=lambda aid: answered.append(aid) or {})
    assert answered == [] and got["decisions"][0]["left_for_veil"]


def test_operator_approver_needs_the_persons_own_sign_in(monkeypatch: pytest.MonkeyPatch
                                                         ) -> None:
    from adk.home import connector_tools, gateway_hearth

    monkeypatch.setattr(connector_tools, "_saved_bearer", lambda: "")
    monkeypatch.setattr(gateway_hearth, "call_gateway",
                        lambda *a, **k: pytest.fail("never through the gateway"))
    assert "sign in" in dr.operator_approver(CARD_ID)["error"]
    assert dr.operator_approver("../x")["error"] == "not an operator card id"


CARD_ID = "opr_0123456789abcdef"


def test_the_default_approval_url_is_the_public_pass_through(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from adk.home import serve

    for k in ("AITHER_OPERATOR_URL", "AITHER_OPERATOR_GENESIS_DIRECT", *serve.TUTOR_URL_ENV):
        monkeypatch.delenv(k, raising=False)
    assert dr.operator_approval_url(CARD_ID) == \
        f"https://api.aitherium.com/api/operator/approvals/{CARD_ID}"
    monkeypatch.setenv("AITHER_OPERATOR_URL", "https://home.example/")
    assert dr.operator_approval_url(CARD_ID) == \
        f"https://home.example/api/operator/approvals/{CARD_ID}"
    # /api/v1/operator only on the explicit in-network flag, never guessed from the URL
    monkeypatch.setenv("AITHER_OPERATOR_URL", "https://aitheros-genesis:8001")
    assert "/api/operator/" in dr.operator_approval_url(CARD_ID)
    monkeypatch.setenv("AITHER_OPERATOR_GENESIS_DIRECT", "1")
    assert dr.operator_approval_url(CARD_ID) == \
        f"https://aitheros-genesis:8001/api/v1/operator/approvals/{CARD_ID}"


class _Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code, self._body = status, body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _post_returns(monkeypatch: pytest.MonkeyPatch, resp: _Resp) -> List[tuple]:
    import httpx

    from adk.home import connector_tools

    sent: List[tuple] = []

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def post(self, url: str, **kw: Any) -> _Resp:
            sent.append((url, kw))
            return resp

    monkeypatch.setattr(connector_tools, "_saved_bearer", lambda: "b-person")
    monkeypatch.setattr(httpx, "Client", _Client)
    for k in ("AITHER_OPERATOR_URL", "AITHER_OPERATOR_GENESIS_DIRECT"):
        monkeypatch.delenv(k, raising=False)
    return sent


@pytest.mark.parametrize("resp", [_Resp(404, {"error": "not_found"}),
                                  _Resp(502, ValueError("html")),
                                  _Resp(200, {"ok": True})])  # 2xx without a decision
def test_an_undelivered_approval_is_a_loud_failure(monkeypatch: pytest.MonkeyPatch,
                                                   resp: _Resp) -> None:
    sent = _post_returns(monkeypatch, resp)
    got = dr.operator_approver(CARD_ID)
    assert got["error"].startswith(f"approval not delivered: {resp.status_code}")
    (url, kw), = sent
    assert url.endswith(f"/api/operator/approvals/{CARD_ID}")
    assert kw["headers"]["Authorization"] == "Bearer b-person" and kw["json"] == {
        "allow": True}
    # ... and the fixer reports it as a failure, not as an answered card
    _gateway(monkeypatch, [{**OURS, "approval_id": CARD_ID}])
    out = dr.gateway_fixer("device.update", "den-pc", True, "nd_den")
    assert out["error"].startswith("approval not delivered")
    assert "answered" not in out["decisions"][0]


def test_a_delivered_approval_returns_the_operators_decision(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _post_returns(monkeypatch, _Resp(200, {"decision": {"status": "executed"}}))
    assert dr.operator_approver(CARD_ID) == {"decision": {"status": "executed"}}


def test_smart_scan_types_are_plain_or_dropped() -> None:
    scan = ("/dev/sda -d sat;rm # x\n/dev/sdb -d megaraid,0 # y\n"
            "--evil -d sat\n/dev/sdc\n")
    assert dr.parse_smart_scan(scan) == [["/dev/sda"], ["/dev/sdb", "-d", "megaraid,0"],
                                         ["/dev/sdc"]]


def test_cli_doctor_exit_codes(env: Dict[str, Any], monkeypatch: pytest.MonkeyPatch,
                               capsys: pytest.CaptureFixture) -> None:
    from adk.home import serve

    monkeypatch.setattr(serve, "receipts_path", lambda: env["receipts"])
    monkeypatch.setattr(dr, "gather_facts", lambda online=True: healthy())
    monkeypatch.setattr(dr, "state_path", lambda root=None: env["state"])
    monkeypatch.setattr(dr, "_store", lambda store=None: env["store"])
    args = argparse.Namespace(doctor_command=None, json=True, node="den-pc", watch=False,
                              interval=60.0, check_only=True)
    assert cli.cmd_doctor(args) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["failing"] == []
    args = argparse.Namespace(doctor_command="approve", card="doc_nope", json=False)
    assert cli.cmd_doctor(args) == cli.EXIT_SETUP
