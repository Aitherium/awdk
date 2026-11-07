"""The home's IT department: look for something broken and propose the fix.

``adk home doctor`` runs read-only checks on this home node; ``adk home doctor --watch``
runs them on an interval. Every check is a pure function over facts gathered first, and
answers one record::

    {"id", "ok", "severity", "finding", "proposed_action", "operator_action_kind",
     "skipped"}

Checks (a check whose tool or data is missing is ``skipped``, never a failure):

* ``disk_space``    -- free space on every local disk (``psutil``; else the home drive)
* ``disk_health``   -- SMART overall health via ``smartctl`` when it is installed
* ``home_services`` -- what ``adk home status`` reports: set up, harness, model reachable
* ``lan_devices``   -- devices the LAN inventory saw in the last day that nobody marked
                       known, plus new-device notices still waiting to send
* ``backup``        -- the family drive backup is overdue (when that API answers)
* ``adk_version``   -- this awdk against the newest on PyPI (read only; never installs)

For a failing check the doctor:

1. sends ONE household notice per finding per 24 h (the platform Hearth's
   ``hearth_home_doctor_notice``: it sends the check id, Hearth words the notice and
   resolves the owner + co-guardians from YOUR household). Offline is retried next run;
2. for a fix in the operator's vocabulary (``operator_action_kind``):
   * a DIAGNOSTIC (``device.diagnostics``, it changes nothing) is asked of the Aither
     Operator at once -- at most once per 24 h per finding;
   * anything that CHANGES STATE (``device.update``) becomes an approval card here
     (:mod:`adk.approval`'s store). ``adk home doctor approve <card>`` is the human tap:
     only then is the operator asked, and the operator still applies the workspace's
     policy tiers and the caller's role (a ``never`` tier refuses). When the policy
     raises its own card, it is answered only if it is exactly this fix on this
     device's node id, raised by this request -- and then with the person's OWN
     sign-in, straight to the operator, never through the agent gateway (no tool can
     answer an operator card). Anything else waits for a guardian in Veil.
3. Every run, notice, card and action writes a signed receipt (:mod:`adk.receipts`).

State lives in ``<agent home>/doctor.json`` (owner-only), one run at a time under an OS
lock beside it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger("adk.home.doctor")

STATE_NAME = "doctor.json"
STATE_VERSION = 1
SEVERITIES = ("ok", "info", "warn", "critical")
CHECK_IDS = ("disk_space", "disk_health", "home_services", "lan_devices", "backup",
             "adk_version")
#: The operator actions a doctor may ask for. Diagnostics change nothing and run at
#: once; every other kind changes state and waits on a card.
DIAGNOSTIC_KINDS = frozenset({"device.diagnostics"})
STATE_KINDS = frozenset({"device.update"})
OPERATOR_KINDS = DIAGNOSTIC_KINDS | STATE_KINDS
#: One notice (and one automatic diagnostic) per finding per this many seconds.
RENOTIFY_S = 24 * 3600
#: The ``--watch`` interval floor and default.
MIN_INTERVAL_S = 60.0
DEFAULT_INTERVAL_S = 3600.0
#: Disk thresholds (percent free).
DISK_WARN_PCT = 10.0
DISK_CRITICAL_PCT = 5.0
#: A device first seen within this window and not marked known counts as unknown.
LAN_WINDOW_S = 24 * 3600
#: The approval store's session for doctor cards.
CARD_SESSION = "home-doctor"
NOTICE_TOOL = "hearth_home_doctor_notice"
OPERATOR_REQUEST_TOOL = "hearth_operator_request"
#: Where ``adk home doctor approve`` answers an operator card, as the signed-in person
#: (never through the agent gateway): this env, else the home's public URL -- through
#: its ``/api/operator/*`` pass-through (see :func:`operator_approval_url`).
OPERATOR_URL_ENV = "AITHER_OPERATOR_URL"
#: Set (1/true) ONLY when ``AITHER_OPERATOR_URL`` is Genesis itself, in-network.
OPERATOR_DIRECT_ENV = "AITHER_OPERATOR_GENESIS_DIRECT"
_TRUE = ("1", "true", "yes", "on")
#: The Identity node id of THIS device (the operator's ``target``).
NODE_ID_ENV = "AITHER_NODE_ID"
#: smartctl by absolute path only (no PATH lookup for what this module runs).
SMARTCTL_PATHS = ("/usr/sbin/smartctl", "/usr/bin/smartctl", "/usr/local/sbin/smartctl",
                  "/opt/homebrew/bin/smartctl",
                  r"C:\Program Files\smartmontools\bin\smartctl.exe")
SMART_MAX_DEVICES = 8
SMART_TIMEOUT_S = 10.0
_NODE_BAD = re.compile(r"[^a-z0-9._-]")
#: ``smartctl --scan``'s ``-d <type>`` is passed back to smartctl only when it is plain.
_SMART_TYPE = re.compile(r"^[a-z0-9,_+-]+$")
_SMART_DEV = re.compile(r"^(/dev/[A-Za-z0-9/_.-]+|[a-z]+\d*)$")
#: The operator's card ids (``opr_<hex>``), as its pass-through accepts them.
_APPROVAL_ID = re.compile(r"^op[ar]_[a-f0-9]{8,32}$")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: ``notifier({"check", "severity", "host"}) -> {"notice_id"} | {"error"}``
Notifier = Callable[[Dict[str, Any]], Dict[str, Any]]
#: ``fixer(kind, node, approved, node_id) -> {"decisions": [...]} | {"error"}``
Fixer = Callable[[str, str, bool, str], Dict[str, Any]]
#: ``approver(approval_id) -> {"decision": ...} | {"error"}`` -- the person's own answer
Approver = Callable[[str], Dict[str, Any]]
Facts = Dict[str, Any]


# ── the record ───────────────────────────────────────────────────────────────

def result(check_id: str, ok: bool, severity: str, finding: str, proposed_action: str = "",
           operator_action_kind: str = "", skipped: bool = False) -> Dict[str, Any]:
    """One check's answer.

    Args:
        check_id: one of :data:`CHECK_IDS`.
        ok: True when nothing needs doing (also True for a skipped check).
        severity: one of :data:`SEVERITIES`.
        finding: what was seen, one sentence.
        proposed_action: what a person (or the operator) should do; "" when ok.
        operator_action_kind: an operator action from :data:`OPERATOR_KINDS`, or "".
        skipped: the check could not look (no tool, no data).

    Raises:
        ValueError: an unknown check id, severity or operator action.
    """
    if check_id not in CHECK_IDS:
        raise ValueError(f"unknown check {check_id!r}")
    if severity not in SEVERITIES:
        raise ValueError(f"unknown severity {severity!r}")
    if operator_action_kind and operator_action_kind not in OPERATOR_KINDS:
        raise ValueError(f"unknown operator action {operator_action_kind!r}")
    return {"id": check_id, "ok": bool(ok), "severity": severity, "finding": finding,
            "proposed_action": proposed_action,
            "operator_action_kind": operator_action_kind, "skipped": bool(skipped)}


def _skip(check_id: str, why: str) -> Dict[str, Any]:
    return result(check_id, True, "info", why, skipped=True)


def _gib(n: float) -> str:
    return f"{n / (1024 ** 3):.1f} GiB"


# ── pure checks ──────────────────────────────────────────────────────────────

def check_disk_space(disks: Optional[List[Dict[str, Any]]],
                     warn_pct: float = DISK_WARN_PCT,
                     critical_pct: float = DISK_CRITICAL_PCT) -> Dict[str, Any]:
    """Free space. ``disks``: ``[{"path", "total", "free"}]`` in bytes; None = unknown."""
    rows = [d for d in disks or [] if float(d.get("total") or 0) > 0]
    if not rows:
        return _skip("disk_space", "no disk usage could be read")
    worst = min(rows, key=lambda d: float(d["free"]) / float(d["total"]))
    pct = 100.0 * float(worst["free"]) / float(worst["total"])
    where = f"{worst.get('path')} has {_gib(float(worst['free']))} free ({pct:.0f}%)"
    if pct >= warn_pct:
        return result("disk_space", True, "ok", f"every disk has room; least is {where}")
    sev = "critical" if pct < critical_pct else "warn"
    return result("disk_space", False, sev, f"{where}",
                  "Free space: empty the recycle bin/trash, move large files off, or "
                  "add a disk.")


def check_disk_health(smart: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """SMART overall health. ``smart``: ``[{"device", "passed": True|False|None}]``;
    None = no smartctl on this machine (skipped)."""
    if smart is None:
        return _skip("disk_health", "smartctl is not installed; disk health not checked")
    judged = [s for s in smart if s.get("passed") is not None]
    if not judged:
        return _skip("disk_health", "smartctl found no disk it could judge")
    failed = [str(s.get("device")) for s in judged if s.get("passed") is False]
    if not failed:
        return result("disk_health", True, "ok",
                      f"{len(judged)} disk(s) report SMART health PASSED")
    return result("disk_health", False, "critical",
                  f"SMART health FAILED on {', '.join(failed)}",
                  "Back up now and replace the disk; collect diagnostics for the record.",
                  "device.diagnostics")


def check_home_services(status: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """What ``adk home status`` reports. ``status``: ``{"initialized", "harness":
    {"available", "detail"}, "model": {"ok", "detail"}}``; None = unreadable."""
    if status is None:
        return _skip("home_services", "the home status could not be read")
    if not status.get("initialized"):
        return result("home_services", False, "warn", "this home is not set up",
                      "Run: adk home init")
    problems: List[str] = []
    harness = status.get("harness") or {}
    if harness and not harness.get("available"):
        problems.append(f"harness {harness.get('kind') or '?'}: {harness.get('detail')}")
    model = status.get("model") or {}
    if model and model.get("ok") is False:
        problems.append(f"model: {model.get('detail')}")
    if not problems:
        return result("home_services", True, "ok", "home set up; harness and model answer")
    return result("home_services", False, "warn", "; ".join(problems)[:400],
                  "Check `adk home status` and `adk home model`; collect diagnostics.",
                  "device.diagnostics")


def _ts(value: Any) -> Optional[float]:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def check_lan_devices(inventory: Optional[Dict[str, Dict[str, Any]]],
                      alerts: Optional[Dict[str, Any]], now: float,
                      window_s: float = LAN_WINDOW_S) -> Dict[str, Any]:
    """Unknown devices. ``inventory``: the LAN inventory's ``devices`` map (None = never
    scanned); ``alerts``: the new-device alert state (``known``, ``queue``)."""
    if inventory is None:
        return _skip("lan_devices", "no LAN inventory yet: run adk home devices scan")
    alerts = alerts or {}
    known = alerts.get("known") or {}
    queued = len(alerts.get("queue") or [])
    recent = [k for k, rec in inventory.items() if k not in known and
              (_ts(rec.get("first_seen")) or 0) >= now - window_s]
    if not recent and not queued:
        return result("lan_devices", True, "ok",
                      f"{len(inventory)} device(s) on record, none new and unrecognised")
    parts = []
    if recent:
        parts.append(f"{len(recent)} device(s) joined in the last day and are not "
                     "marked known")
    if queued:
        parts.append(f"{queued} new-device notice(s) still waiting to send")
    return result("lan_devices", False, "warn", "; ".join(parts),
                  "Yours? adk home devices known <mac> --label <name>. Not yours? Check "
                  "your router's device list and change the Wi-Fi password.")


def check_backup(status: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The family drive backup. ``status``: its backup status body (``configured``,
    ``overdue``, ``last_ok``, ``last_status``); None = not available here."""
    if status is None:
        return _skip("backup", "family backup status is not available on this node")
    if not status.get("configured"):
        return result("backup", False, "warn", "no backup target is set up for the "
                      "family drive", "Set a backup target on the family's node, then "
                      "run: adk family-drive backup now")
    if status.get("overdue"):
        last = status.get("last_ok")
        when = (f"{(time.time() - float(last)) / 3600:.0f} h ago" if last else "never")
        return result("backup", False, "warn", f"backup overdue: last good {when}",
                      "Run: adk family-drive backup now")
    if status.get("last_status") not in (None, "", "ok", "empty"):
        return result("backup", False, "warn",
                      f"last backup run: {status.get('last_status')}",
                      "Run: adk family-drive backup now, then backup drill")
    return result("backup", True, "ok", "family backup is current")


