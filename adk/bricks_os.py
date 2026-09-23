"""The OS brick: awnix, upgraded and rolled back the way awnix is -- with bootc.

awnix is a bootable container image, not a pip package, so ``adk bricks`` cannot
treat it like the others. Everything here goes through ``bootc``:

* installed  -- ``bootc status --json``: the booted image, its version, anything
  staged, whether a rollback deployment exists.
* latest     -- ``bootc upgrade --check``: asks the registry without staging.
* upgrade    -- ``bootc upgrade``: STAGES the new image. Nothing changes until a
  reboot, and the result says so; it never claims the new OS is running.
* rollback   -- ``bootc rollback``: queues the previous deployment, same rule.
* test       -- what the BOOTED system can prove: bootc knows its image, and
  systemd reports no failed units.

Truth rules: a machine without bootc is "not an awnix host", never "up to
date"; a check that needs root and did not get it says so, never "no update".
On a machine that is not awnix, ``published()`` names the newest awnix
release so the owner can still see that one exists.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

OS_ID = "awnix"
RELEASES_URL = os.environ.get(
    "ADK_AWNIX_RELEASES", "https://api.github.com/repos/Aitherium/awnix/releases?per_page=20"
)


def _run(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    """Run a command, never raise: (exit code, combined output). 127 = not found."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out after {timeout}s"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def is_host() -> bool:
    return shutil.which("bootc") is not None


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def _needs_root(out: str) -> bool:
    low = out.lower()
    return "permission denied" in low or "must be root" in low or "requires root" in low


def _image_facts(entry: dict | None) -> dict | None:
    """{ref, version, digest} from one bootc deployment entry, else None."""
    if not isinstance(entry, dict):
        return None
    img = entry.get("image") or {}
    ref = ((img.get("image") or {}).get("image")) or None
    digest = img.get("imageDigest") or None
    version = img.get("version") or (digest[:19] if digest else None)
    if not (ref or digest):
        return None
    return {"ref": ref, "version": version, "digest": digest}


def host_status(run=_run) -> dict:
    """Parsed ``bootc status --json``: booted/staged/rollback facts, or an error."""
    code, out = run(["bootc", "status", "--json"], timeout=60)
    if code != 0:
        why = "needs root: run with sudo" if _needs_root(out) else out.strip()[-300:]
        return {"ok": False, "error": f"bootc status failed: {why}"}
    try:
        doc = json.loads(out[out.index("{") :])
    except (ValueError, json.JSONDecodeError):
        return {"ok": False, "error": "bootc status returned no JSON"}
    st = doc.get("status") or {}
    return {
        "ok": True,
        "booted": _image_facts(st.get("booted")),
        "staged": _image_facts(st.get("staged")),
        "rollback": _image_facts(st.get("rollback")),
        "rollback_queued": bool(st.get("rollbackQueued")),
    }


def check_update(run=_run) -> tuple[str | None, bool, str | None]:
    """(latest version, update available, error) from ``bootc upgrade --check``."""
    code, out = run(["bootc", "upgrade", "--check"], timeout=180)
    if code != 0:
        if _needs_root(out):
            return None, False, "checking needs root: sudo adk bricks list"
        return None, False, f"bootc upgrade --check failed: {out.strip()[-200:]}"
    low = out.lower()
    if "no changes" in low or "no update" in low:
        return None, False, None
    if "update available" in low:
        version = None
        for line in out.splitlines():
            key, _, val = line.strip().partition(":")
            if key.lower() == "version" and val.strip():
                version = val.strip()
                break
            if key.lower() == "digest" and val.strip() and version is None:
                version = val.strip()[:19]
        return version or "newer image", True, None
    return None, False, f"bootc upgrade --check: unrecognised answer: {out.strip()[-160:]}"


def row(check_latest: bool = True, run=_run) -> dict | None:
    """The awnix row for ``adk bricks list``; None when this machine is not awnix."""
    if not is_host():
        return None
    st = host_status(run)
    base = {
        "id": OS_ID,
        "dist": None,
        "source": "bootc",
        "installed": None,
        "editable": False,
        "latest": None,
        "error": None,
        "outdated": False,
        "action": "",
    }
    if not st["ok"]:
        base["error"] = st["error"]
        return base
    booted = st["booted"] or {}
    base["dist"] = booted.get("ref")
    base["installed"] = booted.get("version") or "unknown"
    if st["staged"]:
        base["action"] = f"{st['staged']['version']} is staged: reboot to switch to it"
        return base
    if st["rollback_queued"]:
        base["action"] = "a rollback is queued: reboot to apply it"
        return base
    if check_latest:
        latest, available, err = check_update(run)
        base["error"] = err
        base["latest"] = latest if available else (None if err else base["installed"])
        base["outdated"] = available
        if available:
            base["action"] = "adk bricks upgrade awnix"
    return base


