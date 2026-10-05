"""Join = contribute: decide how much disk this device lends to the mesh storage pool.

When a device enrolls it can also become a storage peer: it measures the disk under
its contribution path, decides whether and how much to lend, and registers with the
mesh storage service (``POST /strata/mesh/peers/register``). Registration is
idempotent by ``node_id``, so the same call is the keepalive.

The policy, in one place so every join path agrees:

* **Opt-out, per device class.** ``AITHER_CONTRIBUTE_STORAGE=1|0`` (or the
  ``enabled=`` argument) always wins. Unset, servers, workstations and cloud nodes
  contribute; laptops and phones do not — they sleep, roam and get lost.
* **Bounded.** At most ``share`` of FREE space (default 30%,
  ``AITHER_STORAGE_CONTRIB_SHARE``) and never so much that less than ``reserve``
  stays free (default 20 GiB, ``AITHER_STORAGE_RESERVE_GB``). Under 1 GiB is not
  worth a peer and contributes nothing.
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
    "device_class_for",
    "measure_disk",
    "plan_contribution",
    "contribution_enabled",
    "decide_contribution",
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

_DEFAULT_ON = frozenset({"server", "workstation", "cloud"})
_PORTABLE = frozenset({"laptop", "phone"})


class DiskProbe(NamedTuple):
    """What the contribution path's filesystem looks like right now."""

    path: str
    total_bytes: int
    free_bytes: int


def device_class_for(node_class: str) -> str:
    """Map an enrollment ``node_class`` onto a storage device class.

    Unknown classes map to ``laptop`` — the conservative answer (no cold tier,
    contribution off by default).
    """
    v = (node_class or "").strip().lower()
    if v in ("server", "workstation", "laptop", "phone", "cloud"):
        return v
    return _NODE_TO_DEVICE.get(v, "laptop")


def _default_path() -> Path:
    env = (os.environ.get("AITHER_STORAGE_PATH") or "").strip()
    if env and Path(env).is_absolute():
        return Path(env)
    home = os.environ.get("AITHER_HOME") or str(Path.home() / ".aither")
    return Path(home) / "strata"


def measure_disk(path: Optional[str] = None) -> DiskProbe:
    """Measure total/free bytes of the filesystem holding the contribution path.

    The path need not exist yet: the nearest existing ancestor is measured (the
    filesystem is what matters), and nothing is created.

    Raises:
        OSError: no ancestor of the path is measurable.
    """
    target = Path(path) if path else _default_path()
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
) -> int:
    """Bytes to lend out of ``free_bytes``: ``min(share * free, free - reserve)``.

    Returns 0 when that is under ``min_bytes`` (including when the disk is already
    below the reserve). ``share``/``reserve_bytes`` default to the env-tunable
    policy (:data:`DEFAULT_SHARE`, :data:`DEFAULT_RESERVE_BYTES`).
    """
    if share is None:
        share = _env_float("AITHER_STORAGE_CONTRIB_SHARE", DEFAULT_SHARE, 0.0, 0.9)
    if reserve_bytes is None:
        reserve_bytes = int(
            _env_float("AITHER_STORAGE_RESERVE_GB", DEFAULT_RESERVE_BYTES / GIB, 0.0, 1e6) * GIB
        )
    free = max(0, int(free_bytes))
    amount = min(int(free * max(0.0, share)), free - max(0, int(reserve_bytes)))
    return amount if amount >= min_bytes else 0


def contribution_enabled(
    device_class: str,
    *,
    enabled: Optional[bool] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Tuple[bool, str]:
    """Whether this device should contribute, and why.

    Precedence: the explicit ``enabled`` argument, then ``AITHER_CONTRIBUTE_STORAGE``,
    then the class default (on for server/workstation/cloud, off for laptop/phone).
    """
    if enabled is not None:
        return bool(enabled), "explicit " + ("opt-in" if enabled else "opt-out")
    source = os.environ if env is None else env
    flag = (source.get("AITHER_CONTRIBUTE_STORAGE") or "").strip().lower()
    if flag in _TRUE:
        return True, "AITHER_CONTRIBUTE_STORAGE opt-in"
    if flag in _FALSE:
        return False, "AITHER_CONTRIBUTE_STORAGE opt-out"
    if device_class in _DEFAULT_ON:
        return True, f"default on for {device_class}"
    return False, f"default off for {device_class} (set AITHER_CONTRIBUTE_STORAGE=1 to opt in)"


def decide_contribution(
    node_class: str,
    *,
    path: Optional[str] = None,
    enabled: Optional[bool] = None,
    probe: Optional[DiskProbe] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """The whole decision for one device, as data (no network).

    Returns ``{"contribute", "reason", "device_class", "path", "total_bytes",
    "free_bytes", "contributed_bytes"}``. ``contribute`` is False when opted out,
    when the disk cannot be measured, or when the bounded amount is below the
    minimum.
    """
    device_class = device_class_for(node_class)
    on, why = contribution_enabled(device_class, enabled=enabled, env=env)
    out: Dict[str, Any] = {
        "contribute": False, "reason": why, "device_class": device_class,
        "path": "", "total_bytes": 0, "free_bytes": 0, "contributed_bytes": 0,
    }
    if not on:
        return out
    try:
        disk = probe or measure_disk(path)
    except OSError as e:
        out["reason"] = f"disk not measurable: {e}"
        return out
    amount = plan_contribution(disk.free_bytes)
    out.update(path=disk.path, total_bytes=disk.total_bytes, free_bytes=disk.free_bytes,
               contributed_bytes=amount)
    if amount <= 0:
        out["reason"] = "too little free space after the reserve"
        return out
    out["contribute"] = True
    return out


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
                          path: Optional[str], interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        decision = decide_contribution(node_class, path=path, enabled=True)
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
                            interval_s: float = KEEPALIVE_INTERVAL_S) -> bool:
    """Re-register on an interval so the pool keeps this peer live. False = no loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    old = _keepalive.get("task")
    if old is not None and not old.done():
        old.cancel()
    _keepalive["task"] = loop.create_task(
        _keepalive_loop(base_url, token, node_id, node_class, path, interval_s)
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
                base, token, node_id, node_class, path
            )
        return result
    except Exception as e:  # noqa: BLE001 -- enrollment must never see this
        log.warning("storage contribution failed: %s", e)
        return {"registered": False, "error": str(e)}