def _version_tuple(v: str) -> tuple:
    clean = str(v or "").lstrip("v").split("-")[0].split("+")[0]
    out = []
    for p in clean.split("."):
        if not p.isdigit():
            break
        out.append(int(p))
    return tuple(out)


def check_adk_version(installed: str, latest: Optional[str],
                      upgrade: str = "pip install --upgrade awdk") -> Dict[str, Any]:
    """This awdk vs PyPI's newest. ``latest`` None = PyPI not reachable (skipped)."""
    if not installed:
        return _skip("adk_version", "awdk is not an installed distribution here")
    if not latest:
        return _skip("adk_version", f"awdk {installed}; the newest release is unknown "
                     "(offline or update checks off)")
    if _version_tuple(latest) > _version_tuple(installed):
        return result("adk_version", False, "warn",
                      f"awdk {installed} is behind {latest}",
                      f"Update: {upgrade} (or approve the operator's update)",
                      "device.update")
    return result("adk_version", True, "ok", f"awdk {installed} is current")


def run_checks(facts: Facts, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Every check over ``facts`` (see :func:`gather_facts`), in :data:`CHECK_IDS` order.
    A check that raises becomes a skipped record, never a crash."""
    now = time.time() if now is None else now
    plan = (
        ("disk_space", lambda: check_disk_space(facts.get("disks"))),
        ("disk_health", lambda: check_disk_health(facts.get("smart"))),
        ("home_services", lambda: check_home_services(facts.get("home_status"))),
        ("lan_devices", lambda: check_lan_devices(facts.get("lan_inventory"),
                                                  facts.get("lan_alerts"), now)),
        ("backup", lambda: check_backup(facts.get("backup"))),
        ("adk_version", lambda: check_adk_version(facts.get("adk_installed") or "",
                                                  facts.get("adk_latest"),
                                                  facts.get("adk_upgrade") or
                                                  "pip install --upgrade awdk")),
    )
    out = []
    for cid, fn in plan:
        try:
            out.append(fn())
        except Exception as exc:  # noqa: BLE001 -- one broken check never hides the rest
            out.append(_skip(cid, f"check failed to run ({type(exc).__name__})"))
    return out


# ── SMART parsing (pure) ─────────────────────────────────────────────────────

def parse_smart_scan(text: str) -> List[List[str]]:
    """``smartctl --scan`` -> ``[[device, "-d", type], ...]`` (comments dropped)."""
    out = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if not parts or not _SMART_DEV.match(parts[0]):
            continue
        if len(parts) >= 3 and parts[1] == "-d" and _SMART_TYPE.match(parts[2]):
            out.append(parts[:3])
        else:  # no type, or one we will not hand back to smartctl: let it probe
            out.append(parts[:1])
        if len(out) >= SMART_MAX_DEVICES:
            break
    return out


def parse_smart_health(text: str) -> Optional[bool]:
    """``smartctl -H`` -> True (PASSED/OK), False (FAILED), None (cannot tell)."""
    for line in (text or "").splitlines():
        low = line.lower()
        if "overall-health" in low or "health status" in low:
            if "passed" in low or low.rstrip().endswith(": ok"):
                return True
            if "fail" in low:
                return False
    return None


# ── gathering (impure; each part never raises) ───────────────────────────────

def _run(argv: List[str], timeout: float = SMART_TIMEOUT_S) -> Optional[str]:
    kw: Dict[str, Any] = {"creationflags": _NO_WINDOW} if os.name == "nt" else {}
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, check=False, **kw)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("doctor: %s did not run: %s", argv[0], type(exc).__name__)
        return None
    return proc.stdout or ""


def smartctl_path() -> str:
    """The first installed smartctl from :data:`SMARTCTL_PATHS`, or ""."""
    return next((p for p in SMARTCTL_PATHS if Path(p).is_file()), "")


def gather_smart() -> Optional[List[Dict[str, Any]]]:
    tool = smartctl_path()
    if not tool:
        return None
    rows = []
    for dev in parse_smart_scan(_run([tool, "--scan"]) or ""):
        text = _run([tool, "-H", *dev])
        rows.append({"device": dev[0], "passed": parse_smart_health(text or "")})
    return rows


def gather_disks() -> Optional[List[Dict[str, Any]]]:
    paths: List[str] = []
    try:
        import psutil

        for part in psutil.disk_partitions(all=False):
            opts = str(getattr(part, "opts", "") or "")
            if "cdrom" in opts or not part.fstype:
                continue
            paths.append(part.mountpoint)
    except Exception as exc:  # noqa: BLE001 -- psutil missing or refused: home drive only
        logger.debug("doctor: psutil partitions unavailable: %s", type(exc).__name__)
    if not paths:
        paths = [Path.home().anchor or "/"]
    out = []
    for p in dict.fromkeys(paths):
        try:
            u = shutil.disk_usage(p)
        except OSError:
            continue
        out.append({"path": p, "total": u.total, "free": u.free})
    return out or None


def gather_home_status() -> Optional[Dict[str, Any]]:
    """The parts of ``adk home status`` that say whether the home WORKS."""
    try:
        from . import config as hc
        from . import harness, models

        if not hc.is_initialized():
            return {"initialized": False}
        cfg = hc.load_config()
        out: Dict[str, Any] = {"initialized": True}
        try:
            out["harness"] = harness.harness_status(cfg.harness.kind)
        except Exception as exc:  # noqa: BLE001 -- reported as unavailable
            out["harness"] = {"kind": cfg.harness.kind, "available": False,
                              "detail": f"probe failed ({type(exc).__name__})"}
        out["model"] = models.probe(cfg.model, timeout=3.0)
        return out
    except Exception as exc:  # noqa: BLE001 -- unreadable status is skipped, not failed
        logger.debug("doctor: home status unreadable: %s", type(exc).__name__)
        return None


def gather_lan() -> tuple:
    try:
        from . import lan_alerts, lan_inventory

        inv_path = lan_inventory.state_path()
        inventory = lan_inventory.load_state(inv_path) if inv_path.exists() else None
        alerts = lan_alerts.load_state(lan_alerts.state_path())
        return inventory, alerts
    except Exception as exc:  # noqa: BLE001 -- a corrupt file is skipped here
        logger.debug("doctor: LAN state unreadable: %s", type(exc).__name__)
        return None, None


def gather_backup() -> Optional[Dict[str, Any]]:
    """The family drive backup status, or None when this build or node has none."""
    try:
        from adk import family_drive_cli as fdc

        _status, body = fdc.request("GET", "/family/storage/backup")
    except Exception as exc:  # noqa: BLE001 -- no route, no sign-in, offline: not here
        logger.debug("doctor: backup status unavailable: %s", type(exc).__name__)
        return None
    return body if isinstance(body, dict) else None


def gather_version(online: bool = True) -> Dict[str, Any]:
    """Installed awdk and the newest release: the update check's cache, else one PyPI
    lookup (``online``). Nothing is installed and the cache is not written."""
    try:
        from adk.shell import update_check as uc
    except Exception:  # noqa: BLE001
        return {"adk_installed": "", "adk_latest": None}
    installed = uc._installed_version()
    cached = uc._load_cache() or {}
    latest = cached.get("latest_version") or None
    if latest is None and online and not uc._disabled():
        latest = uc._fetch_latest_version()
    return {"adk_installed": installed, "adk_latest": latest,
            "adk_upgrade": uc.upgrade_command()}


def gather_facts(*, online: bool = True) -> Facts:
    """Everything the checks read, gathered read-only. Never raises."""
    inventory, alerts = gather_lan()
    facts: Facts = {"disks": gather_disks(), "smart": gather_smart(),
                    "home_status": gather_home_status(), "lan_inventory": inventory,
                    "lan_alerts": alerts, "backup": gather_backup()}
    facts.update(gather_version(online))
    return facts


def node_name(explicit: str = "") -> str:
    """This node's device name for the operator: ``--node``, else the hostname, reduced
    to ``[a-z0-9._-]``."""
    raw = (explicit or os.environ.get("AITHER_HOME_NODE_NAME") or socket.gethostname()
           or "").strip().lower()
    return _NODE_BAD.sub("", raw)[:63].strip("._-")


# ── delivery seams (the gateway by default) ──────────────────────────────────

def gateway_notifier(notice: Dict[str, Any]) -> Dict[str, Any]:
    """The platform Hearth's household notice, as the signed-in caller."""
    from .gateway_hearth import call_gateway

    return call_gateway(NOTICE_TOOL, {"check": str(notice.get("check") or ""),
                                      "severity": str(notice.get("severity") or "warn"),
                                      "host": str(notice.get("host") or "")})


def node_identity(explicit: str = "") -> str:
    """This device's Identity node id (``--node-id``, else ``AITHER_NODE_ID``), or ""."""
    raw = (explicit or os.environ.get(NODE_ID_ENV) or "").strip()
    return raw if re.match(r"^[A-Za-z0-9:._-]{1,128}$", raw) else ""


def operator_approver(approval_id: str) -> Dict[str, Any]:
    """Answer ONE operator card yes, as the person signed in on this home
    (``adk home signin``), to the operator at :func:`operator_approval_url` -- never
    through the agent gateway, whose bearer may be an agent's. No saved sign-in, no
    answer; anything but the operator's 2xx decision is a LOUD error."""
    from .connector_tools import _saved_bearer

    if not _APPROVAL_ID.match(str(approval_id or "")):
        return {"error": "not an operator card id"}
    bearer = _saved_bearer()
    if not bearer:
        return {"error": "sign in first (adk home signin): only your own sign-in can "
                         "answer an operator card"}
    url = operator_approval_url(approval_id)
    try:
        import httpx

        from .._tls import tls_verify

        with httpx.Client(timeout=20.0, verify=tls_verify()) as c:
            r = c.post(url, json={"allow": True},
                       headers={"Authorization": f"Bearer {bearer}",
                                "Accept": "application/json"})
    except Exception as exc:  # noqa: BLE001 -- reported LOUDLY, the card stays for Veil
        return {"error": f"approval not delivered: the operator is unreachable "
                         f"({type(exc).__name__})"}
    return approval_answer(r.status_code, r)


def operator_approval_url(approval_id: str) -> str:
    """Where one card's answer goes. Default: the public host's Veil pass-through
    ``<base>/api/operator/approvals/<id>`` (Veil forwards it to Genesis under the caller's
    own bearer). ``/api/v1/operator`` is used ONLY when ``AITHER_OPERATOR_GENESIS_DIRECT``
    is set and ``AITHER_OPERATOR_URL`` points straight at Genesis (in-network)."""
    from .serve import tutor_url

    explicit = (os.environ.get(OPERATOR_URL_ENV) or "").strip().rstrip("/")
    direct = (os.environ.get(OPERATOR_DIRECT_ENV) or "").strip().lower() in _TRUE
    if direct and explicit:
        return f"{explicit}/api/v1/operator/approvals/{approval_id}"
    base = explicit or tutor_url().rstrip("/")
    return f"{base}/api/operator/approvals/{approval_id}"


def approval_answer(status: int, resp: Any) -> Dict[str, Any]:
    """The operator's answer, or a LOUD error: any non-2xx, or a body that is not the
    operator's ``{"decision": ...}``, is "approval not delivered", never success."""
    try:
        data = resp.json()
    except (ValueError, AttributeError):
        data = None
    if not 200 <= int(status) < 300 or not isinstance(data, dict) \
            or not isinstance(data.get("decision"), dict):
        detail = data.get("detail") or data.get("error") if isinstance(data, dict) else ""
        tail = f" ({str(detail)[:160]})" if detail else ""
        return {"error": f"approval not delivered: {status}{tail}", "status": int(status)}
    return data


def _card_is_ours(d: Dict[str, Any], kind: str, node_id: str) -> str:
    """"" when this pending operator card is exactly the fix the person approved: the
    same action, on this device's node id, raised by THIS request (it carries the audit
    row the request wrote). Otherwise why it is left for Veil."""
    if d.get("status") != "pending_approval" or not d.get("approval_id"):
        return "not a pending card"
    if d.get("action") != kind:
        return f"the card is for {d.get('action') or 'another action'}, not {kind}"
    if not node_id:
        return "this device's node id is unknown (set --node-id or AITHER_NODE_ID)"
    if d.get("target") != node_id:
        return "the card is for another device"
    if not d.get("audit_id"):
        return "the card was not raised by this request (an older card)"
    return ""


def gateway_fixer(kind: str, node: str, approved: bool, node_id: str = "",
                  approver: Optional[Approver] = None) -> Dict[str, Any]:
    """Ask the Aither Operator for ``kind`` on ``node`` through the gateway (as the
    signed-in caller). The gateway can only ASK.

    ``approved``: a person approved THIS fix (``adk home doctor approve``). Only then, and
    only for a card :func:`_card_is_ours` matches, is the policy's card answered -- with
    the person's own sign-in (:func:`operator_approver`); the operator re-checks their
    role and the policy. Anything else is left for a guardian in Veil and says so. An
    automatic diagnostic never answers a card."""
    from .gateway_hearth import call_gateway

    got = call_gateway(OPERATOR_REQUEST_TOOL, {"action": kind, "node": node})
    if got.get("error") or not approved:
        return got
    decisions = list(got.get("decisions") or [])
    for i, d in enumerate(decisions):
        if d.get("status") != "pending_approval":
            continue
        why = _card_is_ours(d, kind, node_id)
        if why:
            decisions[i] = {**d, "left_for_veil": why}
            continue
        ans = (approver or operator_approver)(str(d["approval_id"]))
        if not isinstance(ans, dict) or ans.get("error"):
            # LOUD: the person said yes and the operator never heard it.
            err = (ans or {}).get("error") if isinstance(ans, dict) else "no answer"
            decisions[i] = {**d, "approval_error": err}
            return {"decisions": decisions, "error": str(err)}
        decisions[i] = {**d, "answered": ans.get("decision") or ans}
    return {"decisions": decisions}


def _call(fn: Callable[..., Dict[str, Any]], *args: Any) -> Dict[str, Any]:
    try:
        got = fn(*args)
    except Exception as exc:  # noqa: BLE001 -- a dead channel is retried, never a crash
        return {"error": f"{type(exc).__name__}"}
    return got if isinstance(got, dict) else {"error": "no answer"}


def _ran(got: Dict[str, Any]) -> bool:
    """Did the operator take the action (ran, notified, or queued it on a card)?"""
    if got.get("error"):
        return False
    return any(d.get("status") in ("executed", "notified", "pending_approval")
               for d in got.get("decisions") or [])


# ── state ────────────────────────────────────────────────────────────────────

def state_path(root: Optional[Path] = None) -> Path:
    """``<agent home>/doctor.json``."""
    if root is None:
        from . import config as hc

        root = hc.home_dir()
    return Path(root) / STATE_NAME


def _empty() -> Dict[str, Any]:
    return {"version": STATE_VERSION, "notified": {}, "auto": {}, "last_run": None}


def load_state(path: Path) -> Dict[str, Any]:
    """The doctor state; missing is empty.

    Raises:
        ValueError: the file exists but is unreadable (refuse rather than re-notify all).
    """
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} is not a readable doctor state: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a doctor state object")
    out = _empty()
    for k in ("notified", "auto"):
        if isinstance(data.get(k), dict):
            out[k] = data[k]
    out["last_run"] = data.get("last_run")
    return out


