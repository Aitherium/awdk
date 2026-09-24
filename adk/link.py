"""adk link -- one way to link THIS machine to aitherium.com, for every surface on it.

Before this, each surface signed in its own way: awsh ran a device flow in
TypeScript, `adk login` ran another in Python, the browser extension had four,
and awdesk had none at all -- and none of them knew whether the person signing
in was the platform owner or a stranger. `adk link` is the one implementation:
awdesk's Settings and awsh call it (as they call `adk bricks`), a person can run
it by hand, and every result is the same set of files:

* the sign-in itself, persisted by ``complete_device_login`` exactly as
  `adk login` persists it (config, license, workspace endpoints, and the shell's
  ``auth.json`` profile) -- so awsh, adk and awdesk share one credential;
* ``~/.aither/link-bundle.json``: the role-aware bundle Genesis returns for
  that credential (GET /v1/link/bundle via the portal's Genesis bridge). The
  platform owner's bundle carries the endpoint map, vault routes and fleet
  control; everyone else's is bare bones. It never holds a token or a secret.

Verbs, all with ``--json``:
  start            ask Identity for a device code; prints the approve link
  poll <device>    one poll; on approval persists the sign-in and fetches the bundle
  status           linked or not, who, and as what role
  refresh          re-fetch the bundle with the stored credential

Truth rules: a bundle is only ever what the server returned; a refused
credential CLEARS the stored bundle; a network failure keeps the last good one
and says so; nothing here prints a token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_PORTAL = "https://portal.aitherium.com"
BUNDLE_PATH = "/api/bridge/genesis/v1/link/bundle"
UA = "adk-link"
# Identity answered the device grant in 10.3 s on a loaded evening (measured
# 2026-09-23); a 10 s budget read every such poll as a timeout.


def home() -> Path:
    return Path(os.environ.get("AITHER_HOME") or Path.home() / ".aither")


def bundle_file() -> Path:
    return home() / "link-bundle.json"


def identity_url() -> str:
    """Identity host for the device flow (idp.aitherium.com for the cloud)."""
    explicit = os.environ.get("AITHER_IDENTITY_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    from adk.cli import _resolve_identity_url

    return _resolve_identity_url(os.environ.get("AITHER_PORTAL_URL", DEFAULT_PORTAL))


def portal_url() -> str:
    return os.environ.get("AITHER_PORTAL_URL", DEFAULT_PORTAL).rstrip("/")


def _post(url: str, body: Dict[str, Any], timeout: float = 25.0):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": UA,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read() or b"{}")
        except (ValueError, OSError):
            data = {}
        return exc.code, data


def start() -> Dict[str, Any]:
    """Ask Identity for a device code. Never raises."""
    base = identity_url()
    try:
        status, data = _post(f"{base}/auth/device/code", {"client_name": "adk-link"})
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "error": f"identity unreachable: {type(exc).__name__}"}
    if status != 200 or not isinstance(data.get("device_code"), str):
        return {"ok": False, "error": data.get("detail") or data.get("error") or f"HTTP {status}"}
    return {
        "ok": True,
        "device_code": data["device_code"],
        "user_code": data.get("user_code", ""),
        "approve_url": data.get("verification_uri_complete") or data.get("verification_uri", ""),
        "interval": int(data.get("interval") or 5),
        "expires_in": int(data.get("expires_in") or 900),
    }


def poll(device_code: str) -> Dict[str, Any]:
    """One poll. On approval: persist the sign-in, fetch the bundle."""
    base = identity_url()
    try:
        status, data = _post(f"{base}/auth/device/token", {"device_code": device_code}, timeout=25)
    except (urllib.error.URLError, OSError) as exc:
        # A dropped poll is not a verdict; the caller keeps polling.
        return {"ok": True, "status": "authorization_pending", "transient": type(exc).__name__}
    if data.get("access_token"):
        from adk.cli import complete_device_login

        username = complete_device_login(base, data, sync=False)
        fetched = refresh(token=data["access_token"])
        return {
            "ok": True,
            "status": "complete",
            "username": username,
            "role": (fetched.get("bundle") or {}).get("role"),
            "bundle_error": None if fetched.get("ok") else fetched.get("error"),
        }
    detail = str(data.get("detail") or data.get("error") or data.get("status") or "")
    if detail in ("expired_token", "invalid_device_code"):
        return {"ok": False, "status": "expired", "error": detail}
    if detail == "access_denied":
        return {"ok": False, "status": "denied", "error": detail}
    if status in (200, 400, 428) or detail in ("authorization_pending", "slow_down"):
        return {
            "ok": True,
            "status": "slow_down" if detail == "slow_down" else "authorization_pending",
        }
    return {"ok": False, "status": "error", "error": detail or f"HTTP {status}"}


def _stored_token() -> str:
    try:
        from adk.cli import load_saved_config

        return str(load_saved_config().get("api_key") or "")
    except Exception:  # noqa: BLE001 -- no config is the never-linked state
        return ""


def _is_bundle(b: Any) -> bool:
    return (
        isinstance(b, dict)
        and b.get("role") in ("owner", "user")
        and isinstance(b.get("identity"), dict)
    )


def refresh(token: Optional[str] = None, fetch=None) -> Dict[str, Any]:
    """Fetch the bundle with a credential; store it; never raises."""
    token = token or _stored_token()
    if not token:
        return {"ok": False, "error": "not linked (no stored credential)"}
    url = portal_url() + BUNDLE_PATH
    try:
        if fetch is not None:
            status, data = fetch(url, token)
        else:
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {token}", "User-Agent": UA}
            )
            try:
                with urllib.request.urlopen(req, timeout=25) as resp:
                    status, data = resp.status, json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as exc:
                status, data = exc.code, {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {
            "ok": False,
            "error": f"unreachable: {type(exc).__name__} (kept the last good bundle)",
        }
    if status in (401, 403):
        bundle_file().unlink(missing_ok=True)
        return {"ok": False, "error": "credential not accepted (stored bundle cleared)"}
    if status != 200 or not _is_bundle(data):
        return {"ok": False, "error": f"HTTP {status}: not a link bundle"}
    path = bundle_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({**data, "fetched_at": int(time.time())}, indent=2), encoding="utf-8"
    )
    try:
        os.chmod(path, 0o600)
    except OSError as exc:  # Windows ACLs ignore POSIX modes; say so, do not fail
        print(f"adk link: could not restrict {path.name}: {type(exc).__name__}", file=sys.stderr)
    return {"ok": True, "bundle": data}


def status() -> Dict[str, Any]:
    try:
        bundle = json.loads(bundle_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        bundle = None
    ok_bundle = bundle if _is_bundle(bundle) else None
    ident = (ok_bundle or {}).get("identity") or {}
    return {
        # LINKED means the server answered with a bundle for our credential. A
        # stored key alone is only SIGNED IN -- it may be a local-only token that
        # aitherium.com has never seen.
        "linked": ok_bundle is not None,
        "signed_in": bool(_stored_token()),
        "username": ident.get("username") or ident.get("email") or None,
        "role": (ok_bundle or {}).get("role"),
        "fetched_at": (ok_bundle or {}).get("fetched_at"),
        "portal": portal_url(),
    }


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    ap = argparse.ArgumentParser(prog="adk link", description="Link this machine to aitherium.com.")
    sub = ap.add_subparsers(dest="verb")
    sub.add_parser("start", help="get a device code and the approve link")
    p = sub.add_parser("poll", help="one poll; on approval, save the sign-in and fetch the bundle")
    p.add_argument("device_code")
    sub.add_parser("status", help="linked or not, and as what role")
    sub.add_parser("refresh", help="re-fetch the role-aware bundle")
    args = ap.parse_args(argv)
    verb = args.verb or "status"
    if verb == "start":
        res = start()
    elif verb == "poll":
        res = poll(args.device_code)
    elif verb == "refresh":
        res = refresh()
    else:
        res = status()
    if as_json:
        print(json.dumps(res, indent=2))
    elif verb == "start" and res.get("ok"):
        print(f"Approve this machine: {res['approve_url']}  (code {res['user_code']})")
        print(f"then: adk link poll {res['device_code']}")
    elif verb == "status":
        who = res.get("username") or "unknown"
        role = res.get("role") or "role not fetched"
        if res["linked"]:
            print(f"linked as {who} ({role})")
        elif res["signed_in"]:
            print("signed in, but not linked to aitherium.com -- run: adk link start")
        else:
            print("not linked -- run: adk link start")
    else:
        print(json.dumps(res, indent=2))
    ok = res.get("ok", True) if verb != "status" else True
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
