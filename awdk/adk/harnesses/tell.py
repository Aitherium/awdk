"""``adk harness tell`` — the semantic DRIVE layer of the peer-integration
program (v0.3).

A human colleague who wants a peer's attention does three things: taps them
on the shoulder (a message on the hallway channel), waits for them to look
up, and talks. This command is that loop for Claude sessions:

  1. RESOLVE — the target is a NICK (``claude-<first8>`` from the presence
     plane), a Claude session uuid, or a prefix of either. It must map to
     EXACTLY ONE daemon session via the session registry (``id`` or
     ``harness_session_id``) — never guessed: zero matches lists the
     candidates, many matches demands a longer prefix.
  2. RECORD — the drive is posted to #claude-sessions as an envelope
     (kind MESSAGE, payload ``{type: "drive", target, correlation_id}``),
     so the hallway keeps the record even if delivery fails.
  3. DELIVER — ``POST /sessions/{id}/input`` writes the text into the live
     PTY, exactly like typing.
  4. ECHO (``--await N``) — the session's reply streams back to the channel
     as a FINDING envelope tied by correlation_id.

The daemon can only type into sessions IT started (``adk harness new``) —
a live session that is not harness-managed resolves cleanly and fails with
that sentence, never with a guess.

Relay transport: the host-side rule from the program doc (D-2217) — the
host-published loopback ``https://127.0.0.1:8205``; the wire nick comes from
the relay's own ``POST /v1/auth/relay-token`` mint (decoding one's OWN
freshly-minted token is identity lookup, not impersonation). This mirrors
the aither-presence hook by necessity — the hook lives outside the repo in
``~/.claude/hooks``, and a CLI that imports host config is a CLI that breaks
on every other machine.
"""
from __future__ import annotations

import base64
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

RELAY_DEFAULT = "https://127.0.0.1:8205"
CHANNEL = "claude-sessions"
CA_BUNDLE = Path.home() / ".aither" / "aithernet-ca-bundle.pem"
BEARER_FILE = Path.home() / ".aither" / "session-bearer"
_FENCE_RE = re.compile(r"```awrelay\n(?P<body>.*?)\n```", re.DOTALL)


# ── resolution ──────────────────────────────────────────────────────────────

