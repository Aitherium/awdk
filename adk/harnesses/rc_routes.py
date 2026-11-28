"""Remote Control status and stop routes on the harness daemon (``:8362``).

Exposes ``GET /rc/status`` (readable by owner and paired ``awconnect`` tokens
via :data:`adk.harnesses.pairing.AWCONNECT_PATHS`) and ``POST /rc/off``
(owner-only) so the desktop extension, CLI, and local dashboard can inspect or
stop an active reverse link without touching remote cloud APIs.
"""

from __future__ import annotations

__all__ = [
    "mount",
    "rc_state_path",
    "read_rc_state",
    "get_rc_status",
    "stop_rc",
]

import asyncio
import inspect
import json
import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("adk.harnesses.rc_routes")

_APP_CODE_BASE = "https://app.aitherium.com/code"
_TUNNEL_RC_BASE = "https://a30.link"


def rc_state_path() -> Path:
    """Resolve the persisted RC state file path."""
    explicit = (os.environ.get("AITHER_RC_STATE_FILE") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = (os.environ.get("AITHER_HOME") or "").strip()
    if home:
        return Path(home).expanduser() / "rc-state.json"
    return Path.home() / ".aither" / "rc-state.json"


def read_rc_state(path: Optional[Path] = None) -> dict[str, Any]:
    """Read ``rc-state.json`` safely; returns ``{}`` when absent or malformed."""
    target = path or rc_state_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        log.warning("rc_routes: invalid JSON in %s: %s", target, exc)
        return {}
    return data if isinstance(data, dict) else {}


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
        log.debug("rc_routes: turn_journal unavailable; falling back to OS pid probe")
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
            return code.value == 259  # STILL_ACTIVE
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
    """Best-effort termination of an external ``adk rc`` process."""
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
            log.warning("rc_routes: taskkill failed for pid %s: %s", pid, exc)
            return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return True
    except OSError as exc:
        log.warning("rc_routes: SIGTERM failed for pid %s: %s", pid, exc)
        return False


def _default_tunnel_base() -> str:
    explicit = (os.environ.get("AITHER_RC_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    try:
        from adk.node_link import default_tunnel_url

        return default_tunnel_url().rstrip("/")
    except ImportError:
        return _TUNNEL_RC_BASE


def _active_link(link_provider: Optional[Callable[[], Any]] = None) -> Any:
    if link_provider is not None:
        return link_provider()
    try:
        from adk.fleet_enroll import active_node_link

        return active_node_link()
    except ImportError:
        return None


def _resolve_posture(
    *,
    runtime_tier: Optional[str] = None,
    zero_libc: Optional[bool] = None,
    state: Optional[dict[str, Any]] = None,
) -> tuple[str, bool]:
    try:
        from adk.node_link import resolve_runtime_posture

        return resolve_runtime_posture(
            runtime_tier=runtime_tier,
            zero_libc=zero_libc,
            state=state,
        )
    except ImportError:
        raw_tier = (
            str(runtime_tier or "").strip()
            or (os.environ.get("AITHER_RUNTIME_TIER") or "").strip()
            or (str(state.get("runtime_tier") or "").strip() if isinstance(state, dict) else "")
        )
        tier_low = raw_tier.lower()
        tier = tier_low if tier_low in ("standard", "zero-libc", "awzeal") else "standard"
        z_val: Optional[bool] = zero_libc if isinstance(zero_libc, bool) else None
        if z_val is None:
            env_z = (os.environ.get("AITHER_ZERO_LIBC") or "").strip().lower()
            if env_z in ("1", "true", "yes", "on"):
                z_val = True
            elif env_z in ("0", "false", "no", "off"):
                z_val = False
        if z_val is None and isinstance(state, dict) and isinstance(state.get("zero_libc"), bool):
            z_val = state["zero_libc"]
        if tier in ("zero-libc", "awzeal") and z_val is None:
            z_val = True
        elif z_val is True and not raw_tier:
            tier = "zero-libc"
        return tier, bool(z_val) if z_val is not None else False


def get_rc_status(
    *,
    state_path: Optional[Path] = None,
    link_provider: Optional[Callable[[], Any]] = None,
    pid_alive_fn: Optional[Callable[[int], bool]] = None,
) -> dict[str, Any]:
    """Reconcile in-process ``NodeLink`` state and persisted ``rc-state.json``."""
    state = read_rc_state(state_path)
    raw_pid = state.get("pid")
    alive_check = pid_alive_fn or _pid_alive
    pid_live = (
        bool(alive_check(raw_pid))
        if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and raw_pid > 0
        else False
    )

    link = _active_link(link_provider)
    link_connected = bool(getattr(link, "connected", False)) if link is not None else False
    link_node_id = (
        str(getattr(link, "node_id", "") or "").strip() or None if link is not None else None
    )
    link_instance_id = (
        str(getattr(link, "instance_id", "") or "").strip() or None
        if link is not None
        else None
    )
    link_rc_url = (
        str(getattr(link, "rc_url", "") or "").strip() or None if link is not None else None
    )
    link_app_url = (
        str(getattr(link, "app_url", "") or "").strip() or None if link is not None else None
    )

    active = bool(link_connected or pid_live)
    if link_connected or (pid_live and str(state.get("reach") or "").strip() != "local"):
        reach = "tunnel"
    elif link is not None or pid_live:
        reach = "local"
    else:
        reach = "offline"

    if not active:
        runtime_tier, zero_libc = _resolve_posture(state=state)
        return {
            "active": False,
            "instance_id": None,
            "node_id": None,
            "rc_url": None,
            "app_url": None,
            "reach": reach,
            "runtime_tier": runtime_tier,
            "zero_libc": zero_libc,
        }

    state_node_id = str(state.get("node_id") or "").strip() or None
    state_instance_id = str(state.get("instance_id") or "").strip() or None
    state_usable = pid_live or (
        link_connected
        and (state_node_id is None or link_node_id is None or state_node_id == link_node_id)
        and (
            state_instance_id is None
            or link_instance_id is None
            or state_instance_id == link_instance_id
        )
    )

    node_id = link_node_id or (state_node_id if state_usable else None)
    instance_id = link_instance_id or (state_instance_id if state_usable else None)
    if not instance_id:
        instance_id = node_id

    rc_url = link_rc_url or (
        str(state.get("rc_url") or "").strip() or None
        if state_usable and (state_instance_id == instance_id or state_instance_id is None)
        else None
    )
    if not rc_url and instance_id:
        rc_url = f"{_default_tunnel_base()}/r/{instance_id}"

    app_url = link_app_url or (
        str(state.get("app_url") or "").strip() or None
        if state_usable and (state_node_id == node_id or state_node_id is None)
        else None
    )
    if not app_url and node_id:
        app_url = f"{_APP_CODE_BASE}/{node_id}"

    link_tier = (
        getattr(link, "_runtime_tier_override", getattr(link, "runtime_tier", None))
        if link is not None
        else None
    )
    link_zero = (
        getattr(link, "_zero_libc_override", getattr(link, "zero_libc", None))
        if link is not None
        else None
    )
    runtime_tier, zero_libc = _resolve_posture(
        runtime_tier=link_tier,
        zero_libc=link_zero,
        state=state if state_usable else None,
    )

    return {
        "active": True,
        "instance_id": instance_id,
        "node_id": node_id,
        "rc_url": rc_url,
        "app_url": app_url,
        "reach": reach,
        "runtime_tier": runtime_tier,
        "zero_libc": zero_libc,
    }


async def stop_rc(
    *,
    state_path: Optional[Path] = None,
    link_provider: Optional[Callable[[], Any]] = None,
    stop_link_fn: Optional[Callable[[], Any]] = None,
    pid_alive_fn: Optional[Callable[[int], bool]] = None,
    terminate_pid_fn: Optional[Callable[[int], bool]] = None,
) -> dict[str, Any]:
    """Stop active in-process or external Remote Control link and clear ``rc-state.json``."""
    target = state_path or rc_state_path()
    state = read_rc_state(target)
    ok = True

    if stop_link_fn is not None:
        res = stop_link_fn()
        if inspect.isawaitable(res):
            await res
    else:
        link = _active_link(link_provider)
        if link is not None:
            link.connected = False
            ws = getattr(link, "_ws", None)
            if ws is not None and hasattr(ws, "close"):
                try:
                    closed = ws.close()
                    if inspect.isawaitable(closed):
                        await closed
                except Exception as exc:  # noqa: BLE001
                    log.debug("rc_routes: ws close failed: %s", exc)
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
            log.debug("rc_routes: fleet_enroll unavailable during stop_rc")
        try:
            from adk import autolink

            if hasattr(autolink, "stop_autolink"):
                autolink.stop_autolink()
        except ImportError:
            log.debug("rc_routes: autolink unavailable during stop_rc")

    raw_pid = state.get("pid")
    alive_check = pid_alive_fn or _pid_alive
    term_fn = terminate_pid_fn or _terminate_pid
    if (
        isinstance(raw_pid, int)
        and not isinstance(raw_pid, bool)
        and raw_pid > 0
        and raw_pid != os.getpid()
        and alive_check(raw_pid)
    ):
        if not await asyncio.to_thread(term_fn, raw_pid):
            ok = False

    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("rc_routes: could not remove %s: %s", target, exc)
        ok = False

    return {"ok": ok, "active": False}


def mount(
    app: Any,
    auth: Any,
    require_owner: Any,
    *,
    state_path: Optional[Path] = None,
    link_provider: Optional[Callable[[], Any]] = None,
    stop_link_fn: Optional[Callable[[], Any]] = None,
) -> None:
    """Register ``GET /rc/status`` and ``POST /rc/off`` onto the FastAPI app."""
    from fastapi import Depends

    @app.get("/rc/status", dependencies=[Depends(auth)])
    def rc_status() -> dict[str, Any]:
        return get_rc_status(state_path=state_path, link_provider=link_provider)

    @app.post("/rc/off", dependencies=[Depends(require_owner)])
    async def rc_off() -> dict[str, Any]:
        return await stop_rc(
            state_path=state_path,
            link_provider=link_provider,
            stop_link_fn=stop_link_fn,
        )
