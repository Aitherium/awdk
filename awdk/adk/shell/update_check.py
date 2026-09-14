"""
AitherShell Auto-Update Check
===============================

Non-blocking version check on startup. Fetches the release manifest
from the gateway and compares against the installed version.

- Checks at most once per day (cached in ~/.aither/update-check.json)
- Never blocks startup — runs in background, prints warning after
- Respects AITHER_NO_UPDATE_CHECK=1 to disable
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

UPDATE_CACHE = Path.home() / ".aither" / "update-check.json"
CHECK_INTERVAL = 86400  # 24 hours
MANIFEST_URL = "https://gateway.aitherium.com/api/v1/releases/latest"
GITHUB_URL = "https://api.github.com/repos/Aitherium/AitherOS/releases/latest"


def _installed_version() -> str:
    """Get the installed version from pyproject.toml metadata."""
    try:
        from importlib.metadata import version
        return version("aithershell")
    except (ImportError, ModuleNotFoundError):
        return "0.0.0"


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


def _read_cache() -> Optional[dict]:
    """Read cached update check result."""
    if not UPDATE_CACHE.exists():
        return None
    try:
        data = json.loads(UPDATE_CACHE.read_text())
        if time.time() - data.get("checked_at", 0) < CHECK_INTERVAL:
            return data
    except Exception:
        pass
    return None


def _write_cache(latest: str, update_available: bool):
    """Cache the check result."""
    try:
        UPDATE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        UPDATE_CACHE.write_text(json.dumps({
            "latest_version": latest,
            "update_available": update_available,
            "checked_at": time.time(),
        }))
    except Exception:
        pass


def check_for_update() -> Optional[str]:
    """
    Check if a newer version is available. Returns a warning message
    if an update is available, None otherwise.

    Non-blocking: uses cache, falls back silently on network errors.
    """
    if os.environ.get("AITHER_NO_UPDATE_CHECK", "") == "1":
        return None

    # Check cache first
    cached = _read_cache()
    if cached is not None:
        if cached.get("update_available"):
            return f"Update available: v{cached['latest_version']} (you have v{_installed_version()}). Run: pip install --upgrade aithershell"
        return None

    # Fetch latest version (with aggressive timeout)
    latest = _fetch_latest_version()
    if not latest:
        return None

    installed = _installed_version()
    update_available = _parse_version(latest) > _parse_version(installed)
    _write_cache(latest, update_available)

    if update_available:
        return f"Update available: v{latest} (you have v{installed}). Run: pip install --upgrade aithershell"
    return None


def _fetch_latest_version() -> Optional[str]:
    """Fetch latest version from gateway manifest or GitHub releases."""
    import httpx

    # Try gateway manifest first (fast, Cloudflare edge)
    for url in [MANIFEST_URL, GITHUB_URL]:
        try:
            resp = httpx.get(url, timeout=3.0, follow_redirects=True)
            if resp.status_code == 200:
                data = resp.json()
                # Gateway manifest format
                if "latest_version" in data:
                    v = data["latest_version"]
                    if v and v != "latest":
                        return v
                # GitHub releases format
                if "tag_name" in data:
                    return data["tag_name"].lstrip("v")
                # Check nested products
                shell = (data.get("products") or {}).get("aithershell", {})
                if shell.get("version"):
                    return shell["version"]
        except Exception:
            continue
    return None


def print_update_notice():
    """Print update notice if available. Call from CLI entry point."""
    try:
        msg = check_for_update()
        if msg:
            print(f"\n  {msg}\n", file=sys.stderr)
    except Exception:
        pass  # Never crash on update check
