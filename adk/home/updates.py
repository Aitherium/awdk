"""``adk home updates`` -- keep the home node and its smart-home devices patched.

Three things can be out of date, and each is handled by the seam that already owns it:

* **adk itself** -- :mod:`adk.self_update`. ``check`` asks it where ``import adk``
  resolves now and whether that code is validated; ``apply`` calls its one-time
  ``request_apply`` (the daemons adopt VALIDATED code only, when idle, behind a
  ``/health`` gate that rolls back). Nothing here restarts anything itself.
* **the host OS** -- read with a fixed, read-only query (``apt list --upgradable``,
  ``softwareupdate -l``, the Windows Update COM search with a ``Get-HotFix`` age
  fallback). It is NEVER applied automatically: ``apply`` raises a guardian approval
  card. Approved on Debian/Ubuntu, it runs exactly :data:`APT_UPDATE` then
  :data:`APT_UPGRADE` (allowlisted argvs, ``sudo -n`` straight to ``apt-get`` when not
  root, a fixed minimal environment) and NEVER kills apt/dpkg: past its timeout it gets
  one SIGTERM and is otherwise left to finish, reported as ``still_running``. Anywhere
  else the approval returns what the guardian has to click.
* **Home Assistant** -- every ``update.*`` entity (firmware, add-ons, core). Installing
  one is ``update.install`` through the household device gate as a GUARDED action
  (:mod:`adk.home.devices.policy`): a guardian quorum says yes before it runs. The card
  is bound to the version it shows (``version=`` when the entity supports it, and the
  gate re-reads ``latest_version`` before running: changed, nothing runs).

``schedule`` keeps a weekly window (default Sunday 03:00, local time) in
``<home>/home_updates.json``; ``schedule --run-due`` (for a timer) checks and raises a
card for whatever is pending. The adk card is skipped when its owner already opted in
with ``adk autoupdate on`` -- that consent is the self_update module's, not this one's.

Every apply, every card raised and every approval writes a signed receipt
(:mod:`adk.receipts`). The parsers are pure (captured samples in the tests) and strip
control and bidi characters from everything they return; every command runs from an
absolute path with an argv list, a timeout and no console window.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .devices import approvals as ap
from .devices import policy as pol
from .devices.notify import notify_approval

logger = logging.getLogger("adk.home.updates")

STATE_NAME = "home_updates.json"
STATE_VERSION = 1
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_SCHEDULE: Dict[str, Any] = {"enabled": True, "weekday": 6, "at": "03:00"}
#: How long a raised card waits for a guardian when the household set no expiry rule
#: (the device policy's own ceiling).
CARD_EXPIRES_S = pol.MAX_EXPIRES_S
#: Windows with no COM answer: the newest hotfix older than this reads as "behind".
HOTFIX_STALE_DAYS = 35
OWNER = {"pid": "owner", "name": "owner", "role": pol.OWNER}
SCHEDULER = {"pid": "owner", "name": "scheduled updates", "role": pol.OWNER}
HOUSEHOLD = "local"
#: HA ``UpdateEntityFeature`` bits.
HA_INSTALL, HA_SPECIFIC_VERSION, HA_BACKUP = 1, 2, 8

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ── the fixed commands ───────────────────────────────────────────────────────

APT_LIST = ("/usr/bin/apt", "list", "--upgradable")
SOFTWAREUPDATE_LIST = ("/usr/sbin/softwareupdate", "-l")
APT_UPDATE = ("/usr/bin/apt-get", "-q", "update")
#: Conffile prompts answered by dpkg options (keep the local file), so the run never
#: waits on a terminal; stdin is /dev/null as well.
APT_UPGRADE = ("/usr/bin/apt-get", "-y", "-o", "Dpkg::Options::=--force-confdef",
               "-o", "Dpkg::Options::=--force-confold", "upgrade")
#: Not root: sudo that never prompts, straight to apt-get -- the exact argv a sudoers
#: line can name (:func:`sudoers_hint`). No ``env`` wrapper: that would make any
#: command a sudoers entry for ``/usr/bin/env``.
SUDO_PREFIX = ("/usr/bin/sudo", "-n")
#: The ONLY argvs :func:`run_allowlisted` will execute.
ALLOWED_APPLY = frozenset({APT_UPDATE, APT_UPGRADE,
                           SUDO_PREFIX + APT_UPDATE, SUDO_PREFIX + APT_UPGRADE})
#: The whole environment of an upgrade: nothing inherited (no APT_CONFIG, LD_*, ...).
UPGRADE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C.UTF-8",
               "DEBIAN_FRONTEND": "noninteractive"}
UPDATE_TIMEOUT_S = 900.0
UPGRADE_TIMEOUT_S = 3600.0
#: After the one SIGTERM, how long to keep waiting before reporting "still running".
TERM_GRACE_S = 600.0
#: Upgrade logs: ``<tempdir>/adk-apt-*.log``; the newest is kept for debugging.
LOG_PREFIX = "adk-apt-"

WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$out = @{ source = 'com'; updates = @() }
try {
  $s = New-Object -ComObject Microsoft.Update.Session
  $r = $s.CreateUpdateSearcher().Search('IsInstalled=0 and IsHidden=0')
  $out.updates = @($r.Updates | ForEach-Object { @{
    title = [string]$_.Title; kb = (@($_.KBArticleIDs) -join ',');
    severity = [string]$_.MsrcSeverity; reboot = [bool]$_.RebootRequired } })
} catch {
  $out.source = 'hotfix'; $out.error = [string]$_.Exception.Message
  $h = Get-HotFix | Where-Object { $_.InstalledOn } |
    Sort-Object InstalledOn -Descending | Select-Object -First 1
  if ($h) { $out.last_installed = $h.InstalledOn.ToString('yyyy-MM-dd')
            $out.last_kb = [string]$h.HotFixID }
}
$out | ConvertTo-Json -Compress -Depth 4
"""

