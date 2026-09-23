"""Autolink: the harness daemon keeps this machine reachable from your phone by itself.

`adk rc` is the manual verb: sign in, enrol, hold the reverse link. It only works
while someone keeps a terminal open, and the first sign-in shows its code on a
desktop screen nobody is looking at. Autolink is the same three steps run INSIDE
the harness daemon, which is already supervised:

1. No identity on this box -> start the device flow and hand the approve link to
   the owner's phone (a decision card through ``awask`` when it is installed, and
   ``~/.aither/pending-signin.json`` either way). One tap there signs this box in.
   A code that expires is replaced by a fresh one, so there is never a dead link.
2. Signed in -> mint a scoped harness token and enrol with the reverse link.
3. Hold it. The link reconnects on its own; if enrolment fails, retry with backoff.

OPT-IN. A machine must not phone home just because awdk is installed, so autolink
runs only when ``AITHER_AUTOLINK=1`` (or ``AITHER_FLEET_ENROLL=1``) is set in the
daemon's environment. The root harness token never leaves this process: only the
path-scoped link token is advertised (see ``adk.rc``).
"""

from __future__ import annotations

__all__ = ["autolink_enabled", "start_autolink", "announce_code"]

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("adk.autolink")

PENDING_PATH = Path.home() / ".aither" / "pending-signin.json"
RETRY_S = (30, 60, 120, 300)


def autolink_enabled() -> bool:
    """Opt-in only: an installed package never enrols a machine on its own."""
    for name in ("AITHER_AUTOLINK", "AITHER_FLEET_ENROLL"):
        if (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


def one_tap_uri(uri: str, user_code: str) -> str:
    """The approve link with the code FILLED IN.

    The IdP's /link page reads ``user_code`` (``code`` is reserved for its own
    sign-in round trip), but the device flow hands out ``/link?code=...`` -- which
    opens an EMPTY code box. Measured 2026-09-23 from the owner's phone.
    """
    base = (uri or "").split("?", 1)[0]
    return f"{base}?user_code={user_code}" if base else uri


def _pending() -> dict:
    try:
        data = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def withdraw_card(note: str) -> None:
    """Cancel the card for the previous code: one live card, never a pile."""
    card = str(_pending().get("card") or "")
    awask = shutil.which("awask")
    if not card or not awask:
        return
    try:
        subprocess.run([awask, "cancel", card, "--note", note], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("autolink: could not withdraw %s: %s", card, exc)


def announce_code(user_code: str, uri: str, expires_in: int) -> None:
    """Put the approve link where the owner will see it: their phone, then a file."""
    host = socket.gethostname()
    uri = one_tap_uri(uri, user_code)
    expires_at = time.time() + max(60, int(expires_in or 900))
    withdraw_card("superseded by a fresh code")
    record = {"host": host, "user_code": user_code, "verification_uri": uri,
              "expires_at": expires_at}

    def _save() -> None:
        try:
            PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
            PENDING_PATH.write_text(json.dumps(record), encoding="utf-8")
        except OSError as exc:
            log.warning("autolink: could not write %s: %s", PENDING_PATH, exc)

    _save()

    awask = shutil.which("awask")
    if not awask:
        log.warning("autolink: approve %s at %s (code %s)", host, uri, user_code)
        return
    minutes = max(1, int((expires_in or 900) // 60))
    cmd = [
        awask, "ask", "--kind", "decision", "--urgency", "high",
        "--option", "ok:Done:nothing to choose -- the link does the work",
        "--default", "ok", "--deadline", f"{minutes}m", "--no-prompt",
        "--summary", f"Open this link to connect {host}: {uri}",
        "--detail", (
            f"One tap: {uri} -- signed in, it connects {host} at once; otherwise sign "
            f"in first and it connects on the way back. Its live agent sessions then "
            f"show at https://api.aitherium.com/code. Expires in {minutes} min; a "
            f"replacement card follows automatically and this one is withdrawn."
        ),
        f"Open the link to connect {host}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30)
        match = re.search(r"\bd-[a-z0-9]{3,8}\b", (proc.stdout or "") + (proc.stderr or ""))
        if match:
            record["card"] = match.group(0)
            _save()
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("autolink: awask card failed (%s); approve at %s", exc, uri)


def _signed_in() -> bool:
    from adk.rc import _signed_in as rc_signed_in

    return rc_signed_in()


def _sign_in() -> bool:
    """One device-flow round. True when this box now holds a real identity."""
    from adk.cli import (
        _DEFAULT_IDENTITY_URL,
        _device_flow_login,
        _resolve_identity_url,
        complete_device_login,
    )

    base = (os.environ.get("AITHER_PORTAL_URL") or _DEFAULT_IDENTITY_URL).rstrip("/")
    identity_url = _resolve_identity_url(base)
    try:
        result = _device_flow_login(
            identity_url, client_name=f"adk-autolink ({socket.gethostname()})",
            on_code=announce_code,
            # The IdP emails this account the one-tap link: open it on the phone.
            email=(os.environ.get("AITHER_OWNER_EMAIL") or "").strip(),
        )
        complete_device_login(base, result)
    except (RuntimeError, OSError, KeyError, ValueError) as exc:
        log.info("autolink: sign-in round ended: %s", exc)
        return False
    finally:
        withdraw_card("closed: sign-in round ended")
        try:
            PENDING_PATH.unlink()
        except OSError:
            log.debug("autolink: no pending sign-in file to clear")
    return _signed_in()


async def _hold(harness_url: str) -> bool:
    """Enrol with the reverse link and hold it. False when enrolment was refused."""
    from adk.fleet_enroll import active_node_link, enroll_on_boot
    from adk.harnesses.daemon import mint_scoped_token

    token = mint_scoped_token(f"node:{os.environ.get('AITHER_NODE_ID') or 'this-device'}")
    os.environ.setdefault("AITHER_FLEET_ENROLL", "1")
    result = await enroll_on_boot(
        enable_heartbeat=True, inference_url=None, node_class="laptop",
        start_link=True, harness_url=harness_url, harness_token=token,
    )
    if not result.get("enrolled"):
        log.warning("autolink: enrolment refused: %s",
                    result.get("http_status") or result.get("error"))
        return False
    log.info("autolink: enrolled as %s; holding the link", result.get("node_id"))
    last: Optional[str] = None
    while True:
        await asyncio.sleep(30)
        link = active_node_link()
        now = link.reach_kind() if link is not None else "none"
        if now != last:
            log.info("autolink: reach %s", now)
            last = now


def _run(harness_url: str) -> None:
    attempt = 0
    while True:
        try:
            if not _signed_in() and not _sign_in():
                time.sleep(5)  # a fresh code, right away: never leave a dead link
                continue
            asyncio.run(_hold(harness_url))
        except Exception as exc:  # noqa: BLE001 -- the daemon must outlive this thread's bugs
            log.warning("autolink: %s: %s", type(exc).__name__, exc)
        time.sleep(RETRY_S[min(attempt, len(RETRY_S) - 1)])
        attempt += 1


def start_autolink(harness_url: str) -> Optional[threading.Thread]:
    """Start the autolink thread when opted in. Returns it, or None when off."""
    if not autolink_enabled():
        return None
    t = threading.Thread(target=_run, args=(harness_url,), name="adk-autolink", daemon=True)
    t.start()
    return t
