"""The outbound reverse link a device holds open to AitherTunnel.

WHY THIS EXISTS
`adk enroll` puts a device in the fleet registry and identity hands the owner a
`public_url` of the form `https://gateway.aitherium.com/nodes/{node_id}`. That URL
is served by AitherTunnel's reverse proxy, which forwards to the node over
WireGuard. **A phone has no `wg` module and cannot get one**, so for the device
class the whole feature was built for, the advertised address could never resolve.

The honest transport for a machine that can only make OUTBOUND connections is an
outbound connection it holds open. This module is that connection: a WebSocket to
``wss://tunnel.aitherium.com/tunnel/nodes/{node_id}/attach``, authenticated with
the device's own bearer, over which the tunnel multiplexes HTTP requests that this
process forwards to the node's LOCAL inference server.

WireGuard stays the default wherever `wg` exists -- it is faster and it is a real
network. This is the fallback, not the replacement.

cloudflared was REJECTED for this lane: it needs a per-device Cloudflare tunnel
token, which couples every customer device to our Cloudflare account and makes
"remove this device" an operation in someone else's dashboard.

THE SECURITY PROPERTY THAT MATTERS
This process is one end of a socket whose other end is on the public internet, and
it forwards to the customer's own loopback. So it enforces its OWN allowlist --
:data:`NODE_LINK_ALLOWED_PATHS` -- and never forwards a path the tunnel merely
said was fine. The tunnel enforces the same list. One side trusting the other is
exactly how a reverse link becomes an SSRF primitive aimed at the device's
loopback, where `adk` also runs a harness daemon and (on a dev box) a dozen other
services.

The link carries NO credential outward: the bearer goes in the handshake and
nowhere else, and the request headers the tunnel sends are re-filtered here.
"""

from __future__ import annotations

__all__ = [
    "NodeLink",
    "NODE_LINK_ALLOWED_PATHS",
    "HARNESS_PREFIX",
    "HARNESS_ALLOWED_PREFIXES",
    "HARNESS_REFUSED_SESSION_VERBS",
    "HARNESS_EXACT_ALLOWED",
    "RC_MODES",
    "RUNTIME_TIERS",
    "DEFAULT_RUNTIME_TIER",
    "HELLO_ATTESTATION_FIELDS",
    "detect_harnesses",
    "normalize_runtime_tier",
    "parse_bool_env",
    "resolve_runtime_posture",
    "sign_hello_payload",
    "verify_hello_payload",
    "hello_frame_for",
    "path_target",
    "tunnel_ws_url",
    "default_tunnel_url",
]

import asyncio
import base64
import hmac
import json
import logging
import math
import os
import random
import secrets
import shutil
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import unquote, urlsplit

log = logging.getLogger("adk.node_link")

#: EXACT paths this device will forward to its local inference server. Not
#: prefixes: `v1/` also carries routes awnode serves that have no business on a
#: public socket. The tunnel server enforces the SAME set; if the two ever drift,
#: the narrower one wins, because both sides refuse independently.
NODE_LINK_ALLOWED_PATHS = frozenset({
    "v1/chat/completions",
    "v1/models",
    "health",
    "status",
    "chat",
})

#: Requests under this prefix go to the local harness daemon (`adk rc`), not the
#: inference server. Stripped before forwarding.
HARNESS_PREFIX = "harness/"

#: The tunnel's verdict that the caller is the user who holds this link, relayed
#: to the daemon. Only a link-scoped bearer AND this header together let a remote
#: caller type into a session (`adk.harnesses.daemon._peer_input_text`).
LINK_ACTOR_HEADER = "x-aither-link-actor"
LINK_ACTOR_OWNER = "owner"

#: What a harness-scoped request may reach on the daemon. The daemon ALSO gates
#: this with a scoped token, so a bug here is not a bypass -- it is the first of
#: two independent gates.
HARNESS_ALLOWED_PREFIXES = (
    "sessions", "decisions", "desk/fleet/status",
    # The OS IDE (2026-10-10); the daemon pins the method and requires the owner verdict.
    "fs/list", "fs/read", "fs/write", "git/status", "git/diff",
)
HARNESS_REFUSED_SESSION_VERBS = ("focus",)
HARNESS_EXACT_ALLOWED = ("rc/status",)
RC_MODES = frozenset({"cli", "daemon", "antigravity"})
RUNTIME_TIERS = frozenset({"standard", "zero-libc", "awzeal"})
DEFAULT_RUNTIME_TIER = "standard"
HELLO_ATTESTATION_FIELDS = (
    "t",
    "node_id",
    "instance_id",
    "hostname",
    "harnesses",
    "rc_mode",
    "runtime_tier",
    "zero_libc",
    "public_key",
    "ts",
    "nonce",
)