def resolve_target(target: str, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Daemon sessions matching ``target`` as id or harness_session_id prefix.

    Prefix matching on BOTH keys, case-insensitive, never substring: a nick
    ``claude-b8c0`` matches a harness_session_id ``b8c03004-...`` (the nick's
    first8 is the uuid's first8) and a bare ``b8c0`` matches the same thing.
    The nick strip lives HERE, not in the command, so every caller gets the
    same semantics. Returns the matches — the CALLER enforces exactly-one,
    because the error message is a user contract (list candidates / demand a
    longer prefix).
    """
    if target.startswith("claude-"):
        target = target[len("claude-"):]
    wanted = target.strip().lower()
    if not wanted:
        return []
    out: list[dict[str, Any]] = []
    for session in sessions:
        for key in ("id", "harness_session_id"):
            value = str(session.get(key) or "").strip().lower()
            if value and value.startswith(wanted):
                out.append(session)
                break
    return out


def _nick_to_uuid(target: str) -> str:
    """``claude-<first8>`` -> the uuid prefix it abbreviates.

    resolve_target also strips nicks; this helper exists for the relay
    record's payload, which should carry the STRIPPED form."""
    if target.startswith("claude-"):
        return target[len("claude-"):]
    return target


# ── relay transport (host loopback; mirrors the aither-presence hook) ───────

def _ssl_context() -> ssl.SSLContext:
    if CA_BUNDLE.is_file():
        return ssl.create_default_context(cafile=str(CA_BUNDLE))
    return ssl._create_unverified_context()  # loopback only; bundle missing


def _relay_url() -> str:
    return (os.environ.get("AITHERRELAY_URL") or RELAY_DEFAULT).rstrip("/")


def _token() -> Optional[str]:
    for var in ("AITHER_SESSION_BEARER", "AITHER_RELAY_TOKEN"):
        value = os.environ.get(var)
        if value:
            return value.strip()
    try:
        value = BEARER_FILE.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def _request(
    method: str, path: str, body: "dict | None" = None,
) -> "tuple[int, str]":
    url = _relay_url() + path
    headers = {"Content-Type": "application/json"}
    token = _token()
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10, context=_ssl_context()) as resp:
            return resp.status, resp.read(8192).decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(8192).decode(errors="replace")
    except Exception as exc:  # noqa: BLE001 - reported, the caller decides
        return 0, f"{type(exc).__name__}: {exc}"


def _identity_nick() -> str:
    """The authenticated identity's relay nick via the relay's own mint."""
    status, body = _request("POST", "/v1/auth/relay-token", {})
    if status != 200:
        raise RuntimeError(f"relay-token -> {status} {body[:160]}")
    try:
        token = (json.loads(body).get("relay_token")
                 or json.loads(body).get("token"))
    except ValueError as exc:
        raise RuntimeError(f"relay-token returned non-JSON: {exc}") from exc
    if not token:
        raise RuntimeError("relay-token response carried no token")
    parts = str(token).split(".")
    if len(parts) != 3:
        raise RuntimeError("relay token is not a JWT")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731 - jose-style pad, 3 uses
    claims = json.loads(base64.urlsafe_b64decode(pad(parts[1])))
    nick = claims.get("nick")
    if not nick:
        raise RuntimeError("relay token carries no nick claim")
    return str(nick)


def post_envelope(kind: str, text: str, payload: dict[str, Any],
                  correlation_id: Optional[str] = None) -> str:
    """Post an awrelay envelope to the presence channel. Returns the message
    id or raises — the hallway record is part of the loop, not decoration."""
    wire_nick = _identity_nick()
    sender = "adk-tell"
    now = datetime.now(timezone.utc).isoformat()
    envelope = {
        "kind": kind.lower(), "sender": sender,
        "payload": payload, "correlation_id": correlation_id,
        "sent_at": now,
    }
    content = text + "\n```awrelay\n" + json.dumps(envelope) + "\n```"
    status, body = _request("POST", f"/v1/channels/{CHANNEL}/messages", {
        "channel": f"#{CHANNEL}", "nick": wire_nick,
        "content": content, "agent": False,
    })
    if status not in (200, 201):
        raise RuntimeError(f"message post -> {status} {body[:160]}")
    try:
        return str(json.loads(body).get("id", ""))
    except ValueError:
        return ""


def presence_nick_of(session_id: str) -> Optional[str]:
    """The nick a live session registered with, from the presence channel.

    Used by --dry-run and error messages to talk to the human in nick terms.
    None = no envelope found; RAISES when the relay is unreachable — the two
    are different statements and the caller says which.
    """
    status, body = _request("GET", f"/v1/channels/{CHANNEL}/messages?limit=300")
    if status != 200:
        raise RuntimeError(f"presence lookup -> {status}")
    try:
        messages = json.loads(body).get("messages") or []
    except ValueError:
        return None
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str):
            continue
        match = _FENCE_RE.search(content)
        if not match:
            continue
        try:
            envelope = json.loads(match.group("body"))
        except ValueError:
            continue
        payload = envelope.get("payload") or {}
        if payload.get("type") == "presence" and payload.get("session_id") == session_id:
            return str(envelope.get("sender") or message.get("nick") or "")
    return None


# ── the command ─────────────────────────────────────────────────────────────

