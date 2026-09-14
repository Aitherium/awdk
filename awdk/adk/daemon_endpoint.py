"""Single source of truth for where the local adk daemon is listening.

Three places used to hardcode ``http://127.0.0.1:9001`` independently — the start
script, the MCP delegation tool, and AitherShell's backend resolver. Nothing tied them
together, so changing the port silently degraded delegation back to genesis and dropped
AitherShell onto the slow path, with no error anywhere (fail-safe, but invisible).

The daemon now PUBLISHES its real address here on startup and removes it on shutdown, so
consumers discover it instead of guessing. Python callers use this module; non-Python
callers (AitherShell/TypeScript) read the same JSON file.

Resolution order, most specific first:
  1. ``ADK_DAEMON_URL`` env — explicit override always wins.
  2. ``~/.aither/daemon.json`` — written by the running daemon.
  3. ``DEFAULT_URL`` — last-resort default so a cold start still has a target.
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"  # never "localhost": the IPv6 path costs ~2s per connection
DEFAULT_PORT = 9001
DEFAULT_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"

ENDPOINT_FILE = Path.home() / ".aither" / "daemon.json"


def resolve_daemon_url() -> str:
    """Where the local adk daemon is (or is expected to be)."""
    env = os.environ.get("ADK_DAEMON_URL", "").strip()
    if env:
        return env.rstrip("/")
    try:
        data = json.loads(ENDPOINT_FILE.read_text(encoding="utf-8"))
        url = str(data.get("url") or "").strip()
        # Trust the file only if something is STILL LISTENING there. A daemon killed with
        # SIGKILL/Stop-Process never runs its shutdown hook, so the entry outlives it —
        # and then this function confidently points every consumer (AitherShell, MCP
        # delegation) at a dead port, which is strictly worse than the default it
        # replaced. Observed exactly that on 2026-07-29: the file said :9101 (a
        # force-killed test daemon) while the real daemon served on :9001. A stale
        # pointer beats no pointer only when someone checks it.
        if url and _is_listening(str(data.get("host") or DEFAULT_HOST), data.get("port")):
            return url.rstrip("/")
    except (OSError, ValueError):
        pass
    return DEFAULT_URL


def _is_listening(host: str, port: object) -> bool:
    """Cheap loopback liveness check — microseconds when something is there."""
    try:
        port_num = int(port or 0)
    except (TypeError, ValueError):
        return False
    if not port_num:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        return sock.connect_ex((host, port_num)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def _live_owner_other_than_us(our_url: str) -> bool:
    """True if a DIFFERENT daemon is published here and still answering on that address."""
    try:
        data = json.loads(ENDPOINT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    published = str(data.get("url") or "").rstrip("/")
    if not published or published == our_url.rstrip("/"):
        return False
    if data.get("pid") == os.getpid():
        return False
    return _is_listening(str(data.get("host") or DEFAULT_HOST), data.get("port"))


def publish_daemon_url(host: str, port: int) -> str:
    """Record the address the daemon actually bound, for other processes to discover."""
    # A wildcard bind is not a usable client target — advertise loopback instead.
    advertised_host = DEFAULT_HOST if host in ("0.0.0.0", "::", "") else host
    url = f"http://{advertised_host}:{port}"
    # Do not HIJACK a live daemon's entry. The ownership stamp below protects deletion;
    # this protects the write. Without it, any second daemon (a test instance, a
    # scratch port) silently redirects every consumer — AitherShell, MCP delegation —
    # away from the primary and at itself, and the primary has no idea. Verified by TCP
    # connect rather than by pid: "is something still SERVING that address" is the
    # question consumers actually care about, and it is portable.
    if _live_owner_other_than_us(url):
        return url
    try:
        ENDPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
        ENDPOINT_FILE.write_text(
            json.dumps(
                {
                    "url": url,
                    "host": advertised_host,
                    "port": port,
                    # Ownership stamp — see clear_daemon_url(). Without it, ANY daemon
                    # exiting deletes whatever is in this shared file, including a
                    # different daemon's live entry.
                    "pid": os.getpid(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        # Discovery is an optimisation — never let it stop the daemon from serving.
        pass
    return url


def clear_daemon_url() -> None:
    """Remove the published address on shutdown — but ONLY if it is still ours.

    This file is SHARED by every adk daemon on the box, and the unconditional
    `unlink()` this replaces caused a real outage of the discovery mechanism on
    2026-07-29: a second daemon started on a port it could not bind (a Windows
    excluded range), published itself over the live daemon's entry, failed to bind,
    and then its `finally:` deleted the file — leaving the healthy daemon on :9001
    running and UNDISCOVERABLE. Nothing surfaced it, because every consumer falls back
    to the 127.0.0.1:9001 default and that default happened to be right. The whole
    point of this module is that the port is NOT assumed, so a silent fallback to the
    assumption is the failure mode it exists to prevent.

    Deleting only our own stamp makes the operation safe to run from any daemon.
    """
    try:
        data = json.loads(ENDPOINT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # nothing published, or unreadable — nothing of ours to remove
    if data.get("pid") != os.getpid():
        return  # another daemon owns this entry; leave it alone
    try:
        ENDPOINT_FILE.unlink()
    except OSError:
        pass
