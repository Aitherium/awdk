"""``python -m adk.node_beat`` -- keep this enrolled machine beating, with nothing else.

A machine paired with a code (a Spark, a Deck, a headless laptop) needs ONE long-lived
process that heartbeats to Identity as the device and runs the commands the owner sends
it (``adk.node_commands``). The full daemon is more than that box may want to run, and
asking the owner to write a service unit by hand is the manual step this removes:
``adk pair`` installs this module as a per-user autostart (``install_autostart``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import IO, Any, Dict, Optional

log = logging.getLogger("adk.node_beat")

UNIT = "aither-node-beat.service"
RUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "AitherNodeBeat"


def default_identity() -> str:
    return (os.environ.get("AITHER_IDP_URL") or os.environ.get("AITHER_IDP_BASE_URL")
            or "https://idp.aitherium.com").rstrip("/")


def lock_path() -> Path:
    return Path.home() / ".aither" / "node_beat.lock"


def standby_lock_path() -> Path:
    return Path.home() / ".aither" / "node_beat.standby.lock"


def acquire_singleton(path: Optional[Path] = None) -> Optional[IO[Any]]:
    """Hold this machine's one node_beat lock for the life of the process.

    Returns the open lock file (keep it referenced; the OS drops the lock when the
    process exits, however it exits), or None when another node_beat already holds
    it. Every `adk enroll` and every login used to start ANOTHER detached beater
    (2026-10-14 review); the device lease stopped double beats, not the processes.
    """
    p = path or lock_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = open(p, "a+")  # noqa: SIM115 - held open on purpose
    except OSError:
        return None
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def is_running(path: Optional[Path] = None) -> bool:
    """True when another process holds the node_beat lock."""
    fh = acquire_singleton(path)
    if fh is None:
        return True
    release_singleton(fh)
    return False


def release_singleton(fh: IO[Any]) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError as exc:
        log.debug("node_beat lock release failed: %s", exc)
    fh.close()


#: A starting beater registers the supervised autostart (ensure_autostart). The test
#: suite turns this off (tests/conftest.py): a test that runs _main must never register
#: a real task / unit / cron line on the machine running the tests.
AUTOSTART_ON_START = True
#: A started beater runs the game guard (adk.game_guard); tests/conftest.py turns it off.
GAME_GUARD_ON_START = True


async def _wait_for_singleton(poll_s: float = 5.0) -> IO[Any]:
    """Standby: wait until the beater that holds the lock is gone, then take over.

    The supervised copies (the Windows task, the systemd user unit, cron) start with
    ``--standby``: while another beater lives (a Run-key or Konsole one) they wait here
    instead of exiting, so the moment it dies -- killed, crashed, its terminal closed,
    its files replaced by an upgrade -- this one beats in its place."""
    while True:
        lock = acquire_singleton()
        if lock is not None:
            return lock
        await asyncio.sleep(poll_s)


async def _main(standby: bool = False) -> int:
    from adk import enrollment

    lock = acquire_singleton()
    if lock is None:
        if not standby:
            log.info("node beat: already running on this machine; exiting")
            return 0
        # ONE standby per machine too: cron / the task start one every few minutes.
        spare = acquire_singleton(standby_lock_path())
        if spare is None:
            log.info("node beat: running, and a standby already waits; exiting")
            return 0
        log.info("node beat: another beater is running; standing by")
        lock = await _wait_for_singleton()
        release_singleton(spare)

    # A machine paired before the self-healing autostart existed gets it now, from
    # the first start of this code (an `upgrade` restart is such a start).
    if AUTOSTART_ON_START:
        try:
            auto = await asyncio.to_thread(ensure_autostart)
            log.info("node beat autostart: %s", auto)
        except Exception as exc:  # noqa: BLE001 - autostart must never stop the beat
            log.warning("node beat autostart check failed: %s", exc)

    def _signed_in_token() -> str:
        # Re-read every beat: a sign-in refreshed on disk (adk login, the daemon)
        # reaches this long-lived process without a restart. When it has lapsed the
        # loop falls back to the device's own token (enrollment.resume_heartbeat).
        try:
            from adk.fleet_enroll import _load_auth_config
            return str(_load_auth_config().get("access_token") or "")
        except Exception:  # noqa: BLE001 - no session: the box beats as itself
            return ""

    # Facet `node_beat`: it yields the device lease to the daemon / `adk rc`, which
    # report the harness and the reverse link, and takes over only when they stop.
    out = enrollment.resume_heartbeat(_signed_in_token(), default_base_url=default_identity(),
                                      token_provider=_signed_in_token, facet="node_beat")
    log.info("node beat: %s", out)
    if not out.get("started"):
        return 1
    # Games come first: while the owner plays, the worker stops and the lend server
    # drops to idle priority (adk.game_guard). It lives in this always-on process.
    guard_task = None
    if GAME_GUARD_ON_START:
        from adk import game_guard

        guard_task = asyncio.ensure_future(game_guard.run_forever())
    while True:
        if guard_task is not None and guard_task.done() and not guard_task.cancelled() \
                and guard_task.exception() is not None:
            log.warning("game guard stopped: %s", guard_task.exception())
            guard_task = None
        await asyncio.sleep(300)
        log.info("node beat: %s", enrollment.heartbeat_status())


# ── autostart: supervised, self-healing ──────────────────────────────────────
#
# Measured 2026-10-22: a Windows laptop's Run-key beater died during an `upgrade`
# (pip replaced its files) and nothing brought it back until the next sign-in; a
# Steam Deck's beater ran inside a Konsole scope and died with the window. Every
# autostart below is SUPERVISED: something starts the beat again within minutes.

#: The per-user scheduled task (and the Run value, kept as a second path).
TASK_NAME = "AitherNodeBeat"
#: Repeat interval of the task / cron entry: a dead beater is back within this.
REPEAT_MINUTES = 5
CRON_TAG = "# aither-node-beat"
DESKTOP_FILE = "aither-node-beat.desktop"
_NO_WINDOW = 0x08000000


def _host() -> str:
    """``windows`` | ``linux`` | the platform name -- one seam for tests."""
    if os.name == "nt":
        return "windows"
    return "linux" if sys.platform.startswith("linux") else sys.platform


def _run(argv: list, **kw: Any) -> subprocess.CompletedProcess:
    """A short child: never a console window, never raises on a missing binary."""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    kw.setdefault("timeout", 60)
    if os.name == "nt":
        kw.setdefault("creationflags", _NO_WINDOW)
    try:
        return subprocess.run(argv, check=False, **kw)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{type(exc).__name__}: {exc}")


def beat_interpreter() -> Path:
    """pythonw.exe beside this interpreter on Windows (no console), else this one."""
    py = Path(sys.executable)
    if os.name == "nt":
        pyw = py.with_name("pythonw.exe")
        if pyw.exists():
            return pyw
    return py


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def task_xml(interpreter: str, user: str, start_boundary: str,
             workdir: str = "") -> str:
    """Task Scheduler XML for the beat: at THIS user's logon and every REPEAT_MINUTES,
    never a second instance, hidden, no time limit, least privilege (no elevation)."""
    i, u = _xml_escape(interpreter), _xml_escape(user)
    wd = f"\n      <WorkingDirectory>{_xml_escape(workdir)}</WorkingDirectory>" if workdir else ""
    rep = (f"<Repetition><Interval>PT{REPEAT_MINUTES}M</Interval>"
           "<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>AitherOS device heartbeat (adk.node_beat)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{u}</UserId>{rep}</LogonTrigger>
    <TimeTrigger><Enabled>true</Enabled><StartBoundary>{start_boundary}</StartBoundary>{rep}</TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{u}</UserId><LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{i}</Command>
      <Arguments>-m adk.node_beat --standby</Arguments>{wd}
    </Exec>
  </Actions>
</Task>
"""