#: Reconnect backoff. Capped, and jittered so a fleet that lost the tunnel does
#: not return as a synchronised thundering herd.
_BACKOFF_START = 1.0
_BACKOFF_MAX = 60.0

#: HTTP statuses on the handshake that mean "the tunnel ANSWERED and said no".
#: The tunnel closes before accept with 4401 (auth), 4403 (no tenant) or 4404
#: (node not in its table), and every one of those reaches the client as HTTP
#: 403. Measured 2026-09-30: after each tunnel restart it answered 403 for ~10
#: minutes until identity re-posted the node, and the daemon retried at the full
#: ramp the whole time. A refusal is retried at the CAP, never given up on: it
#: clears on its own when the node table re-syncs.
_REJECT_STATUSES = frozenset({401, 403, 404})

#: Consecutive refusals before the link reports ``state="rejected"``.
REJECTED_AFTER = 5


def _handshake_status(exc: BaseException) -> Optional[int]:
    """The HTTP status of a rejected WebSocket handshake, or None.

    websockets >= 14 raises ``InvalidStatus`` (``.response.status_code``); the
    legacy client raised ``InvalidStatusCode`` (``.status_code``).
    """
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None

#: One client SSL context for every reconnect attempt. websockets builds
#: ``ssl.create_default_context()`` per ``wss://`` connect when none is passed,
#: and on Windows that walks the system certificate store -- while the tunnel is
#: down that ran on every backoff tick (measured 2026-09-27). Same defaults.
_SSL_CONTEXT: Optional[Any] = None


def _client_ssl_context() -> Any:
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        import ssl

        _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


#: Hard cap on one frame's body in either direction.
MAX_FRAME_BYTES = 8 * 1024 * 1024

#: Keepalive. Mobile networks drop an idle socket silently, and a half-open link
#: is indistinguishable from a working one until the first request times out.
PING_INTERVAL_S = 30.0


def tunnel_ws_url(base: str, node_id: str) -> str:
    """Build the attach URL, coercing http(s) to ws(s).

    Refuses to silently downgrade: a plain-http base yields `ws://`, which is what
    a local test harness wants and what a production misconfiguration must LOOK
    like rather than quietly becoming `wss://` and appearing to work.
    """
    b = (base or "").strip().rstrip("/")
    if b.startswith("https://"):
        b = "wss://" + b[len("https://"):]
    elif b.startswith("http://"):
        b = "ws://" + b[len("http://"):]
    elif not b.startswith(("ws://", "wss://")):
        b = "wss://" + b
    return f"{b}/tunnel/nodes/{node_id}/attach"


