"""``adk rc`` — remote control: this machine's sessions, reachable from your phone.

ONE COMMAND, AND IT IS THE WHOLE "ADD DEVICE" FLOW

The session plane already exists on every machine that runs the shell: the harness
daemon on 127.0.0.1:8362 serves `/sessions/unified`, `/sessions/{id}/stream`,
`/sessions/{id}/input` and `/decisions/*`. What has never existed is a verb that
REGISTERS a running daemon with the cloud, so the list is visible from anywhere
but the machine it runs on. `adk rc` is that verb:

    adk login (if needed) -> adk enroll --node-class laptop -> hold the link

and while it holds, the device advertises `harness_url=http://127.0.0.1:8362`
over the same reverse link the inference lane uses.

THE TOKEN IS THE POINT
It advertises a PER-NODE, PATH-SCOPED harness token, never the daemon's root
token. The root token can `POST /sessions`, which spawns a coding agent with
filesystem access on this machine — putting it on a public path would make one
stolen header remote code execution on the owner's laptop. The scoped token
reaches `/sessions*`, `/decisions*` and `/desk/fleet/status` and is refused
everywhere else by the daemon itself, before any handler runs.

The token is minted fresh on every `adk rc` and expires. It is never printed, and
it never leaves this process except into the daemon's own hashed registry.
"""

from __future__ import annotations

__all__ = [
    "APP_URL_BASE",
    "DEVICES_URL",
    "RC_MODES",
    "RC_STATE_PATH",
    "RC_URL_BASE",
    "SESSIONS_URL",
    "app_code_url",
    "clear_rc_state",
    "cmd_rc",
    "cmd_rc_off",
    "cmd_rc_status",
    "default_harness_url",
    "export_rc_bundle",
    "generate_instance_slug",
    "harness_link_enabled",
    "harness_link_status",
    "hold_harness_link",
    "load_rc_state",
    "main",
    "probe_harness",
    "rc_off",
    "rc_state_path",
    "rc_status",
    "rc_url",
    "save_rc_state",
    "validate_instance_slug",
    "verify_rc_bundle",
]

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from adk.device_class import default_node_class

log = logging.getLogger("adk.rc")

#: Where the session daemon listens. Loopback, always: the daemon is reached
#: THROUGH the link by a process on this machine, never by binding a public port.
DEFAULT_HARNESS_PORT = 8362

#: Canonical Remote Control URLs (a30.link primary; tunnel.aitherium.com compat alias).
RC_URL_BASE = "https://a30.link/r"
RC_COMPAT_URL_BASE = "https://tunnel.aitherium.com/r"
APP_URL_BASE = "https://app.aitherium.com/code"
SESSIONS_URL = APP_URL_BASE
DEVICES_URL = "https://app.aitherium.com/settings/connected-devices"
RC_STATE_PATH = Path.home() / ".aither" / "rc-state.json"
RC_MODES = frozenset({"cli", "daemon", "antigravity"})

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_ADJECTIVES = (
    "bold", "bright", "calm", "clear", "crisp", "fast",
    "keen", "lively", "quick", "quiet", "steady", "swift",
)
_NOUNS = (
    "cedar", "comet", "falcon", "harbor", "lynx", "otter",
    "pine", "ridge", "river", "spark", "summit", "trail",
)


