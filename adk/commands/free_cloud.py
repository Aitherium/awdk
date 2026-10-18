"""``adk free-cloud`` -- Free cloud (your own free-tier keys), via FreeLLMAPI.

    adk free-cloud status          # detected? enabled? which models?
    adk free-cloud up [--replace]  # start the pinned FreeLLMAPI image (idempotent)
    adk free-cloud down            # remove the container; your keys volume stays
    adk free-cloud enable [--url URL] [--models a,b]   # route non-local turns to it
    adk free-cloud disable
    adk free-cloud models [--all]  # what it can serve now (--all: whole catalog)

Settings go to ``~/.aither/mods.json`` (``freellmapi``), which awsettings syncs
across your machines. Keys never do: provider keys live in FreeLLMAPI's own
encrypted store, and the unified key in ``adk keys set freellmapi``.

Exit codes: 0 ok; 1 refused / not running; 2 could not judge.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from adk import freellmapi as fl


def register_parser(sub: Any) -> None:
    """Attach ``free-cloud`` to the top-level subparsers."""
    p = sub.add_parser(
        "free-cloud",
        help="Free cloud (your own free-tier keys): run/detect FreeLLMAPI as the non-local route",
    )
    fsub = p.add_subparsers(dest="free_cloud_command")
    st = fsub.add_parser("status", help="Is FreeLLMAPI running, enabled, and serving models?")
    st.add_argument("--json", action="store_true", help="Machine-readable output")
    upp = fsub.add_parser("up", help="Start the pinned FreeLLMAPI image (idempotent)")
    upp.add_argument("--port", type=int, default=3001, help="Host port (default 3001)")
    upp.add_argument("--replace", action="store_true",
                     help="Recreate the container on the pinned image (keys volume kept)")
    fsub.add_parser("down", help="Remove the FreeLLMAPI container (keys volume kept)")
    en = fsub.add_parser("enable", help="Route non-local turns to FreeLLMAPI")
    en.add_argument("--url", default=None, help="FreeLLMAPI URL (default: auto-detect)")
    en.add_argument("--models", default=None,
                    help="Comma-separated preferred models (default: its `auto` router)")
    fsub.add_parser("disable", help="Stop routing to FreeLLMAPI")
    mo = fsub.add_parser("models", help="Models FreeLLMAPI can serve now")
    mo.add_argument("--all", action="store_true", help="The whole catalog, not just ready ones")


def _caveat(out: Callable[[str], None]) -> None:
    out(f"  {fl.LABEL}: personal experimentation; your providers' terms apply.")
    out(f"  Terms review: {fl.TOS_URL}")


def cmd_free_cloud(args: Any, out: Callable[[str], None] = print) -> int:
    """Dispatch ``adk free-cloud <verb>``."""
    verb = getattr(args, "free_cloud_command", None) or "status"
    if verb == "up":
        rc, msg = fl.up(port=int(getattr(args, "port", 3001) or 3001),
                        replace=bool(getattr(args, "replace", False)))
        out(("  [+] " if rc == 0 else "  [!] ") + msg)
        if rc == 0:
            out(f"  Next: open http://127.0.0.1:{getattr(args, 'port', 3001)} , add YOUR "
                "provider keys on the Keys page, copy the unified key, then")
            out("        adk keys set freellmapi && adk free-cloud enable")
            _caveat(out)
        return rc
    if verb == "down":
        rc, msg = fl.down()
        out(("  [+] " if rc == 0 else "  [!] ") + msg)
        return rc
    if verb == "enable":
        changes: dict = {"enabled": True}
        if getattr(args, "url", None):
            changes["url"] = args.url.strip()
        if getattr(args, "models", None):
            changes["preferred_models"] = fl._as_models(args.models)
        try:
            path = fl.save_settings(**changes)
        except ValueError as e:
            out(f"  [!] {e}")
            return 1
        out(f"  [+] {fl.LABEL} enabled ({path})")
        out("      Order: local model first, FreeLLMAPI next, paid/BYO API last.")
        if not fl.resolve_api_key():
            out("  [!] No unified key yet: adk keys set freellmapi")
        _caveat(out)
        return 0
    if verb == "disable":
        try:
            path = fl.save_settings(enabled=False)
        except ValueError as e:
            out(f"  [!] {e}")
            return 1
        out(f"  [+] {fl.LABEL} disabled ({path})")
        return 0
    if verb == "models":
        det = fl.detect_sync(available_only=not getattr(args, "all", False))
        if not det.found:
            out(f"  [!] No FreeLLMAPI found ({det.detail}). Start one: adk free-cloud up")
            return 1
        if not det.authorized:
            out(f"  [!] FreeLLMAPI at {det.url}: {det.detail}. Set it: adk keys set freellmapi")
            return 1
        for m in det.models:
            out(m)
        return 0
    if verb == "status":
        settings = fl.load_settings()
        det = fl.detect_sync(settings=settings)
        payload = {
            "label": fl.LABEL,
            "enabled": settings.enabled,
            "configured_url": settings.url,
            "preferred_models": settings.preferred_models,
            "found": det.found,
            "url": det.url if det.found else "",
            "source": det.source,
            "authorized": det.authorized,
            "models": len(det.models),
            "route_model": fl.pick_model(det.models, settings.preferred_models)
            if det.authorized else "",
            "unified_key_set": bool(fl.resolve_api_key()),
            "image": f"{fl.IMAGE}:{fl.IMAGE_VERSION}",
            "detail": det.detail,
            "tos": fl.TOS_URL,
        }
        if getattr(args, "json", False):
            out(json.dumps(payload, indent=2))
            return 0 if det.found else 1
        out(f"{fl.LABEL}")
        out(f"  enabled:     {settings.enabled}")
        if det.found:
            out(f"  detected:    {det.url} (via {det.source})")
            out(f"  unified key: {'set' if payload['unified_key_set'] else 'NOT set'}"
                f"{'' if det.authorized else ' -- ' + det.detail}")
            if det.authorized:
                ready = fl.detect_sync(settings=settings, available_only=True)
                n_ready = len(fl.callable_models(ready.models))
                payload["ready_models"] = n_ready
                out(f"  models:      {len(det.models)} in catalog, {n_ready} ready; "
                    f"routes to {payload['route_model']}")
                if not n_ready:
                    out("  [!] no provider key connected yet -- add YOURS on its Keys page; "
                        "until then awdk will not route here")
        else:
            out(f"  detected:    no ({det.detail})")
            out("  start one:   adk free-cloud up")
        _caveat(out)
        return 0 if det.found else 1
    out(f"  unknown verb: {verb}")
    return 2