def _rc_state_file() -> Path:
    explicit = (os.environ.get("AITHER_RC_STATE_FILE") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = (os.environ.get("AITHER_HOME") or "").strip()
    if home:
        return Path(home).expanduser() / "rc-state.json"
    return Path.home() / ".aither" / "rc-state.json"


def _read_rc_state_meta() -> Dict[str, Any]:
    path = _rc_state_file()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def detect_harnesses() -> List[str]:
    """Fast, non-blocking list of available coding harnesses on this device."""
    found: List[str] = []
    try:
        from adk.harnesses.registry import detect

        for row in detect(with_version=False):
            hid = str(row.get("id") or "").strip()
            if hid and row.get("installed") and hid not in found:
                found.append(hid)
    except Exception:  # noqa: BLE001 -- detection must never block link attach
        for binary, hid in (("claude", "claude"), ("gemini", "gemini"), ("codex", "codex")):
            if shutil.which(binary) and hid not in found:
                found.append(hid)
        if (Path.home() / ".gemini" / "antigravity").exists() and "antigravity" not in found:
            found.append("antigravity")
    if "awdk" not in found:
        found.append("awdk")
    return found


def normalize_runtime_tier(value: Any) -> str:
    """Normalize a runtime tier string, clamping unknowns to ``standard``."""
    raw = str(value or "").strip().lower()
    return raw if raw in RUNTIME_TIERS else DEFAULT_RUNTIME_TIER


def parse_bool_env(value: Any) -> Optional[bool]:
    """Parse boolean strings/flags without raising; returns ``None`` if unset/unknown."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None


def resolve_runtime_posture(
    *,
    runtime_tier: Optional[str] = None,
    zero_libc: Optional[bool] = None,
    state: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, bool]:
    """Resolve ``(runtime_tier, zero_libc)`` via kwarg -> env -> state -> defaults."""
    raw_tier: Optional[str] = None
    if runtime_tier is not None and str(runtime_tier).strip():
        raw_tier = str(runtime_tier).strip()
    elif (os.environ.get("AITHER_RUNTIME_TIER") or "").strip():
        raw_tier = (os.environ.get("AITHER_RUNTIME_TIER") or "").strip()
    elif isinstance(state, Mapping) and str(state.get("runtime_tier") or "").strip():
        raw_tier = str(state.get("runtime_tier") or "").strip()

    tier_was_set = raw_tier is not None
    resolved_tier = normalize_runtime_tier(raw_tier) if tier_was_set else DEFAULT_RUNTIME_TIER

    resolved_zero: Optional[bool] = None
    if zero_libc is not None:
        resolved_zero = parse_bool_env(zero_libc)
    if resolved_zero is None:
        resolved_zero = parse_bool_env(os.environ.get("AITHER_ZERO_LIBC"))
    if resolved_zero is None and isinstance(state, Mapping) and "zero_libc" in state:
        resolved_zero = parse_bool_env(state.get("zero_libc"))

    if resolved_tier in ("zero-libc", "awzeal") and resolved_zero is None:
        resolved_zero = True
    elif resolved_zero is True and not tier_was_set:
        resolved_tier = "zero-libc"

    return resolved_tier, bool(resolved_zero) if resolved_zero is not None else False


def _canonical_hello_bytes(payload: Mapping[str, Any]) -> bytes:
    obj = {k: payload[k] for k in HELLO_ATTESTATION_FIELDS if k in payload}
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _normalize_pubkey_hex(raw_pub: Any) -> str:
    if raw_pub is None:
        return ""
    if isinstance(raw_pub, (bytes, bytearray)):
        return bytes(raw_pub).hex() if len(raw_pub) == 32 else ""
    text = str(raw_pub).strip()
    if not text:
        return ""
    if len(text) == 64:
        try:
            return bytes.fromhex(text).hex()
        except ValueError as exc:
            log.debug("pubkey hex decode ignored: %s", exc)
    try:
        decoded = base64.b64decode(text, validate=True)
        if len(decoded) == 32:
            return decoded.hex()
    except Exception as exc:  # noqa: BLE001
        log.debug("pubkey b64 decode ignored: %s", exc)
    return ""


def _decode_sig_bytes(raw_sig: Any) -> Optional[bytes]:
    if isinstance(raw_sig, (bytes, bytearray)):
        return bytes(raw_sig) if len(raw_sig) == 64 else None
    text = str(raw_sig or "").strip()
    if not text:
        return None
    if len(text) == 128:
        try:
            raw = bytes.fromhex(text)
            if len(raw) == 64:
                return raw
        except ValueError as exc:
            log.debug("sig hex decode ignored: %s", exc)
    try:
        raw = base64.b64decode(text, validate=True)
        if len(raw) == 64:
            return raw
    except Exception as exc:  # noqa: BLE001
        log.debug("sig b64 decode ignored: %s", exc)
    return None


def _load_ed25519_private_key(
    signing_key: Any = None,
    signing_key_path: Optional[Union[str, Path]] = None,
    *,
    allow_default: bool = False,
) -> Any:
    """Load an Ed25519 private key via optional ``awseal.keys`` or ``cryptography``."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise RuntimeError(
            "cryptography is required for Ed25519 hello attestation"
        ) from exc

    if signing_key is not None and not isinstance(
        signing_key, (str, bytes, bytearray, Path)
    ):
        if hasattr(signing_key, "sign") and hasattr(signing_key, "public_key"):
            return signing_key
        raise ValueError("unsupported Ed25519 signing_key object")

    def _from_bytes(raw: bytes) -> Any:
        if len(raw) == 32:
            return Ed25519PrivateKey.from_private_bytes(raw)
        if b"PRIVATE KEY" in raw:
            return serialization.load_pem_private_key(raw, password=None)
        txt = raw.decode("utf-8", errors="ignore").strip()
        if len(txt) == 64:
            try:
                return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(txt))
            except ValueError as exc:
                log.debug("privkey hex decode ignored: %s", exc)
        try:
            b64 = base64.b64decode(txt, validate=True)
            if len(b64) == 32:
                return Ed25519PrivateKey.from_private_bytes(b64)
        except Exception as exc:  # noqa: BLE001
            log.debug("privkey b64 decode ignored: %s", exc)
        raise ValueError("invalid Ed25519 signing key material")

    def _from_path(p: Path) -> Any:
        target = p.expanduser()
        if not target.is_file():
            raise ValueError(f"Ed25519 signing key file not found: {target}")
        try:
            from awseal import keys as awseal_keys

            return awseal_keys.load_private_key(target)
        except ImportError:
            return _from_bytes(target.read_bytes())
        except Exception:  # noqa: BLE001 -- support raw/hex/b64 key files too
            return _from_bytes(target.read_bytes())

    if signing_key is not None:
        if isinstance(signing_key, Path):
            return _from_path(signing_key)
        if isinstance(signing_key, (bytes, bytearray)):
            return _from_bytes(bytes(signing_key))
        text = str(signing_key).strip()
        if not text:
            raise ValueError("signing_key must not be empty")
        if "PRIVATE KEY" in text:
            return _from_bytes(text.encode("utf-8"))
        try:
            return _from_bytes(text.encode("utf-8"))
        except ValueError:
            cand = Path(text).expanduser()
            if cand.is_file():
                return _from_path(cand)
            raise

    if signing_key_path is not None:
        return _from_path(Path(signing_key_path))

    env_key = (os.environ.get("AITHER_NODE_LINK_SIGNING_KEY") or "").strip()
    if env_key:
        if "PRIVATE KEY" in env_key:
            return _from_bytes(env_key.encode("utf-8"))
        cand = Path(env_key).expanduser()
        if cand.is_file():
            return _from_path(cand)
        return _from_bytes(env_key.encode("utf-8"))

    env_path = (os.environ.get("AWSEAL_KEY_PATH") or "").strip()
    if env_path:
        return _from_path(Path(env_path))

    if allow_default:
        try:
            from awseal import keys as awseal_keys

            if Path(awseal_keys.DEFAULT_KEY_PATH).is_file():
                return awseal_keys.load_private_key(awseal_keys.DEFAULT_KEY_PATH)
        except ImportError as exc:
            log.debug("awseal default key unavailable: %s", exc)

    raise ValueError("no Ed25519 signing key configured for hello attestation")