def _windows_user() -> str:
    dom, name = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")
    return f"{dom}\\{name}" if dom and name else name


def windows_task_current(run: Any = None) -> bool:
    """Is the registered task the self-healing one, for THIS interpreter?"""
    r = (run or _run)(["schtasks", "/query", "/tn", TASK_NAME, "/xml"])
    if r.returncode != 0:
        return False
    text = (r.stdout or "").lower()
    return (f"<interval>pt{REPEAT_MINUTES}m</interval>" in text
            and "ignorenew" in text and "--standby" in text
            and str(beat_interpreter()).lower() in text)


def install_windows_task(run: Any = None) -> Dict[str, str]:
    """Register (or replace) the per-user task. No elevation: a task for the signed-in
    user at least privilege. Returns {"ok": "1"|"", "detail": ...}."""
    import time as _time

    run = run or _run
    xml_path = Path.home() / ".aither" / "node_beat_task.xml"
    try:
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        start = _time.strftime("%Y-%m-%dT%H:%M:%S", _time.localtime(_time.time() + 60))
        xml_path.write_text(task_xml(str(beat_interpreter()), _windows_user(), start,
                                     str(Path.home())), encoding="utf-16")
    except OSError as exc:
        return {"ok": "", "detail": f"task xml not written: {exc}"[:200]}
    r = run(["schtasks", "/create", "/f", "/tn", TASK_NAME, "/xml", str(xml_path)])
    if r.returncode != 0:
        return {"ok": "", "detail": (r.stderr or r.stdout or "").strip()[:200]}
    return {"ok": "1", "detail": ""}


