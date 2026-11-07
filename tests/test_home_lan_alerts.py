"""``adk home devices watch`` / ``known``: one guardian notice per new device, a known
device stays quiet, an offline run queues and the next run flushes, every sent notice is
receipted. The notifier is faked; nothing touches the network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from adk import receipts
from adk.home import cli
from adk.home import lan_alerts as la
from adk.home import lan_inventory as li

GW = {"ip": "192.168.1.1", "mac": "a4:2b:b0:00:00:01", "hostname": None}
TV = {"ip": "192.168.1.23", "mac": "a4:2b:b0:00:00:23", "hostname": "living-room-tv"}
CAM = {"ip": "192.168.1.40", "mac": "a4:2b:b0:00:00:40", "hostname": None}


class FakeNotifier:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.offline = False

    def __call__(self, device: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(device)
        if self.offline:
            return {"error": "could not reach the tool gateway"}
        return {"notice_id": f"n{len(self.calls)}", "sent": 1}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    monkeypatch.setenv("AITHER_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("AITHER_RECEIPTS_PATH", str(tmp_path / "actions.jsonl"))
    monkeypatch.setattr(receipts, "_signing_key", lambda key_path=None, create=True: None)
    monkeypatch.setattr(li, "state_path", lambda root=None: tmp_path / li.STATE_NAME)
    monkeypatch.setattr(li, "ssdp_discover", lambda timeout=2.0: [])
    table: List[List[Dict[str, Any]]] = [[GW]]
    monkeypatch.setattr(li, "read_neighbor_table", lambda platform=None: list(table[0]))
    fake = FakeNotifier()
    monkeypatch.setattr(la, "gateway_notifier", fake)
    return {"tmp": tmp_path, "table": table, "fake": fake}


def _watch(home: Dict[str, Any]) -> Dict[str, Any]:
    return la.watch(use_ssdp=False, receipts_path=home["tmp"] / "actions.jsonl")


def test_baseline_announces_nothing(home: Dict[str, Any]) -> None:
    home["table"][0] = [GW, TV]
    res = _watch(home)
    assert res["first_scan"] is True and res["alerts"]["sent"] == []
    assert home["fake"].calls == []


def test_new_device_is_announced_once_and_receipted(home: Dict[str, Any]) -> None:
    _watch(home)
    home["table"][0] = [GW, TV]
    res = _watch(home)
    (sent,) = res["alerts"]["sent"]
    assert sent["key"] == TV["mac"] and sent["notice_id"] == "n1"
    (call,) = home["fake"].calls
    assert call["ip"] == TV["ip"] and call["hostname"] == "living-room-tv"
    rows = receipts.tail(5, path=home["tmp"] / "actions.jsonl")
    assert [r["name"] for r in rows] == ["home.lan.new_device"]
    assert rows[0]["seq"] == sent["receipt_seq"]
    # the next scans see it as known-to-the-inventory: never a second notice
    _watch(home)
    _watch(home)
    assert len(home["fake"].calls) == 1


def test_announce_dedupes_even_if_the_inventory_forgot(home: Dict[str, Any]) -> None:
    path = home["tmp"] / la.STATE_NAME
    la.announce([dict(TV, new=True)], path=path, receipts_path=home["tmp"] / "a.jsonl")
    out = la.announce([dict(TV, new=True)], path=path, receipts_path=home["tmp"] / "a.jsonl")
    assert out["sent"] == [] and [s["key"] for s in out["skipped"]] == [TV["mac"]]
    assert len(home["fake"].calls) == 1


def test_known_device_is_never_announced(home: Dict[str, Any]) -> None:
    _watch(home)
    la.mark_known("A4-2B-B0-00-00-23", "our TV")
    home["table"][0] = [GW, TV]
    res = _watch(home)
    assert res["alerts"]["sent"] == []
    (quiet,) = res["alerts"]["suppressed"]
    assert quiet["key"] == TV["mac"] and quiet["label"] == "our TV"
    assert home["fake"].calls == []


def test_offline_queues_then_flushes_next_run(home: Dict[str, Any]) -> None:
    _watch(home)
    home["fake"].offline = True
    home["table"][0] = [GW, TV]
    res = _watch(home)
    assert res["alerts"]["sent"] == [] and [q["key"] for q in res["alerts"]["queued"]] == [
        TV["mac"]]
    state = json.loads((home["tmp"] / la.STATE_NAME).read_text(encoding="utf-8"))
    assert [q["key"] for q in state["queue"]] == [TV["mac"]]
    assert not (home["tmp"] / "actions.jsonl").exists()  # nothing sent, nothing receipted

    home["fake"].offline = False
    home["table"][0] = [GW, TV, CAM]  # TV is no longer "new" to the inventory
    res = _watch(home)
    assert sorted(s["key"] for s in res["alerts"]["sent"]) == sorted([TV["mac"], CAM["mac"]])
    state = json.loads((home["tmp"] / la.STATE_NAME).read_text(encoding="utf-8"))
    assert state["queue"] == []


def test_marking_known_drops_it_from_the_queue(home: Dict[str, Any]) -> None:
    _watch(home)
    home["fake"].offline = True
    home["table"][0] = [GW, TV]
    _watch(home)
    got = la.mark_known(TV["mac"], "our TV")
    assert got["dequeued"] == 1
    home["fake"].offline = False
    res = _watch(home)
    assert res["alerts"]["sent"] == [] and len(home["fake"].calls) == 1  # the offline try


def test_notifier_that_raises_queues(home: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
                                     ) -> None:
    def boom(device: Dict[str, Any]) -> Dict[str, Any]:
        raise ConnectionError("down")

    out = la.announce([dict(TV, new=True)], notifier=boom, path=home["tmp"] / la.STATE_NAME,
                      receipts_path=home["tmp"] / "a.jsonl")
    assert out["queued"][0]["error"] == "notifier raised ConnectionError"


def _fleet(n: int) -> List[Dict[str, Any]]:
    return [{"ip": f"192.168.1.{100 + i}", "mac": f"a4:2b:b0:00:01:{i:02x}",
             "hostname": None, "new": True} for i in range(n)]


def test_a_flood_is_five_notices_plus_one_summary(home: Dict[str, Any]) -> None:
    path = home["tmp"] / la.STATE_NAME
    out = la.announce(_fleet(40), path=path, receipts_path=home["tmp"] / "a.jsonl")
    calls = home["fake"].calls
    assert len(calls) == la.MAX_NOTICES_PER_RUN + 1
    assert [c.get("more") for c in calls] == [None] * 5 + [34]
    assert len(out["sent"]) == 40 and sum(1 for s in out["sent"] if s["summary"]) == 35
    assert len(receipts.tail(50, path=home["tmp"] / "a.jsonl")) == 6
    again = la.announce(_fleet(40), path=path, receipts_path=home["tmp"] / "a.jsonl")
    assert again["sent"] == [] and len(home["fake"].calls) == 6  # all 40 count as told


def test_a_failed_summary_queues_every_device_it_covered(home: Dict[str, Any]) -> None:
    def notifier(device: Dict[str, Any]) -> Dict[str, Any]:
        return {"error": "down"} if device.get("more") is not None else {"notice_id": "n"}

    out = la.announce(_fleet(8), notifier=notifier, path=home["tmp"] / la.STATE_NAME,
                      receipts_path=home["tmp"] / "a.jsonl")
    assert len(out["sent"]) == 5 and len(out["queued"]) == 3


def test_gateway_notifier_passes_the_summary_count(monkeypatch: pytest.MonkeyPatch) -> None:
    from adk.home import gateway_hearth

    seen: Dict[str, Any] = {}
    monkeypatch.setattr(gateway_hearth, "call_gateway",
                        lambda name, arguments: seen.update(arguments) or {"notice_id": "x"})
    la.gateway_notifier({"ip": TV["ip"], "mac": TV["mac"], "more": 12})
    assert seen["more"] == 12


def test_a_second_run_refuses_while_the_lock_is_held(home: Dict[str, Any]) -> None:
    path = home["tmp"] / la.STATE_NAME
    with la.run_lock(path):
        with pytest.raises(RuntimeError, match="another"):
            with la.run_lock(path, timeout=0.2):
                pass  # pragma: no cover - never entered
    with la.run_lock(path, timeout=0.2):  # released: free again
        pass


def test_a_crashed_runs_leftover_lock_file_does_not_block(home: Dict[str, Any]) -> None:
    """The OS lock dies with its holder: an old lock FILE alone is no lock, and a long
    run is never "stale" (no time-based takeover)."""
    lock = home["tmp"] / (la.STATE_NAME + ".lock")
    lock.write_text("", encoding="utf-8")
    with la.run_lock(home["tmp"] / la.STATE_NAME, timeout=0.2):
        pass
    assert not hasattr(la, "LOCK_STALE_S")


def test_control_chars_never_reach_a_notice(home: Dict[str, Any]) -> None:
    la.announce([{**TV, "hostname": "tv\x1b]0;pwn\x07", "new": True,
                  "ssdp": {"server": "UPnP‮/1.0"}}],
                path=home["tmp"] / la.STATE_NAME, receipts_path=home["tmp"] / "a.jsonl")
    (call,) = home["fake"].calls
    assert call["hostname"] == "tv]0;pwn" and call["ssdp_server"] == "UPnP/1.0"


def test_alert_state_is_owner_only(home: Dict[str, Any]) -> None:
    import os
    import sys

    la.mark_known(TV["mac"], "tv")
    p = home["tmp"] / la.STATE_NAME
    # no tmp file left behind; the lock file stays (the OS lock lives on it, not its name)
    assert sorted(x.name for x in home["tmp"].iterdir()) == [la.STATE_NAME,
                                                             la.STATE_NAME + ".lock"]
    if sys.platform != "win32":
        assert (os.stat(p).st_mode & 0o777) == 0o600


def test_concurrent_watches_announce_a_device_once(home: Dict[str, Any]) -> None:
    import threading

    path = home["tmp"] / la.STATE_NAME
    errs: List[BaseException] = []

    def go() -> None:
        try:
            la.announce([dict(TV, new=True)], path=path,
                        receipts_path=home["tmp"] / "a.jsonl")
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errs.append(exc)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errs == [] and len(home["fake"].calls) == 1


def test_key_for_rejects_garbage_and_accepts_ip() -> None:
    assert la.key_for("192.168.1.9") == "ip:192.168.1.9"
    assert la.key_for("ip:10.0.0.2") == "ip:10.0.0.2"
    with pytest.raises(ValueError):
        la.key_for("our tv")
    with pytest.raises(ValueError):
        la.key_for("300.1.1.1")


def test_corrupt_state_is_refused(home: Dict[str, Any]) -> None:
    (home["tmp"] / la.STATE_NAME).write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError):
        la.announce([], path=home["tmp"] / la.STATE_NAME)


def test_gateway_notifier_calls_the_hearth_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    from adk.home import gateway_hearth

    seen: Dict[str, Any] = {}

    def call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        seen.update(name=name, arguments=arguments)
        return {"notice_id": "n9"}

    monkeypatch.setattr(gateway_hearth, "call_gateway", call)
    out = la.gateway_notifier({"ip": TV["ip"], "mac": TV["mac"], "hostname": "tv",
                               "vendor": None, "ssdp_server": "Linux UPnP/1.0"})
    assert out == {"notice_id": "n9"} and seen["name"] == la.NOTICE_TOOL
    assert seen["arguments"] == {"ip": TV["ip"], "mac": TV["mac"], "hostname": "tv",
                                 "vendor": "", "detail": "Linux UPnP/1.0"}


def test_cli_watch_and_known(home: Dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["devices", "watch", "--no-ssdp"]) == 0
    capsys.readouterr()
    home["table"][0] = [GW, TV]
    assert cli.main(["devices", "watch", "--no-ssdp", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [s["key"] for s in out["alerts"]["sent"]] == [TV["mac"]]
    assert cli.main(["devices", "known", CAM["mac"], "--label", "doorbell"]) == 0
    assert "doorbell" in capsys.readouterr().out
    home["table"][0] = [GW, TV, CAM]
    assert cli.main(["devices", "watch", "--no-ssdp"]) == 0
    assert "guardians told: 0" in capsys.readouterr().out
    assert cli.main(["devices", "known", "not-a-mac"]) == 2
    assert cli.main(["devices", "known", CAM["mac"], "--forget"]) == 0
    assert "no longer known" in capsys.readouterr().out