def sign_hello_payload(
    frame: Mapping[str, Any],
    *,
    node_id: str = "",
    signing_key: Any = None,
    signing_key_path: Optional[Union[str, Path]] = None,
    ts: Optional[int] = None,
    nonce: str = "",
) -> Dict[str, Any]:
    """Attach canonical Ed25519 attestation fields to a ``hello`` control frame."""
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:
        raise RuntimeError(
            "cryptography is required for Ed25519 hello attestation"
        ) from exc

    priv = _load_ed25519_private_key(
        signing_key=signing_key,
        signing_key_path=signing_key_path,
        allow_default=True,
    )
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    out = dict(frame)
    if node_id or "node_id" not in out:
        out["node_id"] = str(node_id or out.get("node_id") or "")
    out["ts"] = int(time.time() if ts is None else ts)
    out["nonce"] = str(nonce or secrets.token_hex(12))
    out["public_key"] = pub_raw.hex()
    out["sig_alg"] = "ed25519"
    sig_bytes = priv.sign(_canonical_hello_bytes(out))
    out["sig"] = base64.b64encode(sig_bytes).decode("ascii")
    return out


def verify_hello_payload(
    frame: Mapping[str, Any],
    *,
    expected_pubkey: str = "",
    expected_node_id: str = "",
    now: Optional[float] = None,
    max_skew_s: int = 300,
) -> Tuple[bool, str]:
    """Fail-closed verification for an attested ``hello`` frame."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False, "cryptography unavailable for Ed25519 verification"

    if str(frame.get("sig_alg") or "").strip().lower() != "ed25519":
        return False, "unsupported or missing sig_alg"
    if expected_node_id and str(frame.get("node_id") or "") != expected_node_id:
        return False, "node_id mismatch"
    nonce = str(frame.get("nonce") or "").strip()
    if len(nonce) < 8:
        return False, "missing or invalid nonce"
    try:
        ts_val = float(frame["ts"])
    except (KeyError, TypeError, ValueError):
        return False, "missing or invalid ts"
    if not math.isfinite(ts_val):
        return False, "missing or invalid ts"
    cur = time.time() if now is None else float(now)
    if not math.isfinite(cur) or abs(cur - ts_val) > max_skew_s:
        return False, "hello timestamp outside skew window"

    pub_hex = _normalize_pubkey_hex(frame.get("public_key"))
    if not pub_hex:
        return False, "missing or invalid public_key"
    if expected_pubkey:
        exp_hex = _normalize_pubkey_hex(expected_pubkey)
        if not exp_hex or not hmac.compare_digest(pub_hex, exp_hex):
            return False, "public_key does not match expected node key"

    sig_bytes = _decode_sig_bytes(frame.get("sig"))
    if sig_bytes is None:
        return False, "missing or invalid signature"

    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
        pub.verify(sig_bytes, _canonical_hello_bytes(frame))
    except Exception:  # noqa: BLE001 -- fail closed on any verification error
        return False, "Ed25519 signature verification failed"
    return True, "ok"


def hello_frame_for(
    *,
    instance_id: str = "",
    hostname: str = "",
    harnesses: Optional[Sequence[str]] = None,
    rc_mode: str = "",
    runtime_tier: Optional[str] = None,
    zero_libc: Optional[bool] = None,
    node_id: str = "",
    signing_key: Any = None,
    signing_key_path: Optional[Union[str, Path]] = None,
    attest: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build the initial ``hello`` control frame (never includes ``owner``)."""
    meta = _read_rc_state_meta()
    resolved_instance = (
        (instance_id or "").strip()
        or (os.environ.get("AITHER_RC_INSTANCE_ID") or "").strip()
        or str(meta.get("instance_id") or "").strip()
    )
    resolved_host = (hostname or "").strip() or socket.gethostname()
    resolved_harnesses = (
        [str(h) for h in harnesses] if harnesses is not None else detect_harnesses()
    )
    resolved_mode = (
        (rc_mode or "").strip()
        or (os.environ.get("AITHER_RC_MODE") or "").strip()
        or str(meta.get("rc_mode") or "").strip()
        or "cli"
    )
    if resolved_mode not in RC_MODES:
        resolved_mode = "cli"
    resolved_tier, resolved_zero = resolve_runtime_posture(
        runtime_tier=runtime_tier,
        zero_libc=zero_libc,
        state=meta,
    )
    frame: Dict[str, Any] = {
        "t": "hello",
        "instance_id": resolved_instance,
        "hostname": resolved_host,
        "harnesses": resolved_harnesses,
        "rc_mode": resolved_mode,
        "runtime_tier": resolved_tier,
        "zero_libc": resolved_zero,
    }
    has_key_cfg = bool(
        signing_key is not None
        or signing_key_path is not None
        or (os.environ.get("AITHER_NODE_LINK_SIGNING_KEY") or "").strip()
        or (os.environ.get("AWSEAL_KEY_PATH") or "").strip()
    )
    if attest is not False and (attest is True or has_key_cfg):
        return sign_hello_payload(
            frame,
            node_id=node_id,
            signing_key=signing_key,
            signing_key_path=signing_key_path,
        )
    return frame