def rc_state_path() -> Path:
    """Resolve ``~/.aither/rc-state.json`` respecting ``AITHER_RC_STATE_FILE`` / ``AITHER_HOME``."""
    explicit = (os.environ.get("AITHER_RC_STATE_FILE") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = (os.environ.get("AITHER_HOME") or "").strip()
    if home:
        return Path(home).expanduser() / "rc-state.json"
    return Path.home() / ".aither" / "rc-state.json"


def generate_instance_slug(hostname: str = "") -> str:
    """Generate a human-friendly ``<adj>-<noun>-<short>`` instance slug."""
    adj = secrets.choice(_ADJECTIVES)
    noun = secrets.choice(_NOUNS)
    suffix = secrets.token_hex(2)
    return f"{adj}-{noun}-{suffix}"


def validate_instance_slug(name: str) -> str:
    """Validate and normalize a custom ``--name`` instance slug."""
    raw = (name or "").strip().lower()
    if not raw or ".." in raw or "/" in raw or "\\" in raw or not _SLUG_RE.match(raw):
        raise ValueError(
            "invalid instance slug: use 1-64 lowercase letters, digits, or internal hyphens"
        )
    return raw


def rc_url(instance_id: str) -> str:
    """Build ``https://tunnel.aitherium.com/r/<instance_id>`` (honouring test overrides)."""
    explicit = (os.environ.get("AITHER_RC_BASE_URL") or "").strip()
    if explicit:
        base = explicit.rstrip("/")
    else:
        from adk.node_link import default_tunnel_url

        base = default_tunnel_url().rstrip("/")
    return f"{base}/r/{instance_id}"


def app_code_url(node_id: str) -> str:
    """Build ``https://app.aitherium.com/code/<node_id>``."""
    explicit = (os.environ.get("AITHER_APP_URL") or "").strip().rstrip("/")
    if explicit:
        base = explicit if explicit.endswith("/code") else f"{explicit}/code"
    else:
        base = APP_URL_BASE
    return f"{base}/{node_id}"


def load_rc_state(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read persisted ``rc-state.json``, or ``{}`` if absent/invalid."""
    target = path or rc_state_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_rc_state(state: Dict[str, Any], path: Optional[Path] = None) -> Path:
    """Persist ``rc-state.json`` atomically with ``0600`` permissions best-effort."""
    target = path or rc_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        log.debug("rc: could not chmod %s", tmp)
    os.replace(tmp, target)
    return target


def clear_rc_state(path: Optional[Path] = None) -> bool:
    """Remove persisted ``rc-state.json``. Returns True if a file was removed."""
    target = path or rc_state_path()
    if not target.exists():
        return False
    try:
        target.unlink()
        return True
    except OSError as exc:
        log.warning("rc: could not clear %s: %s", target, exc)
        return False


def _pid_alive(pid: Any) -> bool:
    """Windows-safe process liveness check (never calls ``os.kill(pid, 0)`` on nt)."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        from adk.turn_journal import pid_alive as _tj_pid_alive

        return bool(_tj_pid_alive(pid))
    except ImportError:
        log.debug("rc: turn_journal unavailable; falling back to OS pid probe")
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_pid(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                creationflags=flags,
            )
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("rc: taskkill failed for pid %s: %s", pid, exc)
            return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return True
    except OSError as exc:
        log.warning("rc: SIGTERM failed for pid %s: %s", pid, exc)
        return False


def _read_harness_token() -> str:
    env_tok = (os.environ.get("AITHER_HARNESS_TOKEN") or "").strip()
    if env_tok:
        return env_tok
    home = (os.environ.get("AITHER_HOME") or "").strip()
    tok_path = (
        Path(home).expanduser() / "harness_token"
        if home
        else Path.home() / ".aither" / "harness_token"
    )
    try:
        return tok_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _http_json(
    url: str,
    *,
    method: str = "GET",
    token: str = "",
    timeout: float = 1.5,
) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if method == "POST":
        req.data = b"{}"
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read(16384).decode("utf-8", errors="replace"))
            return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 -- daemon may be down or route unmounted
        return None


def default_harness_url() -> str:
    """The local harness base URL this device advertises."""
    explicit = (os.environ.get("AITHER_HARNESS_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = (os.environ.get("AITHER_HARNESS_PORT") or "").strip() or str(DEFAULT_HARNESS_PORT)
    return f"http://127.0.0.1:{port}"


def probe_harness(base: str, token: str = "", timeout: float = 3.0) -> Tuple[bool, str]:
    """Is the session daemon answering, and does the scoped token reach it?

    Returns ``(ready, detail)``. A 200 on ``/sessions/unified`` with the SCOPED
    token is the only thing that counts as ready — probing with the root token,
    or probing an unauthenticated route, would prove the daemon is up while the
    thing the phone will actually do still 403s.
    """
    import json
    import urllib.error
    import urllib.request

    url = f"{base.rstrip('/')}/sessions/unified"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = int(getattr(r, "status", 200) or 200)
            body = r.read(4096).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} from {url}"
    except Exception as e:  # noqa: BLE001 -- a dead daemon is a normal outcome here
        return False, f"{type(e).__name__}: {e}"
    if status != 200:
        return False, f"HTTP {status} from {url}"
    try:
        data = json.loads(body)
        n = len(data.get("sessions") or []) if isinstance(data, dict) else 0
        return True, f"{n} session(s)"
    except ValueError:
        return True, "answered (non-JSON body)"


# ── The same link, held by a long-running process ───────────────────────────
#
# `adk rc` holds the link for as long as its terminal stays open. A device
# registered from the desktop ("Register this device") has no terminal, so the
# daemon that served the click holds it instead — same scoped token, same
# reverse link, same refusal to ever advertise the root token.

_OFF = frozenset({"0", "false", "no", "off"})


def harness_link_enabled() -> bool:
    """``AITHER_HARNESS_LINK=0`` keeps a long-running process from holding the link."""
    return (os.environ.get("AITHER_HARNESS_LINK") or "1").strip().lower() not in _OFF


class _HarnessReadiness:
    """``(harness_url, harness_ready)`` for the heartbeat, without blocking it.

    The probe is a blocking request, and the heartbeat calls its provider on the
    event loop. Each call answers with the last result and refreshes it on a
    worker thread, so readiness is at most one beat stale and a dead session
    daemon can never stall the process that reports it.
    """

    def __init__(self, url: str, token: str, ready: bool = False) -> None:
        self.url = url
        self.token = token
        self.ready = ready

    def refresh(self) -> bool:
        self.ready = probe_harness(self.url, self.token)[0]
        return self.ready

    def __call__(self) -> Tuple[str, bool]:
        try:
            asyncio.get_running_loop().run_in_executor(None, self.refresh)
        except RuntimeError:
            log.debug("no event loop; harness readiness not refreshed")
        return self.url, self.ready


#: The readiness reporter of the link this process holds (None = no link held).
_held_readiness: Optional[_HarnessReadiness] = None


def _held_link() -> Optional[Any]:
    """The reverse link this process is actually driving, or None."""
    from adk import fleet_enroll

    link = fleet_enroll.active_node_link()
    task = fleet_enroll._node_link_task
    if link is None or task is None or task.done():
        return None
    return link


def harness_link_status() -> Dict[str, Any]:
    """Is this process holding the link that makes its sessions reachable?

    ``held`` is true only while a link is being driven AND it carries a scoped
    harness token. No credential and no address is returned.
    """
    link = _held_link()
    readiness = _held_readiness
    held = bool(
        link is not None and readiness is not None
        and getattr(link, "harness_url", "") and getattr(link, "harness_token", "")
    )
    if not held:
        return {"held": False, "reach": "none", "state": "", "harness_ready": False}
    return {
        "held": True,
        "reach": link.reach_kind(),
        "state": str(link.status().get("state") or ""),
        "harness_ready": bool(readiness.ready),
    }


async def hold_harness_link(
    node_id: str,
    token: str,
    *,
    inference_url: str = "",
    harness_url: str = "",
    ttl_days: int = 30,
) -> Dict[str, Any]:
    """Hold the reverse link for an ALREADY-REGISTERED node, advertising its sessions.

    What ``adk rc`` does after it enrols, for a caller that stays running. Mints
    a fresh path-scoped harness token (never the daemon's root token — a failed
    mint holds nothing), starts the link if this process has none, and attaches
    the harness to a link that is already up.

    Returns:
        ``{"held": bool, "code": str, "detail": str, "harness_ready": bool,
        "reach_provider": callable, "harness_provider": callable}``. The two
        providers are what the heartbeat needs to advertise the link; they are
        present only when ``held``. Never raises.
    """
    global _held_readiness
    if not harness_link_enabled():
        return {"held": False, "code": "disabled",
                "detail": "AITHER_HARNESS_LINK is off on this device"}
    if not node_id or not token:
        return {"held": False, "code": "not_registered",
                "detail": "no registered node or no sign-in to hold a link with"}
    url = (harness_url or "").strip().rstrip("/") or default_harness_url()
    try:
        from adk.harnesses.daemon import mint_scoped_token

        # The registry write takes a file lock; keep it off the event loop.
        scoped = await asyncio.to_thread(mint_scoped_token, f"node:{node_id}", ttl_days=ttl_days)
    except Exception as e:  # noqa: BLE001 -- no scoped token means no link, not the root token
        log.warning("Could not mint a scoped harness token: %s", e)
        return {"held": False, "code": "token_mint_failed",
                "detail": "could not mint a scoped session token; nothing was advertised"}
    readiness = _HarnessReadiness(url, scoped)
    await asyncio.to_thread(readiness.refresh)

    from adk import fleet_enroll

    link = fleet_enroll._start_node_link(
        node_id, token, inference_url=inference_url, harness_url=url, harness_token=scoped,
    )
    if link is None or _held_link() is None:
        return {"held": False, "code": "link_unavailable",
                "detail": "the reverse link could not be started on this device"}
    # A link this process already held (started without a harness, or with the
    # previous token) keeps its socket and gains the fresh scope and sign-in.
    link.token = token
    link.harness_url = url
    link.harness_token = scoped
    _held_readiness = readiness
    reach: Callable[[], str] = link.reach_kind
    return {
        "held": True,
        "code": "",
        "detail": "",
        "harness_ready": readiness.ready,
        "reach_provider": reach,
        "harness_provider": readiness,
    }


def _signed_in() -> bool:
    """Is there a real identity on this box? (Never the local-root placeholder.)"""
    try:
        from adk.fleet_enroll import _load_auth_config
    except Exception:  # noqa: BLE001
        return False
    auth = _load_auth_config()
    return bool(
        auth.get("access_token") or auth.get("api_key") or auth.get("token")
        or auth.get("tenant_slug") or (auth.get("user") or {}).get("tenant_slug")
    ) or _paired()


def _paired() -> bool:
    """Was this machine paired with a code? Its own device token is an identity:
    Identity issued it to this node, for this node, at confirm time."""
    try:
        from adk.fleet_enroll import _load_node_auth
        rec = _load_node_auth()
    except Exception:  # noqa: BLE001
        return False
    return rec.get("enrolled_via") == "pairing-code" and bool(rec.get("bearer_token"))


def _resolve_posture(
    *,
    runtime_tier: Optional[str] = None,
    zero_libc: Optional[bool] = None,
    state: Optional[Dict[str, Any]] = None,
) -> Tuple[str, bool]:
    from adk.node_link import resolve_runtime_posture

    return resolve_runtime_posture(
        runtime_tier=runtime_tier,
        zero_libc=zero_libc,
        state=state,
    )


def rc_status(harness_url: str = "") -> Dict[str, Any]:
    """Return normalized Remote Control status from ``rc-state.json`` and ``:8362``."""
    state = load_rc_state()
    resolved_harness = (
        (harness_url or "").strip().rstrip("/")
        or str(state.get("harness_url") or "").strip().rstrip("/")
        or default_harness_url()
    )
    raw_pid = state.get("pid")
    pid_live = _pid_alive(raw_pid)

    token = _read_harness_token()
    daemon_status: Optional[Dict[str, Any]] = None
    daemon_status_authoritative = False
    if token:
        daemon_status = _http_json(f"{resolved_harness}/rc/status", token=token)
        if isinstance(daemon_status, dict):
            daemon_status_authoritative = True
    if daemon_status is None:
        health = _http_json(f"{resolved_harness}/health")
        if isinstance(health, dict) and isinstance(health.get("node_link"), dict):
            nl = health["node_link"]
            if nl.get("connected"):
                daemon_status = {
                    "active": True,
                    "instance_id": state.get("instance_id") if pid_live else None,
                    "node_id": state.get("node_id") if pid_live else None,
                    "rc_url": state.get("rc_url") if pid_live else None,
                    "app_url": state.get("app_url") if pid_live else None,
                    "reach": "tunnel",
                }

    daemon_active = bool(daemon_status and daemon_status.get("active"))
    active = bool(pid_live or daemon_active)

    if not active:
        runtime_tier, zero_libc = _resolve_posture(state=state)
        return {
            "active": False,
            "instance_id": None,
            "node_id": None,
            "rc_url": None,
            "app_url": None,
            "pid": None,
            "started_at": None,
            "harness_url": resolved_harness,
            "rc_mode": None,
            "reach": str((daemon_status or {}).get("reach") or "offline"),
            "runtime_tier": runtime_tier,
            "zero_libc": zero_libc,
        }

    src = daemon_status if daemon_active and isinstance(daemon_status, dict) else {}
    state_usable = pid_live or (
        daemon_status_authoritative
        and (
            not src.get("node_id")
            or not state.get("node_id")
            or src.get("node_id") == state.get("node_id")
        )
        and (
            not src.get("instance_id")
            or not state.get("instance_id")
            or src.get("instance_id") == state.get("instance_id")
        )
    )
    fallback_state = state if state_usable else {}
    node_id = str(src.get("node_id") or fallback_state.get("node_id") or "").strip() or None
    instance_id = (
        str(src.get("instance_id") or fallback_state.get("instance_id") or "").strip()
        or node_id
    )
    resolved_rc_url = str(src.get("rc_url") or fallback_state.get("rc_url") or "").strip() or (
        rc_url(instance_id) if instance_id else None
    )
    resolved_app_url = str(src.get("app_url") or fallback_state.get("app_url") or "").strip() or (
        app_code_url(node_id) if node_id else None
    )
    reach = str(src.get("reach") or fallback_state.get("reach") or "tunnel")
    default_mode = "daemon" if daemon_active and not pid_live else "cli"
    src_tier = str(src.get("runtime_tier") or "").strip() or None
    src_zero = src.get("zero_libc") if isinstance(src.get("zero_libc"), bool) else None
    runtime_tier, zero_libc = _resolve_posture(
        runtime_tier=src_tier,
        zero_libc=src_zero,
        state=fallback_state,
    )
    return {
        "active": True,
        "instance_id": instance_id,
        "node_id": node_id,
        "rc_url": resolved_rc_url,
        "app_url": resolved_app_url,
        "pid": raw_pid if pid_live else None,
        "started_at": fallback_state.get("started_at"),
        "harness_url": resolved_harness,
        "rc_mode": fallback_state.get("rc_mode") or default_mode,
        "reach": reach,
        "runtime_tier": runtime_tier,
        "zero_libc": zero_libc,
    }


def export_rc_bundle(
    out_dir: Optional[Path] = None,
    *,
    sign: bool = True,
    key_path: Optional[Path] = None,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Export RC state/status metadata to a directory, optionally sealed with ``awseal``."""
    target_dir = Path(out_dir) if out_dir is not None else (rc_state_path().parent / "rc-bundle")
    target_dir.mkdir(parents=True, exist_ok=True)
    status = rc_status()
    state = load_rc_state()
    inst_id = str(status.get("instance_id") or state.get("instance_id") or "local-rc")
    payload: Dict[str, Any] = {
        "schema": "aither.rc.bundle.v1",
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "active": bool(status.get("active")),
        "instance_id": status.get("instance_id"),
        "node_id": status.get("node_id"),
        "rc_url": status.get("rc_url"),
        "app_url": status.get("app_url"),
        "reach": status.get("reach"),
        "rc_mode": status.get("rc_mode") or state.get("rc_mode"),
        "runtime_tier": status.get("runtime_tier", "standard"),
        "zero_libc": bool(status.get("zero_libc", False)),
    }
    if extra_meta:
        payload["meta"] = dict(extra_meta)

    bundle_file = target_dir / "rc_bundle.json"
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    bundle_file.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    seal_file = target_dir / "awseal.json"

    if not sign:
        seal_file.unlink(missing_ok=True)
        return {
            "bundle_dir": str(target_dir),
            "bundle_file": str(bundle_file),
            "sha256": digest,
            "sealed": False,
            "public_key": "",
            "seal_backend": "sha256-only",
        }

    try:
        from awseal import SealError
        from awseal import keys as seal_keys
        from awseal import sign as seal_sign
        from awseal import write as seal_write
    except ImportError as exc:
        if key_path is not None:
            raise RuntimeError(
                f"awseal is required to sign RC bundle with key_path: {exc}"
            ) from exc
        seal_file.unlink(missing_ok=True)
        return {
            "bundle_dir": str(target_dir),
            "bundle_file": str(bundle_file),
            "sha256": digest,
            "sealed": False,
            "public_key": "",
            "seal_backend": "sha256-only",
        }

    if key_path is not None:
        seal = seal_sign(target_dir, key_path=Path(key_path), subject=f"rc:{inst_id}")
        seal_write(seal, target_dir)
        return {
            "bundle_dir": str(target_dir),
            "bundle_file": str(bundle_file),
            "sha256": digest,
            "sealed": True,
            "public_key": seal.public_key,
            "seal_backend": "awseal",
        }

    env_key = (os.environ.get(seal_keys.KEY_PATH_ENV) or "").strip()
    cand_key = Path(env_key).expanduser() if env_key else Path(seal_keys.DEFAULT_KEY_PATH)
    if cand_key.is_file():
        try:
            seal = seal_sign(target_dir, key_path=cand_key, subject=f"rc:{inst_id}")
            seal_write(seal, target_dir)
            return {
                "bundle_dir": str(target_dir),
                "bundle_file": str(bundle_file),
                "sha256": digest,
                "sealed": True,
                "public_key": seal.public_key,
                "seal_backend": "awseal",
            }
        except SealError:
            if env_key:
                raise
    seal_file.unlink(missing_ok=True)
    return {
        "bundle_dir": str(target_dir),
        "bundle_file": str(bundle_file),
        "sha256": digest,
        "sealed": False,
        "public_key": "",
        "seal_backend": "sha256-only",
    }


def _verify_rc_files_compat(
    seal: Any,
    files: Dict[str, str],
    *,
    expect_key: Optional[str],
    load_public_key: Any,
    seal_canonical: Any,
    tree_digest: Any,
    seal_diff: Any,
) -> Dict[str, Any]:
    """Verify ``seal`` against ``files`` using ``awseal >= 0.1.0`` primitives."""
    from cryptography.exceptions import InvalidSignature

    result: Dict[str, Any] = {
        "subject": seal.subject,
        "created": seal.created,
        "public_key": seal.public_key,
        "signature_ok": False,
        "content_ok": False,
        "key_trusted": None,
        "diff": {"added": [], "removed": [], "modified": []},
    }
    pub = load_public_key(seal.public_key)
    try:
        pub.verify(bytes.fromhex(seal.signature), seal_canonical(seal.payload()))
        result["signature_ok"] = True
    except (InvalidSignature, ValueError, TypeError):
        result["signature_ok"] = False
    actual = {str(k): str(v) for k, v in dict(files or {}).items()}
    result["content_ok"] = (
        tree_digest(actual) == seal.tree_digest
        and tree_digest(seal.files) == seal.tree_digest
    )
    result["diff"] = seal_diff(seal.files, actual)
    if expect_key is not None:
        result["key_trusted"] = (
            str(seal.public_key).strip().lower() == expect_key.strip().lower()
        )
    result["ok"] = bool(
        result["signature_ok"]
        and result["content_ok"]
        and (result["key_trusted"] is not False)
    )
    return result


def verify_rc_bundle(
    bundle_dir: Path,
    *,
    expect_key: str = "",
    require_seal: Optional[bool] = None,
) -> Dict[str, Any]:
    """Fail-closed verification for an exported RC bundle directory."""
    root = Path(bundle_dir)
    bundle_file = root / "rc_bundle.json"
    if not bundle_file.is_file():
        raise ValueError(f"RC bundle missing: {bundle_file}")
    raw = bundle_file.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"RC bundle JSON invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("RC bundle payload must be a JSON object")
    digest = hashlib.sha256(raw).hexdigest()

    key = (expect_key or os.environ.get("AITHER_RC_BUNDLE_EXPECT_KEY") or "").strip()
    seal_file = root / "awseal.json"
    must_verify = bool(seal_file.is_file() or key or require_seal)
    if not must_verify:
        return {
            "ok": True,
            "sealed": False,
            "public_key": "",
            "sha256": digest,
            "payload": payload,
        }

    try:
        import awseal as awseal_mod
        from awseal import SealError, file_digest, load, load_public_key, tree_digest
        from awseal import diff as seal_diff
        from awseal.seal import canonical as seal_canonical
    except ImportError as exc:
        raise RuntimeError(
            f"awseal is required to verify sealed RC bundle {root}: {exc}"
        ) from exc

    if key:
        try:
            load_public_key(key)
        except SealError as exc:
            raise ValueError(f"invalid RC bundle expect_key: {exc}") from exc
    try:
        seal = load(root)
    except SealError as exc:
        raise ValueError(f"RC bundle seal load failed: {exc}") from exc
    if "rc_bundle.json" not in seal.files:
        raise ValueError("rc_bundle.json is not covered by awseal.json")

    root_resolved = root.resolve()
    check_files: Dict[str, str] = {}
    for rel_path in seal.files:
        if (
            not isinstance(rel_path, str)
            or not rel_path
            or "\\" in rel_path
            or "\x00" in rel_path
            or PurePosixPath(rel_path).is_absolute()
            or ".." in PurePosixPath(rel_path).parts
        ):
            raise ValueError(f"Unsafe sealed path in {root / 'awseal.json'}: {rel_path!r}")
        raw_target = root / Path(*PurePosixPath(rel_path).parts)
        try:
            resolved_target = raw_target.resolve()
            resolved_target.relative_to(root_resolved)
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"Sealed path escapes bundle directory {root}: {rel_path!r}"
            ) from exc
        if not resolved_target.is_file():
            raise ValueError(f"Sealed RC bundle file missing on disk: {raw_target}")
        try:
            check_files[rel_path] = file_digest(resolved_target)
        except SealError as exc:
            raise ValueError(f"RC bundle sealed file read failed: {exc}") from exc
    check_files["rc_bundle.json"] = digest
    verify_fn = getattr(awseal_mod, "verify_files", None)
    try:
        if callable(verify_fn):
            res = verify_fn(seal, check_files, expect_key=key or None)
        else:
            res = _verify_rc_files_compat(
                seal,
                check_files,
                expect_key=key or None,
                load_public_key=load_public_key,
                seal_canonical=seal_canonical,
                tree_digest=tree_digest,
                seal_diff=seal_diff,
            )
    except SealError as exc:
        raise ValueError(f"RC bundle seal verification failed: {exc}") from exc

    if (
        not res.get("ok")
        or not res.get("signature_ok")
        or not res.get("content_ok")
        or (key and res.get("key_trusted") is not True)
        or seal.files.get("rc_bundle.json") != digest
    ):
        raise ValueError(
            f"RC bundle failed seal verification "
            f"(signature_ok={res.get('signature_ok')}, "
            f"content_ok={res.get('content_ok')}, "
            f"key_trusted={res.get('key_trusted')})"
        )
    return {
        "ok": True,
        "sealed": True,
        "public_key": seal.public_key,
        "sha256": digest,
        "payload": payload,
    }


def rc_off(harness_url: str = "") -> Dict[str, Any]:
    """Stop any live ``adk rc`` process / daemon RC link and clear ``rc-state.json``."""
    state = load_rc_state()
    resolved_harness = (
        (harness_url or "").strip().rstrip("/")
        or str(state.get("harness_url") or "").strip().rstrip("/")
        or default_harness_url()
    )
    stopped_pid: Optional[int] = None
    raw_pid = state.get("pid")
    if (
        isinstance(raw_pid, int)
        and not isinstance(raw_pid, bool)
        and raw_pid > 0
        and raw_pid != os.getpid()
        and _pid_alive(raw_pid)
    ):
        if _terminate_pid(raw_pid):
            stopped_pid = raw_pid

    token = _read_harness_token()
    if token:
        _http_json(f"{resolved_harness}/rc/off", method="POST", token=token)

    try:
        from adk import fleet_enroll

        task = getattr(fleet_enroll, "_node_link_task", None)
        if task is not None and hasattr(task, "done") and not task.done():
            task.cancel()
        if hasattr(fleet_enroll, "_node_link_task"):
            fleet_enroll._node_link_task = None
        if hasattr(fleet_enroll, "_node_link"):
            fleet_enroll._node_link = None
    except ImportError:
        log.debug("rc: fleet_enroll unavailable during rc_off")

    clear_rc_state()
    return {"ok": True, "active": False, "stopped_pid": stopped_pid}


def cmd_rc_status(args: Any) -> int:
    """Print current Remote Control status (human-readable or ``--json``)."""
    harness_url = (getattr(args, "harness_url", None) or "").strip()
    st = rc_status(harness_url=harness_url)
    if getattr(args, "json", False):
        print(json.dumps(st, indent=2))
        return 0
    if not st["active"]:
        print(f"Remote control: inactive (reach: {st['reach']})")
        return 0
    print(f"Remote control: active ({st['reach']})")
    if st.get("instance_id"):
        print(f"  Instance:       {st['instance_id']}")
    if st.get("node_id"):
        print(f"  Device:         {st['node_id']}")
    if st.get("rc_url"):
        print(f"  Remote Control: {st['rc_url']}")
    if st.get("app_url"):
        print(f"  App URL:        {st['app_url']}")
    if st.get("runtime_tier") and st.get("runtime_tier") != "standard":
        print(f"  Runtime tier:   {st['runtime_tier']} (zero_libc={bool(st.get('zero_libc'))})")
    if st.get("pid"):
        print(f"  PID:            {st['pid']}")
    return 0


def cmd_rc_off(args: Any) -> int:
    """Stop Remote Control and clear persisted state."""
    harness_url = (getattr(args, "harness_url", None) or "").strip()
    res = rc_off(harness_url=harness_url)
    if res.get("stopped_pid"):
        print(f"Remote control stopped (pid {res['stopped_pid']}).")
    else:
        print("Remote control stopped.")
    return 0


def _resolve_instance_id(requested_name: Optional[str]) -> str:
    if requested_name is not None and str(requested_name).strip():
        return validate_instance_slug(str(requested_name))
    existing = str(load_rc_state().get("instance_id") or "").strip()
    if existing:
        try:
            return validate_instance_slug(existing)
        except ValueError as exc:
            log.debug("rc: ignoring invalid stored instance_id %r: %s", existing, exc)
    return generate_instance_slug()


def cmd_rc(args) -> int:
    """Enrol this machine and hold the link so its sessions are reachable."""
    subcmd = (getattr(args, "rc_command", None) or "").strip().lower()
    if subcmd == "status":
        return cmd_rc_status(args)
    if subcmd == "off":
        return cmd_rc_off(args)

    requested_name = getattr(args, "name", None)
    try:
        instance_id = _resolve_instance_id(requested_name)
    except ValueError as exc:
        print(f"x {exc}")
        return 2

    node_class = default_node_class(getattr(args, "node_class", None))
    harness_url = (getattr(args, "harness_url", None) or "").strip() or default_harness_url()
    ttl_days = int(getattr(args, "token_ttl_days", None) or 30)
    once = bool(getattr(args, "once", False))
    rc_mode = (
        "antigravity"
        if (os.environ.get("AITHER_RC_MODE") or "").strip().lower() == "antigravity"
        else "cli"
    )
    runtime_tier, zero_libc = _resolve_posture(
        runtime_tier=getattr(args, "runtime_tier", None),
        zero_libc=getattr(args, "zero_libc", None),
        state=load_rc_state(),
    )

    # 1. IDENTITY. `adk enroll` refuses without one, and it must: an
    #    unauthenticated enrol binds the device to the tenant "personal" and
    #    looks like it worked.
    if not _signed_in():
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if getattr(args, "api_key", None) or interactive:
            # ONE command means rc signs you in itself: the device flow on a
            # terminal, the key when one was passed. `cmd_login` reads flags the
            # rc parser never defines (`portal_url` crashed the --api-key path
            # with AttributeError), so it gets its own complete namespace.
            from adk.cli import cmd_login  # local import: cli imports this module

            rc = cmd_login(argparse.Namespace(
                portal_url=None, api_key=getattr(args, "api_key", None),
                email=None, password=None, github=False, no_sync=False,
            ))
            if rc != 0:
                return rc
            if not _signed_in():
                print("x Sign-in finished but no identity was saved; run `adk login`.")
                return 1
        else:
            print("x Not signed in.")
            print()
            print("  adk login                    # browser device flow")
            print("  adk login --api-key <key>    # headless / phone / VM")
            print("  then re-run: adk rc")
            return 1

    # 2. THE SCOPED TOKEN, minted fresh every run. Never the daemon's root token.
    harness_token = ""
    try:
        from adk.harnesses.daemon import SCOPED_LINK_PATHS, mint_scoped_token

        harness_token = mint_scoped_token(
            f"node:{os.environ.get('AITHER_NODE_ID') or 'this-device'}",
            ttl_days=ttl_days,
        )
        scope_note = ", ".join(SCOPED_LINK_PATHS)
    except Exception as e:  # noqa: BLE001
        # LOUD. Falling back to the root token here would be the whole point of
        # this command, inverted.
        print(f"x Could not mint a scoped harness token: {e}")
        print("  Refusing to advertise this machine with the daemon's root token.")
        return 1

    ready, detail = probe_harness(harness_url, harness_token)
    status_label = f"ready — {detail}" if ready else f"NOT answering — {detail}"
    print("Remote control")
    print(f"  Harness:  {harness_url}  ({status_label})")
    print(f"  Scope:    {scope_note}  (root token is NOT advertised)")
    if not ready:
        # Not fatal. The daemon may still be starting, and `harness_ready` is a
        # field on the heartbeat precisely so this state is visible rather than
        # guessed. Say it plainly instead of failing the command.
        print("  Note:     start the shell (or `adk-serve`) so the session daemon is up;")
        print("            the heartbeat re-reports harness_ready every 60s.")

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    initial_rc_url = rc_url(instance_id)
    save_rc_state({
        "instance_id": instance_id,
        "node_id": "",
        "rc_url": initial_rc_url,
        "app_url": "",
        "pid": os.getpid(),
        "started_at": started_at,
        "harness_url": harness_url,
        "rc_mode": rc_mode,
        "runtime_tier": runtime_tier,
        "zero_libc": zero_libc,
    })

    # 3. ENROL + HOLD. The link is what makes the sessions reachable; without it
    #    this is just an enrollment.
    #
    # AITHER_FLEET_ENROLL gates the BOOT-time enrollment path, so that a machine
    # does not phone home just because adk is installed. An operator typing
    # `adk rc` has already decided, exactly as `adk enroll` reasons.
    os.environ.setdefault("AITHER_FLEET_ENROLL", "1")
    from adk.fleet_enroll import active_node_link, enroll_on_boot

    async def _run() -> int:
        result = await enroll_on_boot(
            enable_heartbeat=True,
            inference_url=None,
            node_class=node_class,
            start_link=True,
            harness_url=harness_url,
            harness_token=harness_token,
        )
        if not result.get("enrolled"):
            clear_rc_state()
            if result.get("http_status"):
                print(f"x Enrollment refused: HTTP {result['http_status']}")
                print(result.get("body") or "(empty body)")
            else:
                print(f"ERROR: {result.get('error', 'enrollment failed')}")
            return 1
        node_id = str(result.get("node_id") or "?")
        url_rc = rc_url(instance_id)
        url_app = app_code_url(node_id)
        save_rc_state({
            "instance_id": instance_id,
            "node_id": node_id,
            "rc_url": url_rc,
            "app_url": url_app,
            "pid": os.getpid(),
            "started_at": started_at,
            "harness_url": harness_url,
            "rc_mode": rc_mode,
            "runtime_tier": runtime_tier,
            "zero_libc": zero_libc,
        })
        link = active_node_link()
        if link is not None:
            link.instance_id = instance_id
            link.rc_mode = rc_mode
            link.runtime_tier = runtime_tier
            link.zero_libc = zero_libc
            if getattr(link, "_ws", None) is not None and hasattr(link, "send_hello"):
                await link.send_hello()
        print(f"  Device:         {node_id} ({node_class})")
        print(f"  Instance:       {instance_id}")
        print(f"  Remote Control: {url_rc}   (scan to open on your phone)")
        print(f"  App URL:        {url_app}")
        from adk.term_qr import print_qr

        print_qr(url_rc, print, indent="    ")
        print()
        if once:
            return 0
        print("Holding the link. Ctrl-C to stop.")
        last = None
        try:
            while True:
                await asyncio.sleep(15)
                now = link.reach_kind() if link is not None else "none"
                if now != last:
                    # State CHANGES only. A line every 15s is noise nobody reads,
                    # and the transition is the only thing worth seeing.
                    err = getattr(link, "last_error", "") if link is not None else ""
                    print(f"  reach: {now}" + (f" ({err})" if err and now != "ws" else ""))
                    last = now
        except (KeyboardInterrupt, asyncio.CancelledError):
            if load_rc_state().get("pid") == os.getpid():
                clear_rc_state()
            print("\nStopped. The device stays enrolled; re-run `adk rc` to reconnect.")
            return 0

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        if load_rc_state().get("pid") == os.getpid():
            clear_rc_state()
        print("\nStopped.")
        return 0


def _build_main_parsers() -> Tuple[
    argparse.ArgumentParser, argparse.ArgumentParser, argparse.ArgumentParser
]:
    start_p = argparse.ArgumentParser(prog="adk rc", description="Remote control")
    start_p.add_argument("--name", default=None, help="Custom instance slug (e.g. dev-box)")
    start_p.add_argument(
        "--node-class",
        choices=["phone", "laptop", "desktop", "deck", "spark", "sovereign"],
        default=None,
    )
    start_p.add_argument("--harness-url", default=None)
    start_p.add_argument("--token-ttl-days", type=int, default=30)
    start_p.add_argument("--api-key", default=None)
    start_p.add_argument("--once", action="store_true")
    start_p.add_argument(
        "--runtime-tier",
        choices=["standard", "zero-libc", "awzeal"],
        default=None,
        help="Runtime TCB posture advertised on hello (standard|zero-libc|awzeal)",
    )
    start_p.add_argument(
        "--zero-libc",
        action="store_true",
        default=None,
        help="Advertise zero-libc minimal-TCB posture",
    )

    status_p = argparse.ArgumentParser(prog="adk rc status", description="Remote control status")
    status_p.add_argument("--json", action="store_true", help="Print JSON status")
    status_p.add_argument("--harness-url", default=None)

    off_p = argparse.ArgumentParser(prog="adk rc off", description="Stop remote control")
    off_p.add_argument("--harness-url", default=None)
    return start_p, status_p, off_p


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for ``adk rc`` (supports ``status``, ``off``, and ``--name``)."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    start_p, status_p, off_p = _build_main_parsers()
    if raw_argv and raw_argv[0] == "status":
        ns = status_p.parse_args(raw_argv[1:])
        ns.rc_command = "status"
        return cmd_rc(ns)
    if raw_argv and raw_argv[0] == "off":
        ns = off_p.parse_args(raw_argv[1:])
        ns.rc_command = "off"
        return cmd_rc(ns)
    ns = start_p.parse_args(raw_argv)
    ns.rc_command = None
    return cmd_rc(ns)