def save_state(path: Path, state: Dict[str, Any]) -> None:
    from .._private_file import write_private_text

    write_private_text(path, json.dumps(state, indent=2, sort_keys=True))


def _receipt(kind: str, name: str, args: Any, res: Any, approval: Any,
             receipts_path: Optional[Path]) -> Optional[int]:
    from .. import receipts

    try:
        row = receipts.append(kind, name, args=args, result=res, approval=approval,
                              path=receipts_path)
    except OSError as exc:  # reported in the run, never fatal
        logger.warning("doctor receipt not written: %s", exc)
        return None
    return int(row.get("seq", -1))


# ── approval cards ───────────────────────────────────────────────────────────

def _store(store: Any = None) -> Any:
    if store is not None:
        return store
    from ..approval import get_approval_store

    return get_approval_store()


def list_cards(store: Any = None) -> List[Dict[str, Any]]:
    """Open doctor cards: ``[{"card", "check", "kind", "node", "summary", "created"}]``."""
    entry = _store(store).get(CARD_SESSION) or {}
    out = []
    for p in entry.get("pending") or []:
        if isinstance(p, dict) and p.get("tool_use_id"):
            a = p.get("args") or {}
            out.append({"card": p["tool_use_id"], "check": a.get("check"),
                        "kind": p.get("tool"), "node": a.get("node"),
                        "summary": p.get("summary", ""), "created": p.get("created")})
    return out