_MAX_UNQUOTE_PASSES = 5


def _strip_single_edge_slashes(path: str) -> str:
    """Strip at most one leading and one trailing slash (preserving ``//``)."""
    if path.startswith("/"):
        path = path[1:]
    if path.endswith("/"):
        path = path[:-1]
    return path


def _has_unsafe_segments(path: str) -> bool:
    return any(seg.strip() in (".", "..", "") for seg in path.split("/"))


def _has_unsafe_path_chars(raw: str) -> bool:
    if not raw or "\\" in raw or "\x00" in raw or "#" in raw or "?" in raw:
        return True
    cur = raw
    for _ in range(_MAX_UNQUOTE_PASSES):
        if (
            "\\" in cur
            or "\x00" in cur
            or "#" in cur
            or "?" in cur
            or _has_unsafe_segments(cur)
        ):
            return True
        nxt = unquote(cur)
        if nxt == cur:
            return False
        cur = nxt
    if (
        "\\" in cur
        or "\x00" in cur
        or "#" in cur
        or "?" in cur
        or _has_unsafe_segments(cur)
    ):
        return True
    return unquote(cur) != cur


def _decode_path(raw: str) -> Optional[str]:
    cur = raw
    for _ in range(_MAX_UNQUOTE_PASSES):
        nxt = unquote(cur)
        if nxt == cur:
            return cur
        cur = nxt
    return None if unquote(cur) != cur else cur


def path_target(path: str, method: str = "") -> Tuple[str, str]:
    """Classify a requested path.

    Returns ``(target, local_path)`` where target is ``inference``, ``harness`` or
    ``""`` (refused). The refusal is the default: an unrecognised path is never
    forwarded, so adding a route to the tunnel does not implicitly open one here.
    """
    raw_input = str(path or "")
    if "#" in raw_input or "\\" in raw_input or "\x00" in raw_input:
        return "", ""
    raw = raw_input.split("?", 1)[0]
    clean = _strip_single_edge_slashes(raw)
    if not clean or _has_unsafe_path_chars(clean):
        return "", ""
    decoded = _decode_path(clean)
    if decoded is None:
        return "", ""
    decoded = _strip_single_edge_slashes(decoded)
    if not decoded:
        return "", ""
    low = clean.lower()
    dec_low = decoded.lower()
    verb = (method or "").strip().upper()
    if low.startswith(HARNESS_PREFIX):
        if not dec_low.startswith(HARNESS_PREFIX):
            return "", ""
        rest = decoded[len(HARNESS_PREFIX):]
        rest_low = rest.lower()
        if not rest:
            return "", ""
        parts = rest_low.split("/")
        if len(parts) >= 3 and parts[0] == "sessions" and parts[2] in HARNESS_REFUSED_SESSION_VERBS:
            return "", ""
        if rest_low == "sessions" and verb == "POST":
            return "", ""
        if rest_low in HARNESS_EXACT_ALLOWED:
            if verb in ("", "GET", "HEAD"):
                return "harness", rest
            return "", ""
        for allowed in HARNESS_ALLOWED_PREFIXES:
            if rest_low == allowed or rest_low.startswith(allowed + "/"):
                return "harness", rest
        return "", ""
    if low in NODE_LINK_ALLOWED_PATHS and dec_low == low:
        return "inference", clean
    return "", ""


