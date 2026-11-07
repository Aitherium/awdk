"""Tell the household's guardians when a device joins the home network.

``adk home devices watch`` runs one :func:`lan_inventory.scan` and, for every device
flagged ``new`` (never seen before; the first scan is the baseline and flags none),
sends ONE notice to the household's guardians through the platform Hearth's notice
path (the gateway's ``hearth_new_device_notice`` tool, which posts it as the signed-in
caller with ``audience="guardians"``: Hearth resolves who the guardians are from the
caller's own roster, nothing here names a person). Every notice that went out is
written to the signed receipt log (:mod:`adk.receipts`).

Rules, all kept in ``<agent home>/lan_alerts.json``:

* **once per device** -- a device is announced at most once (``announced``), keyed the
  same way the inventory keys it (the MAC; an SSDP-only device by its IP);
* **known devices stay quiet** -- ``adk home devices known <mac> --label "our TV"``
  records it as known; a known device is never announced, and is dropped from the
  queue if it was waiting there;
* **offline is not lost** -- when the notice cannot be delivered (no sign-in, the
  gateway unreachable, Hearth refusing) the device waits in ``queue`` and the next run
  retries it before anything else. The inventory flags a device ``new`` only on the
  scan that first sees it, so without the queue an offline run would drop it forever;
* **a flood is one notice** -- at most ``MAX_NOTICES_PER_RUN`` devices get their own
  notice in one run; the rest are rolled into ONE summary notice ("and N more"), so
  anything on the LAN that fakes a hundred devices cannot page a family a hundred times.

Both state files are owner-only, and one run holds ``lan_alerts.json.lock`` for its
whole scan-and-announce, so two concurrent watches cannot announce a device twice.

The notice carries the device's IP, MAC and hostname (if any) -- what a guardian needs
to recognise it -- and nothing else from this machine.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from . import lan_inventory

logger = logging.getLogger("adk.home.lan_alerts")

STATE_NAME = "lan_alerts.json"
STATE_VERSION = 1
#: The gateway tool that records + delivers the guardian notice.
NOTICE_TOOL = "hearth_new_device_notice"
#: A queue that never drains must not grow without bound (oldest are dropped first).
MAX_QUEUE = 64
LABEL_MAX = 60
#: Individual notices per run; the rest become ONE summary notice.
MAX_NOTICES_PER_RUN = 5

#: ``notifier(device) -> {"notice_id": ...}`` on delivery, ``{"error": ...}`` otherwise.
Notifier = Callable[[Dict[str, Any]], Dict[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def state_path(root: Optional[Path] = None) -> Path:
    """``<agent home>/lan_alerts.json`` (beside the inventory)."""
    return lan_inventory.state_path(root).with_name(STATE_NAME)


def _empty() -> Dict[str, Any]:
    return {"version": STATE_VERSION, "announced": {}, "known": {}, "queue": []}


def load_state(path: Path) -> Dict[str, Any]:
    """The alert state at ``path``; missing is empty.

    Raises:
        ValueError: the file exists but is not a readable alert state (refuse rather
            than overwrite it: every device would be re-announced).
    """
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is not a readable alert state: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not an alert state object")
    out = _empty()
    for k in ("announced", "known"):
        if isinstance(data.get(k), dict):
            out[k] = data[k]
    if isinstance(data.get("queue"), list):
        out["queue"] = [q for q in data["queue"] if isinstance(q, dict) and q.get("key")]
    return out


def save_state(path: Path, state: Dict[str, Any]) -> None:
    """Atomically write the alert state, owner-only (unique tmp + replace).

    Raises:
        PermissionError: the file could not be restricted to its owner.
    """
    from .._private_file import write_private_text

    write_private_text(path, json.dumps(state, indent=2, sort_keys=True))


@contextmanager
def run_lock(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """One watch (or ``known`` edit) at a time on this home: an OS lock (``fcntl.flock``
    / ``msvcrt.locking``) on a file beside the state (:mod:`.filelock`). The kernel drops
    it when the holder exits or crashes, so a long run is never "stale" and nothing is
    taken over.

    Raises:
        RuntimeError: another run held it for ``timeout`` seconds (named in the message).
    """
    from .filelock import run_lock as _run_lock

    with _run_lock(path, holder="adk home devices", timeout=timeout,
                   fallback="another `adk home devices` run"):
        yield


def key_for(ident: str) -> str:
    """A user-typed device id -> the inventory key: a MAC in any OS spelling, an
    ``ip:<addr>`` key, or a bare IPv4 (an SSDP-only device).

    Raises:
        ValueError: ``ident`` is none of those.
    """
    raw = str(ident or "").strip()
    mac = lan_inventory.normalize_mac(raw)
    if mac:
        return mac
    ip = raw[3:] if raw.startswith("ip:") else raw
    parts = ip.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return f"ip:{ip}"
    raise ValueError(f"not a MAC address or IPv4 address: {raw!r}")


def mark_known(ident: str, label: str = "", *, path: Optional[Path] = None,
               now: Optional[str] = None) -> Dict[str, Any]:
    """Record a device as known ("this is our TV"): it is never announced again.

    Args:
        ident: its MAC (any spelling) or IP.
        label: what the guardian calls it.
        path: alert state file (default :func:`state_path`).
        now: timestamp override (tests).

    Returns:
        ``{"key", "label", "dequeued"}``.
    """
    path = path or state_path()
    key = key_for(ident)
    with run_lock(path):
        state = load_state(path)
        state["known"][key] = {"label": lan_inventory.clean_text(label, LABEL_MAX) or "",
                               "at": now or _now()}
        before = len(state["queue"])
        state["queue"] = [q for q in state["queue"] if q.get("key") != key]
        save_state(path, state)
    return {"key": key, "label": state["known"][key]["label"],
            "dequeued": before - len(state["queue"])}


def forget_known(ident: str, *, path: Optional[Path] = None) -> bool:
    """Undo :func:`mark_known`. True when it was known."""
    path = path or state_path()
    key = key_for(ident)
    with run_lock(path):
        state = load_state(path)
        if state["known"].pop(key, None) is None:
            return False
        save_state(path, state)
    return True


def _public(rec: Dict[str, Any]) -> Dict[str, Any]:
    """What a notice may carry about a device: how a guardian recognises it."""
    clean = lan_inventory.clean_text
    out = {"ip": rec.get("ip"), "mac": rec.get("mac"),
           "hostname": clean(rec.get("hostname"), lan_inventory.HOSTNAME_MAX),
           "vendor": clean(rec.get("vendor"), 64)}
    server = (rec.get("ssdp") or {}).get("server") if isinstance(rec.get("ssdp"), dict) else None
    server = clean(server, 120)
    if server:
        out["ssdp_server"] = server
    return out


def gateway_notifier(device: Dict[str, Any]) -> Dict[str, Any]:
    """Default notifier: the platform Hearth's guardian notice, as the signed-in caller."""
    from .gateway_hearth import call_gateway

    args: Dict[str, Any] = {k: str(device.get(k) or "")
                            for k in ("ip", "mac", "hostname", "vendor")}
    if device.get("ssdp_server"):
        args["detail"] = str(device["ssdp_server"])
    if device.get("more"):
        args["more"] = int(device["more"])
    return call_gateway(NOTICE_TOOL, args)