def _windows_run_key(run: Any = None) -> bool:
    cmd = f'"{beat_interpreter()}" -m adk.node_beat'
    r = (run or _run)(["reg", "add", RUN_KEY, "/v", RUN_VALUE, "/t", "REG_SZ", "/d", cmd, "/f"])
    return r.returncode == 0


def _systemd_user_ok(run: Any) -> bool:
    import shutil

    if not shutil.which("systemctl"):
        return False
    return run(["systemctl", "--user", "show-environment"]).returncode == 0


def unit_text() -> str:
    return ("[Unit]\nDescription=AitherOS device heartbeat (Identity /v1/nodes)\n"
            "After=network-online.target\n\n[Service]\nType=simple\n"
            f"ExecStart={sys.executable} -m adk.node_beat --standby\n"
            "Restart=always\nRestartSec=30\nNice=10\n\n"
            "[Install]\nWantedBy=default.target\n")


def _install_systemd_user(run: Any) -> Dict[str, str]:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    path = unit_dir / UNIT
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    enabled = run(["systemctl", "--user", "is-enabled", UNIT]).returncode == 0
    active = run(["systemctl", "--user", "is-active", UNIT]).returncode == 0
    if current == unit_text() and enabled and active:
        return {"autostart": "systemd-user", "changed": ""}
    path.write_text(unit_text(), encoding="utf-8")
    for argv in (["systemctl", "--user", "daemon-reload"],
                 ["systemctl", "--user", "enable", UNIT],
                 # restart, not start: a changed unit must run its new ExecStart
                 ["systemctl", "--user", "restart" if active else "start", UNIT]):
        r = run(argv)
        if r.returncode != 0:
            return {"autostart": "failed", "detail": (r.stderr or r.stdout).strip()[:200]}
    # Without linger a user unit stops at logout; a headless box needs it.
    run(["loginctl", "enable-linger"])
    return {"autostart": "systemd-user", "changed": "1"}


def _install_desktop_and_cron(run: Any) -> Dict[str, str]:
    """No user systemd: a login autostart entry plus a cron line that restarts a dead
    beater every REPEAT_MINUTES (and at boot). Both start it in standby."""
    cmd = f"{sys.executable} -m adk.node_beat --standby"
    done = []
    try:
        auto_dir = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart"
        auto_dir.mkdir(parents=True, exist_ok=True)
        (auto_dir / DESKTOP_FILE).write_text(
            "[Desktop Entry]\nType=Application\nName=Aither node heartbeat\n"
            f"Exec={cmd}\nNoDisplay=true\nTerminal=false\nX-GNOME-Autostart-enabled=true\n",
            encoding="utf-8")
        done.append("autostart-desktop")
    except OSError as exc:
        log.warning("autostart .desktop not written: %s", exc)
    import shutil

    if shutil.which("crontab"):
        cur = run(["crontab", "-l"])
        lines = [ln for ln in (cur.stdout or "").splitlines()
                 if cur.returncode == 0 and CRON_TAG not in ln]
        logf = Path.home() / ".aither" / "logs" / "node-beat.log"
        entry = f"{cmd} >>{logf} 2>&1"
        want = lines + [f"@reboot {entry} {CRON_TAG}",
                        f"*/{REPEAT_MINUTES} * * * * {entry} {CRON_TAG}"]
        if want != (cur.stdout or "").splitlines():
            r = run(["crontab", "-"], input="\n".join(want) + "\n")
            if r.returncode == 0:
                done.append("cron")
        else:
            done.append("cron")
    if not done:
        return {"autostart": "failed", "detail": "no user systemd, no autostart dir, no crontab"}
    return {"autostart": "+".join(done)}