class NodeLink:
    """Holds the reverse link and forwards allowlisted requests locally.

    Not started automatically by anything: ``adk enroll`` and ``adk rc`` own the
    decision, because a process that silently opens a public-facing socket is not
    something a CLI should do on a user's behalf without being asked.
    """

    def __init__(
        self,
        *,
        tunnel_url: str,
        node_id: str,
        token: str,
        inference_url: str = "",
        harness_url: str = "",
        harness_token: str = "",
        instance_id: str = "",
        hostname: str = "",
        harnesses: Optional[Sequence[str]] = None,
        rc_mode: str = "",
        runtime_tier: Optional[str] = None,
        zero_libc: Optional[bool] = None,
        signing_key: Any = None,
        signing_key_path: Optional[Union[str, Path]] = None,
        attest: Optional[bool] = None,
    ) -> None:
        self.tunnel_url = tunnel_url
        self.node_id = node_id
        self.token = token
        self.inference_url = (inference_url or "").rstrip("/")
        self.harness_url = (harness_url or "").rstrip("/")
        self.harness_token = harness_token or ""
        self.instance_id = (instance_id or "").strip()
        self.hostname = (hostname or "").strip()
        self.harnesses: Optional[List[str]] = (
            [str(h) for h in harnesses] if harnesses is not None else None
        )
        self.rc_mode = (rc_mode or "").strip()
        self._runtime_tier_override: Optional[str] = runtime_tier
        self._zero_libc_override: Optional[bool] = zero_libc
        self.signing_key = signing_key
        self.signing_key_path = signing_key_path
        self.attest = attest
        self._ws: Optional[Any] = None
        self._send_lock: Optional[asyncio.Lock] = None
        self._active_tasks: Dict[str, asyncio.Task[Any]] = {}
        self.connected = False
        #: Why the last attempt ended. Surfaced by `adk devices status` and the
        #: heartbeat rather than being swallowed: a link that cannot attach is
        #: the difference between "enrolled" and "usable".
        self.last_error = ""
        self._attempts = 0
        #: Handshakes the tunnel refused in a row (HTTP 401/403/404).
        self.consecutive_rejects = 0
        self.last_attached_at = ""
        self.last_status: Optional[int] = None

    @property
    def runtime_tier(self) -> str:
        tier, _ = resolve_runtime_posture(
            runtime_tier=self._runtime_tier_override,
            zero_libc=self._zero_libc_override,
            state=_read_rc_state_meta(),
        )
        return tier

    @runtime_tier.setter
    def runtime_tier(self, value: Optional[str]) -> None:
        self._runtime_tier_override = value

    @property
    def zero_libc(self) -> bool:
        _, zlibc = resolve_runtime_posture(
            runtime_tier=self._runtime_tier_override,
            zero_libc=self._zero_libc_override,
            state=_read_rc_state_meta(),
        )
        return zlibc

    @zero_libc.setter
    def zero_libc(self, value: Optional[bool]) -> None:
        self._zero_libc_override = value

    def hello_frame(self) -> Dict[str, Any]:
        """Build the initial ``hello`` control frame for this link (no ``owner``)."""
        return hello_frame_for(
            instance_id=self.instance_id,
            hostname=self.hostname,
            harnesses=self.harnesses,
            rc_mode=self.rc_mode,
            runtime_tier=self._runtime_tier_override,
            zero_libc=self._zero_libc_override,
            node_id=self.node_id,
            signing_key=self.signing_key,
            signing_key_path=self.signing_key_path,
            attest=self.attest,
        )

    async def send_hello(self, ws: Any = None) -> bool:
        """Send (or re-send) the ``hello`` frame over the active WebSocket."""
        sock = ws if ws is not None else self._ws
        if sock is None:
            return False
        await self._send(sock, self.hello_frame())
        return True

    def status(self) -> Dict[str, Any]:
        """What the daemon's /health reports. No credential, no node id.

        ``state`` is ``connected``, ``rejected`` (the tunnel keeps answering no --
        see :data:`_REJECT_STATUSES`), ``retrying`` or ``starting``.
        """
        if self.connected:
            state = "connected"
        elif self.consecutive_rejects >= REJECTED_AFTER:
            state = "rejected"
        elif self._attempts:
            state = "retrying"
        else:
            state = "starting"
        return {
            "state": state,
            "connected": self.connected,
            "attempts": self._attempts,
            "consecutive_rejects": self.consecutive_rejects,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "last_attached_at": self.last_attached_at,
        }

    # ── what the heartbeat reports ────────────────────────────────────────
    def reach_kind(self) -> str:
        """``ws`` while the link is up, ``none`` otherwise.

        Deliberately not sticky. A phone that loses its link must stop claiming
        reach within one heartbeat, or the owner's page offers a device that
        cannot answer.
        """
        return "ws" if self.connected else "none"

    # ── the loop ──────────────────────────────────────────────────────────
    async def run(self, *, max_attempts: Optional[int] = None) -> None:
        """Connect, serve, reconnect forever. Never raises.

        ``max_attempts`` bounds the loop for tests; ``None`` runs until cancelled.
        """
        try:
            import websockets  # noqa: F401
        except ImportError:
            # LOUD, once. A silently absent transport is how "the phone is
            # enrolled but unreachable" becomes a mystery.
            self.last_error = "websockets is not installed; the reverse link cannot start"
            log.error("%s", self.last_error)
            return

        backoff = _BACKOFF_START
        while max_attempts is None or self._attempts < max_attempts:
            self._attempts += 1
            try:
                await self._serve_once()
                backoff = _BACKOFF_START          # a clean session resets the ramp
            except asyncio.CancelledError:
                log.info("Node link cancelled")
                raise
            except Exception as e:  # noqa: BLE001 -- a link failure is never fatal
                self.last_error = str(e)[:200]
                self.last_status = _handshake_status(e)
                if self.last_status in _REJECT_STATUSES:
                    self.consecutive_rejects += 1
                    # Straight to the cap: a refusal does not clear in seconds.
                    backoff = _BACKOFF_MAX
                    if self.consecutive_rejects == REJECTED_AFTER:
                        log.error(
                            "Node link: the tunnel refused %d handshakes in a row "
                            "(HTTP %s). It answers 403 until identity re-posts this "
                            "node after a tunnel restart (minutes); if it persists, "
                            "re-enroll with `adk enroll`. Retrying every ~%ds.",
                            self.consecutive_rejects, self.last_status, int(_BACKOFF_MAX))
                else:
                    self.consecutive_rejects = 0
                log.warning("Node link attempt %d failed: %s", self._attempts, self.last_error)
            finally:
                self.connected = False
            if max_attempts is not None and self._attempts >= max_attempts:
                return
            # Jitter so a fleet that lost the tunnel does not come back in lockstep.
            await asyncio.sleep(backoff * (0.5 + random.random()))
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _serve_once(self) -> None:
        import websockets

        url = tunnel_ws_url(self.tunnel_url, self.node_id)
        # The bearer rides in the handshake and NOWHERE else. websockets renamed
        # this parameter, so try both rather than silently falling back to an
        # unauthenticated connect (which would 4401 and read as "tunnel down").
        tls: Dict[str, Any] = (
            {"ssl": _client_ssl_context()} if url.lower().startswith("wss://") else {}
        )
        try:
            conn = websockets.connect(
                url, additional_headers={"Authorization": f"Bearer {self.token}"},
                max_size=MAX_FRAME_BYTES * 2, open_timeout=20, **tls,
            )
        except TypeError:
            conn = websockets.connect(
                url, extra_headers={"Authorization": f"Bearer {self.token}"},
                max_size=MAX_FRAME_BYTES * 2, open_timeout=20, **tls,
            )
        async with conn as ws:
            self._ws = ws
            self._send_lock = asyncio.Lock()
            self._active_tasks.clear()
            self.connected = True
            self.last_error = ""
            self.last_status = None
            from datetime import datetime, timezone

            self.last_attached_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            # WARNING, not INFO: the daemon logs at WARNING, and a log holding
            # 1,079 failures and zero successes read as "never attached" when it
            # had attached repeatedly (measured 2026-09-30).
            log.warning("Node link attached after %d attempt(s): %s", self._attempts, url)
            self.consecutive_rejects = 0
            await self._send(ws, self.hello_frame())
            pinger = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    await self._on_frame(ws, raw)
            finally:
                pinger.cancel()
                pending = list(self._active_tasks.values())
                for task in pending:
                    if not task.done():
                        task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await asyncio.gather(pinger, return_exceptions=True)
                self._active_tasks.clear()
                self._send_lock = None
                self._ws = None

    async def _ping_loop(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            await self._send(ws, {"t": "ping"})

    async def _on_frame(self, ws: Any, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        try:
            frame = json.loads(raw)
        except ValueError:
            log.warning("Tunnel sent an unparseable frame; ignoring")
            return
        if not isinstance(frame, dict):
            return
        kind = frame.get("t")
        if kind == "pong":
            return
        if kind == "cancel":
            rid = str(frame.get("id") or "")
            if rid:
                task = self._active_tasks.get(rid)
                if task is not None and not task.done():
                    task.cancel()
            return
        if kind != "req":
            return
        rid = str(frame.get("id") or "")
        if not rid:
            return
        prev = self._active_tasks.get(rid)
        if prev is not None and not prev.done():
            prev.cancel()
        self._active_tasks[rid] = asyncio.create_task(self._run_request(ws, rid, frame))

    async def _run_request(self, ws: Any, rid: str, frame: Dict[str, Any]) -> None:
        current = asyncio.current_task()
        try:
            await self._handle_request(ws, rid, frame)
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001 -- one bad request never drops the link
            log.warning("Node link request %s failed: %s", rid, e)
            try:
                await self._send(ws, {
                    "t": "err",
                    "id": rid,
                    "status": 502,
                    "detail": str(e)[:200],
                })
            except Exception as send_exc:  # noqa: BLE001
                log.debug("Node link failed sending error frame for %s: %s", rid, send_exc)
        finally:
            if current is not None and self._active_tasks.get(rid) is current:
                self._active_tasks.pop(rid, None)

    async def _handle_request(self, ws: Any, rid: str, frame: Dict[str, Any]) -> None:
        raw_path = str(frame.get("path") or "")
        method = str(frame.get("method") or "GET").upper()
        query = str(frame.get("query") or "")
        target, local_path = path_target(raw_path, method=method)
        if not target or "#" in query or "\x00" in query or "\r" in query or "\n" in query:
            # REFUSED HERE, not just at the tunnel. This is the gate that holds if
            # the tunnel is ever wrong or compromised.
            log.warning("Refusing to forward %r over the node link", raw_path[:120])
            await self._send(ws, {"t": "err", "id": rid, "status": 403,
                                  "detail": "path not permitted on this link"})
            return

        if target == "harness":
            base, bearer = self.harness_url, self.harness_token
            if not base:
                await self._send(ws, {"t": "err", "id": rid, "status": 503,
                                      "detail": "no harness on this device"})
                return
        else:
            base, bearer = self.inference_url, ""
            if not base:
                await self._send(ws, {"t": "err", "id": rid, "status": 503,
                                      "detail": "no local inference server"})
                return

        url = f"{base}/{local_path}"
        if query:
            url += f"?{query}"
        headers = self._local_headers(frame.get("headers") or {}, bearer)
        if target == "harness" and frame.get("actor") == LINK_ACTOR_OWNER:
            # The tunnel verified the caller is the USER who holds this link and
            # said so in a FRAME field (a caller cannot set one; `_local_headers`
            # already dropped any caller-sent copy of this header).
            headers[LINK_ACTOR_HEADER] = LINK_ACTOR_OWNER
        body = _decode_body(frame.get("body_b64"))

        import httpx

        # Streaming is the point, not an optimisation: `v1/chat/completions` with
        # `stream: true` is SSE, and buffering it turns a token stream into a
        # blank page that fills in at the end.
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            async with client.stream(method, url, headers=headers, content=body) as resp:
                await self._send(ws, {
                    "t": "res", "id": rid, "status": resp.status_code,
                    "headers": dict(resp.headers), "stream": True,
                })
                async for chunk in resp.aiter_raw():
                    if not chunk:
                        continue
                    await self._send(ws, {
                        "t": "chunk", "id": rid,
                        "body_b64": base64.b64encode(chunk).decode("ascii"),
                    })
                await self._send(ws, {"t": "end", "id": rid})

    @staticmethod
    def _local_headers(incoming: Dict[str, Any], bearer: str) -> Dict[str, str]:
        """Rebuild the headers for the LOCAL request.

        Allowlisted, not filtered: the tunnel already strips the caller's
        credentials, but this process is the one talking to loopback and must not
        depend on that. Only the few headers a chat/completions call actually
        needs survive, and the harness bearer is added HERE -- it never crosses
        the link.
        """
        keep = ("content-type", "accept", "accept-encoding", "user-agent")
        out = {
            str(k): str(v) for k, v in incoming.items()
            if str(k).lower() in keep
        }
        out.setdefault("content-type", "application/json")
        if bearer:
            out["authorization"] = f"Bearer {bearer}"
        return out

    async def _send(self, ws: Any, frame: Dict[str, Any]) -> None:
        payload = json.dumps(frame)
        lock = self._send_lock
        if lock is not None:
            async with lock:
                await ws.send(payload)
        else:
            await ws.send(payload)


def _decode_body(raw: Any) -> bytes:
    """Decode a base64 frame body, refusing anything oversized or malformed."""
    if not isinstance(raw, str) or not raw:
        return b""
    if len(raw) > MAX_FRAME_BYTES * 2:
        log.warning("Dropping an oversized request body from the tunnel")
        return b""
    try:
        return base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001
        log.warning("Tunnel frame body was not valid base64; dropping it")
        return b""


def default_tunnel_url() -> str:
    """Where the reverse link dials. Env first, then primary public tunnel (a30.link)."""
    explicit = (os.environ.get("AITHER_TUNNEL_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    gateway = (os.environ.get("AITHER_GATEWAY_URL") or "").strip()
    if gateway:
        host = urlsplit(gateway if "//" in gateway else f"https://{gateway}").hostname or ""
        if host.startswith("gateway."):
            return "https://tunnel." + host[len("gateway."):]
    return "https://a30.link"
