"""``adk home updates``: pure parsers on captured samples (apt, softwareupdate, the
Windows Update script, HA update entities), the weekly window, the allowlisted
upgrade, and the apply paths end to end -- a fake Home Assistant on a real port, a
fake self_update, a fake command runner and a faked approval notifier. Nothing here
runs a real package manager or touches the network beyond 127.0.0.1."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from adk import receipts
from adk.home import cli as home_cli
from adk.home import updates as up
from adk.home import updates_cli as ucli
from adk.home.devices import notify
from adk.home.devices import policy as pol
from adk.home.devices.ha import HAClient
from adk.home.devices.testing import FakeHomeAssistant, state

# ── captured samples ─────────────────────────────────────────────────────────

APT_SAMPLE = """\
Listing... Done
libssl3/jammy-updates,jammy-security 3.0.2-0ubuntu1.15 amd64 [upgradable from: 3.0.2-0ubuntu1.14]
openssl/jammy-updates,jammy-security 3.0.2-0ubuntu1.15 amd64 [upgradable from: 3.0.2-0ubuntu1.14]
tzdata/jammy-updates 2024a-0ubuntu0.22.04 all [upgradable from: 2023c-0ubuntu0.22.04.2]
"""
APT_WARNING = "\nWARNING: apt does not have a stable CLI interface. Use with caution in scripts.\n"

SU_SAMPLE = """\
Software Update Tool

