"""The device heartbeat's autostart is SUPERVISED: a dead beater comes back by itself.

Measured 2026-10-22: a Windows laptop's Run-key beater died during an `upgrade` and
stayed dead (Identity last_seen frozen for 80+ min, laptop awake); a Steam Deck's
beater lived in a Konsole scope and died with the window. These pin what
``adk.node_beat`` registers instead -- a per-user scheduled task on Windows (logon +
every 5 min, one instance, hidden), a ``systemctl --user`` unit where user systemd
exists, else a login .desktop entry plus cron -- and that every supervised copy
starts in standby, so it waits for a live beater instead of piling up. No real task,
unit, crontab or process is touched: every command goes to a recording fake.
"""

import asyncio
import subprocess
import xml.etree.ElementTree as ET

import pytest

from adk import node_beat, node_commands

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class _Run:
    """Records argv; answers from a table of (argv-prefix -> CompletedProcess)."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        for prefix, ans in self.answers.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return ans(argv, kw) if callable(ans) else ans
        return subprocess.CompletedProcess(argv, 0, "", "")

    def argvs(self):
        return [c[0] for c in self.calls]


def _cp(rc=0, out=""):
    return subprocess.CompletedProcess([], rc, out, "")


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("USERDOMAIN", "LAPTOP")
    monkeypatch.setenv("USERNAME", "owner")
    yield tmp_path


def _parse(xml_text):
    return ET.fromstring(xml_text.split("?>", 1)[1])


def test_task_xml_is_self_healing_per_user_hidden_and_single_instance():
    root = _parse(node_beat.task_xml(r"C:\venv\Scripts\pythonw.exe", r"LAPTOP\owner",
                                     "2026-10-22T18:00:00", r"C:\Users\owner"))
    logon = root.find("t:Triggers/t:LogonTrigger", NS)
    assert logon.find("t:UserId", NS).text == r"LAPTOP\owner"  # this user: no admin
    for trig in root.find("t:Triggers", NS):
        assert trig.find("t:Repetition/t:Interval", NS).text == "PT5M"
    st = root.find("t:Settings", NS)
    assert st.find("t:MultipleInstancesPolicy", NS).text == "IgnoreNew"
    assert st.find("t:Hidden", NS).text == "true"
    assert st.find("t:ExecutionTimeLimit", NS).text == "PT0S"  # never killed at 72 h
    assert st.find("t:DisallowStartIfOnBatteries", NS).text == "false"
    pr = root.find("t:Principals/t:Principal", NS)
    assert pr.find("t:RunLevel", NS).text == "LeastPrivilege"
    ex = root.find("t:Actions/t:Exec", NS)
    assert ex.find("t:Command", NS).text.endswith("pythonw.exe")  # no console window
    assert ex.find("t:Arguments", NS).text == "-m adk.node_beat --standby"


def test_windows_registers_the_task_and_keeps_the_run_key(monkeypatch, tmp_path):
    monkeypatch.setattr(node_beat, "_host", lambda: "windows")
    run = _Run({("schtasks", "/query"): _cp(1)})
    out = node_beat.ensure_autostart(run)
    assert out == {"autostart": "windows-task", "changed": "1"}
    argvs = run.argvs()
    assert argvs[0][:2] == ["reg", "add"] and node_beat.RUN_VALUE in argvs[0]
    create = next(a for a in argvs if a[:2] == ["schtasks", "/create"])
    assert create[2:5] == ["/f", "/tn", "AitherNodeBeat"] and create[5] == "/xml"
    xml = (tmp_path / ".aither" / "node_beat_task.xml").read_text(encoding="utf-16")
    assert "--standby" in xml and "PT5M" in xml and r"LAPTOP\owner" in xml
    assert not any(a[:2] == ["schtasks", "/run"] for a in argvs)  # ensure never starts


def test_windows_current_task_is_left_alone(monkeypatch):
    monkeypatch.setattr(node_beat, "_host", lambda: "windows")
    xml = node_beat.task_xml(str(node_beat.beat_interpreter()), r"LAPTOP\owner", "x")
    run = _Run({("schtasks", "/query"): _cp(0, xml)})
    assert node_beat.ensure_autostart(run) == {"autostart": "windows-task", "changed": ""}
    assert not any(a[:2] == ["schtasks", "/create"] for a in run.argvs())


def test_an_old_logon_only_task_is_replaced(monkeypatch):
    """remote_onboard used to register an ONLOGON-only task under the same name."""
    monkeypatch.setattr(node_beat, "_host", lambda: "windows")
    old = "<Task><Triggers><LogonTrigger/></Triggers><Exec>pythonw.exe -m adk.node_beat</Exec>"
    run = _Run({("schtasks", "/query"): _cp(0, old)})
    assert node_beat.ensure_autostart(run)["changed"] == "1"


def test_windows_task_refused_falls_back_to_the_run_key(monkeypatch):
    monkeypatch.setattr(node_beat, "_host", lambda: "windows")
    run = _Run({("schtasks", "/query"): _cp(1),
                ("schtasks", "/create"): subprocess.CompletedProcess(
                    [], 1, "", "Access is denied.")})
    out = node_beat.ensure_autostart(run)
    assert out["autostart"] == "windows-run-key" and "denied" in out["detail"]


def test_install_starts_the_task_when_no_beater_runs(monkeypatch):
    monkeypatch.setattr(node_beat, "_host", lambda: "windows")
    monkeypatch.setattr(node_beat, "is_running", lambda path=None: False)
    run = _Run({("schtasks", "/query"): _cp(1)})
    started = []
    out = node_beat.install_autostart(run=run, popen=lambda *a, **k: started.append(a))
    assert out["autostart"] == "windows-task"
    assert ["schtasks", "/run", "/tn", "AitherNodeBeat"] in run.argvs() and started == []


def test_linux_user_systemd_unit_is_supervised_and_lingers(monkeypatch, tmp_path):
    import shutil

    monkeypatch.setattr(node_beat, "_host", lambda: "linux")
    monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}")
    run = _Run({("systemctl", "--user", "is-enabled"): _cp(1),
                ("systemctl", "--user", "is-active"): _cp(1)})
    out = node_beat.ensure_autostart(run)
    assert out["autostart"] == "systemd-user"
    unit = (tmp_path / ".config" / "systemd" / "user" / node_beat.UNIT).read_text()
    assert "-m adk.node_beat --standby" in unit and "Restart=always" in unit
    argvs = run.argvs()
    assert ["systemctl", "--user", "enable", node_beat.UNIT] in argvs
    assert ["systemctl", "--user", "start", node_beat.UNIT] in argvs
    assert ["loginctl", "enable-linger"] in argvs


def test_linux_without_user_systemd_gets_desktop_and_cron(monkeypatch, tmp_path):
    """The Deck case: no usable user unit -> login entry + cron every 5 min and @reboot."""
    import shutil

    monkeypatch.setattr(node_beat, "_host", lambda: "linux")
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/crontab" if n == "crontab" else None)
    installed = {"text": "0 3 * * * backup.sh\n"}

    def crontab(argv, kw):
        if argv[1] == "-l":
            return _cp(0, installed["text"])
        installed["text"] = kw["input"]
        return _cp(0)
    run = _Run({("crontab",): crontab})
    out = node_beat.ensure_autostart(run)
    assert out["autostart"] == "autostart-desktop+cron"
    lines = installed["text"].splitlines()
    assert lines[0] == "0 3 * * * backup.sh"  # the owner's own lines survive
    assert sum(node_beat.CRON_TAG in ln for ln in lines) == 2
    assert any(ln.startswith("*/5 * * * * ") and "--standby" in ln for ln in lines)
    assert any(ln.startswith("@reboot ") for ln in lines)
    desktop = (tmp_path / "cfg" / "autostart" / node_beat.DESKTOP_FILE).read_text()
    assert "adk.node_beat --standby" in desktop
    n = len(run.calls)
    node_beat.ensure_autostart(run)  # idempotent: no second crontab write
    assert not any(a == ["crontab", "-"] for a in run.argvs()[n:])
    assert sum(node_beat.CRON_TAG in ln for ln in installed["text"].splitlines()) == 2


def test_a_standby_waits_for_the_live_beater_then_takes_over(tmp_path):
    held = node_beat.acquire_singleton()
    assert held is not None

    async def go():
        async def release():
            await asyncio.sleep(0.15)
            node_beat.release_singleton(held)
        asyncio.ensure_future(release())
        return await asyncio.wait_for(node_beat._wait_for_singleton(poll_s=0.05), 5)
    lock = asyncio.run(go())
    assert lock is not None
    node_beat.release_singleton(lock)


def test_only_one_standby_per_machine(monkeypatch):
    """cron / the task start a copy every 5 minutes; they must not pile up."""
    main_lock = node_beat.acquire_singleton()
    spare = node_beat.acquire_singleton(node_beat.standby_lock_path())
    try:
        assert asyncio.run(asyncio.wait_for(node_beat._main(standby=True), 5)) == 0
    finally:
        node_beat.release_singleton(spare)
        node_beat.release_singleton(main_lock)


def test_a_starting_beater_installs_the_supervised_autostart(monkeypatch):
    """Machines paired before this existed adopt it on their next start (an upgrade
    restart is one)."""
    from adk import enrollment

    seen = []
    monkeypatch.setattr(node_beat, "AUTOSTART_ON_START", True)
    monkeypatch.setattr(node_beat, "ensure_autostart", lambda: seen.append(1) or {})
    monkeypatch.setattr(enrollment, "resume_heartbeat",
                        lambda *a, **k: {"started": False, "reason": "test"})
    assert asyncio.run(node_beat._main()) == 1
    assert seen == [1]


def test_upgrade_restart_runs_the_beats_own_task(monkeypatch):
    import json
    import os
    import sys

    monkeypatch.setattr(node_commands, "_host_kind", lambda: "windows")
    monkeypatch.setattr(node_commands, "_beat_task_name", lambda: "AitherNodeBeat")
    monkeypatch.setattr(node_commands, "_relaunch_argv",
                        lambda: [sys.executable, "-m", "adk.node_beat"])
    plan = node_commands._plan_restart()
    assert plan["mechanism"] == "schtasks-run" and plan["task"] == "AitherNodeBeat"
    spawned = []

    class _Popen:
        def __init__(self, argv, **kw):
            spawned.append((argv, kw))

    class _ExitError(Exception):
        pass

    def _exit(code):
        raise _ExitError()
    monkeypatch.setattr(subprocess, "Popen", _Popen)
    monkeypatch.setattr(os, "_exit", _exit)
    with pytest.raises(_ExitError):
        node_commands.perform_pending_restart()
    argv, kw = spawned[0]
    spec = json.loads(argv[4])
    assert spec["task"] == "AitherNodeBeat" and spec["argv"][1:] == ["-m", "adk.node_beat"]
    assert 'schtasks", "/run", "/tn", spec["task"]' in argv[3]
    assert kw["creationflags"] & 0x08000000
    node_commands._PENDING_RESTART.clear()


def test_the_daemon_is_relaunched_not_swapped_for_the_beat_task(monkeypatch):
    import sys

    monkeypatch.setattr(node_commands, "_host_kind", lambda: "windows")
    monkeypatch.setattr(node_commands, "_beat_task_name", lambda: "AitherNodeBeat")
    monkeypatch.setattr(node_commands, "_relaunch_argv", lambda: [sys.executable, "-m", "adk",
                                                                  "serve"])
    assert node_commands._plan_restart()["mechanism"] == "detached-relaunch"
    node_commands._PENDING_RESTART.clear()