def _put_cards(store: Any, pending: List[Dict[str, Any]]) -> None:
    store.put_pending(CARD_SESSION, user_message="adk home doctor", agent="home-doctor",
                      pending=pending)


def _raise_card(store: Any, r: Dict[str, Any], node: str, now: float) -> Optional[str]:
    """A card for ``r``'s state-changing fix unless one is open for that check + kind."""
    entry = store.get(CARD_SESSION) or {}
    pending = [p for p in entry.get("pending") or [] if isinstance(p, dict)]
    for p in pending:
        a = p.get("args") or {}
        if p.get("tool") == r["operator_action_kind"] and a.get("check") == r["id"]:
            return None
    card = "doc_" + uuid.uuid4().hex[:12]
    pending.append({"tool_use_id": card, "tool": r["operator_action_kind"],
                    # the card id is in the args, so each card is its own call to decide
                    "args": {"check": r["id"], "node": node, "card": card},
                    "summary": f"{r['finding']} -> {r['proposed_action']}"[:300],
                    "created": now})
    _put_cards(store, pending)
    return card


def _drop_resolved_cards(store: Any, failing: Iterable[str]) -> List[str]:
    """Close the cards of checks that now pass (nothing left to fix)."""
    entry = store.get(CARD_SESSION) or {}
    pending = [p for p in entry.get("pending") or [] if isinstance(p, dict)]
    failing = set(failing)
    keep = [p for p in pending if (p.get("args") or {}).get("check") in failing]
    gone = [str(p.get("tool_use_id")) for p in pending if p not in keep]
    if gone:
        _put_cards(store, keep)
    return gone


