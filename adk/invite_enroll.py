"""``adk enroll --invite <code|link token>`` -- join an organisation's device mesh with
an onboarding invite and enroll THIS device under it.

The steps, each answered by the platform as the signed-in account (``adk login``):

    1. peek       what the invite joins and with which role (spends nothing)
    2. human      the short human check, when the invite asks for one
    3. accept     join the organisation's mesh with exactly the invited role
                  ("already a member" is fine: a second PC under the same invite)
    4. device     enroll this device under the invite; the platform stamps the
                  invite and its role on the device and records it in the audit log

The invite secret (the ten-character code, or an ``awb1.`` token from a link) only ever
travels in a request BODY, never a URL, and is never printed in full. The device token
the platform returns is written to ``~/.aither/invite_devices.json`` (owner-only).

Python 3.10 compatible; depends only on httpx.
"""

from __future__ import annotations

import json
import logging
import os
import platform as _platform
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger("adk.invite_enroll")

DEFAULT_API_BASE = "https://api.aitherium.com/api"
API_ENV = "AITHER_INVITES_URL"
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LEN = 10
_TOKEN_RE = re.compile(r"^awb1\.[A-Za-z0-9_.\-]{8,2000}$")
STATE_FILE = Path.home() / ".aither" / "invite_devices.json"
TIMEOUT_S = 75.0  # the human check waits on a judge

#: Exit codes. 0 enrolled · 1 refused/failed · 2 bad input · 3 waiting for approval.
EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_PENDING = 0, 1, 2, 3

_WORDS = {
    "invite_invalid": "That invite code is not valid. Check it, or ask for a new link.",
    "invite_expired": "That invite has expired. Ask your admin for a new link.",
    "invite_used": "That invite has been used up. Ask your admin for a new link.",
    "invite_revoked": "That invite was cancelled. Ask your admin for a new link.",
    "invite_tampered": "That invite failed its integrity check and was refused.",
    "invite_addressed_elsewhere": "That invite is for a different account.",
    "too_many_tries": "Too many wrong codes. Wait an hour and try again.",
    "request_denied": "Your request to join was declined.",
    "human_check_required": "This invite needs the human check first.",
    "not_a_mesh_member": "This account is not a member of that organisation's mesh.",
    "auth_required": "Not signed in. Run `adk login` first.",
}


def normalize_secret(value: str) -> Tuple[str, str]:
    """``(secret, hint)`` for a code (any case, dashes/spaces ok) or an ``awb1.`` token.

    ``hint`` is the last four characters, the only part ever printed. Raises
    ``ValueError`` on anything else, so a typo never reaches the network.
    """
    raw = str(value or "").strip()
    if raw.startswith("awb1."):
        if not _TOKEN_RE.match(raw):
            raise ValueError("that does not look like an invite link token")
        return raw, raw[-4:]
    code = "".join(ch for ch in raw.upper() if ch not in " -")
    if len(code) != CODE_LEN or any(ch not in CODE_ALPHABET for ch in code):
        raise ValueError("an invite code is ten letters and digits, like ABCDE-23456")
    formatted = f"{code[:5]}-{code[5:]}"
    return formatted, code[-4:]


def api_base() -> str:
    return (os.environ.get(API_ENV) or DEFAULT_API_BASE).strip().rstrip("/")


def _bearer() -> str:
    try:
        from adk.connectors import daemon_bearer

        return daemon_bearer()
    except Exception:  # noqa: BLE001 -- no sign-in readable = not signed in
        return ""


def _detail(resp: Any) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 -- a non-JSON refusal is reported by status
        return f"http_{resp.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) and detail else f"http_{resp.status_code}"


def _words(code: str) -> str:
    return _WORDS.get(code, code.replace("_", " "))


def _device_facts(label: str) -> Dict[str, str]:
    try:
        from adk import __version__ as agent_version
    except Exception:  # noqa: BLE001 -- the version is reported, never required
        agent_version = ""
    system = _platform.system().lower()
    plat = {"windows": "windows", "darwin": "macos", "linux": "linux"}.get(system, "other")
    host = _platform.node()[:60]
    return {k: v for k, v in {
        "label": (label or host)[:60], "model": host, "platform": plat,
        "os_version": _platform.release()[:64], "agent_version": str(agent_version)[:64],
    }.items() if v}


