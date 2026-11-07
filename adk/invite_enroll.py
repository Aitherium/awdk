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


# ── adk join --workspace: "Join the Aitherium workspace" ─────────────────────
#
# One GUI-shaped flow per client (owner rule: browser SSO, never a pasted command):
#   1. sign in      the machine login if one exists, else the RFC 8628 browser device
#                   flow (the browser page does the sign-in; nothing is typed here)
#   2. device       POST /api/v1/me/device/enroll-self -- the existing device plane,
#                   caller-scoped; skipped when this machine already holds a live
#                   device for the same member (heartbeat 200), so a rerun is a no-op
#   3. bearer       ~/.aither/session-bearer (mode 600) for the MCP bridge
#   4. settings     awsettings config keyed by the signed-in user id (per-member
#                   namespaces), then a pull of the member's settings domains
#   5. room         the Company Room (Relay Room) -- Genesis enrols the member into
#                   it on invite accept; the client records the link

JOIN_ENV = "AITHER_JOIN_URL"
#: Veil's authenticated pass-through to Genesis (forwards the caller's bearer); the
#: same door awnode sync uses. AITHER_JOIN_URL wins (self-hosted Genesis).
DEFAULT_JOIN_BASE = "https://api.aitherium.com/api/bridge/genesis"
JOIN_STATE_FILE = Path.home() / ".aither" / "workspace_join.json"
ENROLL_SELF = "/api/v1/me/device/enroll-self"
HEARTBEAT = "/api/v1/me/device/heartbeat"
ROOM_URL = "https://app.aitherium.com/companyroom"
SETTINGS_HUB = "https://api.aitherium.com/api/v1/settings"
#: Domains a joined member pulls. Credentials are dropped by NAME inside awsettings.
MEMBER_DOMAINS = ("claude", "desk", "mods")


def join_base() -> str:
    return (os.environ.get(JOIN_ENV) or DEFAULT_JOIN_BASE).strip().rstrip("/")


def _write_private_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError as exc:  # Windows: the profile directory is already owner-only
        log.debug("join state file: chmod skipped (%s)", type(exc).__name__)
    os.replace(tmp, path)


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _browser_sign_in(workspace: str) -> Tuple[str, Dict[str, Any]]:
    """(bearer, user) from the machine login, else the browser device flow."""
    from adk import credentials

    existing = credentials.user_bearer()
    if existing:
        return existing, {}
    from adk.cli import _DEFAULT_IDENTITY_URL, _device_flow_login, _resolve_identity_url

    issuer = _resolve_identity_url(os.environ.get("AITHER_PORTAL_URL") or _DEFAULT_IDENTITY_URL)
    result = _device_flow_login(issuer, client_name="adk-join")
    token = str(result.get("access_token") or "")
    user = result.get("user") if isinstance(result.get("user"), dict) else {}
    if token:
        credentials.save_login(issuer, token, result.get("expires_at") or "",
                               str(result.get("token_type") or "session"), user,
                               extra={"workspace_id": workspace} if workspace else None)
    return token, user


def _whoami(get: Callable[[str], Any]) -> str:
    """The signed-in user id, read from the platform (never from a local file)."""
    for path in ("/api/v1/me", "/auth/me"):
        try:
            r = get(path)
        except Exception:  # noqa: BLE001 -- try the next door
            continue
        if getattr(r, "status_code", 0) == 200:
            try:
                body = r.json()
            except ValueError:
                continue
            uid = str((body or {}).get("user_id") or (body or {}).get("id") or "")
            if uid:
                return uid
    return ""


def _write_member_settings(uid: str, bearer_file: Optional[Path]) -> Optional[str]:
    """awsettings config for THIS member. Returns the config path, or None."""
    try:
        from awsettings import config as aws_config
    except ImportError:
        return None
    from adk import credentials

    try:
        path = aws_config.save({
            "member": uid,
            "url": aws_config.get("AWSETTINGS_URL") or SETTINGS_HUB,
            "token_file": str(bearer_file or credentials.BEARER_FILE),
        })
    except ValueError:  # an awsettings older than per-member keys: never write unscoped
        log.info("join: awsettings has no member key; upgrade awsettings to sync")
        return None
    return str(path)


