"""Is every Aither program on this machine linked to the one Aither ID, and to each other?

One answer to a question that used to be six: the awdk daemon, the awsh harness, Claude Code
running as an awsh session, AitherConnect (the browser extension), awnode and awsettings each
knew only its own state, so a machine could be "set up" and still have a dead link nobody saw
(2026-11-16 on the owner's PC: the daemon was signed in, the node was beating, and nothing could
say so; on the DGX Spark a stale person sign-in shadowed the device token and the node read
offline for five days while serving).

``measure()`` reads every link from where it lives, on loopback, with a short timeout, and never
raises: a link it cannot read is ``unknown``, never ``ok``. The awdk daemon serves it at
``GET /stack/links`` (read-only, names and counts, no credential) for the "This device" page and
the dev-side checker. Every state names the plain-words fix; a terminal command, when one exists,
rides separately in
``command`` so a page can keep it behind a details toggle (a person never has to read it).

States: ``ok`` linked and working; ``attention`` present but not linked or not syncing;
``down`` expected and not answering; ``absent`` not installed here; ``unknown`` could not be read.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from adk.rc import default_harness_url

__all__ = ["measure", "LINK_IDS"]



def _port(name: str, default: int) -> int:
    """An env port; a bad value falls back instead of killing the import."""
    try:
        port = int(os.environ.get(name) or default)
    except ValueError:
        return default
    return port if 0 < port < 65536 else default


#: The awnode hub's own port and override (awnode/hub.py: AW_HUB_PORT). Never :8090, which is
#: the local llama-server's port and would read a Bonsai-only machine as "awnode running".
AWNODE_HUB_PORT = _port("AW_HUB_PORT", 47933)
TIMEOUT_S = 1.5
#: awsettings runs a subprocess that makes a network round-trip to the settings hub, so it is
#: the slowest link and the only one with a budget of its own. It gets less than the page
#: waits, so one slow link can never make the whole panel vanish.
#:
#: History, all measured on the owner box: the original 5.0 was too tight once the token was
#: fixed (a real credential helper costs a Python start); 7.0 then still lost the race when
#: the hub or IDP was slow. 2026-11-23, eight back-to-back status runs: 1.8-3.8 s idle, but
#: 8-14 s while the host was loaded, and every run over the budget read "Settings sync could
#: not report" for a sync that was in step. 12 s covers the measured worst case and stays
#: under DEADLINE_S; the subprocess has its own 15 s httpx timeout, so this is a bound, not a
#: hope.
AWSETTINGS_BUDGET_S = 12.0
#: The whole measurement is bounded below the page's own timeout; a probe still running at the
#: deadline reads ``unknown``, never ``ok``. It is 13 s because the awsettings bound is 12 s;
#: the page's /stack/links client timeout is raised to 18 s to match.
DEADLINE_S = 13.0
#: The page polls every 30 s; one measurement serves everyone for this long.
CACHE_TTL_S = 15.0

LINK_IDS = ("aither-id", "device", "awsh", "claude", "awsettings", "awconnect", "awnode")

Link = Dict[str, Any]
HttpGet = Callable[[str, Dict[str, str], float], Tuple[int, Any]]
Run = Callable[[List[str], float], Tuple[int, str]]


def _http_get(url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, Any]:
    """(status, json-or-None); status 0 when nothing answered. Never raises."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 -- loopback
            raw = resp.read(1_000_000)
            try:
                return resp.status, json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                return resp.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (OSError, ValueError):
        return 0, None