def _send(notifier: Notifier, device: Dict[str, Any]) -> Dict[str, Any]:
    try:
        result = notifier(dict(device))
    except Exception as exc:  # noqa: BLE001 -- a dead channel queues, never crashes
        result = {"error": f"notifier raised {type(exc).__name__}"}
    if not isinstance(result, dict):
        result = {"error": "notifier returned no answer"}
    return result


def _receipt(receipts_path: Optional[Path], key: str, device: Dict[str, Any],
             result: Dict[str, Any]) -> Optional[int]:
    from .. import receipts

    try:
        row = receipts.append("notice", "home.lan.new_device", args={"key": key, **device},
                              result=result, approval="auto:new-device",
                              path=receipts_path)
    except OSError as exc:  # the notice went out; a receipt failure is reported, not fatal
        logger.warning("new-device receipt not written: %s", exc)
        return None
    return int(row.get("seq", -1))


def announce(new_devices: List[Dict[str, Any]], *, notifier: Optional[Notifier] = None,
             path: Optional[Path] = None, receipts_path: Optional[Path] = None,
             now: Optional[str] = None) -> Dict[str, Any]:
    """Announce each never-announced, not-known device once; queue what cannot go out.

    The queue (from earlier offline runs) is flushed first, then ``new_devices``.

    Args:
        new_devices: inventory records flagged ``new`` by :func:`lan_inventory.scan`.
        notifier: delivery seam (default :func:`gateway_notifier`).
        path: alert state file (default :func:`state_path`).
        receipts_path: receipt log (default: the receipts module's own default).
        now: timestamp override (tests).

    Returns:
        ``{"sent": [...], "queued": [...], "suppressed": [...], "skipped": [...]}``; each
        entry is ``{"key", "device", ...}``. A device covered by the run's summary
        notice is in ``sent`` with ``"summary": True``.

    Raises:
        ValueError: the alert state file is corrupt.
        RuntimeError: another run holds the lock.
    """
    path = path or state_path()
    with run_lock(path):
        return _announce(new_devices, notifier or gateway_notifier, path, receipts_path,
                         now or _now())


