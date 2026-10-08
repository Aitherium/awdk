"""Join = contribute: decide how much disk this device lends to the mesh storage pool.

When a device enrolls it can also become a storage peer: it measures the disk under
its contribution path, decides whether and how much to lend, and registers with the
mesh storage service (``POST /strata/mesh/peers/register``). Registration is
idempotent by ``node_id``, so the same call is the keepalive.

The policy, in one place so every join path agrees:

* **Opt-in, for every device class** (owner, 2026-10-07: "all configurable and opt
  in"). Precedence: the ``enabled=`` argument, then ``AITHER_CONTRIBUTE_STORAGE=1|0``,
  then the ``storage:`` section of ``~/.aither/config.yaml`` (``adk storage on``), then
  ``storage`` in the owner's ``lend:`` list. Nothing set means nothing is lent. Until
  2026-10-07 servers and workstations contributed by default; those nodes now stop
  unless they had opted in explicitly (the env flag or the lend list), which still
  wins exactly as before.
* **Bounded.** At most ``share`` of FREE space (default 30%,
  ``AITHER_STORAGE_CONTRIB_SHARE``), never more than the owner's ``quota_gb``, and
  never so much that less than ``reserve`` stays free (default 20 GiB,
  ``AITHER_STORAGE_RESERVE_GB``). Under 1 GiB is not worth a peer and contributes
  nothing.
* **Paused, not lost.** On battery or a metered network (each on by default,
  ``pause_on_battery`` / ``pause_on_metered``) the device stops serving; the
  keepalive resumes it when the condition clears.
* **Tiered by class.** Fixed machines offer warm + cold; portable ones offer
  cache + warm only (the pool also refuses to place cold/lockbox copies on them).

Everything is best-effort and never raises into enrollment.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_SHARE",
    "DEFAULT_RESERVE_BYTES",
    "MIN_CONTRIBUTION_BYTES",
    "DiskProbe",
    "StorageSettings",
    "load_settings",
    "save_settings",
    "power_state",
    "device_class_for",
    "measure_disk",
    "plan_contribution",
    "contribution_enabled",
    "decide_contribution",
    "storage_capability",
    "used_bytes",
    "build_storage_peer_payload",
    "resolve_storage_endpoint",
    "register_storage_peer",
    "contribute_after_enroll",
    "start_storage_keepalive",
    "stop_storage_keepalive",
]

import asyncio
import logging
import os
import platform
import shutil
from pathlib import Path
from typing import Any, Dict, Mapping, NamedTuple, Optional, Tuple

log = logging.getLogger("adk.storage_contribution")

GIB = 1024 ** 3

#: Largest share of FREE space a device lends by default.
DEFAULT_SHARE = 0.30

#: Free space a contribution may never eat into.
DEFAULT_RESERVE_BYTES = 20 * GIB

#: Below this a contribution is noise — register nothing.
MIN_CONTRIBUTION_BYTES = 1 * GIB

#: Seconds between keepalive re-registers. The pool marks a peer stale after
#: 300 s of silence, so this stays well inside it.
KEEPALIVE_INTERVAL_S = 120.0

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")

#: Enrollment node classes -> storage device classes.
_NODE_TO_DEVICE = {
    "phone": "phone",
    "laptop": "laptop",
    "deck": "laptop",
    "desktop": "workstation",
    "spark": "server",
    "sovereign": "server",
}

_PORTABLE = frozenset({"laptop", "phone"})


class DiskProbe(NamedTuple):
    """What the contribution path's filesystem looks like right now."""

    path: str
    total_bytes: int
    free_bytes: int


class StorageSettings(NamedTuple):
    """The owner's ``storage:`` section. ``enabled=None`` = never set (= off)."""

    enabled: Optional[bool] = None
    quota_gb: Optional[float] = None
    path: str = ""
    pause_on_battery: bool = True
    pause_on_metered: bool = True


def _settings_from(section: Any) -> StorageSettings:
    if not isinstance(section, Mapping):
        return StorageSettings()
    enabled = section.get("enabled")
    quota = section.get("quota_gb")
    try:
        quota_f: Optional[float] = float(quota) if quota not in (None, "") else None
    except (TypeError, ValueError):
        quota_f = None
    if quota_f is not None and quota_f <= 0:
        quota_f = None
    path = str(section.get("path") or "").strip()
    return StorageSettings(
        enabled=enabled if isinstance(enabled, bool) else None,
        quota_gb=quota_f,
        path=path if path and Path(path).is_absolute() else "",
        pause_on_battery=section.get("pause_on_battery") is not False,
        pause_on_metered=section.get("pause_on_metered") is not False,
    )