def decide_card(card: str, allow: bool, *, fixer: Optional[Fixer] = None, store: Any = None,
                receipts_path: Optional[Path] = None,
                node_id: str = "") -> Dict[str, Any]:
    """A person's answer to one doctor card. Allow: the operator is asked now (its policy
    tiers still decide). Deny: nothing runs. Either way the card closes and a receipt is
    written.

    Raises:
        KeyError: no open card with that id.
    """
    store = _store(store)
    open_cards = {c["card"]: c for c in list_cards(store)}
    if card not in open_cards:
        raise KeyError(card)
    store.record_decisions(CARD_SESSION, [{"tool_use_id": card,
                                          "result": "allow" if allow else "deny"}])
    allowed, denied = store.take_decided(CARD_SESSION)
    out: Dict[str, Any] = {"card": card, "allowed": bool(allow), "ran": None}
    for p in allowed:
        if p.get("tool_use_id") != card:
            continue
        a = p.get("args") or {}
        kind, node = str(p.get("tool") or ""), str(a.get("node") or "")
        if kind not in OPERATOR_KINDS:
            out["ran"] = {"error": f"not an operator action: {kind}"}
        else:
            out["ran"] = _call(fixer or gateway_fixer, kind, node, True,
                               node_identity(node_id))
        out["receipt_seq"] = _receipt("action", f"home.doctor.{kind}",
                                      {"card": card, "check": a.get("check"), "node": node},
                                      out["ran"], f"owner:yes:{card}", receipts_path)
    for p in denied:
        if p.get("tool_use_id") == card:
            out["receipt_seq"] = _receipt("approval", "home.doctor.card",
                                          {"card": card, "check": (p.get("args") or {})
                                           .get("check")},
                                          {"denied": True}, f"owner:no:{card}",
                                          receipts_path)
    entry = store.get(CARD_SESSION) or {}
    _put_cards(store, [p for p in entry.get("pending") or []
                       if isinstance(p, dict) and p.get("tool_use_id") != card])
    return out