def cmd_tell(args: Any) -> int:
    from adk.harnesses.cli import _die_if_down
    from adk.harnesses.cli import _request as daemon_request

    target = _nick_to_uuid(getattr(args, "target", "") or "")
    text = getattr(args, "text", "") or ""
    if not target or not text:
        print("usage: adk harness tell <nick|session-id-prefix> \"<text>\"",
              file=sys.stderr)
        return 2

    status, payload = daemon_request(args, "/sessions")
    _die_if_down(status, payload)
    matches = resolve_target(target, payload.get("sessions") or [])
    if not matches:
        print(f"no harness-managed session matches '{target}'", file=sys.stderr)
        print("live sessions the daemon knows:", file=sys.stderr)
        for session in payload.get("sessions") or []:
            print(f"  {session['id'][:12]}  {session.get('title') or ''}",
                  file=sys.stderr)
        print("(a live session not listed here was not started by the daemon "
              "— `adk harness new` — and cannot be typed into)", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"'{target}' matches {len(matches)} sessions — use a longer prefix:",
              file=sys.stderr)
        for session in matches:
            print(f"  {session['id'][:12]}  {session.get('title') or ''}",
                  file=sys.stderr)
        return 1

    session = matches[0]
    session_id = session["id"]
    correlation = uuid.uuid4().hex[:12]
    presence = "unknown"
    try:
        hs_id = str(session.get("harness_session_id") or "")
        if hs_id:
            nick = presence_nick_of(hs_id)
            presence = (f"claude-{hs_id[:8]} on the relay"
                        if nick else f"no presence envelope for {hs_id[:8]}")
    except Exception as exc:  # noqa: BLE001 - enrichment, never the gate
        presence = f"relay unreachable ({type(exc).__name__})"
    if getattr(args, "dry_run", False):
        print(f"would tell {session_id[:12]} ({session.get('title') or ''})")
        print(f"  presence: {presence}")
        print(f"  text: {text}")
        print(f"  relay record: REQUEST drive on #{CHANNEL} "
              f"(correlation {correlation})")
        print(f"  delivery: POST /sessions/{session_id}/input")
        return 0

    try:
        post_envelope(
            "message", f"drive -> {target[:12]}: {text[:80]}",
            {"type": "drive", "target": target, "text": text,
             "session_id": session_id, "correlation_id": correlation},
            correlation_id=correlation,
        )
    except Exception as exc:  # noqa: BLE001 - reported; delivery still proceeds
        print(f"relay record failed (delivery continues): {exc}", file=sys.stderr)

    status, payload = daemon_request(
        args, f"/sessions/{session_id}/input", "POST", {"text": text})
    _die_if_down(status, payload)

    await_secs = int(getattr(args, "await", 0) or 0)
    if await_secs > 0:
        _echo_reply(args, session, correlation, await_secs)
    return 0


def _echo_reply(args: Any, session: dict[str, Any], correlation: str,
                await_secs: int) -> None:
    """Poll the event stream and post the reply summary as a FINDING."""
    from adk.harnesses.cli import _request as daemon_request

    session_id = session["id"]
    since = int(session.get("last_seq") or 0)
    deadline = time.time() + await_secs
    chunks: list[str] = []
    while time.time() < deadline:
        status, payload = daemon_request(
            args, f"/sessions/{session_id}/events?since={since}")
        if status == 0:
            break
        if status >= 400:
            return
        for event in payload.get("events") or []:
            since = event["seq"]
            if event["kind"] == "text.delta":
                chunks.append(event.get("text") or "")
            elif event["kind"] == "turn.completed":
                reply = "".join(chunks).strip()
                if reply:
                    summary = reply[:400] + ("…" if len(reply) > 400 else "")
                    try:
                        post_envelope(
                            "finding", f"reply from {session_id[:12]}: "
                            f"{summary[:120]}",
                            {"type": "drive_reply", "session_id": session_id,
                             "reply": reply[:2000]},
                            correlation_id=correlation,
                        )
                    except Exception as exc:  # noqa: BLE001
                        print(f"relay reply echo failed: {exc}", file=sys.stderr)
                return
        time.sleep(0.4)
    print(f"(no completed turn within {await_secs}s — reply not echoed)",
          file=sys.stderr)