def _saved_config() -> Dict[str, Any]:
    try:
        from adk.config import load_saved_config
        return dict(load_saved_config() or {})
    except Exception as e:  # noqa: BLE001 -- an unreadable config is "nothing set"
        log.debug("saved config unreadable: %s", e)
        return {}


def load_settings(saved: Optional[Mapping[str, Any]] = None) -> StorageSettings:
    """The ``storage:`` section of the saved config (``~/.aither/config.yaml``)."""
    return _settings_from((saved if saved is not None else _saved_config()).get("storage"))


def save_settings(settings: StorageSettings) -> StorageSettings:
    """Persist ``settings`` as the ``storage:`` section; returns what was written."""
    from adk.config import save_saved_config

    section: Dict[str, Any] = {
        "enabled": bool(settings.enabled),
        "pause_on_battery": bool(settings.pause_on_battery),
        "pause_on_metered": bool(settings.pause_on_metered),
    }
    if settings.quota_gb:
        section["quota_gb"] = float(settings.quota_gb)
    if settings.path:
        section["path"] = settings.path
    save_saved_config({"storage": section})  # merges; other keys are kept
    return _settings_from(section)


def _lend_has_storage(saved: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    try:
        from adk.node_capabilities import lend_set
        return "storage" in lend_set(saved, env)
    except Exception:  # noqa: BLE001
        return False


def _metered(env: Mapping[str, str]) -> bool:
    flag = (env.get("AITHER_NETWORK_METERED") or "").strip().lower()
    if flag in _TRUE:
        return True
    if flag in _FALSE:
        return False
    if platform.system() != "Linux" or not shutil.which("nmcli"):
        return False
    try:
        import subprocess
        out = subprocess.run(["nmcli", "-t", "-f", "GENERAL.METERED", "dev", "show"],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:  # noqa: BLE001
        return False
    return any(line.split(":", 1)[-1].startswith("yes") for line in out.splitlines())


def _on_battery(env: Mapping[str, str]) -> bool:
    flag = (env.get("AITHER_ON_BATTERY") or "").strip().lower()
    if flag in _TRUE:
        return True
    if flag in _FALSE:
        return False
    try:
        import psutil  # optional
        bat = psutil.sensors_battery()
    except Exception:  # noqa: BLE001 -- no psutil, or no battery API
        return False
    return bool(bat is not None and bat.power_plugged is False)


def power_state(env: Optional[Mapping[str, str]] = None) -> Dict[str, bool]:
    """``{"on_battery", "metered"}``, best-effort; an unknown reads as False."""
    source = os.environ if env is None else env
    return {"on_battery": _on_battery(source), "metered": _metered(source)}


def device_class_for(node_class: str) -> str:
    """Map an enrollment ``node_class`` onto a storage device class.

    Unknown classes map to ``laptop`` — the conservative answer (no cold tier,
    contribution off by default).
    """
    v = (node_class or "").strip().lower()
    if v in ("server", "workstation", "laptop", "phone", "cloud"):
        return v
    return _NODE_TO_DEVICE.get(v, "laptop")


def _default_path(settings: Optional[StorageSettings] = None) -> Path:
    env = (os.environ.get("AITHER_STORAGE_PATH") or "").strip()
    if env and Path(env).is_absolute():
        return Path(env)
    if settings is not None and settings.path:
        return Path(settings.path)
    home = os.environ.get("AITHER_HOME") or str(Path.home() / ".aither")
    return Path(home) / "strata"


def measure_disk(path: Optional[str] = None,
                 settings: Optional[StorageSettings] = None) -> DiskProbe:
    """Measure total/free bytes of the filesystem holding the contribution path.

    The path need not exist yet: the nearest existing ancestor is measured (the
    filesystem is what matters), and nothing is created.

    Raises:
        OSError: no ancestor of the path is measurable.
    """
    target = Path(path) if path else _default_path(settings)
    probe = target
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(str(probe))
    return DiskProbe(str(target), int(usage.total), int(usage.free))


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return min(hi, max(lo, v))


def plan_contribution(
    free_bytes: int,
    *,
    share: Optional[float] = None,
    reserve_bytes: Optional[int] = None,
    min_bytes: int = MIN_CONTRIBUTION_BYTES,
    quota_bytes: Optional[int] = None,
) -> int:
    """Bytes to lend out of ``free_bytes``: ``min(share * free, free - reserve, quota)``.

    Returns 0 when that is under ``min_bytes`` (including when the disk is already
    below the reserve). ``share``/``reserve_bytes`` default to the env-tunable
    policy (:data:`DEFAULT_SHARE`, :data:`DEFAULT_RESERVE_BYTES`); ``quota_bytes`` is
    the owner's cap (None = no cap beyond the share).
    """
    if share is None:
        share = _env_float("AITHER_STORAGE_CONTRIB_SHARE", DEFAULT_SHARE, 0.0, 0.9)
    if reserve_bytes is None:
        reserve_bytes = int(
            _env_float("AITHER_STORAGE_RESERVE_GB", DEFAULT_RESERVE_BYTES / GIB, 0.0, 1e6) * GIB
        )
    free = max(0, int(free_bytes))
    amount = min(int(free * max(0.0, share)), free - max(0, int(reserve_bytes)))
    if quota_bytes is not None and quota_bytes > 0:
        amount = min(amount, int(quota_bytes))
    return amount if amount >= min_bytes else 0


def contribution_enabled(
    device_class: str,
    *,
    enabled: Optional[bool] = None,
    env: Optional[Mapping[str, str]] = None,
    saved: Optional[Mapping[str, Any]] = None,
) -> Tuple[bool, str]:
    """Whether this device should contribute, and why. Off unless opted in.

    Precedence: the explicit ``enabled`` argument, then ``AITHER_CONTRIBUTE_STORAGE``,
    then the saved ``storage.enabled`` (``adk storage on|off``), then ``storage`` in the
    owner's ``lend:`` list. ``saved`` is the saved config (read when None, unless an
    explicit ``env`` was given: tests pass ``env={}`` to mean "nothing configured").
    """
    if enabled is not None:
        return bool(enabled), "explicit " + ("opt-in" if enabled else "opt-out")
    source = os.environ if env is None else env
    flag = (source.get("AITHER_CONTRIBUTE_STORAGE") or "").strip().lower()
    if flag in _TRUE:
        return True, "AITHER_CONTRIBUTE_STORAGE opt-in"
    if flag in _FALSE:
        return False, "AITHER_CONTRIBUTE_STORAGE opt-out"
    if saved is None:
        saved = _saved_config() if env is None else {}
    settings = load_settings(saved)
    if settings.enabled is True:
        return True, "opted in (adk storage on)"
    if settings.enabled is False:
        return False, "opted out (adk storage off)"
    if _lend_has_storage(saved, source):
        return True, "storage is in the lend list"
    return False, (f"default off for {device_class}: storage is opt-in "
                   "(adk storage on --quota <GB>)")


def decide_contribution(
    node_class: str,
    *,
    path: Optional[str] = None,
    enabled: Optional[bool] = None,
    probe: Optional[DiskProbe] = None,
    env: Optional[Mapping[str, str]] = None,
    saved: Optional[Mapping[str, Any]] = None,
    power: Optional[Mapping[str, bool]] = None,
) -> Dict[str, Any]:
    """The whole decision for one device, as data (no network).

    Returns ``{"contribute", "reason", "device_class", "path", "total_bytes",
    "free_bytes", "contributed_bytes", "quota_bytes", "paused"}``. ``contribute`` is
    False when not opted in, when paused (battery / metered), when the disk cannot be
    measured, or when the bounded amount is below the minimum. ``paused`` names why a
    device that IS opted in is resting ("" otherwise). ``power`` overrides
    :func:`power_state` (tests).
    """
    device_class = device_class_for(node_class)
    if saved is None:
        saved = _saved_config() if env is None else {}
    on, why = contribution_enabled(device_class, enabled=enabled, env=env, saved=saved)
    settings = load_settings(saved)
    quota = int(settings.quota_gb * GIB) if settings.quota_gb else None
    out: Dict[str, Any] = {
        "contribute": False, "reason": why, "device_class": device_class,
        "path": "", "total_bytes": 0, "free_bytes": 0, "contributed_bytes": 0,
        "quota_bytes": quota or 0, "paused": "",
    }
    if not on:
        return out
    state = dict(power) if power is not None else power_state(env)
    if settings.pause_on_battery and state.get("on_battery"):
        out.update(paused="on battery", reason="paused: on battery")
        return out
    if settings.pause_on_metered and state.get("metered"):
        out.update(paused="metered network", reason="paused: metered network")
        return out
    try:
        disk = probe or measure_disk(path, settings)
    except OSError as e:
        out["reason"] = f"disk not measurable: {e}"
        return out
    amount = plan_contribution(disk.free_bytes, quota_bytes=quota)
    out.update(path=disk.path, total_bytes=disk.total_bytes, free_bytes=disk.free_bytes,
               contributed_bytes=amount)
    if amount <= 0:
        out["reason"] = "too little free space after the reserve"
        return out
    out["contribute"] = True
    return out


def used_bytes(path: str, *, max_files: int = 20000) -> int:
    """Bytes the pool has placed under the contribution path (bounded walk; 0 if none)."""
    total = 0
    try:
        for n, p in enumerate(Path(path).rglob("*")):
            if n >= max_files:
                break
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def storage_capability(
    node_class: str = "",
    *,
    saved: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    power: Optional[Mapping[str, bool]] = None,
    probe: Optional[DiskProbe] = None,
) -> Dict[str, Any]:
    """What a heartbeat says about this device's storage lending.

    ``{"opted_in", "serving", "reason", "detail"}``; ``detail`` holds at most six
    scalars (the platform keeps eight per kind): contributed / quota / used / free in
    GiB, the pause reason, and the device class. Nothing is measured unless opted in.
    """
    if saved is None:
        saved = _saved_config() if env is None else {}
    opted, _why = contribution_enabled(device_class_for(node_class), env=env, saved=saved)
    decision = decide_contribution(node_class, saved=saved, env=env, power=power, probe=probe)
    used = used_bytes(decision["path"]) if decision["contribute"] and decision["path"] else 0
    return {
        "opted_in": bool(opted),
        "serving": bool(decision["contribute"]),
        "reason": decision["reason"],
        "detail": {
            "contributed_gib": round(decision["contributed_bytes"] / GIB, 2),
            "quota_gib": round(decision["quota_bytes"] / GIB, 2),
            "used_gib": round(used / GIB, 2),
            "free_gib": round(decision["free_bytes"] / GIB, 1),
            "paused": decision["paused"],
            "device_class": decision["device_class"],
        },
    }


def build_storage_peer_payload(
    node_id: str,
    decision: Mapping[str, Any],
    *,
    hostname: Optional[str] = None,
    failure_domain: str = "",
) -> Dict[str, Any]:
    """The register body for the mesh storage service."""
    device_class = str(decision.get("device_class") or "laptop")
    tiers = ["cache", "warm"] if device_class in _PORTABLE else ["warm", "cold"]
    return {
        "node_id": node_id,
        "hostname": hostname if hostname is not None else platform.node(),
        "role": "cache" if device_class in _PORTABLE else "edge",
        "volume_path": str(decision.get("path") or ""),
        "contributed_bytes": int(decision.get("contributed_bytes") or 0),
        "free_bytes": int(decision.get("free_bytes") or 0),
        "total_bytes": int(decision.get("total_bytes") or 0),
        "storage_tiers": tiers,
        "device_class": device_class,
        "failure_domain": (
            failure_domain or os.environ.get("AITHER_FAILURE_DOMAIN", "")
        ).strip(),
    }


def resolve_storage_endpoint(enroll_response: Optional[Mapping[str, Any]] = None) -> str:
    """Base URL of the mesh storage service, or ``""`` when none is known.

    ``AITHER_STRATA_URL`` wins; then a ``strata_url`` the enrollment response
    handed back; then the mesh proxy (``AITHER_MESH_URL`` or
    ``AITHER_AITHERNET_URL`` + ``/proxy/strata``). Nothing is guessed.
    """
    direct = (os.environ.get("AITHER_STRATA_URL") or "").strip()
    if direct:
        return direct.rstrip("/")
    if enroll_response and enroll_response.get("strata_url"):
        return str(enroll_response["strata_url"]).rstrip("/")
    mesh = (os.environ.get("AITHER_MESH_URL") or os.environ.get("AITHER_AITHERNET_URL") or "")
    mesh = mesh.strip()
    if mesh:
        return mesh.rstrip("/") + "/proxy/strata"
    return ""


async def register_storage_peer(
    base_url: str,
    token: str,
    payload: Mapping[str, Any],
    *,
    client: Any = None,
) -> Dict[str, Any]:
    """POST the register body. Returns ``{"registered", "peer_id", "http_status", ...}``.

    Never raises; a transport failure is ``{"registered": False, "error": ...}``.
    """
    url = base_url.rstrip("/") + "/strata/mesh/peers/register"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        if client is None:
            import httpx

            async with httpx.AsyncClient(timeout=15.0) as c:
                resp = await c.post(url, json=dict(payload), headers=headers)
        else:
            resp = await client.post(url, json=dict(payload), headers=headers)
    except Exception as e:  # noqa: BLE001 -- contribution must never break enrollment
        return {"registered": False, "error": str(e), "url": url}
    if resp.status_code != 200:
        return {"registered": False, "http_status": resp.status_code,
                "error": (resp.text or "")[:200], "url": url}
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return {"registered": True, "http_status": 200, "peer_id": body.get("peer_id", ""),
            "url": url}


_keepalive: Dict[str, Any] = {"task": None}


async def _keepalive_loop(base_url: str, token: str, node_id: str, node_class: str,
                          path: Optional[str], interval_s: float,
                          enabled: Optional[bool] = None) -> None:
    while True:
        await asyncio.sleep(interval_s)
        # Re-read the owner's choice every round: `adk storage off` stops the loop, a
        # pause (battery, metered) only skips this round so it resumes on its own.
        decision = decide_contribution(node_class, path=path, enabled=enabled)
        if decision["paused"]:
            log.info("storage keepalive: paused (%s)", decision["paused"])
            continue
        if not decision["contribute"]:
            log.info("storage keepalive: no longer contributing (%s)", decision["reason"])
            return
        res = await register_storage_peer(
            base_url, token, build_storage_peer_payload(node_id, decision)
        )
        if not res["registered"]:
            log.warning("storage keepalive re-register failed: %s",
                        res.get("http_status") or res.get("error"))


def start_storage_keepalive(base_url: str, token: str, node_id: str, node_class: str,
                            path: Optional[str] = None,
                            interval_s: float = KEEPALIVE_INTERVAL_S,
                            enabled: Optional[bool] = None) -> bool:
    """Re-register on an interval so the pool keeps this peer live. False = no loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    old = _keepalive.get("task")
    if old is not None and not old.done():
        old.cancel()
    _keepalive["task"] = loop.create_task(
        _keepalive_loop(base_url, token, node_id, node_class, path, interval_s, enabled)
    )
    return True


def stop_storage_keepalive() -> None:
    """Cancel the keepalive task, if one is running."""
    task = _keepalive.get("task")
    if task is not None and not task.done():
        task.cancel()
    _keepalive["task"] = None


async def contribute_after_enroll(
    node_id: str,
    node_class: str,
    token: str,
    *,
    enroll_response: Optional[Mapping[str, Any]] = None,
    enabled: Optional[bool] = None,
    path: Optional[str] = None,
    keepalive: bool = True,
) -> Dict[str, Any]:
    """Decide, then register as a storage peer. Never raises.

    ``token`` should be the node's own capability token from enrollment (it carries
    the mesh scope); the result says what happened and why, including when nothing
    was attempted.
    """
    try:
        decision = decide_contribution(node_class, path=path, enabled=enabled)
        result: Dict[str, Any] = {"decision": decision, "registered": False}
        if not decision["contribute"]:
            result["skipped"] = decision["reason"]
            return result
        base = resolve_storage_endpoint(enroll_response)
        if not base:
            result["skipped"] = ("no storage endpoint (set AITHER_STRATA_URL or "
                                 "AITHER_MESH_URL)")
            return result
        reg = await register_storage_peer(
            base, token, build_storage_peer_payload(node_id, decision)
        )
        result.update(reg)
        if reg["registered"] and keepalive:
            result["keepalive"] = start_storage_keepalive(
                base, token, node_id, node_class, path, enabled=enabled
            )
        return result
    except Exception as e:  # noqa: BLE001 -- enrollment must never see this
        log.warning("storage contribution failed: %s", e)
        return {"registered": False, "error": str(e)}