# ── one run ──────────────────────────────────────────────────────────────────

def _due(stamp: Any, now: float, every: float = RENOTIFY_S) -> bool:
    try:
        return now - float(stamp) >= every
    except (TypeError, ValueError):
        return True


def run_once(*, facts: Optional[Facts] = None, notifier: Optional[Notifier] = None,
             fixer: Optional[Fixer] = None, store: Any = None, node: str = "",
             path: Optional[Path] = None, receipts_path: Optional[Path] = None,
             notify: bool = True, now: Optional[float] = None) -> Dict[str, Any]:
    """Check, tell the household once per finding per day, propose the fixes.

    Args:
        facts: pre-gathered facts (default :func:`gather_facts`).
        notifier: household notice seam (default :func:`gateway_notifier`).
        fixer: operator seam (default :func:`gateway_fixer`).
        store: approval store (default :func:`adk.approval.get_approval_store`).
        node: this device's name for the operator (default :func:`node_name`).
        path: doctor state file (default :func:`state_path`).
        receipts_path: receipt log (default: the receipts module's own).
        notify: False = check and propose only; no notice, no operator call.
        now: epoch override (tests).

    Returns:
        ``{"results", "failing", "notices", "actions", "cards", "closed_cards",
        "receipt_seq"}``.

    Raises:
        ValueError: the doctor state file is corrupt.
        RuntimeError: another doctor run holds the lock.
    """
    from .lan_alerts import run_lock

    now = time.time() if now is None else now
    path = path or state_path()
    node = node or node_name()
    with run_lock(path):
        facts = gather_facts() if facts is None else facts
        results = run_checks(facts, now)
        failing = [r for r in results if not r["ok"] and not r["skipped"]]
        state = load_state(path)
        store_ = _store(store)
        out: Dict[str, Any] = {"results": results, "failing": [r["id"] for r in failing],
                               "notices": [], "actions": [], "cards": [],
                               "closed_cards": [], "node": node}
        for r in failing:
            if notify and _due((state["notified"].get(r["id"]) or {}).get("at"), now):
                got = _call(notifier or gateway_notifier,
                            {"check": r["id"], "severity": "critical"
                             if r["severity"] == "critical" else "warn", "host": node})
                seq = None
                if not got.get("error"):
                    seq = _receipt("notice", "home.doctor.notice",
                                   {"check": r["id"], "severity": r["severity"]}, got,
                                   "auto:home-doctor", receipts_path)
                    state["notified"][r["id"]] = {"at": now, "severity": r["severity"],
                                                  "notice_id": got.get("notice_id")}
                out["notices"].append({"check": r["id"], "sent": not got.get("error"),
                                       "error": got.get("error"), "receipt_seq": seq})
            kind = r["operator_action_kind"]
            if kind in DIAGNOSTIC_KINDS and notify and node and _due(
                    (state["auto"].get(r["id"]) or {}).get("at"), now):
                got = _call(fixer or gateway_fixer, kind, node, False, "")
                seq = _receipt("action", f"home.doctor.{kind}",
                               {"check": r["id"], "node": node}, got, "auto:diagnostics",
                               receipts_path)
                if _ran(got):
                    state["auto"][r["id"]] = {"at": now}
                out["actions"].append({"check": r["id"], "kind": kind, "result": got,
                                       "receipt_seq": seq})
            elif kind in STATE_KINDS:
                card = _raise_card(store_, r, node, now)
                if card:
                    seq = _receipt("approval", "home.doctor.card",
                                   {"card": card, "check": r["id"], "kind": kind,
                                    "node": node}, {"pending": True}, "pending",
                                   receipts_path)
                    out["cards"].append({"card": card, "check": r["id"], "kind": kind,
                                         "receipt_seq": seq})
        out["closed_cards"] = _drop_resolved_cards(store_, out["failing"])
        # The 24 h stamps outlive a pass on purpose: a finding that flaps is still told
        # once a day, not once per flap.
        state["last_run"] = now
        save_state(path, state)
    out["receipt_seq"] = _receipt(
        "doctor", "home.doctor.run", {"node": node},
        {r["id"]: ("skipped" if r["skipped"] else "ok" if r["ok"] else r["severity"])
         for r in results}, "auto:read-only", receipts_path)
    return out


