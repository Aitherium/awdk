"""owner_steer -- the owner's voice from the phone, typed into an idle Claude tab.

Design: ``.claude/reference/phone-owner-steering-design.md`` (v2, security reviewed).
Owner ruling 2026-09-28 ("YES GO"): a message PROVEN to be the owner may be typed
into an idle, DISCOVERED Claude Code tab. Every other sender keeps the 09-19 peer path.

WHAT MAKES AN EVENT THE OWNER'S (review 2026-09-28, finding 1)
------------------------------------------------------------------
Not a registry row. Every Claude tab on this host runs as the owner's OS user and
``mcp_stdio.mint_agent_token`` already writes ``~/.aither/harness_tokens.json`` from
each tab, so any principal id / plan a registry row can say, an agent can mint in one
line -- and any plaintext token under ``~/.aither`` an agent can read. The first cut of
this module keyed on principal ``gateway:owner-steer`` + plan ``owner`` and was
forgeable that way.

The proof is now an **Ed25519 owner assertion** the MCP gateway signs, per event, only
after Identity proved the caller is the owner (``lib.security.owner_proof``). The
private seed lives in the vault (``AWSH_OWNER_STEER_SIGNING_SEED``) and is read by the
gateway CONTAINER; this daemon holds only the PUBLIC key, loaded ONCE when the
dispatcher is built (never per request). Nothing on the host is a credential for it:
reading every file under ``~/.aither`` yields the public key, which verifies and cannot
sign. The assertion binds the event id, the target, the actor id, sha256 of the exact
text and a short expiry; an accepted event id is remembered until it expires, so a copy
lifted from the room history cannot be replayed.

Residual, stated: a same-user process that REWRITES the pinned public key (or the
daemon's code) and restarts the daemon can still forge. That is replacing the trust
anchor, not using a credential, and no check inside a same-user daemon can stop it;
minting a token or reading a file no longer does it.

HOW IT REACHES THE TAB (findings 3 and 4)
-----------------------------------------
* :func:`verify_tab_identity` -- right before typing, re-read Claude Code's OWN state
  file ``~/.claude/sessions/<pid>.json`` from disk (never the directory's cached
  snapshot): it must still name the target session, and the live process's creation
  time must match its ``procStart`` within discovery's tolerance. A PID Windows reused
  for a hook's ``cmd.exe`` fails here.
* :func:`type_owner_draft` -- the typing CHILD re-checks the creation time and that the
  image is ``claude``/``node`` immediately before ``AttachConsole``, then types the text
  and SUBMITS it (owner ruling 2026-09-28). Nothing can read what is already in Claude
  Code's input box, so Enter also sends any leftover draft; only the owner can reach
  this path. :func:`owner_submit_enabled` switches it back to a draft the owner sends.
* :func:`sanitize_owner_text` -- one line, no control or format characters, at most
  :data:`OWNER_TEXT_MAX` characters, never starting with ``!`` / ``/`` / ``#``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

#: First-cut registry principal. Kept ONLY so ``mint`` can revoke the row and delete
#: the token file a first-cut mint left behind; nothing grants owner on it any more.
OWNER_STEER_PRINCIPAL_ID = "gateway:owner-steer"
OWNER_STEER_LEGACY_LABEL = "gateway-owner-steer"

#: Registry principal the gateway presents as its TRANSPORT bearer (plan ``agent``).
#: Authority never comes from it: the owner assertion carries that.
GATEWAY_AGENT_PRINCIPAL_ID = "gateway:agent"

#: Longest owner text typed into a console. Mirrors ``autopilot.AUTOPILOT_TEXT_MAX``.
OWNER_TEXT_MAX = 2000

#: Leading characters Claude Code treats as a COMMAND rather than a prompt:
#: ``!`` runs a shell command, ``/`` a slash command (``/clear``, ``/login``...),
#: ``#`` writes to memory (CLAUDE.md). A voice message never needs to start with one.
REJECTED_LEADERS = ("!", "/", "#")

#: Default home of the gateway's agent token file and the pinned owner PUBLIC key.
DEFAULT_TOKEN_DIR = Path.home() / ".aither" / "gateway-daemon"
OWNER_TOKEN_FILENAME = "owner-steer.token"   # first cut only; ``mint`` deletes it
AGENT_TOKEN_FILENAME = "agent.token"
PUBKEY_FILENAME = "owner-steer.pub"
PUBKEY_ENV = "AITHER_OWNER_STEER_PUBKEY"

#: Vault secret (AitherSecrets) holding the 32-byte Ed25519 seed, base64. Read by the
#: gateway container only; never written on the daemon host.
SIGNING_SECRET_NAME = "AWSH_OWNER_STEER_SIGNING_SEED"

#: Where the assertion rides: ``event.payload[OWNER_ASSERTION_FIELD]``.
OWNER_ASSERTION_FIELD = "owner_assertion"
ASSERTION_VERSION = 1
#: Longest lifetime a verifier accepts (exp - iat) and the clock skew it tolerates.
ASSERTION_MAX_TTL = 120
ASSERTION_SKEW = 5

#: Discovery's own identity tolerance (``discovery.PROCSTART_TOLERANCE_SECONDS``).
PROCSTART_TOLERANCE_SECONDS = 10.0
#: Images a Claude Code tab runs as. Anything else behind the PID is not typed into.
CLAUDE_IMAGES = frozenset({"claude.exe", "claude", "node.exe", "node"})

#: Characters folded to a space before anything else: every line/paragraph break.
_LINE_BREAKS = ("\r\n", "\r", "\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e",
                "\x85", " ", " ", "\t")


def sanitize_owner_text(text: Any) -> Tuple[Optional[str], str]:
    """``(clean, "")`` when ``text`` may be typed, else ``(None, reason)``.

    Multi-line text is folded to ONE line (a newline would submit early and the rest
    would arrive as a second prompt); control (Cc), format (Cf: bidi overrides,
    zero-width) and surrogate (Cs) characters are stripped (an ESC could drive the TUI);
    whitespace is collapsed. Then REFUSED, never repaired: empty text, text over
    :data:`OWNER_TEXT_MAX` (silently truncating the owner's words is worse than saying
    so), and text whose first character is in :data:`REJECTED_LEADERS`.
    """
    if not isinstance(text, str):
        return None, "owner text must be a string"
    folded = text
    for brk in _LINE_BREAKS:
        folded = folded.replace(brk, " ")
    kept = "".join(
        ch for ch in folded if unicodedata.category(ch) not in ("Cc", "Cf", "Cs")
    )
    clean = " ".join(kept.split())
    if not clean:
        return None, "owner text is empty after sanitising"
    if len(clean) > OWNER_TEXT_MAX:
        return None, (f"owner text is {len(clean)} characters; the live-typing cap is "
                      f"{OWNER_TEXT_MAX}")
    if clean.startswith(REJECTED_LEADERS):
        return None, (f"owner text starts with {clean[0]!r}, which Claude Code runs as a "
                      "command (! shell, / slash command, # memory); refused")
    return clean, ""


# ── the owner assertion ──────────────────────────────────────────────────────────


def _b64d(value: str) -> bytes:
    raw = (value or "").strip()
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def key_id(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()[:16]


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_claims(claims: Dict[str, Any]) -> bytes:
    """The exact bytes signed. The gateway (mcp_awsh_steer) builds the same bytes."""
    body = {k: v for k, v in claims.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def public_key_from_seed(seed: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)


def sign_owner_assertion(seed: bytes, *, event_id: str, target: str, actor_id: str,
                         text: str, now: Optional[float] = None,
                         ttl: int = 60) -> Dict[str, Any]:
    """Build and sign an assertion (the gateway's half; here for tests and tooling)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    iat = int(time.time() if now is None else now)
    claims: Dict[str, Any] = {
        "v": ASSERTION_VERSION, "kid": key_id(public_key_from_seed(seed)),
        "eid": event_id, "to": target, "actor": actor_id,
        "sha": text_digest(text), "iat": iat, "exp": iat + int(ttl),
    }
    claims["sig"] = _b64e(Ed25519PrivateKey.from_private_bytes(seed).sign(
        canonical_claims(claims)))
    return claims


class OwnerAssertionVerifier:
    """Verifies owner assertions against ONE pinned Ed25519 public key.

    Holds no secret. Remembers every event id it accepted until that assertion
    expires, so the same signed event cannot be delivered twice (the dispatcher's own
    LRU forgets on eviction; this does not need to outlive ``exp``).
    """

    def __init__(self, public_key: bytes, *, now: Callable[[], float] = time.time) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        if len(public_key) != 32:
            raise ValueError("an Ed25519 public key is 32 bytes")
        self._key = Ed25519PublicKey.from_public_bytes(public_key)
        self.kid = key_id(public_key)
        self._now = now
        self._seen: Dict[str, float] = {}
        self._lock = threading.Lock()

    def verify(self, event: Dict[str, Any], target_id: str) -> Tuple[bool, str]:
        """``(True, "")`` only for a fresh, correctly signed assertion for THIS
        event, THIS target, THIS actor and THIS exact text."""
        from cryptography.exceptions import InvalidSignature

        payload = event.get("payload")
        if not isinstance(payload, dict):
            return False, "no payload"
        claims = payload.get(OWNER_ASSERTION_FIELD)
        if not isinstance(claims, dict):
            return False, "no owner assertion"
        text = payload.get("text")
        if not isinstance(text, str):
            return False, "no text"
        event_id = str(event.get("id") or "")
        actor_id = str((event.get("actor") or {}).get("id") or "")
        now = self._now()
        checks = (
            (claims.get("v") == ASSERTION_VERSION, "wrong assertion version"),
            (claims.get("kid") == self.kid, "signed by a key this daemon does not pin"),
            (bool(event_id) and claims.get("eid") == event_id, "event id mismatch"),
            (claims.get("to") == target_id, "target mismatch"),
            (bool(actor_id) and claims.get("actor") == actor_id, "actor mismatch"),
            (claims.get("sha") == text_digest(text), "text does not match the signature"),
        )
        for ok, why in checks:
            if not ok:
                return False, why
        try:
            iat, exp = int(claims.get("iat")), int(claims.get("exp"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False, "malformed assertion"
        if iat > now + ASSERTION_SKEW:
            return False, "assertion issued in the future"
        if exp <= now:
            return False, "assertion expired"
        if exp - iat > ASSERTION_MAX_TTL:
            return False, "assertion lifetime too long"
        try:
            self._key.verify(_b64d(str(claims.get("sig") or "")), canonical_claims(claims))
        except (InvalidSignature, ValueError):
            return False, "bad signature"
        with self._lock:
            for eid in [e for e, until in self._seen.items() if until <= now]:
                del self._seen[eid]
            if event_id in self._seen:
                return False, "assertion already used (replay)"
            self._seen[event_id] = float(exp)
        return True, ""


def load_owner_verifier(env: Optional[Dict[str, str]] = None,
                        path: Optional[Path] = None) -> Optional[OwnerAssertionVerifier]:
    """The pinned public key (``AITHER_OWNER_STEER_PUBKEY``, else
    ``~/.aither/gateway-daemon/owner-steer.pub``) as a verifier, or None.

    None means no event is ever the owner's (fail closed). Called ONCE per daemon.
    """
    env = os.environ if env is None else env
    raw = (env.get(PUBKEY_ENV) or "").strip()
    if not raw:
        try:
            raw = (path or DEFAULT_TOKEN_DIR / PUBKEY_FILENAME).read_text(
                encoding="utf-8").strip()
        except OSError:
            return None
    try:
        return OwnerAssertionVerifier(_b64d(raw))
    except (ValueError, TypeError) as exc:
        sys.stderr.write(f"[owner-steer] pinned public key is unusable ({exc}); "
                         "owner steering is OFF\n")
        return None


def is_owner_steer_event(event: Dict[str, Any], target_id: str,
                         verifier: Optional[OwnerAssertionVerifier]) -> bool:
    """Is this event the OWNER's words for ``target_id``?

    All of: a pinned verifier exists; the daemon stamped the event (it arrived through
    an authenticated ``POST /events``); the actor is human; and the gateway's owner
    assertion verifies (:class:`OwnerAssertionVerifier`). The bearer's principal and
    plan are NOT consulted: any registry row is same-user mintable. Malformed = False.
    """
    if verifier is None:
        return False
    try:
        auth = event.get("auth")
        if not isinstance(auth, dict) or not auth.get("principal"):
            return False
        actor = event.get("actor") or {}
        if not (isinstance(actor, dict) and str(actor.get("kind") or "") == "human"):
            return False
        ok, _why = verifier.verify(event, target_id)
        return ok
    except Exception:  # noqa: BLE001 - a stamp that cannot be judged is no stamp
        return False


# ── the tab's identity, re-proved right before typing ────────────────────────────


def _process_start_time(pid: int) -> Optional[float]:
    from adk.harnesses.discovery import _get_process_start_time

    return _get_process_start_time(int(pid))


def _process_image(pid: int) -> str:
    try:
        import psutil

        return str(psutil.Process(int(pid)).name() or "")
    except Exception:  # noqa: BLE001 - an unreadable image is not a Claude image
        return ""


def _ticks_to_unix(ticks: int) -> float:
    return (int(ticks) - 116444736000000000) / 10000000


def _start_matches(pid: int, proc_start: int,
                   start_time_of: Callable[[int], Optional[float]]) -> Tuple[bool, str]:
    started = start_time_of(pid)
    if started is None:
        return False, f"process {pid} is gone"
    try:
        claimed = _ticks_to_unix(int(proc_start))
    except (TypeError, ValueError, OverflowError):
        return False, "unusable procStart"
    if abs(started - claimed) > PROCSTART_TOLERANCE_SECONDS:
        return False, f"process {pid} is not the tab's process (PID reused)"
    return True, ""


def verify_tab_identity(
    pid: int,
    session_id: str,
    *,
    sessions_dir: Optional[Path] = None,
    start_time_of: Optional[Callable[[int], Optional[float]]] = None,
) -> Tuple[bool, str, Optional[int]]:
    """``(ok, why, procStart)``: is ``pid`` STILL the process of ``session_id``?

    Reads Claude Code's own ``<sessions_dir>/<pid>.json`` fresh from disk (a real
    discovery read, not the directory snapshot), requires it to name ``session_id`` and
    carry a ``procStart``, and requires the live process's creation time to match it.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False, "no usable pid", None
    if pid <= 0:
        return False, "the tab has no recorded process", None
    base = Path(sessions_dir) if sessions_dir else Path.home() / ".claude" / "sessions"
    try:
        state = json.loads((base / f"{pid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, f"no live Claude state for process {pid}", None
    if not isinstance(state, dict) or str(state.get("sessionId") or "") != session_id:
        return False, f"process {pid} no longer hosts that session", None
    try:
        proc_start = int(state.get("procStart"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False, f"process {pid} has no procStart to prove its identity", None
    ok, why = _start_matches(pid, proc_start, start_time_of or _process_start_time)
    if not ok:
        return False, why, None
    return True, "", proc_start


def owner_submit_enabled() -> bool:
    """Submit owner text (default, per the 2026-09-28 ruling) unless switched off.

    Env wins when set (so one process can opt out); else ``owner_submit`` in
    ~/.aither/decisions.json; absent means ON.
    """
    raw = os.environ.get("AITHER_OWNER_STEER_SUBMIT", "").strip().lower()
    if raw:
        return raw not in ("0", "false", "no", "off")
    try:
        data = json.loads((Path.home() / ".aither" / "decisions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(data, dict) or "owner_submit" not in data:
        return True
    return str(data.get("owner_submit")).strip().lower() not in ("0", "false", "no", "off")


def type_owner_draft(
    pid: int,
    text: str,
    proc_start: int,
    *,
    typer: Optional[Callable[..., Tuple[bool, str]]] = None,
    start_time_of: Optional[Callable[[int], Optional[float]]] = None,
    image_of: Optional[Callable[[int], str]] = None,
) -> Tuple[bool, str]:
    """The typing CHILD's half: re-prove identity, then type and SUBMIT.

    Immediately before ``AttachConsole`` the process's creation time must still match
    ``proc_start`` and its image must be a Claude Code image.

    Submitted by default -- owner ruling 2026-09-28: "may it type straight into an idle
    Claude tab? YES". An Enter also submits whatever the tab's input box already held,
    and nothing can read that box; but only the OWNER can put text there now (agents
    never reach this path without a signed owner assertion), so a leftover draft is the
    owner's own. ``owner_submit: "0"`` in ~/.aither/decisions.json (or
    AITHER_OWNER_STEER_SUBMIT=0) falls back to typing a draft the owner sends with one key.
    """
    ok, why = _start_matches(pid, proc_start, start_time_of or _process_start_time)
    if not ok:
        return False, why
    image = (image_of or _process_image)(int(pid)).strip().lower()
    if image not in CLAUDE_IMAGES:
        return False, f"process {pid} is {image or 'unknown'}, not a Claude Code tab"
    if typer is None:
        from adk.decisions.terminal import type_into_console

        typer = type_into_console
    return typer(int(pid), text, submit=owner_submit_enabled())


# ── minting the gateway's transport principal ────────────────────────────────────


def _write_token(target: Path, token: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    os.replace(tmp, target)


def mint_gateway_principals(
    out_dir: Optional[Path] = None,
    *,
    registry: Optional[Path] = None,
    ttl_days: int = 30,
) -> Dict[str, Path]:
    """Mint the gateway's AGENT transport principal and write its token (0600).

    Returns ``{"agent": path}``. No owner token is minted or written: owner authority
    is the per-event signed assertion. A first-cut owner-steer registry row and token
    file are REVOKED and DELETED on the way through.
    """
    from adk.harnesses.daemon import mint_scoped_token, revoke_label
    from adk.harnesses.mcp_stdio import AGENT_TOKEN_PATHS

    base = Path(out_dir) if out_dir else DEFAULT_TOKEN_DIR
    revoke_label(OWNER_STEER_LEGACY_LABEL, path=registry)
    legacy = base / OWNER_TOKEN_FILENAME
    try:
        legacy.unlink(missing_ok=True)
    except OSError as exc:
        sys.stderr.write(f"[owner-steer] could not delete the first-cut owner token "
                         f"{legacy}: {exc}; delete it by hand\n")
    agent_token = mint_scoped_token(
        GATEWAY_AGENT_PRINCIPAL_ID, paths=AGENT_TOKEN_PATHS, plan="agent",
        ttl_days=ttl_days, path=registry, label="gateway-agent",
    )
    agent_path = base / AGENT_TOKEN_FILENAME
    _write_token(agent_path, agent_token)
    return {"agent": agent_path}


def pin_public_key(public_key_b64: str, out_dir: Optional[Path] = None) -> Path:
    """Write the gateway's PUBLIC key where :func:`load_owner_verifier` reads it."""
    raw = _b64d(public_key_b64)
    OwnerAssertionVerifier(raw)  # refuse a malformed key before writing it
    target = (Path(out_dir) if out_dir else DEFAULT_TOKEN_DIR) / PUBKEY_FILENAME
    _write_token(target, _b64e(raw) + "\n")
    return target


def _type_draft_child() -> int:
    """``type-draft``: read {pid, proc_start, text} JSON on stdin, print [ok, why]."""
    try:
        req = json.loads(sys.stdin.read() or "{}")
        ok, why = type_owner_draft(int(req["pid"]), str(req["text"]), int(req["proc_start"]))
    except Exception as exc:  # noqa: BLE001 - reported, never raised past the child
        ok, why = False, f"type-draft failed: {type(exc).__name__}"
    print(json.dumps([bool(ok), str(why)]))
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m adk.harnesses.owner_steer",
        description="Owner phone steering: mint the gateway's transport principal, pin "
                    "the gateway's owner-assertion public key.")
    sub = parser.add_subparsers(dest="cmd")
    mint = sub.add_parser("mint", help="mint the gateway agent principal; revoke the "
                                       "first-cut owner token")
    mint.add_argument("--out", default=str(DEFAULT_TOKEN_DIR))
    mint.add_argument("--ttl-days", type=int, default=30)
    pin = sub.add_parser("pin", help="pin the gateway's owner-assertion PUBLIC key")
    pin.add_argument("public_key")
    pin.add_argument("--out", default=str(DEFAULT_TOKEN_DIR))
    sub.add_parser("type-draft", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.cmd == "type-draft":
        return _type_draft_child()
    if args.cmd == "pin":
        print(f"pinned: {pin_public_key(args.public_key, Path(args.out))} "
              "(restart the daemon to load it)")
        return 0
    if args.cmd != "mint":
        parser.print_help()
        return 2
    paths = mint_gateway_principals(Path(args.out), ttl_days=args.ttl_days)
    # Paths only -- never the token values.
    for kind, path in paths.items():
        print(f"{kind}: {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