Finding available software
Software Update found the following new or updated software:
* Label: macOS Sonoma 14.2.1-23C71
\tTitle: macOS Sonoma 14.2.1, Version: 14.2.1, Size: 1253347KiB, Recommended: YES, Action: restart,
* Label: Safari17.2.1SonomaAuto-17.2.1
\tTitle: Safari, Version: 17.2.1, Size: 158976KiB, Recommended: YES,
"""
SU_NONE_STDERR = "No new software available.\n"

WIN_COM = json.dumps({"source": "com", "updates": [
    {"title": "2024-01 Cumulative Update for Windows 11 Version 23H2 (KB5034123)",
     "kb": "5034123", "severity": "Critical", "reboot": True},
    {"title": "Security Intelligence Update for Microsoft Defender Antivirus (KB2267602)",
     "kb": "2267602", "severity": "", "reboot": False}]})
WIN_COM_ONE = ('{"source":"com","updates":{"title":"Update X","kb":"1",'
               '"severity":"","reboot":false}}')
WIN_HOTFIX = json.dumps({"source": "hotfix", "error": "Exception from HRESULT: 0x80072EE2",
                         "last_installed": "2024-01-10", "last_kb": "KB5034123"})

HA_STATES = [
    state("update.home_assistant_core_update", "on", name="Home Assistant Core Update",
          installed_version="2024.1.0", latest_version="2024.1.3", title="Home Assistant Core",
          in_progress=False, supported_features=1 | 2 | 4 | 8 | 16,
          release_url="https://www.home-assistant.io/blog/"),
    state("update.hallway_bulb_firmware", "on", name="Hallway bulb firmware",
          device_class="firmware", installed_version="1.2", latest_version="1.4",
          in_progress=False, supported_features=1 | 4),
    state("update.zigbee_hub", "on", name="Zigbee hub", installed_version="7",
          latest_version="8", supported_features=0),
    state("update.mqtt_addon", "off", name="Mosquitto", installed_version="6.4.0",
          latest_version="6.4.0", supported_features=1 | 8),
    state("update.skipped_thing", "on", name="Skipped", installed_version="1",
          latest_version="2", skipped_version="2", supported_features=1),
    state("update.offline", "unavailable", name="Offline device"),
    state("light.kitchen", "on", name="Kitchen"),
]


# ── parsers ──────────────────────────────────────────────────────────────────

def test_apt_parser_reads_packages_and_security() -> None:
    rows = up.parse_apt_upgradable(APT_SAMPLE + APT_WARNING)
    assert [r["name"] for r in rows] == ["libssl3", "openssl", "tzdata"]
    assert rows[0]["version"] == "3.0.2-0ubuntu1.15"
    assert rows[0]["from_version"] == "3.0.2-0ubuntu1.14"
    assert [r["security"] for r in rows] == [True, True, False]
    assert up.parse_apt_upgradable("Listing... Done\n") == []


def test_softwareupdate_parser() -> None:
    rows = up.parse_softwareupdate(SU_SAMPLE)
    assert [r["label"] for r in rows] == ["macOS Sonoma 14.2.1-23C71",
                                         "Safari17.2.1SonomaAuto-17.2.1"]
    assert rows[0]["title"] == "macOS Sonoma 14.2.1" and rows[0]["version"] == "14.2.1"
    assert rows[0]["restart"] is True and rows[1]["restart"] is False
    assert rows[1]["recommended"] is True
    assert up.parse_softwareupdate("Software Update Tool\n\n" + SU_NONE_STDERR) == []


def test_windows_parser_com_hotfix_and_garbage() -> None:
    com = up.parse_windows_updates(WIN_COM)
    assert com["source"] == "com" and len(com["updates"]) == 2
    assert com["updates"][0]["reboot"] is True and com["updates"][0]["kb"] == "5034123"
    assert len(up.parse_windows_updates(WIN_COM_ONE)["updates"]) == 1
    old = up.parse_windows_updates(WIN_HOTFIX, today=date(2024, 3, 1))
    assert old["source"] == "hotfix" and old["stale"] is True and old["age_days"] == 51
    fresh = up.parse_windows_updates(WIN_HOTFIX, today=date(2024, 1, 20))
    assert fresh["stale"] is False
    never = up.parse_windows_updates('{"source":"hotfix","error":"x"}')
    assert never["stale"] is True and never["age_days"] is None
    with pytest.raises(ValueError):
        up.parse_windows_updates("")
    with pytest.raises(ValueError):
        up.parse_windows_updates('{"source":"other"}')


def test_ha_update_entity_parser() -> None:
    ents = {e["entity_id"]: e for e in up.parse_ha_updates(HA_STATES)}
    assert "light.kitchen" not in ents and len(ents) == 6
    core = ents["update.home_assistant_core_update"]
    assert core["pending"] and core["can_install"] and core["backup"]
    assert (core["installed_version"], core["latest_version"]) == ("2024.1.0", "2024.1.3")
    bulb = ents["update.hallway_bulb_firmware"]
    assert bulb["can_install"] and not bulb["backup"]
    assert ents["update.zigbee_hub"]["can_install"] is False
    assert ents["update.mqtt_addon"]["pending"] is False
    assert ents["update.skipped_thing"]["skipped"] is True
    assert ents["update.offline"]["pending"] is False


def test_detect_platform() -> None:
    assert up.detect_platform("win32") == "windows"
    assert up.detect_platform("darwin") == "macos"
    assert up.detect_platform("linux", 'ID=ubuntu\nID_LIKE=debian\n') == "debian"
    assert up.detect_platform("linux", 'ID=raspbian\nID_LIKE="debian"\n') == "debian"
    assert up.detect_platform("linux", "ID=fedora\n") == "unsupported"
    assert up.detect_platform("freebsd14", "") == "unsupported"


# ── host check through a fake runner ─────────────────────────────────────────

class Runner:
    def __init__(self, answers: Dict[str, Tuple[int, str, str]]):
        self.answers = answers
        self.calls: List[Tuple[Tuple[str, ...], float]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> Tuple[int, str, str]:
        self.calls.append((tuple(argv), timeout))
        for key, ans in self.answers.items():
            if key in argv[0] or key in " ".join(argv[1:3]):
                return ans
        return 127, "", "not found"


def test_check_host_each_platform() -> None:
    r = Runner({"apt": (0, APT_SAMPLE, APT_WARNING)})
    deb = up.check_host("debian", r)
    assert deb["ok"] and deb["count"] == 3 and "2 security" in deb["note"]
    assert r.calls[0][0] == up.APT_LIST
    mac = up.check_host("macos", Runner({"softwareupdate": (0, SU_SAMPLE, "")}))
    assert mac["count"] == 2 and mac["note"] == "restart needed"
    none = up.check_host("macos", Runner({"softwareupdate": (0, "", SU_NONE_STDERR)}))
    assert none["ok"] and none["count"] == 0
    rw = Runner({"powershell": (0, WIN_COM, "")})
    win = up.check_host("windows", rw)
    assert win["count"] == 2 and rw.calls[0][0][0].endswith("powershell.exe")
    assert Path(rw.calls[0][0][0]).is_absolute() or rw.calls[0][0][0].startswith("C:")
    stale = up.check_host("windows", Runner({"powershell": (0, WIN_HOTFIX, "")}),
                          today=date(2024, 6, 1))
    assert stale["ok"] and stale["count"] == 1 and "older than" in stale["note"]


def test_check_host_failure_is_cannot_judge_never_zero() -> None:
    bad = up.check_host("debian", Runner({"apt": (100, "", "E: broken")}))
    assert bad["ok"] is False and bad["count"] is None and "broken" in bad["error"]
    assert up.check_host("windows", Runner({}))["count"] is None
    assert up.check_host("unsupported", Runner({}))["ok"] is False


def test_every_fixed_command_is_absolute() -> None:
    for argv in (up.APT_LIST, up.SOFTWAREUPDATE_LIST, up.APT_UPGRADE, up.SUDO_PREFIX):
        assert argv[0].startswith("/")
    assert PureWindowsPath(up.windows_argv()[0]).is_absolute()


# ── schedule ─────────────────────────────────────────────────────────────────

TZ = timezone(timedelta(hours=-5))


def test_window_and_due() -> None:
    s = up.validate_schedule(up.DEFAULT_SCHEDULE)
    assert (s["weekday"], s["at"]) == (6, "03:00")
    sun_0400 = datetime(2026, 11, 8, 4, 0, tzinfo=TZ)        # a Sunday
    assert up.last_window(s, sun_0400) == datetime(2026, 11, 8, 3, 0, tzinfo=TZ)
    sun_0200 = datetime(2026, 11, 8, 2, 0, tzinfo=TZ)
    assert up.last_window(s, sun_0200) == datetime(2026, 11, 1, 3, 0, tzinfo=TZ)
    assert up.next_window(s, sun_0400) == datetime(2026, 11, 15, 3, 0, tzinfo=TZ)
    assert up.is_due(s, sun_0400, None)
    assert not up.is_due(s, sun_0400, "2026-11-08T03:05:00-05:00")
    assert up.is_due(s, sun_0400, "2026-11-07T23:00:00-05:00")
    wed = datetime(2026, 11, 11, 12, 0, tzinfo=TZ)            # machine was off on Sunday
    assert up.is_due(s, wed, "2026-11-02T03:00:00-05:00")
    assert not up.is_due({**s, "enabled": False}, wed, None)


def test_schedule_parsing() -> None:
    assert up.parse_weekday("Saturday") == 5 and up.parse_weekday("0") == 0
    assert up.parse_at("4:30") == (4, 30)
    for bad in ("someday", "su"):
        with pytest.raises(ValueError):
            up.parse_weekday(bad)
    for bad in ("25:00", "3pm", "03:60"):
        with pytest.raises(ValueError):
            up.parse_at(bad)


# ── the allowlisted upgrade ──────────────────────────────────────────────────

def test_update_entities_are_guarded() -> None:
    assert pol.entity_class("update.hallway_bulb_firmware", "firmware") == pol.GUARDED
    cfg = pol.default_config()
    now = datetime.now(timezone.utc)
    adult = {"pid": "a1", "name": "Sam", "role": "adult"}
    d = pol.decide(cfg=cfg, person=adult, entity_id="update.x", action="install", params={},
                   tainted=False, now=now, exposed=["update.x"])
    assert d.verdict == pol.ASK_GUARDIANS
    owner = {"pid": "o", "name": "Dana", "role": "owner"}
    d = pol.decide(cfg=cfg, person=owner, entity_id="update.x", action="install", params={},
                   tainted=False, now=now, exposed=["update.x"])
    assert d.verdict == pol.ASK_GUARDIANS


# ── end to end ───────────────────────────────────────────────────────────────

class FakeSelfUpdate:
    def __init__(self, available: bool = True, validated: bool = True, auto: bool = False):
        self.available, self.validated, self.auto = available, validated, auto
        self.applied = 0

    def check(self, probe: Any = None) -> Dict[str, Any]:
        return {"running_from": "/old", "installed_at": "/new" if self.available else "/old",
                "update_available": self.available, "running_code_missing": False,
                "check_error": "", "auto_restart": self.auto}

    def validation_of(self, root: Optional[Path], name: str = "") -> Tuple[bool, str]:
        return (True, "validated abc") if self.validated else (False, "not validated")

    def request_apply(self) -> List[str]:
        self.applied += 1
        return ["adk-daemon"]

    def auto_enabled(self) -> bool:
        return self.auto


class UpgradeRunner:
    """Fake :func:`up.run_no_kill`: records argv + env, answers per apt verb."""

    def __init__(self, answers: Optional[Dict[str, Dict[str, Any]]] = None):
        self.answers = answers or {}
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, argv: Sequence[str], timeout: float,
                 env: Dict[str, str]) -> Dict[str, Any]:
        self.calls.append({"argv": tuple(argv), "timeout": timeout, "env": dict(env)})
        verb = "update" if argv[-1] == "update" else "upgrade"
        return dict(self.answers.get(verb) or {"state": "exited", "exit": 0, "out": "ok",
                                               "err": ""})


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AITHER_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("AITHER_RECEIPTS_PATH", str(tmp_path / "actions.jsonl"))
    monkeypatch.setattr(receipts, "_signing_key", lambda key_path=None, create=True: None)
    seen: List[Dict[str, Any]] = []
    notify.set_approval_notifier(seen.append)
    with FakeHomeAssistant(copy.deepcopy(HA_STATES)) as ha:
        yield {"tmp": tmp_path, "ha": ha, "notes": seen}
    notify.set_approval_notifier(None)


def make_home(env: Dict[str, Any], *, platform: str = "debian", su: Any = None,
              runner: Optional[Runner] = None, now_local: Optional[datetime] = None,
              ha: bool = True, upgrader: Any = None) -> ucli.Home:
    client = HAClient(env["ha"].url, lambda: env["ha"].token) if ha else None
    r = runner or Runner({"apt": (0, APT_SAMPLE, ""),
                          "/usr/bin/apt-get": (0, "3 upgraded", "")})
    when = now_local or datetime(2026, 11, 8, 4, 0, tzinfo=TZ)
    return ucli.Home(env["tmp"], client=client, su=su or FakeSelfUpdate(), runner=r,
                     platform=platform, clock=Clock(), now_local=lambda: when, euid=0,
                     upgrader=upgrader if upgrader is not None else UpgradeRunner())


def ns(cmd: str, **kw: Any) -> argparse.Namespace:
    base = {"updates_command": cmd, "no_host": False, "no_ha": False, "json": True,
            "approve": "", "deny": "", "day": "", "at": "", "off": False, "on": False,
            "run_due": False}
    base.update(kw)
    return argparse.Namespace(**base)


def run(args: argparse.Namespace, home: ucli.Home, capsys: Any) -> Tuple[int, Any]:
    code = asyncio.run(ucli.run(args, home))
    out = capsys.readouterr().out
    try:
        return code, json.loads(out)
    except ValueError:
        return code, out


def rows(env: Dict[str, Any]) -> List[Dict[str, Any]]:
    return receipts.tail(100, path=env["tmp"] / "actions.jsonl")


def test_check_is_read_only(env: Dict[str, Any], capsys: Any) -> None:
    su = FakeSelfUpdate()
    home = make_home(env, su=su)
    code, out = run(ns("check"), home, capsys)
    assert code == 0
    assert out["adk"]["pending"] and out["adk"]["adoptable"]
    assert out["host"]["count"] == 3
    pending = [e["entity_id"] for e in out["ha"]["pending"]]
    assert pending == ["update.hallway_bulb_firmware", "update.home_assistant_core_update",
                       "update.zigbee_hub"]
    assert su.applied == 0
    assert not [c for c in env["ha"].calls if c.get("method") != "GET"]
    assert not (env["tmp"] / "actions.jsonl").exists()


def test_apply_adk_now_ha_and_host_become_cards(env: Dict[str, Any], capsys: Any) -> None:
    su = FakeSelfUpdate()
    runner = Runner({"apt": (0, APT_SAMPLE, ""), "/usr/bin/apt-get": (0, "done", "")})
    home = make_home(env, su=su, runner=runner)
    code, out = run(ns("apply"), home, capsys)
    assert code == 0
    acts = out["actions"]
    assert acts["adk"]["status"] == "requested" and su.applied == 1
    ha = {e["entity_id"]: e for e in acts["ha"]}
    assert ha["update.home_assistant_core_update"]["status"] == "waiting_for_guardians"
    assert ha["update.zigbee_hub"]["status"] == "manual"
    assert acts["host"]["status"] == "waiting_for_guardians"
    # Nothing was installed: no service call to HA, no apt-get.
    assert not [c for c in env["ha"].calls if c.get("service")]
    assert all(c[0] != up.APT_UPGRADE for c in runner.calls)
    kinds = [(r["kind"], r["name"]) for r in rows(env)]
    assert ("update_apply", "home.updates.adk") in kinds
    assert ("update_request", "home.updates.host") in kinds
    assert kinds.count(("device_request", "home_act")) == 2
    assert len(env["notes"]) == 3
    # A second apply does not raise the same cards twice.
    _, again = run(ns("apply"), home, capsys)
    assert again["actions"]["host"]["status"] == "already_waiting"
    assert {e["status"] for e in again["actions"]["ha"]} == {"already_waiting", "manual"}


def test_unvalidated_adk_is_refused_and_receipted(env: Dict[str, Any], capsys: Any) -> None:
    su = FakeSelfUpdate(validated=False)
    _, out = run(ns("apply", no_host=True, no_ha=True), make_home(env, su=su), capsys)
    assert out["actions"]["adk"]["status"] == "refused" and su.applied == 0
    assert rows(env)[-1]["kind"] == "update_refused"


def test_approving_ha_card_installs_through_the_gate(env: Dict[str, Any], capsys: Any) -> None:
    home = make_home(env)
    _, out = run(ns("apply", no_host=True), home, capsys)
    card = next(e for e in out["actions"]["ha"]
                if e["entity_id"] == "update.home_assistant_core_update")["approval"]["id"]
    code, res = run(ns("apply", approve=card), home, capsys)
    assert code == 0 and res["ran"] is True
    calls = [c for c in env["ha"].calls if c.get("service")]
    assert calls == [{"path": "rest", "domain": "update", "service": "install",
                      "body": {"entity_id": "update.home_assistant_core_update",
                               "backup": True, "version": "2024.1.3"}}]
    assert rows(env)[-1]["kind"] == "device_action"
    # A second answer to the same card runs nothing.
    code, _ = run(ns("apply", approve=card), home, capsys)
    assert code == 2 and len([c for c in env["ha"].calls if c.get("service")]) == 1


def test_host_card_on_debian_runs_only_the_allowlisted_argv(env: Dict[str, Any],
                                                            capsys: Any) -> None:
    ur = UpgradeRunner()
    home = make_home(env, upgrader=ur)
    _, out = run(ns("apply", no_ha=True), home, capsys)
    card = out["actions"]["host"]["approval"]["id"]
    assert "apt-get -q update" in out["actions"]["host"]["summary"]
    assert ur.calls == []                              # raising the card ran nothing
    code, res = run(ns("apply", approve=card), home, capsys)
    assert code == 0 and res["ran"] is True
    assert [c["argv"] for c in ur.calls] == [up.APT_UPDATE, up.APT_UPGRADE]
    last = rows(env)[-1]
    assert (last["kind"], last["name"]) == ("update_apply", "home.updates.host")


def test_host_card_on_windows_only_says_what_to_click(env: Dict[str, Any],
                                                      capsys: Any) -> None:
    runner = Runner({"powershell": (0, WIN_COM, "")})
    home = make_home(env, platform="windows", runner=runner, ha=False)
    _, out = run(ns("apply"), home, capsys)
    card = out["actions"]["host"]["approval"]["id"]
    code, res = run(ns("apply", approve=card), home, capsys)
    assert code == 0 and res["ran"] is False
    assert "Windows Update" in res["result"]["instructions"]
    assert len(runner.calls) == 1                     # the read-only search only


def test_denied_host_card_runs_nothing(env: Dict[str, Any], capsys: Any) -> None:
    runner = Runner({"apt": (0, APT_SAMPLE, ""), "/usr/bin/apt-get": (0, "", "")})
    home = make_home(env, runner=runner, ha=False)
    _, out = run(ns("apply"), home, capsys)
    card = out["actions"]["host"]["approval"]["id"]
    code, res = run(ns("apply", deny=card), home, capsys)
    assert code == 0 and res["ran"] is False and res["approval"]["status"] == "denied"
    assert all(c[0] != up.APT_UPGRADE for c in runner.calls)


def test_schedule_set_and_run_due(env: Dict[str, Any], capsys: Any) -> None:
    su = FakeSelfUpdate(auto=False)
    runner = Runner({"apt": (0, APT_SAMPLE, ""), "/usr/bin/apt-get": (0, "", "")})
    home = make_home(env, su=su, runner=runner)
    code, out = run(ns("schedule"), home, capsys)
    assert code == 0 and out["schedule"] == {"enabled": True, "weekday": 6, "at": "03:00"}
    assert not up.state_path(env["tmp"]).exists()
    code, out = run(ns("schedule", day="sat", at="02:30"), home, capsys)
    assert out["schedule"]["weekday"] == 5 and out["schedule"]["at"] == "02:30"
    code, out = run(ns("schedule", run_due=True), home, capsys)
    assert code == 0 and out["ran"] is True
    assert out["actions"]["adk"]["status"] == "waiting_for_guardians"   # not opted in
    assert out["actions"]["host"]["status"] == "waiting_for_guardians"
    assert su.applied == 0
    assert all(c[0] != up.APT_UPGRADE for c in runner.calls)
    assert not [c for c in env["ha"].calls if c.get("service")]
    _, out = run(ns("schedule", run_due=True), home, capsys)
    assert out["ran"] is False


def test_run_due_respects_adk_autoupdate_consent(env: Dict[str, Any], capsys: Any) -> None:
    su = FakeSelfUpdate(auto=True)
    home = make_home(env, su=su, ha=False)
    _, out = run(ns("schedule", run_due=True, no_host=True), home, capsys)
    assert out["actions"]["adk"]["status"] == "requested" and su.applied == 1


def test_adk_card_approval_requests_the_validated_apply(env: Dict[str, Any],
                                                       capsys: Any) -> None:
    su = FakeSelfUpdate(auto=False)
    home = make_home(env, su=su, ha=False)
    _, out = run(ns("schedule", run_due=True, no_host=True), home, capsys)
    card = out["actions"]["adk"]["approval"]["id"]
    code, res = run(ns("apply", approve=card), home, capsys)
    assert code == 0 and su.applied == 1


def test_bad_schedule_is_a_setup_error(env: Dict[str, Any], capsys: Any) -> None:
    code, out = run(ns("schedule", at="25:99"), make_home(env), capsys)
    assert code == 2 and "time of day" in out


def test_cli_parser_wires_updates(env: Dict[str, Any], capsys: Any) -> None:
    assert home_cli.main(["updates", "schedule", "--day", "mon", "--at", "05:15",
                          "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schedule"] == {"enabled": True, "weekday": 0, "at": "05:15"}
    saved = json.loads(up.state_path(env["tmp"]).read_text(encoding="utf-8"))
    assert saved["schedule"]["at"] == "05:15"


# ── security review of #15153 (M1-M3, L1-L5) ─────────────────────────────────

def test_m1_sudo_runs_apt_get_directly_with_no_env_wrapper() -> None:
    for argv in up.ALLOWED_APPLY:
        assert "/usr/bin/env" not in argv
        assert not any(a.startswith("DEBIAN_FRONTEND") for a in argv)
    assert up.upgrade_argv(1000) == ("/usr/bin/sudo", "-n") + up.APT_UPGRADE
    assert up.update_argv(1000) == ("/usr/bin/sudo", "-n") + up.APT_UPDATE
    assert up.upgrade_argv(0) == up.APT_UPGRADE
    assert "--force-confold" in " ".join(up.APT_UPGRADE)
    hint = up.sudoers_hint("dana")
    assert "/usr/bin/env" not in hint
    assert hint.startswith("dana ALL=(root) NOPASSWD: /usr/bin/apt-get -q update, "
                           "/usr/bin/apt-get -y -o Dpkg\\:\\:Options\\:\\:\\=--force-confdef")


def test_m1_l1_apply_runs_update_then_upgrade_with_a_fixed_env(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APT_CONFIG", "/tmp/evil.conf")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.so")
    r = UpgradeRunner()
    res = up.run_host_upgrade(1000, r)
    assert res["ok"] is True
    assert [c["argv"] for c in r.calls] == [up.update_argv(1000), up.upgrade_argv(1000)]
    for c in r.calls:
        assert c["env"] == up.UPGRADE_ENV
    assert up.UPGRADE_ENV == {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C.UTF-8",
                              "DEBIAN_FRONTEND": "noninteractive"}
    nopw = up.run_host_upgrade(1000, UpgradeRunner({"update": {
        "state": "exited", "exit": 1, "out": "", "err": "sudo: a password is required"}}))
    assert nopw["ok"] is False and "NOPASSWD" in nopw["error"]
    assert "/usr/bin/env" not in nopw["error"]
    failed_update = UpgradeRunner({"update": {"state": "exited", "exit": 100, "out": "",
                                              "err": "E: network"}})
    assert up.run_host_upgrade(0, failed_update)["ok"] is False
    assert len(failed_update.calls) == 1               # no upgrade after a failed update


def test_m1_refuses_any_argv_off_the_allowlist() -> None:
    r = UpgradeRunner()
    for bad in (("/usr/bin/apt-get", "-y", "dist-upgrade"), ("/bin/sh", "-c", "x"),
                ("/usr/bin/sudo", "-n", "/usr/bin/env", "A=1", "/usr/bin/apt-get")):
        assert up.run_allowlisted(bad, 10, r)["state"] == "refused"
    assert r.calls == []


def test_m3_a_slow_upgrade_is_never_killed_and_reads_still_running() -> None:
    import sys

    env_ = {**up.UPGRADE_ENV, "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    # Ignores SIGTERM like a dpkg in a critical section: it must outlive the one TERM.
    slow = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(30)")
    res = up.run_no_kill([sys.executable, "-c", slow], 1.5, env_, grace=0.5)
    proc = res.pop("_proc")
    try:
        assert res["state"] == "still_running" and res["pid"] > 0
        assert proc.poll() is None                     # nobody killed it
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(5)
    done = up.run_no_kill([sys.executable, "-c", "print('fine')"], 20, env_)
    done.pop("_proc", None)
    assert done["state"] == "exited" and done["exit"] == 0 and "fine" in done["out"]


def test_m3_still_running_is_reported_not_failed(env: Dict[str, Any], capsys: Any) -> None:
    ur = UpgradeRunner({"upgrade": {"state": "still_running", "pid": 4242}})
    home = make_home(env, ha=False, upgrader=ur)
    _, out = run(ns("apply"), home, capsys)
    card = out["actions"]["host"]["approval"]["id"]
    code, res = run(ns("apply", approve=card), home, capsys)
    assert code == 1 and res["result"]["state"] == "still_running"
    assert res["approval"]["status"] == "executing"
    assert rows(env)[-1]["kind"] == "update_running"


def test_m2_card_is_bound_to_the_version(env: Dict[str, Any], capsys: Any) -> None:
    home = make_home(env)
    _, out = run(ns("apply", no_host=True), home, capsys)
    by = {e["entity_id"]: e for e in out["actions"]["ha"]}
    core = by["update.home_assistant_core_update"]
    assert "2024.1.0 -> 2024.1.3" in core["summary"]
    store = json.loads((env["tmp"] / "device_approvals.json").read_text(encoding="utf-8"))
    row = store["approvals"][core["approval"]["id"]]
    assert row["action"]["params"] == {"backup": True, "version": "2024.1.3"}
    bulb = by["update.hallway_bulb_firmware"]
    assert "version" not in store["approvals"][bulb["approval"]["id"]]["action"]["params"]
    # A newer release lands before the guardian says yes: nothing runs.
    for s in env["ha"].states:
        if s["entity_id"] == "update.hallway_bulb_firmware":
            s["attributes"]["latest_version"] = "1.5"
    code, res = run(ns("apply", approve=bulb["approval"]["id"]), home, capsys)
    assert code == 2 and "changed" in json.dumps(res)
    assert not [c for c in env["ha"].calls if c.get("service")]
    code, res = run(ns("apply", approve=core["approval"]["id"]), home, capsys)
    assert code == 0 and res["ran"] is True
    sent = [c for c in env["ha"].calls if c.get("service")]
    assert sent[0]["body"] == {"entity_id": "update.home_assistant_core_update",
                               "backup": True, "version": "2024.1.3"}


def test_l2_control_and_bidi_chars_are_stripped() -> None:
    evil = "Bulb\x1b[31m‮RED\x07" + "x" * 500
    ents = up.parse_ha_updates([state("update.b", "on", name=evil, latest_version="1\x00.2",
                                      title=evil, supported_features=1)])
    for key in ("name", "title", "latest_version"):
        v = ents[0][key]
        assert not any(ord(c) < 0x20 or ord(c) == 0x7f or 0x202a <= ord(c) <= 0x202e
                       for c in v)
    assert len(ents[0]["name"]) <= 80
    win = up.parse_windows_updates(json.dumps({"source": "com", "updates": [
        {"title": "KB⁦x\x1b", "kb": "1", "severity": "", "reboot": False}]}))
    assert win["updates"][0]["title"] == "KBx"
    su = up.parse_softwareupdate("* Label: Evil‮\x1b\n")
    assert su[0]["label"] == "Evil"
    lines = ucli.report_lines({"adk": {"ok": True}, "host": {"skipped": True}, "ha": {
        "configured": True, "ok": True, "entities": [{}],
        "pending": [{"entity_id": "update.b", "installed_version": "\x1b[2J",
                     "latest_version": "‮2"}]}})
    assert not any("\x1b" in ln or "‮" in ln for ln in lines)


def test_l3_malformed_rows_are_dropped_and_enabled_is_strict(tmp_path: Path) -> None:
    p = tmp_path / "home_updates.json"
    good = {"id": "ha-aaaaaaaaaaaa", "status": "pending", "action": dict(up.ADK_ACTION),
            "digest": "d" * 64, "approvers": ["owner"], "required": 1, "votes": {},
            "approvals": 0, "expires_at": 1.0, "created_at": 0.0, "household": "local",
            "summary": "s", "policy": {}, "requested_by": {}}
    p.write_text(json.dumps({"approvals": {
        "ha-aaaaaaaaaaaa": good, "ha-bbbbbbbbbbbb": {"id": "ha-bbbbbbbbbbbb"},
        "ha-cccccccccccc": "junk", "ha-dddddddddddd": {**good, "id": "ha-other"},
        "ha-eeeeeeeeeeee": {**good, "id": "ha-eeeeeeeeeeee", "status": "bogus"}}}),
        encoding="utf-8")
    assert list(up.load_state(p)["approvals"]) == ["ha-aaaaaaaaaaaa"]
    for bad in ("no", 1, "yes"):
        p.write_text(json.dumps({"schedule": {"enabled": bad}}), encoding="utf-8")
        with pytest.raises(ValueError):
            up.load_state(p)
    p.write_text(json.dumps({"schedule": {"enabled": False}}), encoding="utf-8")
    assert up.load_state(p)["schedule"]["enabled"] is False


def test_l4_household_expiry_rules_win(env: Dict[str, Any], capsys: Any) -> None:
    (env["tmp"] / "devices.json").write_text(json.dumps(
        {"classes": {"guarded": {"approvals_required": 1, "approvers": "any_guardian",
                                 "expires_s": 1800}}}), encoding="utf-8")
    home = make_home(env)
    _, out = run(ns("apply", no_host=True), home, capsys)
    a = next(e for e in out["actions"]["ha"]
             if e["entity_id"] == "update.home_assistant_core_update")["approval"]
    assert a["expires_at"] - a["created_at"] == 1800
    (env["tmp"] / "devices.json").unlink()
    (env["tmp"] / "device_approvals.json").unlink()
    _, out = run(ns("apply", no_host=True), home, capsys)
    a = next(e for e in out["actions"]["ha"]
             if e["entity_id"] == "update.home_assistant_core_update")["approval"]
    assert a["expires_at"] - a["created_at"] == up.CARD_EXPIRES_S


def test_l5_executing_is_persisted_before_the_run_and_never_regresses(
        env: Dict[str, Any], capsys: Any) -> None:
    seen: List[str] = []
    path = up.state_path(env["tmp"])

    class Peek(UpgradeRunner):
        def __call__(self, argv: Sequence[str], timeout: float,
                     env_: Dict[str, str]) -> Dict[str, Any]:
            rows_ = json.loads(path.read_text(encoding="utf-8"))["approvals"]
            seen.append(next(iter(rows_.values()))["status"])
            return super().__call__(argv, timeout, env_)

    home = make_home(env, ha=False, upgrader=Peek())
    _, out = run(ns("apply"), home, capsys)
    card = out["actions"]["host"]["approval"]["id"]
    stale = up.load_state(path)                        # a copy read before the run
    code, _ = run(ns("apply", approve=card), home, capsys)
    assert code == 0 and seen and set(seen) == {"executing"}
    up.save_state(path, stale)                         # a stale writer saves "pending"
    assert up.load_state(path)["approvals"][card]["status"] == "executed"