def watch(*, interval: float = DEFAULT_INTERVAL_S, rounds: Optional[int] = None,
          sleep: Callable[[float], None] = time.sleep,
          on_round: Optional[Callable[[Dict[str, Any]], None]] = None,
          **kw: Any) -> int:
    """:func:`run_once` every ``interval`` seconds (floor :data:`MIN_INTERVAL_S`) until
    ``rounds`` runs or Ctrl+C. A failed round is logged and the loop goes on.

    Returns:
        The number of rounds run.
    """
    interval = max(MIN_INTERVAL_S, float(interval))
    done = 0
    try:
        while rounds is None or done < rounds:
            try:
                res = run_once(**kw)
                if on_round is not None:
                    on_round(res)
            except (RuntimeError, ValueError, OSError) as exc:
                print(f"home doctor round failed: {exc}", file=sys.stderr)
            done += 1
            if rounds is not None and done >= rounds:
                break
            sleep(interval)
    except KeyboardInterrupt:
        print(f"home doctor watch stopped after {done} round(s)", file=sys.stderr)
    return done


def render(res: Dict[str, Any]) -> str:
    """The terminal view of one run."""
    mark = {"ok": "ok  ", "info": "info", "warn": "WARN", "critical": "CRIT"}
    lines = []
    for r in res["results"]:
        tag = "skip" if r["skipped"] else mark[r["severity"]]
        lines.append(f"  [{tag}] {r['id']:<14} {r['finding']}")
        if not r["ok"] and r["proposed_action"]:
            lines.append(f"         fix: {r['proposed_action']}")
    head = (f"home doctor on {res.get('node') or 'this node'}: "
            f"{len(res['failing'])} finding(s)")
    tail = []
    for n in res.get("notices") or []:
        tail.append(f"  guardians {'told' if n['sent'] else 'NOT told (retried next run)'}:"
                    f" {n['check']}" + (f" ({n['error']})" if n.get("error") else ""))
    for a in res.get("actions") or []:
        ds = (a["result"].get("decisions") or [{}])
        tail.append(f"  operator {a['kind']} for {a['check']}: "
                    f"{a['result'].get('error') or ds[0].get('status')}")
    for c in res.get("cards") or []:
        tail.append(f"  waiting for your OK: {c['kind']} ({c['check']}) -> "
                    f"adk home doctor approve {c['card']}")
    return "\n".join([head, *lines, *tail])