def _pull_member_settings(run: Callable[..., Any]) -> int:
    """``awsettings --domain <d> pull`` for each member domain; returns how many passed."""
    ok = 0
    for dom in MEMBER_DOMAINS:
        try:
            r = run([sys.executable, "-m", "awsettings", "--quiet", "--domain", dom, "pull"],
                    capture_output=True, text=True, encoding="utf-8", timeout=120)
        except Exception as exc:  # noqa: BLE001 -- a pull failure is reported, never fatal
            log.info("join: settings pull %s failed: %s", dom, type(exc).__name__)
            continue
        ok += int(getattr(r, "returncode", 1) == 0)
    return ok


def run_workspace_join(workspace: str = "", *, label: str = "", client: Any = None,
                       sign_in: Optional[Callable[[str], Tuple[str, Dict[str, Any]]]] = None,
                       run: Optional[Callable[..., Any]] = None,
                       out: Callable[[str], None] = print,
                       state_file: Optional[Path] = None,
                       bearer_file: Optional[Path] = None) -> int:
    """``adk join --workspace [ID]``: browser sign-in, add this device, sync, room.

    Idempotent: a rerun on a joined machine heartbeats the saved device and, while it
    is live, enrolls nothing new.
    """
    import subprocess

    from adk import credentials

    state_file = state_file or JOIN_STATE_FILE
    sign_in = sign_in or _browser_sign_in
    run = run or subprocess.run
    out("Join the Aitherium workspace: signing in through your browser")
    try:
        bearer, user = sign_in(workspace)
    except RuntimeError as exc:
        out(f"x Sign-in did not finish: {exc}")
        return EXIT_FAIL
    if not bearer:
        out("x Sign-in did not finish. Run this again and approve it in the browser.")
        return EXIT_FAIL
    own_client = client is None
    if own_client:
        import httpx

        from adk._tls import tls_verify

        client = httpx.Client(base_url=join_base(), timeout=30.0, verify=tls_verify(),
                              headers={"Authorization": f"Bearer {bearer}"})
    try:
        uid = str((user or {}).get("id") or (user or {}).get("user_id") or "")
        uid = uid or _whoami(client.get)
        state = _load_json(state_file)
        dev = state.get("device") if isinstance(state.get("device"), dict) else {}
        facts = _device_facts(label)
        live = False
        if dev.get("device_token") and dev.get("user_id") == uid:
            tel = {k: v for k, v in facts.items()
                   if k in ("platform", "os_version", "agent_version")}
            r = client.post(HEARTBEAT, json={"token": dev["device_token"], **tel})
            live = r.status_code == 200
        if live:
            out(f"= This device is already joined (device {dev.get('device_id')})")
        else:
            r = client.post(ENROLL_SELF, json=facts)
            if r.status_code != 201:
                out(f"x Device not added: {_words(_detail(r))}")
                return EXIT_FAIL
            reply = r.json()
            dev = {k: reply.get(k) for k in ("device_id", "device_token", "workspace_id",
                                              "profile_kind", "heartbeat_s")}
            dev["user_id"] = uid
            state["device"] = dev
            _write_private_json(state_file, state)
            out(f"+ Device added (device {dev.get('device_id')})")
        if credentials.is_user_token(bearer):
            credentials.write_session_bearer(bearer, bearer_path=bearer_file)
            out("+ MCP session bearer written (owner-only file)")
        cfg = _write_member_settings(uid, bearer_file) if uid else None
        if cfg:
            pulled = _pull_member_settings(run)
            out(f"+ Settings: {pulled}/{len(MEMBER_DOMAINS)} domains pulled for your account")
        else:
            out("- Settings sync skipped (awsettings not installed, or no user id)")
        state.update(room=ROOM_URL, member=uid,
                     workspace_id=dev.get("workspace_id") or workspace)
        _write_private_json(state_file, state)
        out(f"+ Company Room: {ROOM_URL}")
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 -- transport failures are reported, not raised
        out(f"x Could not reach {join_base()}: {type(exc).__name__}")
        return EXIT_FAIL
    finally:
        if own_client:
            client.close()


__all__ = ["API_ENV", "DEFAULT_API_BASE", "DEFAULT_JOIN_BASE", "EXIT_FAIL", "EXIT_OK",
           "EXIT_PENDING", "EXIT_USAGE", "api_base", "join_base", "normalize_secret",
           "run_invite_enroll", "run_workspace_join"]