#: What a guardian clicks where nothing here may install for them.
INSTRUCTIONS = {
    "windows": "Open Settings > Windows Update and click 'Download & install all' "
               "(restart when it asks).",
    "macos": "Open System Settings > General > Software Update and click 'Update Now'.",
    "debian": "Run: sudo apt-get update && sudo apt-get upgrade",
    "unsupported": "Use this system's own updater; adk cannot read or install its updates.",
}

Runner = Callable[[Sequence[str], float], Tuple[int, str, str]]
#: ``(argv, timeout, env) -> {"state": exited|still_running|could_not_run, ...}``
UpgradeRunner = Callable[[Sequence[str], float, Dict[str, str]], Dict[str, Any]]
Receipt = Callable[[str, str, Any, Any, str], Any]

# ── text hygiene ─────────────────────────────────────────────────────────────

_BIDI = frozenset(list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A))
                  + [0x200E, 0x200F, 0x061C])


def clean(value: Any, limit: int = 200) -> str:
    """``value`` as text with control characters (C0, DEL, C1) and bidi overrides
    removed, capped at ``limit``. Everything a device, a vendor or a package mirror
    wrote passes through here before it is stored or printed."""
    s = "" if value is None else str(value)
    s = "".join(c for c in s if not (ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0
                                     or ord(c) in _BIDI))
    return s.strip()[:limit]


def powershell_path() -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def windows_argv() -> Tuple[str, ...]:
    return (powershell_path(), "-NoProfile", "-NonInteractive", "-Command", WINDOWS_SCRIPT)


def run_command(argv: Sequence[str], timeout: float) -> Tuple[int, str, str]:
    """Run one fixed READ-ONLY argv: ``(returncode, stdout, stderr)``; -1 when it could
    not run. (A timeout kills it: only queries come through here, never apt-get.)"""
    env = {**os.environ, "LC_ALL": "C", "DEBIAN_FRONTEND": "noninteractive"}
    try:
        out = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout, env=env,
                             stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                             check=False)
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {int(timeout)} s"
    except OSError as exc:
        return -1, "", f"could not run {Path(argv[0]).name}: {type(exc).__name__}"
    return out.returncode, out.stdout or "", out.stderr or ""


def _tail(path: Path, n: int = 4000) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - n))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def run_no_kill(argv: Sequence[str], timeout: float, env: Dict[str, str], *,
                grace: float = TERM_GRACE_S) -> Dict[str, Any]:
    """Run ``argv`` and NEVER kill it: a package manager killed mid-dpkg leaves the
    system half-configured. Output goes to a log file (not a pipe, so the child cannot
    die of SIGPIPE if this process exits), in its own session (a Ctrl+C here does not
    reach it). Past ``timeout`` it gets ONE SIGTERM (POSIX) and ``grace`` more seconds;
    still alive, it is left running and reported ``still_running`` with its pid and log.
    """
    fd, name = tempfile.mkstemp(prefix=LOG_PREFIX, suffix=".log")
    log = Path(name)
    kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": fd,
                          "stderr": subprocess.STDOUT, "env": dict(env),
                          "creationflags": _NO_WINDOW}
    if os.name != "nt":
        kw["start_new_session"] = True
    try:
        proc = subprocess.Popen(list(argv), **kw)  # noqa: S603 -- allowlisted argv
    except OSError as exc:
        os.close(fd)
        return {"state": "could_not_run", "exit": None,
                "err": f"could not run {Path(argv[0]).name}: {type(exc).__name__}"}
    os.close(fd)
    try:
        code = proc.wait(timeout)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                proc.send_signal(signal.SIGTERM)
            except OSError as exc:
                logger.warning("SIGTERM to %s failed: %s", proc.pid, exc)
        try:
            code = proc.wait(grace)
        except subprocess.TimeoutExpired:
            return {"state": "still_running", "pid": proc.pid, "log": str(log),
                    "_proc": proc}
    out = _tail(log)
    return {"state": "exited", "exit": code, "out": out, "err": out, "log": str(log)}


# ── pure parsers ─────────────────────────────────────────────────────────────

_APT_LINE = re.compile(
    r"^(?P<name>[a-z0-9][a-z0-9+.\-]*)/(?P<suites>\S+)\s+(?P<version>\S+)\s+(?P<arch>\S+)"
    r"(?:\s+\[upgradable from: (?P<frm>[^\]]+)\])?")