def ensure_autostart(run: Any = None) -> Dict[str, str]:
    """Make sure a SUPERVISED autostart is registered; never starts a beater (the caller
    is one). Idempotent and cheap when everything is already in place. Never raises."""
    run = run or _run
    try:
        if _host() == "windows":
            _windows_run_key(run)  # the second path; harmless with the task's standby
            if windows_task_current(run):
                return {"autostart": "windows-task", "changed": ""}
            task = install_windows_task(run)
            if task["ok"]:
                return {"autostart": "windows-task", "changed": "1"}
            return {"autostart": "windows-run-key", "detail": task["detail"]}
        if _host() == "linux":
            if _systemd_user_ok(run):
                out = _install_systemd_user(run)
                if out.get("autostart") != "failed":
                    return out
            return _install_desktop_and_cron(run)
        return {"autostart": "unsupported", "detail": f"no autostart for {sys.platform} yet"}
    except Exception as exc:  # noqa: BLE001 - say what failed, never raise
        return {"autostart": "failed", "detail": f"{type(exc).__name__}: {exc}"[:200]}


def install_autostart(run: Any = None, popen: Any = None) -> Dict[str, str]:
    """Start this module at login, per user, no elevation, supervised; start it now
    when no beater runs. Never raises."""
    run = run or _run
    popen = popen or subprocess.Popen
    out = ensure_autostart(run)
    if out.get("autostart") in ("failed", "unsupported") or is_running():
        if is_running() and out.get("autostart") not in ("failed", "unsupported"):
            out = {**out, "running": "already"}  # one beater per machine
        return out
    if _host() == "windows":
        if out["autostart"] == "windows-task" and \
                run(["schtasks", "/run", "/tn", TASK_NAME]).returncode == 0:
            return out
        try:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0x8) | _NO_WINDOW
            popen([str(beat_interpreter()), "-m", "adk.node_beat"], creationflags=flags,
                  close_fds=True)
        except OSError as exc:
            return {**out, "detail": f"not started: {exc}"[:200]}
        return out
    if out["autostart"] == "systemd-user":
        return out  # the unit is started
    try:
        logf = Path.home() / ".aither" / "logs" / "node-beat.log"
        logf.parent.mkdir(parents=True, exist_ok=True)
        with open(logf, "ab") as fh:
            popen([sys.executable, "-m", "adk.node_beat"], stdin=subprocess.DEVNULL,
                  stdout=fh, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    except OSError as exc:
        return {**out, "detail": f"not started: {exc}"[:200]}
    return out


def main(argv: Optional[list] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if "--install" in args:
        # `python -m adk.node_beat --install`: register the supervised autostart and
        # start the beat; prints the outcome as JSON (remote onboarding reads it).
        import json

        out = install_autostart()
        print(json.dumps(out))
        return 0 if out.get("autostart") not in ("failed", "unsupported") else 1
    # A crash of the beater is queued for the company app's support (opt-out:
    # `adk feedback off`); `adk tenant watch` uploads the outbox.
    try:
        from adk import support_report
        support_report.install_excepthook("awdk.node_beat")
    except Exception as exc:  # noqa: BLE001 - reporting must never stop the beat
        log.debug("support excepthook not installed: %s", exc)
    try:
        return asyncio.run(_main(standby="--standby" in args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
