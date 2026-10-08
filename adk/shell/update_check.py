"""
adk shell update check
======================

Non-blocking version check on startup of the `aither` shell (adk.shell.cli
entry). Asks PyPI for the newest published ``awdk`` and compares it with the
installed ``awdk`` distribution.

This used to check the retired ``aithershell`` package (installed version via
``version("aithershell")``, latest via a gateway manifest / AitherOS GitHub
release) and told people to ``pip install --upgrade aithershell``. The shell
ships inside awdk now, so on every current install ``version("aithershell")``
raised and the check either said nothing or compared against an unrelated
release tag -- and the command it printed installed the wrong package.

- Checks at most once per day (latest version cached in
  ~/.aither/update-check-awdk.json; the comparison is redone against the
  installed version on every start, so an upgrade silences the notice at once)
- Never blocks startup for more than ~1.5s once a day, and never raises. The
  attempt is stamped in the cache before the lookup starts, so a box that
  cannot reach pypi.org also waits only once a day, not on every start
- Off with AITHER_NO_UPDATE_CHECK=1 or ADK_NO_UPDATE_CHECK=1, and on an
  offline box (AITHER_OFFLINE=1), matching adk.cli._check_for_updates
"""

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

PACKAGE = "awdk"
# New file name on purpose: the old aithershell cache could hold
# {"update_available": true, "latest_version": <an AitherOS release tag>}.
UPDATE_CACHE = Path.home() / ".aither" / "update-check-awdk.json"
CHECK_INTERVAL = 86400  # 24 hours
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
FETCH_TIMEOUT = 3.0
#: How long startup waits for a fresh lookup before moving on without it.
STARTUP_WAIT = 1.5

_TRUTHY = ("1", "true", "yes", "on")


def _disabled() -> bool:
    env = os.environ
    return any(
        (env.get(k) or "").strip().lower() in _TRUTHY
        for k in ("AITHER_NO_UPDATE_CHECK", "ADK_NO_UPDATE_CHECK", "AITHER_OFFLINE")
    )


def _installed_version() -> str:
    """The installed awdk version, or "" when it is not an installed dist."""
    try:
        from importlib.metadata import version
        return version(PACKAGE)
    except Exception:
        return ""


def _parse_version(v: str) -> Tuple[int, ...]:
    """Parse semver-ish string to tuple for comparison."""
    clean = v.lstrip("v").split("-")[0].split("+")[0]
    parts = []
    for p in clean.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts) if parts else (0,)


def upgrade_command(prefix: Optional[str] = None) -> str:
    """The upgrade command for how this interpreter was installed.

    pipx and `uv tool` each keep the package in their own venv, where a bare
    `pip install --upgrade` either hits the wrong environment or is refused.
    """
    p = (prefix if prefix is not None else sys.prefix).replace("\\", "/").lower()
    if "/pipx/venvs/" in p:
        return f"pipx upgrade {PACKAGE}"
    if "/uv/tools/" in p:
        return f"uv tool upgrade {PACKAGE}"
    return f"pip install --upgrade {PACKAGE}"


def _last_try(data: dict) -> float:
    """When PyPI was last asked: a success (checked_at) or any attempt."""
    stamps = []
    for k in ("checked_at", "attempted_at"):
        try:
            stamps.append(float(data.get(k) or 0))
        except (TypeError, ValueError):
            pass
    return max(stamps or [0.0])


def _load_cache() -> Optional[dict]:
    """The awdk cache record whatever its age, or None."""
    try:
        data = json.loads(UPDATE_CACHE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("package") == PACKAGE:
            return data
    except Exception:
        pass
    return None


def _read_cache() -> Optional[dict]:
    """The cache record when PyPI was asked (successfully or not) within CHECK_INTERVAL."""
    data = _load_cache()
    if data is not None and time.time() - _last_try(data) < CHECK_INTERVAL:
        return data
    return None


def _save(data: dict) -> None:
    try:
        UPDATE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        UPDATE_CACHE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _stamp_attempt(prev: Optional[dict]) -> None:
    """Record that a lookup is starting, before it can fail or hang.

    Without this a failed lookup left no trace and every start waited the
    full STARTUP_WAIT again. Keeps any older latest_version/checked_at.
    """
    data = dict(prev or {})
    data["package"] = PACKAGE
    data["attempted_at"] = time.time()
    _save(data)


def _write_cache(latest: str) -> None:
    now = time.time()
    _save({
        "package": PACKAGE,
        "latest_version": latest,
        "checked_at": now,
        "attempted_at": now,
    })


def _fetch_latest_version() -> Optional[str]:
    """The newest awdk on PyPI, or None on any failure."""
    try:
        from urllib.request import urlopen
        with urlopen(PYPI_URL, timeout=FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read())
        latest = (data.get("info") or {}).get("version") or ""
        return latest or None
    except Exception:
        return None


def _notice(latest: str, installed: str) -> Optional[str]:
    # Only when PyPI is genuinely NEWER: a dev install ahead of PyPI must not be
    # told to "update" to an older release (see adk.cli._check_for_updates).
    if latest and installed and _parse_version(latest) > _parse_version(installed):
        return (f"Update available: awdk {installed} -> {latest}. "
                f"Run: {upgrade_command()}")
    return None


def check_for_update(wait: float = STARTUP_WAIT) -> Optional[str]:
    """
    Return an update notice when a newer awdk is on PyPI, else None.

    Uses the cache when PyPI was asked within the day (a failed ask counts,
    so an offline box is not re-asked on every start). Otherwise stamps the
    attempt, looks PyPI up on a daemon thread and waits at most `wait`
    seconds for it; a slower answer is still cached for the next start.
    """
    if _disabled():
        return None
    installed = _installed_version()

    cached = _read_cache()
    if cached is not None:
        return _notice(cached.get("latest_version") or "", installed)

    _stamp_attempt(_load_cache())
    result: dict = {}

    def _lookup() -> None:
        latest = _fetch_latest_version()
        if latest:
            _write_cache(latest)
            result["latest"] = latest

    t = threading.Thread(target=_lookup, name="awdk-update-check", daemon=True)
    t.start()
    t.join(wait)
    return _notice(result.get("latest") or "", installed)


def print_update_notice():
    """Print update notice if available. Call from CLI entry point."""
    try:
        msg = check_for_update()
        if msg:
            print(f"\n  {msg}\n", file=sys.stderr)
    except Exception:
        pass  # Never crash on update check