def parse_apt_upgradable(text: str) -> List[Dict[str, Any]]:
    """``apt list --upgradable`` -> one row per package (header and warnings ignored)."""
    rows = []
    for line in (text or "").splitlines():
        m = _APT_LINE.match(line.strip())
        if not m:
            continue
        suites = [clean(s, 64) for s in m.group("suites").split(",")][:8]
        rows.append({"name": clean(m.group("name"), 128),
                     "version": clean(m.group("version"), 128),
                     "from_version": clean(m.group("frm"), 128),
                     "arch": clean(m.group("arch"), 32), "suites": suites,
                     "security": any(s.endswith("-security") for s in suites)})
    return rows


_SU_LABEL = re.compile(r"^\s*\*\s*(?:Label:\s*)?(?P<label>.+?)\s*$")
_SU_FIELD = re.compile(r"(?P<k>Title|Version|Size|Recommended|Action):\s*(?P<v>[^,]*)")


def parse_softwareupdate(text: str) -> List[Dict[str, Any]]:
    """``softwareupdate -l`` (stdout + stderr) -> one row per offered update."""
    rows: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        m = _SU_LABEL.match(line)
        if m:
            label = clean(m.group("label"), 160)
            rows.append({"label": label, "title": label, "version": "",
                         "recommended": False, "restart": False})
            continue
        if rows and ("Title:" in line or "Version:" in line):
            fields = {f.group("k"): clean(f.group("v"), 160)
                      for f in _SU_FIELD.finditer(line)}
            cur = rows[-1]
            cur["title"] = fields.get("Title") or cur["title"]
            cur["version"] = fields.get("Version", cur["version"])
            if "Size" in fields:
                cur["size"] = fields["Size"]
            cur["recommended"] = fields.get("Recommended", "").upper() == "YES"
            cur["restart"] = fields.get("Action", "").lower() in ("restart", "shut down")
    return rows


def parse_windows_updates(text: str, today: Optional[date] = None) -> Dict[str, Any]:
    """The :data:`WINDOWS_SCRIPT` JSON -> ``{source, updates, ...}``.

    ``source`` is ``com`` (the Windows Update search answered: ``updates`` is what is
    pending) or ``hotfix`` (it did not: only the newest hotfix's age is known, and
    ``stale`` says whether it is older than :data:`HOTFIX_STALE_DAYS`).

    Raises:
        ValueError: the output is not the script's JSON.
    """
    data = json.loads((text or "").strip() or "null")
    if not isinstance(data, dict) or data.get("source") not in ("com", "hotfix"):
        raise ValueError("not the update script's answer")
    raw = data.get("updates") or []
    if isinstance(raw, dict):
        raw = [raw]
    updates = [{"title": clean(u.get("title")), "kb": clean(u.get("kb"), 64),
                "severity": clean(u.get("severity"), 32), "reboot": bool(u.get("reboot"))}
               for u in raw if isinstance(u, dict)][:200]
    out: Dict[str, Any] = {"source": data["source"], "updates": updates}
    if data["source"] == "hotfix":
        out["error"] = clean(data.get("error"))
        last = clean(data.get("last_installed"), 10)
        out["last_installed"] = last
        out["last_kb"] = clean(data.get("last_kb"), 32)
        try:
            age = ((today or date.today()) - date.fromisoformat(last)).days
        except ValueError:
            age = None
        out["age_days"] = age
        out["stale"] = age is None or age > HOTFIX_STALE_DAYS
    return out


