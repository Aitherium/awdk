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


async def _main() -> int:
    from adk import enrollment

    lock = acquire_singleton()
    if lock is None:
        log.info("node beat: already running on this machine; exiting")
        return 0

    def _signed_in_token() -> str:
        # Re-read every beat: a sign-in refreshed on disk (adk login, the daemon)
        # reaches this long-lived process without a restart. When it has lapsed the
        # loop falls back to the device's own token (enrollment.resume_heartbeat).
        try:
            from adk.fleet_enroll import _load_auth_config
            return str(_load_auth_config().get("access_token") or "")
        except Exception:  # noqa: BLE001 - no session: the box beats as itself
            return ""

    out = enrollment.resume_heartbeat(_signed_in_token(), default_base_url=default_identity(),
                                      token_provider=_signed_in_token)
    log.info("node beat: %s", out)
    if not out.get("started"):
        return 1
    while True:
        await asyncio.sleep(300)
        log.info("node beat: %s", enrollment.heartbeat_status())


def install_autostart() -> Dict[str, str]:
    """Start this module at login, per user, no elevation. Never raises."""
    py = Path(sys.executable)
    try:
        if os.name == "nt":
            pyw = py.with_name("pythonw.exe")
            cmd = f'"{pyw if pyw.exists() else py}" -m adk.node_beat'
            r = subprocess.run(["reg", "add", RUN_KEY, "/v", RUN_VALUE, "/t", "REG_SZ",
                                "/d", cmd, "/f"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0:
                return {"autostart": "failed", "detail": (r.stderr or r.stdout).strip()[:200]}
            if is_running():
                # One beater per machine: a re-run of `adk enroll` adds nothing.
                return {"autostart": "windows-run-key", "running": "already"}
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen([str(pyw if pyw.exists() else py), "-m", "adk.node_beat"],
                             creationflags=flags, close_fds=True)
            return {"autostart": "windows-run-key"}
        if sys.platform.startswith("linux"):
            unit_dir = Path.home() / ".config" / "systemd" / "user"
            unit_dir.mkdir(parents=True, exist_ok=True)
            (unit_dir / UNIT).write_text(
                "[Unit]\nDescription=AitherOS device heartbeat (Identity /v1/nodes)\n"
                "After=network-online.target\n\n[Service]\nType=simple\n"
                f"ExecStart={py} -m adk.node_beat\nRestart=always\nRestartSec=30\nNice=10\n\n"
                "[Install]\nWantedBy=default.target\n", encoding="utf-8")
            for argv in (["systemctl", "--user", "daemon-reload"],
                         ["systemctl", "--user", "enable", "--now", UNIT]):
                r = subprocess.run(argv, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
                if r.returncode != 0:
                    return {"autostart": "failed", "detail": (r.stderr or r.stdout).strip()[:200]}
            # Without linger a user unit stops at logout; a headless box needs it.
            subprocess.run(["loginctl", "enable-linger"], capture_output=True)
            return {"autostart": "systemd-user"}
        return {"autostart": "unsupported", "detail": f"no autostart for {sys.platform} yet"}
    except Exception as exc:  # noqa: BLE001 - pairing already succeeded; say what failed
        return {"autostart": "failed", "detail": f"{type(exc).__name__}: {exc}"[:200]}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # A crash of the beater is queued for the company app's support (opt-out:
    # `adk feedback off`); `adk tenant watch` uploads the outbox.
    try:
        from adk import support_report
        support_report.install_excepthook("awdk.node_beat")
    except Exception as exc:  # noqa: BLE001 - reporting must never stop the beat
        log.debug("support excepthook not installed: %s", exc)
    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