def upgrade(run=_run) -> dict:
    if not is_host():
        return {"id": OS_ID, "ok": False, "error": "this machine is not an awnix (bootc) host"}
    if not _is_root():
        return {
            "id": OS_ID,
            "ok": False,
            "error": "upgrading the OS needs root: sudo adk bricks upgrade awnix",
        }
    before = (host_status(run).get("booted") or {}).get("version")
    code, out = run(["bootc", "upgrade"], timeout=3600)
    if code != 0:
        return {
            "id": OS_ID,
            "ok": False,
            "from": before,
            "error": "bootc upgrade failed",
            "detail": out[-1500:],
        }
    staged = (host_status(run).get("staged") or {}).get("version")
    if not staged:
        return {
            "id": OS_ID,
            "ok": True,
            "from": before,
            "to": before,
            "note": "already at the latest image",
        }
    return {
        "id": OS_ID,
        "ok": True,
        "from": before,
        "to": staged,
        "staged": True,
        "note": "staged, NOT running: reboot to switch; adk bricks rollback awnix to undo",
    }


def rollback(run=_run) -> dict:
    if not is_host():
        return {"id": OS_ID, "ok": False, "error": "this machine is not an awnix (bootc) host"}
    if not _is_root():
        return {
            "id": OS_ID,
            "ok": False,
            "error": "rolling the OS back needs root: sudo adk bricks rollback awnix",
        }
    st = host_status(run)
    if st.get("ok") and not st.get("rollback") and not st.get("staged"):
        return {"id": OS_ID, "ok": False, "error": "no previous deployment to roll back to"}
    current = (st.get("booted") or {}).get("version")
    target = (st.get("rollback") or {}).get("version")
    code, out = run(["bootc", "rollback"], timeout=600)
    if code != 0:
        return {"id": OS_ID, "ok": False, "error": "bootc rollback failed", "detail": out[-1500:]}
    return {
        "id": OS_ID,
        "ok": True,
        "from": current,
        "to": target,
        "staged": True,
        "note": "rollback queued, NOT running: reboot to apply it",
    }


def test(run=_run) -> dict:
    """What the booted OS can prove about itself right now."""
    if not is_host():
        return {"id": OS_ID, "ok": False, "import": "not an awnix host", "selftest": "skipped"}
    st = host_status(run)
    if not st["ok"] or not st.get("booted"):
        return {
            "id": OS_ID,
            "ok": False,
            "import": "bootc has no booted image",
            "selftest": "skipped",
            "detail": st.get("error"),
        }
    code, out = run(["systemctl", "--failed", "--no-legend", "--plain"], timeout=30)
    if code == 127:
        return {"id": OS_ID, "ok": True, "import": "ok", "selftest": "none (no systemctl to ask)"}
    failed = [line.split()[0] for line in out.splitlines() if line.strip()]
    ok = code == 0 and not failed
    return {
        "id": OS_ID,
        "ok": ok,
        "import": "ok",
        "selftest": "pass" if ok else f"{len(failed)} failed unit(s)",
        "detail": ", ".join(failed[:20]) or None,
    }


def published(timeout: float = 8.0) -> dict:
    """Newest awnix release per flavour, for a machine that is not awnix yet."""
    try:
        req = urllib.request.Request(
            RELEASES_URL, headers={"Accept": "application/vnd.github+json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            releases = json.load(resp)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"releases HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return {"ok": False, "error": f"releases unreachable: {type(e).__name__}"}
    newest: dict[str, dict] = {}
    for rel in releases if isinstance(releases, list) else []:
        tag = str(rel.get("tag_name") or "")
        if not tag.startswith("awnix-") or rel.get("draft"):
            continue
        # awnix-iso-<flavour>-YYYY.MM.DD
        stem, _, date = tag.rpartition("-")
        flavour = stem.replace("awnix-iso-", "").replace("awnix-", "") or "base"
        prev = newest.get(flavour)
        if prev is None or date > prev["version"]:
            newest[flavour] = {
                "flavour": flavour,
                "version": date,
                "tag": tag,
                "url": rel.get("html_url"),
            }
    return {"ok": True, "releases": sorted(newest.values(), key=lambda r: r["flavour"])}