def _run(cmd: List[str], timeout: float) -> Tuple[int, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        done = subprocess.run(  # noqa: S603 -- a fixed argv, no shell
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=flags, check=False)
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _link(id_: str, label: str, state: str, detail: str, fix: str = "", command: str = "") -> Link:
    return {"id": id_, "label": label, "state": state, "detail": detail, "fix": fix,
            "command": command}


def _aither_id() -> Link:
    label = "Your Aither account"
    try:
        from adk.auth import AuthStore, resolve_credentials

        store = AuthStore()
        prof = store.get_active_profile()
    except Exception:  # noqa: BLE001 -- a broken auth.json is "not signed in", said so
        return _link("aither-id", label, "unknown", "Aither could not read who is signed in here",
                     "Sign in again.", "adk login")
    if not prof or not prof.get("access_token"):
        return _link("aither-id", label, "attention", "Nobody is signed in on this computer yet",
                     "Sign in with your Aither account.", "adk login")
    if prof.get("token_type") == "local" or (prof.get("endpoint") or "") == "local":
        return _link("aither-id", label, "attention", "Only the offline login is here",
                     "Sign in with your Aither account to link this computer.", "adk login")
    try:
        expired = bool(resolve_credentials(store=store).is_expired)
    except Exception:  # noqa: BLE001 -- Identity is the authority on expiry
        expired = False
    who = (prof.get("user") or {}).get("display_name") or (prof.get("user") or {}).get("username")
    if expired:
        return _link("aither-id", label, "attention", "Your sign-in on this computer has run out",
                     "Sign in again.", "adk login")
    return _link("aither-id", label, "ok", f"Signed in as {who}" if who else "Signed in")


def _device(now: float) -> Link:
    label = "This computer on your account"
    try:
        from adk.device_identity import beat_is_for, leader_beat
        from adk.fleet_enroll import _load_node_auth

        rec = _load_node_auth() or {}
        beat = leader_beat(now=now)
    except Exception:  # noqa: BLE001
        return _link("device", label, "unknown", "This computer's record could not be read",
                     "Look again in a moment.")
    node = str(rec.get("node_id") or "")
    if not node:
        return _link("device", label, "attention", "This computer is not added to your account yet",
                     "Use Set up this computer.")
    if not beat or not beat_is_for(beat, node):
        return _link("device", label, "down", "It is added, but nothing is reporting that it is on",
                     "Restart Aither on this computer; it keeps reporting after that.")
    ok_age = beat["ok_age"]
    if ok_age is None:
        return _link("device", label, "attention",
                     "It reports in, but the last report was not confirmed",
                     "Update Aither on this computer; this fixes itself afterwards.",
                     "adk autoupdate apply")
    if ok_age > beat["interval"] * 3:
        return _link("device", label, "down", "Your account has not heard from it for a while",
                     "Sign in again, or restart Aither on this computer.")
    return _link("device", label, "ok", "Reporting in; your account sees it online")


def _harness_token() -> str:
    try:
        return (Path.home() / ".aither" / "harness_token").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _awsh(get: HttpGet) -> Tuple[Link, Optional[Dict[str, Any]]]:
    label = "Assistant sessions"
    status, body = get(f"{default_harness_url()}/health", {}, TIMEOUT_S)
    if status != 200 or not isinstance(body, dict):
        return (_link("awsh", label, "down",
                      "The service that keeps your assistants running is off",
                      "Aither turns it on by itself within a minute; "
                      "if it stays off, restart Aither.",
                      "adk harness serve"), None)
    n = body.get("sessions")
    return (_link("awsh", label, "ok",
                  f"{n} running" if isinstance(n, int) else "Running"), body)


def _claude(get: HttpGet, health: Optional[Dict[str, Any]], which: Callable[[str], Any]) -> Link:
    label = "Claude Code"
    installed = (health or {}).get("harnesses_installed")
    has_binary = bool(which("claude"))
    if health is None:
        return _link("claude", label, "down",
                     "Claude Code has nowhere to run until the assistant service is on",
                     "Wait a minute for it to start, or restart Aither.")
    if isinstance(installed, list) and "claude" not in installed and not has_binary:
        return _link("claude", label, "absent", "Claude Code is not installed on this computer",
                     "Install Claude Code; Aither runs it for you after that.")
    token = _harness_token()
    status, body = get(f"{default_harness_url()}/sessions",
                       {"Authorization": f"Bearer {token}"}, TIMEOUT_S)
    if status != 200 or not isinstance(body, dict):
        return _link("claude", label, "unknown", "Aither could not list your Claude Code sessions",
                     "Restart Aither.")
    live = [s for s in body.get("sessions") or []
            if isinstance(s, dict) and str(s.get("harness", "")).startswith("claude")
            and not s.get("exit_code") and s.get("state") != "exited"]
    if live:
        return _link("claude", label, "ok", f"{len(live)} running")
    # Installed, hosted and answering: the link works. No open session is idle, not broken.
    return _link("claude", label, "ok", "Ready; none open right now")


def _awsettings(run: Run, which: Callable[[str], Any]) -> Link:
    label = "Your settings, kept in step"
    exe = which("awsettings")
    if not exe:
        return _link("awsettings", label, "absent",
                     "Settings sync is not installed on this computer",
                     "Install it so your settings follow you.")
    # --user: the ACCOUNT-level settings this device follows. Without it status reads whatever
    # project the daemon happens to be running in (measured 2026-11-16: "would apply 185
    # changes" for a worktree with no settings of its own, on a machine whose sync was in step).
    code, text = run([str(exe), "--user", "--quiet", "status"], AWSETTINGS_BUDGET_S)
    low = text.lower()
    if "dead:" in low:
        # awsettings reached for the account's store and the SERVICE failed (a 503 on
        # 2026-11-16): not a fault of this device, and not "in step" either.
        return _link("awsettings", label, "unknown",
                     "Your account's settings service is not answering",
                     "Nothing to do here; it catches up when the service is back.")
    if code != 0:
        return _link("awsettings", label, "unknown", "Settings sync could not report",
                     "Look again in a minute.", "awsettings status")
    if "no remote" in low or "remote: none" in low or "not configured" in low:
        return _link("awsettings", label, "attention",
                     "Settings sync is not linked to your account",
                     "Link this computer to your account.", "adk enroll")
    if "in step" in low:
        return _link("awsettings", label, "ok", "In step with your account")
    return _link("awsettings", label, "attention", "Your settings here differ from your account",
                 "Bring them back in step.", "awsettings pull")


def _awconnect(now: float) -> Link:
    label = "Aither in your browser"
    try:
        from adk import extension_pair

        paired = len(extension_pair._load(now=now))  # noqa: SLF001 -- the store's own reader
    except Exception:  # noqa: BLE001
        return _link("awconnect", label, "unknown", "The list of paired browsers could not be read",
                     "Look again in a moment.")
    if paired:
        plural = "" if paired == 1 else "s"
        return _link("awconnect", label, "ok", f"{paired} browser{plural} paired")
    installed = (Path.home() / ".aither" / "awconnect").is_dir()
    if installed:
        return _link("awconnect", label, "attention",
                     "Installed, but not approved for this computer yet",
                     "Open Aither in your browser and approve the pairing.")
    return _link("awconnect", label, "absent", "Not added to a browser on this computer",
                 "Add the Aither extension to your browser.")


def _awnode(get: HttpGet) -> Link:
    label = "Aither helper on this computer"
    status, _ = get(f"http://127.0.0.1:{AWNODE_HUB_PORT}/health", {}, TIMEOUT_S)
    if status == 200:
        return _link("awnode", label, "ok", "Running")
    return _link("awnode", label, "down", "Not running",
                 "Start Aither's helper on this computer.", "awnode up")


def _awsh_and_claude(get: HttpGet, which: Callable[[str], Any]) -> Tuple[Link, Link]:
    awsh_link, health = _awsh(get)
    return awsh_link, _claude(get, health, which)


_LABELS = {
    "aither-id": "Your Aither account", "device": "This computer on your account",
    "awsh": "Assistant sessions", "claude": "Claude Code",
    "awsettings": "Your settings, kept in step", "awconnect": "Aither in your browser",
    "awnode": "Aither helper on this computer",
}


def _unreadable(id_: str) -> Link:
    """A probe still running at the deadline: not ok, and not claimed broken either."""
    return _link(id_, _LABELS[id_], "unknown", "Aither did not hear back in time",
                 "Look again in a moment.")


_cache: Dict[str, Any] = {"at": 0.0, "value": None}
_cache_lock = threading.Lock()


def measure(*, now: Optional[float] = None, http_get: Optional[HttpGet] = None,
            run: Optional[Run] = None,
            which: Optional[Callable[[str], Any]] = None,
            deadline: float = DEADLINE_S) -> Dict[str, Any]:
    """Every link, measured now. Never raises; the first four arguments are for tests.

    The probes run side by side (one slow link cannot starve the rest) and are bounded by
    ``deadline``. The live measurement, with no injected probe, is cached for ``CACHE_TTL_S``
    and measured once at a time, so a page polling every 30 s costs one measurement.
    """
    injected = any(x is not None for x in (now, http_get, run, which))
    if injected:
        return _measure(now, http_get, run, which, deadline)
    with _cache_lock:
        if _cache["value"] is not None and time.monotonic() - _cache["at"] < CACHE_TTL_S:
            return _cache["value"]
        value = _measure(None, None, None, None, deadline)
        _cache.update(at=time.monotonic(), value=value)
        return value


def _measure(now: Optional[float], http_get: Optional[HttpGet], run: Optional[Run],
             which: Optional[Callable[[str], Any]], deadline: float) -> Dict[str, Any]:
    t = time.time() if now is None else now
    get = http_get or _http_get
    runner = run or _run
    find = which or shutil.which
    pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="stack-links")
    try:
        f_id = pool.submit(_aither_id)
        f_dev = pool.submit(_device, t)
        f_sh = pool.submit(_awsh_and_claude, get, find)
        f_set = pool.submit(_awsettings, runner, find)
        f_con = pool.submit(_awconnect, t)
        f_node = pool.submit(_awnode, get)
        end = time.monotonic() + deadline

        def take(fut: Any, fallback: Callable[[], Any]) -> Any:
            try:
                return fut.result(timeout=max(0.0, end - time.monotonic()))
            except (FutureTimeout, Exception):  # noqa: BLE001 -- a probe bug reads "unknown"
                return fallback()

        awsh_link, claude_link = take(f_sh, lambda: (_unreadable("awsh"), _unreadable("claude")))
        links: List[Link] = [
            take(f_id, lambda: _unreadable("aither-id")),
            take(f_dev, lambda: _unreadable("device")),
            awsh_link, claude_link,
            take(f_set, lambda: _unreadable("awsettings")),
            take(f_con, lambda: _unreadable("awconnect")),
            take(f_node, lambda: _unreadable("awnode")),
        ]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    ok = sum(1 for lk in links if lk["state"] == "ok")
    return {
        "links": links, "ok": ok, "total": len(links),
        # A link that is absent by choice is not a fault; down/attention/unknown are.
        "all_ok": all(lk["state"] in ("ok", "absent") for lk in links),
        "python": sys.version_info[:2] >= (3, 10),
    }