def parse_ha_updates(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """HA ``/api/states`` rows -> the ``update.*`` entities, pending or not.

    ``state`` ``on`` = an update is available, ``off`` = current; ``unavailable`` /
    ``unknown`` cannot be judged (``pending`` False, ``state`` kept so it is shown)."""
    out = []
    for r in rows or []:
        eid = str(r.get("entity_id") or "")
        if not eid.startswith("update.") or not pol._ENTITY_RE.match(eid):
            continue
        a = r.get("attributes") or {}
        try:
            feats = int(a.get("supported_features") or 0)
        except (TypeError, ValueError):
            feats = 0
        st = clean(r.get("state"), 32)
        latest = clean(a.get("latest_version"), 64)
        skipped = clean(a.get("skipped_version"), 64)
        out.append({
            "entity_id": eid, "name": clean(a.get("friendly_name") or eid, 80),
            "state": st, "pending": st == "on",
            "installed_version": clean(a.get("installed_version"), 64),
            "latest_version": latest, "title": clean(a.get("title"), 80),
            "in_progress": a.get("in_progress") not in (None, False),
            "can_install": bool(feats & HA_INSTALL), "backup": bool(feats & HA_BACKUP),
            "specific_version": bool(feats & HA_SPECIFIC_VERSION),
            "skipped": bool(skipped) and skipped == latest,
            "release_url": clean(a.get("release_url"), 300),
        })
    return sorted(out, key=lambda u: u["entity_id"])


# ── probes ───────────────────────────────────────────────────────────────────

def detect_platform(sys_platform: Optional[str] = None,
                    os_release: Optional[str] = None) -> str:
    """``windows`` | ``macos`` | ``debian`` (Debian, Ubuntu and their ID_LIKE kin) |
    ``unsupported``."""
    plat = sys_platform or sys.platform
    if plat == "win32":
        return "windows"
    if plat == "darwin":
        return "macos"
    if plat.startswith("linux"):
        if os_release is None:
            try:
                os_release = Path("/etc/os-release").read_text(encoding="utf-8")
            except OSError:
                os_release = ""
        ids = set()
        for line in os_release.splitlines():
            k, _, v = line.partition("=")
            if k in ("ID", "ID_LIKE"):
                ids.update(v.strip().strip('"').lower().split())
        if ids & {"debian", "ubuntu"}:
            return "debian"
    return "unsupported"


def _last_line(text: str, default: str) -> str:
    return clean((text.strip().splitlines() or [default])[-1])


def check_host(platform: Optional[str] = None, runner: Runner = run_command,
               today: Optional[date] = None) -> Dict[str, Any]:
    """The host's pending OS updates: ``{platform, ok, count, updates, note, error}``.

    ``ok`` False means it could not judge (``count`` is then None, never 0)."""
    plat = platform or detect_platform()
    res: Dict[str, Any] = {"platform": plat, "ok": False, "count": None, "updates": [],
                           "note": "", "error": ""}
    if plat == "unsupported":
        res["error"] = "no read-only update query is known for this system"
        return res
    if plat == "debian":
        rc, out, err = runner(APT_LIST, 120)
        if rc != 0:
            res["error"] = _last_line(err, "apt list failed")
            return res
        ups = parse_apt_upgradable(out)
        res.update(ok=True, count=len(ups), updates=ups,
                   note=f"{sum(u['security'] for u in ups)} security; as of the last "
                        "`apt-get update`")
        return res
    if plat == "macos":
        rc, out, err = runner(SOFTWAREUPDATE_LIST, 300)
        text = out + "\n" + err
        if rc != 0 and "No new software available" not in text:
            res["error"] = _last_line(err, "softwareupdate failed")
            return res
        ups = parse_softwareupdate(text)
        res.update(ok=True, count=len(ups), updates=ups,
                   note="restart needed" if any(u["restart"] for u in ups) else "")
        return res
    rc, out, err = runner(windows_argv(), 300)
    try:
        win = parse_windows_updates(out, today)
    except ValueError:
        res["error"] = _last_line(err, "the update query gave no answer")
        return res
    if win["source"] == "com":
        res.update(ok=True, count=len(win["updates"]), updates=win["updates"],
                   note="restart needed" if any(u["reboot"] for u in win["updates"]) else "")
        return res
    # Only the newest hotfix's age is known: "behind" counts as one pending item.
    stale = win["stale"]
    res.update(ok=True, count=1 if stale else 0, updates=[],
               note=(f"Windows Update search unavailable; newest hotfix "
                     f"{win['last_kb'] or '?'} installed {win['last_installed'] or 'never'}"
                     + (f" -- older than {HOTFIX_STALE_DAYS} days" if stale else "")))
    return res


def check_adk(su: Any = None, probe: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
    """adk's own update picture, from :mod:`adk.self_update` (read-only)."""
    if su is None:
        from adk import self_update as su
    try:
        st = su.check(probe) if probe is not None else su.check()
    except Exception as exc:  # noqa: BLE001 -- a probe failure is "cannot judge"
        return {"ok": False, "pending": False, "error": clean(f"{type(exc).__name__}: {exc}")}
    installed = st.get("installed_at")
    adoptable, why = su.validation_of(Path(installed) if installed else None)
    pending = bool(st.get("update_available") or st.get("running_code_missing"))
    return {"ok": not st.get("check_error"), "pending": pending,
            "running_from": st.get("running_from"), "installed_at": installed,
            "adoptable": bool(adoptable), "why": clean(why, 300),
            "auto_restart": st.get("auto_restart"), "error": clean(st.get("check_error"))}


async def check_ha(client: Any) -> Dict[str, Any]:
    """Home Assistant's update entities (``client`` None = not configured)."""
    if client is None or not getattr(client, "configured", False):
        return {"configured": False, "ok": True, "entities": [], "pending": []}
    from .devices.ha import HAError

    try:
        ents = parse_ha_updates(await client.raw_states())
    except HAError as exc:
        return {"configured": True, "ok": False, "entities": [], "pending": [],
                "error": clean(exc)}
    return {"configured": True, "ok": True, "entities": ents,
            "pending": [e for e in ents if e["pending"] and not e["skipped"]]}


async def check_all(*, client: Any = None, host: bool = True, ha: bool = True,
                    su: Any = None, runner: Runner = run_command,
                    platform: Optional[str] = None) -> Dict[str, Any]:
    """Everything ``adk home updates check`` reports. Reads only."""
    out: Dict[str, Any] = {"checked_at": _iso(_now_local()), "adk": check_adk(su)}
    out["host"] = check_host(platform, runner) if host else {"skipped": True}
    out["ha"] = await check_ha(client) if ha else {"skipped": True}
    return out


# ── state + schedule ─────────────────────────────────────────────────────────

def _now_local() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def state_path(root: Optional[Path] = None) -> Path:
    from .config import home_dir

    return (root or home_dir()) / STATE_NAME


_STATUSES = frozenset({ap.PENDING, ap.APPROVED, ap.DENIED, ap.EXPIRED, ap.EXECUTING,
                       ap.EXECUTED, ap.FAILED, ap.CANCELLED})
#: How far along a row is; a save never moves a row back down this order.
_RANK = {ap.PENDING: 0, ap.APPROVED: 1, ap.EXECUTING: 2}


def _rank(row: Mapping[str, Any]) -> int:
    return _RANK.get(str(row.get("status")), 3)


def valid_row(aid: str, row: Any) -> bool:
    """The shape :mod:`.devices.approvals` writes; anything else is not trusted."""
    if not isinstance(row, dict) or row.get("id") != aid:
        return False
    act = row.get("action")
    num = (int, float)
    return (row.get("status") in _STATUSES
            and isinstance(act, dict)
            and all(isinstance(act.get(k), str) for k in ("domain", "action", "target"))
            and isinstance(act.get("params", {}), dict)
            and isinstance(row.get("digest"), str)
            and isinstance(row.get("household"), str) and bool(row["household"])
            and isinstance(row.get("approvers"), list)
            and all(isinstance(p, str) for p in row["approvers"])
            and isinstance(row.get("required"), int) and not isinstance(row["required"], bool)
            and row["required"] >= 1
            and isinstance(row.get("votes"), dict)
            and isinstance(row.get("expires_at"), num)
            and isinstance(row.get("created_at"), num))


def valid_rows(raw: Any, where: str) -> Dict[str, Any]:
    out = {}
    for aid, row in (raw.items() if isinstance(raw, dict) else []):
        if valid_row(str(aid), row):
            out[str(aid)] = row
        else:
            logger.warning("%s: dropped a malformed approval row %r", where, str(aid)[:40])
    return out


def load_state(path: Path) -> Dict[str, Any]:
    """The updates state; missing is the default. Raises ValueError when unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} is not a readable updates state: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a readable updates state")
    sched = data.get("schedule") or {}
    if not isinstance(sched, dict):
        raise ValueError(f"{path}: schedule is not an object")
    last = data.get("last_run")
    return {"version": STATE_VERSION,
            "schedule": validate_schedule({**DEFAULT_SCHEDULE, **sched}),
            "last_run": last if isinstance(last, str) else None,
            "last_summary": data.get("last_summary"),
            "approvals": valid_rows(data.get("approvals"), str(path))}


def merge_rows(mine: Dict[str, Any], disk: Mapping[str, Any]) -> Dict[str, Any]:
    """Rows to write: ours, except where the file holds a row further along (a stale
    copy must never move ``executing`` / ``executed`` back to ``pending``)."""
    out = dict(mine)
    for aid, row in disk.items():
        if aid not in out or _rank(row) > _rank(out[aid]):
            out[aid] = row
    return out


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    from .._private_file import write_private_text

    try:
        disk = load_state(path)["approvals"]
    except ValueError:
        disk = {}
    body = {**state, "approvals": merge_rows(dict(state.get("approvals") or {}), disk)}
    write_private_text(path, json.dumps(body, indent=1, sort_keys=True, default=str))


def parse_weekday(text: Any) -> int:
    """``sun`` / ``Sunday`` / ``6`` -> 0 (Monday) .. 6 (Sunday). Raises ValueError."""
    if isinstance(text, bool):
        raise ValueError(f"not a weekday: {text!r} (mon..sun)")
    s = str(text).strip().lower()
    if s.isdigit() and 0 <= int(s) <= 6:
        return int(s)
    for i, d in enumerate(WEEKDAYS):
        if s[:3] == d and len(s) >= 3:
            return i
    raise ValueError(f"not a weekday: {text!r} (mon..sun)")


def parse_at(text: Any) -> Tuple[int, int]:
    """``03:00`` -> (3, 0). Raises ValueError."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"not a time of day: {text!r} (HH:MM, 24-hour)")
    return int(m.group(1)), int(m.group(2))


def validate_schedule(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Raises ValueError; ``enabled`` must be a JSON boolean (no "no", no 1)."""
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"schedule enabled must be true or false, not {enabled!r}")
    h, m = parse_at(raw.get("at", DEFAULT_SCHEDULE["at"]))
    return {"enabled": enabled,
            "weekday": parse_weekday(raw.get("weekday", DEFAULT_SCHEDULE["weekday"])),
            "at": f"{h:02d}:{m:02d}"}


def last_window(schedule: Mapping[str, Any], now: datetime) -> datetime:
    """The most recent window start at or before ``now`` (``now``'s time zone)."""
    h, m = parse_at(schedule["at"])
    start = now.replace(hour=h, minute=m, second=0, microsecond=0)
    start -= timedelta(days=(now.weekday() - int(schedule["weekday"])) % 7)
    if start > now:
        start -= timedelta(days=7)
    return start


def next_window(schedule: Mapping[str, Any], now: datetime) -> datetime:
    return last_window(schedule, now) + timedelta(days=7)


def is_due(schedule: Mapping[str, Any], now: datetime, last_run: Optional[str]) -> bool:
    """True when the schedule is on and no run happened since the latest window began
    (a machine that was off at 03:00 catches up at its next run)."""
    if not schedule.get("enabled", True):
        return False
    if not last_run:
        return True
    try:
        prev = datetime.fromisoformat(str(last_run))
    except ValueError:
        return True
    if prev.tzinfo is None:
        prev = prev.astimezone()
    return prev < last_window(schedule, now)


# ── cards ────────────────────────────────────────────────────────────────────

def host_action(platform: str) -> Dict[str, Any]:
    return pol.normalize_action("host", "os_upgrade", f"host.{platform}", {})


ADK_ACTION = {"domain": "adk", "action": "self_update", "target": "adk.daemons", "params": {}}


def _pending_digests(store: Mapping[str, Any], now: float) -> set:
    return {r["digest"] for r in ap.pending_for(store, OWNER["pid"], now)}


def host_summary(host: Mapping[str, Any]) -> str:
    plat, n = host["platform"], host.get("count")
    if plat == "debian":
        return (f"install {n} pending OS update(s) on this Debian/Ubuntu host: runs "
                "`apt-get -q update`, then `apt-get -y upgrade` (keeps local config files)")
    label = {"windows": "Windows", "macos": "macOS"}.get(plat, plat)
    return f"{label} has {n} pending update(s): approve to get the steps"


async def _card(store: Dict[str, Any], action: Mapping[str, Any], summary: str,
                receipt: Receipt, now: float, kind: str) -> Dict[str, Any]:
    if pol.action_digest(action, HOUSEHOLD) in _pending_digests(store, now):
        return {"status": "already_waiting", "summary": summary}
    row = ap.create(store, action=action, household=HOUSEHOLD, verdict=pol.ASK_GUARDIANS,
                    required=1, approvers=[OWNER["pid"]], requested_by=SCHEDULER,
                    expires_s=CARD_EXPIRES_S, now=now, summary=summary,
                    policy={"cls": pol.GUARDED, "kind": kind})
    await _log(receipt, "update_request", f"home.updates.{kind}",
               {"action": dict(action), "digest": row["digest"]},
               {"approval": row["id"], "expires_at": row["expires_at"]},
               f"quorum:{row['id']}:0/1")
    public = ap.public(row)
    await notify_approval(public)
    return {"status": "waiting_for_guardians", "approval": public, "summary": summary}


async def _log(receipt: Receipt, kind: str, name: str, args: Any, result: Any,
               approval: str) -> None:
    out = receipt(kind, name, args, result, approval)
    if hasattr(out, "__await__"):
        await out


def household_sets_expiry(raw_cfg: Optional[Mapping[str, Any]], entity_id: str) -> bool:
    """Did the household's OWN policy file give guarded requests (or this entity) an
    expiry? Then it stands; :data:`CARD_EXPIRES_S` is only the default."""
    if not isinstance(raw_cfg, Mapping):
        return False
    cls = ((raw_cfg.get("classes") or {}).get(pol.GUARDED)) or {}
    dev = ((raw_cfg.get("devices") or {}).get(entity_id)) or {}
    return (isinstance(cls, Mapping) and "expires_s" in cls) or \
        (isinstance(dev, Mapping) and "expires_s" in dev)


async def request_ha(gate: Any, cfg: Mapping[str, Any], store: Dict[str, Any],
                     pending: Sequence[Mapping[str, Any]], now: float,
                     raw_cfg: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
    """One guarded ``update.install`` request per pending, installable entity (through
    the device gate: its policy, its quorum, its receipts). Bound to the version shown:
    ``version=`` when the entity can install a specific one, and ``latest_version`` is
    re-checked by the gate before it runs. Not raised twice for the same action."""
    import copy

    cfg2 = copy.deepcopy(dict(cfg))
    devices = cfg2.setdefault("devices", {})
    waiting = _pending_digests(store, now)
    out = []
    for e in pending:
        eid = e["entity_id"]
        if not e.get("can_install"):
            out.append({"entity_id": eid, "status": "manual",
                        "note": "this update cannot be installed from Home Assistant"})
            continue
        if e.get("in_progress"):
            out.append({"entity_id": eid, "status": "in_progress"})
            continue
        latest = e.get("latest_version") or ""
        params: Dict[str, Any] = {"backup": True} if e.get("backup") else {}
        if e.get("specific_version") and latest:
            params["version"] = latest
        act = pol.normalize_action("update", "install", eid, params)
        if pol.action_digest(act, HOUSEHOLD) in waiting:
            out.append({"entity_id": eid, "status": "already_waiting"})
            continue
        if not household_sets_expiry(raw_cfg, eid):
            # A card raised at 03:00 must outlive the night.
            devices[eid] = {**dict(devices.get(eid) or {}), "expires_s": CARD_EXPIRES_S}
        res = await gate.request(OWNER, cfg2, store, domain="update", action="install",
                                 target=eid, params=params, tainted=False,
                                 guardians=[OWNER["pid"]],
                                 note=f"{e.get('installed_version') or '?'} -> {latest or '?'}",
                                 expect={"latest_version": latest})
        out.append({"entity_id": eid, **{k: v for k, v in res.items() if k != "policy"}})
    return out


def apply_adk(su: Any, adk: Mapping[str, Any]) -> Dict[str, Any]:
    """self_update's one-time, validated, health-gated apply -- or why not."""
    if not adk.get("pending"):
        return {"status": "current"}
    if not adk.get("adoptable"):
        return {"status": "refused", "why": adk.get("why") or "not validated"}
    names = su.request_apply()
    return {"status": "requested", "daemons": names,
            "note": "each daemon adopts it at its next check, when idle; a failed "
                    "/health rolls it back" if names else "no daemon is running: the "
                    "next start runs the new code"}


async def raise_cards(report: Mapping[str, Any], *, state: Dict[str, Any], gate: Any,
                      cfg: Mapping[str, Any], device_store: Dict[str, Any],
                      receipt: Receipt, now: float, su: Any = None,
                      adk_direct: bool = False,
                      raw_cfg: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Act on a :func:`check_all` report: adk (directly when ``adk_direct``, else a
    card), Home Assistant (guarded gate requests), the host OS (a card, never a run)."""
    out: Dict[str, Any] = {}
    adk = report.get("adk") or {}
    if adk.get("pending"):
        if adk_direct:
            if su is None:
                from adk import self_update as su
            res = apply_adk(su, adk)
            await _log(receipt, "update_apply" if res["status"] == "requested"
                       else "update_refused", "home.updates.adk",
                       {"installed_at": adk.get("installed_at")}, res, "owner:cli")
            out["adk"] = res
        else:
            out["adk"] = await _card(state, ADK_ACTION,
                                     "restart the adk daemons onto validated new code",
                                     receipt, now, "adk")
    ha = report.get("ha") or {}
    if ha.get("pending") and gate is not None:
        out["ha"] = await request_ha(gate, cfg, device_store, ha["pending"], now, raw_cfg)
    host = report.get("host") or {}
    if host.get("ok") and host.get("count"):
        out["host"] = await _card(state, host_action(host["platform"]),
                                  host_summary(host), receipt, now, "host")
    return out


# ── the approved host upgrade ────────────────────────────────────────────────

def _euid(euid: Optional[int]) -> int:
    if euid is not None:
        return euid
    return os.geteuid() if hasattr(os, "geteuid") else -1


def upgrade_argv(euid: Optional[int] = None) -> Tuple[str, ...]:
    return APT_UPGRADE if _euid(euid) == 0 else SUDO_PREFIX + APT_UPGRADE


def update_argv(euid: Optional[int] = None) -> Tuple[str, ...]:
    return APT_UPDATE if _euid(euid) == 0 else SUDO_PREFIX + APT_UPDATE


def _sudoers_arg(arg: str) -> str:
    for ch in ("\\", ",", ":", "="):
        arg = arg.replace(ch, "\\" + ch)
    return arg


def sudoers_hint(user: str = "") -> str:
    """The one sudoers line that lets ``user`` run exactly these two commands with
    ``sudo -n`` (sudoers needs ``\\ , : =`` escaped inside arguments)."""
    if not user:
        try:
            import getpass

            user = getpass.getuser()
        except (OSError, KeyError, ImportError):
            user = "<user>"
    cmds = [" ".join([argv[0]] + [_sudoers_arg(a) for a in argv[1:]])
            for argv in (APT_UPDATE, APT_UPGRADE)]
    return f"{user} ALL=(root) NOPASSWD: {', '.join(cmds)}"


def run_allowlisted(argv: Sequence[str], timeout: float,
                    runner: UpgradeRunner = run_no_kill) -> Dict[str, Any]:
    """Run one argv from :data:`ALLOWED_APPLY` with :data:`UPGRADE_ENV`; refuse others."""
    if tuple(argv) not in ALLOWED_APPLY:
        return {"state": "refused", "error": "refused: not an allowlisted apt-get command"}
    return runner(tuple(argv), timeout, dict(UPGRADE_ENV))


def run_host_upgrade(euid: Optional[int] = None,
                     runner: UpgradeRunner = run_no_kill) -> Dict[str, Any]:
    """``apt-get update`` then ``apt-get upgrade``, each allowlisted, never killed.

    Returns ``{"ok", "state", "steps", ...}``; ``state`` is ``done``, ``failed`` or
    ``still_running`` (left to finish: check ``log``; it is not a failure)."""
    steps: List[Dict[str, Any]] = []
    for argv, timeout in ((update_argv(euid), UPDATE_TIMEOUT_S),
                          (upgrade_argv(euid), UPGRADE_TIMEOUT_S)):
        r = run_allowlisted(argv, timeout, runner)
        r.pop("_proc", None)
        step = {"argv": " ".join(argv), "state": r.get("state"), "exit": r.get("exit")}
        steps.append(step)
        if r.get("state") == "still_running":
            return {"ok": False, "state": "still_running", "steps": steps,
                    "pid": r.get("pid"), "log": r.get("log"),
                    "note": f"`{argv[-1]}` is still running (pid {r.get('pid')}); it was "
                            "not killed -- let it finish"}
        if r.get("state") != "exited" or r.get("exit") != 0:
            why = _last_line(str(r.get("err") or r.get("error") or ""),
                             f"apt-get {argv[-1]} failed")
            if "password is required" in why or "a terminal is required" in why:
                why += (" -- run it as root, or allow exactly these commands with this "
                        "sudoers line (visudo -f /etc/sudoers.d/adk-updates): "
                        + sudoers_hint())
            prune_apt_logs()
            return {"ok": False, "state": "failed", "steps": steps, "error": why}
        step["tail"] = _last_line(str(r.get("out") or ""), "")
    prune_apt_logs()
    return {"ok": True, "state": "done", "steps": steps, "tail": steps[-1].get("tail", "")}


def prune_apt_logs(directory: Optional[Path] = None, *, keep: int = 1,
                   active_s: float = TERM_GRACE_S, now: Optional[float] = None) -> List[str]:
    """Delete old ``adk-apt-*.log`` files once their run has been reported: the
    newest ``keep`` stay (debugging), and so does any written in the last
    ``active_s`` seconds (it may belong to an upgrade that is still running).
    Returns the paths removed; a file that cannot be removed is left alone."""
    import time as _time

    d = Path(directory) if directory is not None else Path(tempfile.gettempdir())
    when = _time.time() if now is None else now
    found = []
    for f in d.glob(LOG_PREFIX + "*.log"):
        try:
            found.append((f.stat().st_mtime, f))
        except OSError:
            continue
    found.sort(key=lambda t: t[0], reverse=True)
    removed = []
    for mtime, f in found[max(0, keep):]:
        if when - mtime < active_s:
            continue
        try:
            f.unlink()
        except OSError as exc:
            logger.debug("apt log %s not removed: %s", f, exc)
            continue
        removed.append(str(f))
    return removed


async def decide_card(state: Dict[str, Any], approval_id: str, allow: bool, *,
                      receipt: Receipt, now: float, platform: Optional[str] = None,
                      su: Any = None, euid: Optional[int] = None,
                      upgrader: UpgradeRunner = run_no_kill,
                      persist: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
    """A guardian's answer to an adk or host card. Approved, it runs once: adk via
    self_update; the host only on Debian/Ubuntu, with the allowlisted argvs; elsewhere
    the result is what to click. ``persist`` saves the row as ``executing`` BEFORE
    anything runs."""
    store = state                         # its "approvals" key holds the rows
    row = ap.get(store, approval_id)
    if row is None:
        return {"error": "no such update request; nothing changed"}
    try:
        ap.vote(row, pid=OWNER["pid"], allow=allow, now=now, eligible=[OWNER["pid"]])
    except ap.ApprovalError as exc:
        return {"error": str(exc), "approval": ap.public(row)}
    kind = (row.get("policy") or {}).get("kind", "host")
    await _log(receipt, "update_vote", f"home.updates.{kind}",
               {"approval": row["id"], "digest": row["digest"], "allow": bool(allow)},
               {"status": row["status"]}, f"quorum:{row['id']}:{row['approvals']}/1")
    if row["status"] != ap.APPROVED:
        if persist is not None:
            persist()
        await notify_approval(ap.public(row))
        return {"ok": True, "ran": False, "approval": ap.public(row)}
    try:
        act = ap.claim_for_run(row, now)
    except ap.ApprovalError as exc:
        return {"error": str(exc), "approval": ap.public(row)}
    if persist is not None:
        persist()
    approval = f"quorum:{row['id']}:1/1"
    args = {"action": act, "digest": row["digest"], "approved_by": [OWNER["pid"]]}
    if act["domain"] == "adk":
        if su is None:
            from adk import self_update as su
        res = apply_adk(su, {**check_adk(su), "pending": True})
        ok = res["status"] == "requested"
    else:
        plat = platform or detect_platform()
        wanted = act["target"].split(".", 1)[1]
        if wanted != plat:
            res = {"ok": False, "error": f"this card is for {wanted}, this host is {plat}"}
            ok = False
        elif plat == "debian":
            res = run_host_upgrade(euid, upgrader)
            if res["state"] == "still_running":
                row["result"] = res               # stays ``executing``: not a failure
                await _log(receipt, "update_running", f"home.updates.{kind}", args, res,
                           approval)
                await notify_approval(ap.public(row))
                return {"ok": False, "ran": False, "result": res,
                        "approval": ap.public(row)}
            ok = bool(res["ok"])
        else:
            res = {"ok": True, "ran": False, "instructions": INSTRUCTIONS.get(
                plat, INSTRUCTIONS["unsupported"])}
            ok = True
    await _log(receipt, "update_apply" if ok else "update_failed", f"home.updates.{kind}",
               args, res, approval)
    ap.finish(row, ok, res, now)
    await notify_approval(ap.public(row))
    return {"ok": ok, "ran": bool(ok and not res.get("instructions")), "result": res,
            "approval": ap.public(row)}


__all__ = [
    "check_all", "check_adk", "check_host", "check_ha", "parse_apt_upgradable",
    "parse_softwareupdate", "parse_windows_updates", "parse_ha_updates", "detect_platform",
    "raise_cards", "request_ha", "decide_card", "run_host_upgrade", "run_allowlisted",
    "run_no_kill", "prune_apt_logs", "upgrade_argv", "update_argv", "sudoers_hint",
    "clean", "load_state",
    "save_state", "merge_rows", "valid_row", "state_path", "is_due", "last_window",
    "next_window", "parse_weekday", "parse_at", "validate_schedule", "APT_UPDATE",
    "APT_UPGRADE", "ALLOWED_APPLY", "UPGRADE_ENV", "INSTRUCTIONS",
]