def _save_device(invite_id: str, reply: Dict[str, Any], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[invite_id] = {k: reply.get(k) for k in (
        "device_id", "device_token", "workspace_id", "tenant_id", "device_role",
        "network", "heartbeat_s")}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError as exc:  # Windows: the profile directory is already owner-only
        log.debug("invite device file: chmod skipped (%s)", type(exc).__name__)
    os.replace(tmp, path)


def _ask_human_check(post: Callable[[str, Dict[str, Any]], Any], secret: str,
                     ask: Callable[[str], str], out: Callable[[str], None]
                     ) -> Optional[str]:
    """Run the human check. Returns the attestation, or None (already reported)."""
    r = post("/invites/human/challenge", {"invite": secret})
    if r.status_code != 200:
        out(f"x Human check unavailable: {_words(_detail(r))}")
        return None
    data = r.json()
    out("This invite asks for a short human check. Answer in your own words.")
    answers: Dict[str, str] = {}
    for ch in data.get("challenges") or []:
        out("")
        out(str(ch.get("prompt") or ""))
        answers[str(ch.get("id"))] = ask("> ").strip()
    r = post("/invites/human/answer", {"invite": secret, "set_id": data.get("set_id", ""),
                                       "answers": answers})
    if r.status_code != 200:
        out(f"x Human check failed: {_words(_detail(r))}")
        return None
    verdict = r.json()
    if not verdict.get("passed"):
        out(f"x {verdict.get('reason') or 'Those answers did not pass. Run it again.'}")
        return None
    return str(verdict.get("attestation") or "")


def run_invite_enroll(invite: str, *, label: str = "", client: Any = None,
                      ask: Callable[[str], str] = input,
                      out: Callable[[str], None] = print,
                      state_file: Path = STATE_FILE, assume_yes: bool = False,
                      interactive: Optional[bool] = None) -> int:
    """Peek -> confirm -> (human check) -> accept -> enroll this device.

    The confirmation names the organisation the CODE belongs to, read from the
    server, not the page the link was opened on: a link on one org's setup page
    can carry another org's code. Unattended runs must pass ``assume_yes``.
    """
    try:
        secret, hint = normalize_secret(invite)
    except ValueError as exc:
        out(f"x {exc}")
        return EXIT_USAGE
    bearer = _bearer()
    if not bearer:
        out("x Not signed in. Run `adk login`, then run this again.")
        return EXIT_FAIL
    own_client = client is None
    if own_client:
        import httpx

        from adk._tls import tls_verify

        client = httpx.Client(base_url=api_base(), timeout=TIMEOUT_S, verify=tls_verify(),
                              headers={"Authorization": f"Bearer {bearer}"})

    def post(path: str, body: Dict[str, Any]) -> Any:
        return client.post(path, json=body)

    try:
        out(f"Invite ...{hint}: checking")
        r = post("/invites/peek", {"invite": secret})
        if r.status_code != 200:
            out(f"x {_words(_detail(r))}")
            return EXIT_FAIL
        view = r.json()
        network = str(view.get("network") or "the organisation")
        role = str(view.get("role") or "")
        if view.get("target") != "mesh":
            out("x That invite is not a device onboarding invite. Open its link in a browser.")
            return EXIT_FAIL
        out(f"Joining {network} as {role}")
        if not assume_yes:
            if interactive is None:
                interactive = bool(sys.stdin and sys.stdin.isatty())
            if not interactive:
                out(f"x Not confirmed: this joins {network}. Pass --yes to run unattended.")
                return EXIT_USAGE
            answer = ask(f"Enroll this device in {network} as {role}? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                out("Not enrolled.")
                return EXIT_FAIL
        # Always redeem, member or not: the server records an existing member's
        # redemption without touching their role, and the device step admits only
        # someone who redeemed this invite.
        attestation = ""
        if view.get("requires_human"):
            if not sys.stdin or not sys.stdin.isatty():
                out("x This invite needs a human check; run this in a terminal.")
                return EXIT_FAIL
            got = _ask_human_check(post, secret, ask, out)
            if got is None:
                return EXIT_FAIL
            attestation = got
        r = post("/invites/accept", {"invite": secret, "attestation": attestation})
        detail = "" if r.status_code == 200 else _detail(r)
        if r.status_code == 200 and r.json().get("status") == "pending_approval":
            out(f"Waiting for an admin of {network} to approve you. Run the same "
                "command again once they have.")
            return EXIT_PENDING
        if r.status_code != 200 and detail != "already_member":
            out(f"x {_words(detail)}")
            return EXIT_FAIL
        body = {"invite_id": str(view.get("invite_id") or ""), **_device_facts(label)}
        r = post("/invites/device", body)
        if r.status_code != 201:
            out(f"x Device not enrolled: {_words(_detail(r))}")
            return EXIT_FAIL
        reply = r.json()
        _save_device(body["invite_id"], reply, state_file)
        out(f"+ This device is enrolled in {reply.get('network') or network} "
            f"as {reply.get('device_role') or role} (device {reply.get('device_id')})")
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 -- transport failures are reported, not raised
        out(f"x Could not reach {api_base()}: {type(exc).__name__}")
        return EXIT_FAIL
    finally:
        if own_client:
            client.close()


__all__ = ["API_ENV", "DEFAULT_API_BASE", "EXIT_FAIL", "EXIT_OK", "EXIT_PENDING",
           "EXIT_USAGE", "api_base", "normalize_secret", "run_invite_enroll"]