def _announce(new_devices: List[Dict[str, Any]], notifier: Notifier, path: Path,
              receipts_path: Optional[Path], now: str) -> Dict[str, Any]:
    state = load_state(path)
    pending: Dict[str, Dict[str, Any]] = {}
    for q in state["queue"]:
        pending.setdefault(str(q["key"]), {"device": q.get("device") or {},
                                           "queued_at": q.get("queued_at") or now})
    for rec in new_devices:
        key = lan_inventory.device_key(rec)
        pending.setdefault(key, {"device": _public(rec), "queued_at": None})
    out: Dict[str, List[Dict[str, Any]]] = {"sent": [], "queued": [], "suppressed": [],
                                            "skipped": []}
    queue: List[Dict[str, Any]] = []
    due: List[tuple] = []
    for key, item in pending.items():
        device = item["device"]
        if key in state["known"]:
            out["suppressed"].append({"key": key, "device": device,
                                      "label": state["known"][key].get("label", "")})
        elif key in state["announced"]:
            out["skipped"].append({"key": key, "device": device})
        else:
            due.append((key, item))

    def _queue(batch: List[tuple], error: Any) -> None:
        for key, item in batch:
            queue.append({"key": key, "device": item["device"],
                          "queued_at": item["queued_at"] or now,
                          "last_error": str(error)[:200], "last_try": now})
            out["queued"].append({"key": key, "device": item["device"],
                                  "error": str(error)[:200]})

    def _mark(batch: List[tuple], result: Dict[str, Any], summary: bool) -> None:
        lead_key, lead = batch[0]
        seq = _receipt(receipts_path, lead_key, lead["device"] if not summary else
                       {**lead["device"], "more": len(batch) - 1}, result)
        for key, item in batch:
            state["announced"][key] = {"at": now, "notice_id": result.get("notice_id"),
                                       "receipt_seq": seq, "summary": summary}
            out["sent"].append({"key": key, "device": item["device"], "summary": summary,
                                "notice_id": result.get("notice_id"), "receipt_seq": seq})

    single, rest = due[:MAX_NOTICES_PER_RUN], due[MAX_NOTICES_PER_RUN:]
    for key, item in single:
        result = _send(notifier, item["device"])
        if result.get("error"):
            _queue([(key, item)], result["error"])
        else:
            _mark([(key, item)], result, summary=False)
    if rest:
        result = _send(notifier, {**rest[0][1]["device"], "more": len(rest) - 1})
        if result.get("error"):
            _queue(rest, result["error"])
        else:
            _mark(rest, result, summary=True)
    state["queue"] = queue[-MAX_QUEUE:]
    save_state(path, state)
    return out


def watch(*, use_ssdp: bool = True, resolve: bool = False,
          notifier: Optional[Notifier] = None, inventory_path: Optional[Path] = None,
          path: Optional[Path] = None, receipts_path: Optional[Path] = None,
          now: Optional[str] = None) -> Dict[str, Any]:
    """One scan, then :func:`announce` its new devices.

    Returns:
        The scan result plus ``"alerts"`` (the :func:`announce` result).

    Raises:
        RuntimeError: no neighbor table is readable.
        ValueError: the inventory or the alert state is corrupt.
    """
    if path is None:
        path = (Path(inventory_path).with_name(STATE_NAME) if inventory_path is not None
                else state_path())
    with run_lock(path):  # scan + announce as one step: a second watch waits or refuses
        res = lan_inventory.scan(use_ssdp=use_ssdp, resolve=resolve, path=inventory_path,
                                 now=now)
        res["alerts"] = _announce(res["new"], notifier or gateway_notifier, path,
                                  receipts_path, now or _now())
    return res
