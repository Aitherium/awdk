"""Can a LOCAL model profile answer right now -- asked before a session is spawned.

A ``transport: bridge`` profile names a model the fleet serves through the model
spine (MicroScheduler, ``https://127.0.0.1:8150``). Which local models are served is
a property of the active fleet POSTURE, and the spine already says so per model
(``aither.available`` / ``parked`` / ``note`` on ``GET /v1/models``). Nothing asked it
at spawn: measured 2026-11-12, ``awsh_spawn(model_profile="aither-orchestrator")``
started a session whose first turn died nine seconds later with Claude Code's
"There's an issue with the selected model", because that model is "served only in
posture(s) ['full-mesh', 'pool-lean']" and the active posture was a pool one.

This module answers at spawn instead, naming the posture note and the local models
that ARE served. It never picks a different model for the caller (a silent swap to a
smaller brain is the fallback this repo refuses) and it never changes the posture.

CANNOT JUDGE IS NOT A REFUSAL. An unreachable spine, a listing with no row for the
model, or a row with no availability verdict all answer "" -- the session starts as
it always did. Only an explicit ``available: false`` refuses.

Stdlib only; the fetch is injectable so tests never touch a live spine.
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.request
from typing import Any, Callable, Optional

SPINE_URL_ENV = "AITHER_MICROSCHEDULER_URL"
DEFAULT_SPINE_URL = "https://127.0.0.1:8150"
_CACHE_TTL_S = 20.0
_cache: dict[str, Any] = {"at": 0.0, "rows": None}
_cache_lock = threading.Lock()

Fetch = Callable[[str, float], Any]


def spine_url(env: Optional[dict] = None) -> str:
    env = os.environ if env is None else env
    return (env.get(SPINE_URL_ENV) or DEFAULT_SPINE_URL).rstrip("/")


def _http_fetch(url: str, timeout: float) -> Any:
    """GET ``url`` as JSON. The spine is loopback https with the fleet's own CA, which
    a host process may not hold; this is a read of a public listing on 127.0.0.1, so
    the certificate is not verified for a loopback host (and is for any other)."""
    ctx = None
    if url.startswith("https://127.0.0.1") or url.startswith("https://localhost"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    # ProxyHandler({}): never consult the Windows proxy registry for a loopback read.
    handlers: list = [urllib.request.ProxyHandler({})]
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def spine_models(fetch: Optional[Fetch] = None, timeout: float = 4.0,
                 use_cache: bool = True) -> Optional[list]:
    """The spine's model rows, or None when it cannot be read (cannot judge)."""
    now = time.time()
    if use_cache and fetch is None:
        with _cache_lock:
            if _cache["rows"] is not None and now - _cache["at"] < _CACHE_TTL_S:
                return _cache["rows"]
    try:
        doc = (fetch or _http_fetch)(spine_url() + "/v1/models", timeout)
    except Exception:  # noqa: BLE001 - every failure is the same verdict: cannot judge
        return None
    rows = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return None
    rows = [r for r in rows if isinstance(r, dict)]
    if use_cache and fetch is None:
        with _cache_lock:
            _cache.update(at=now, rows=rows)
    return rows


def upstream_model(profile_model: str, aliases: Optional[dict] = None) -> str:
    """The spine id a bridge profile's model resolves to.

    The bridge maps its own names (``aither-fast: microscheduler/gemma4-12b``); a name
    with no alias is asked for as itself.
    """
    target = str((aliases or {}).get(profile_model) or profile_model or "")
    return target.split("/", 1)[1] if "/" in target else target


def _is_local_chat(row: dict) -> bool:
    owner = str(row.get("owned_by") or "")
    return not owner.endswith("_api") and "embed" not in str(row.get("id") or "").lower()


def served_now(rows: list) -> list:
    """Local chat models the spine answers for now, one name per engine."""
    seen, out = set(), []
    for row in rows:
        verdict = row.get("aither") if isinstance(row.get("aither"), dict) else {}
        if not _is_local_chat(row) or verdict.get("available") is not True:
            continue
        engine = str(verdict.get("upstream_model") or row.get("id"))
        if engine in seen:
            continue
        seen.add(engine)
        out.append(str(row.get("id")))
    return out


def model_verdict(model_id: str, rows: list) -> dict:
    """``{available: True|False|None, note}`` for one spine id (None = no verdict)."""
    for row in rows:
        if str(row.get("id")) == model_id:
            verdict = row.get("aither") if isinstance(row.get("aither"), dict) else {}
            return {"available": verdict.get("available"),
                    "note": str(verdict.get("note") or "")}
    return {"available": None, "note": ""}


def profile_refusal(name: str, profile: dict, rows: Optional[list],
                    aliases: Optional[dict] = None,
                    profiles: Optional[dict] = None) -> str:
    """Why profile ``name`` cannot answer now, or "" when it can or cannot be judged."""
    if not isinstance(profile, dict) or profile.get("transport") != "bridge" or not rows:
        return ""
    model = upstream_model(str(profile.get("model") or ""), aliases)
    verdict = model_verdict(model, rows)
    if verdict["available"] is not False:
        return ""
    served = served_now(rows)
    usable = []
    for other, spec in (profiles or {}).items():
        if other == name or not isinstance(spec, dict) or spec.get("transport") != "bridge":
            continue
        target = upstream_model(str(spec.get("model") or ""), aliases)
        if model_verdict(target, rows)["available"] is True:
            usable.append(f"{other} ({target}, {int(spec.get('context_window') or 0)} ctx)")
    return (
        f"model profile {name!r} cannot answer now: local model {model!r} is not served"
        + (f" -- {verdict['note']}" if verdict["note"] else "")
        + ". Local models served now: " + (", ".join(served) or "none")
        + ". Local profiles that resolve to one: " + (", ".join(usable) or "none")
        + ". The posture was not changed and no other model was substituted."
    )


def bridge_aliases(tool: Any) -> dict:
    """``model_aliases`` from the bridge config beside the profile tool ({} if unreadable)."""
    try:
        import yaml

        with open(tool.BRIDGE_CONFIG_PATH, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        aliases = doc.get("model_aliases") or {}
        return aliases if isinstance(aliases, dict) else {}
    except Exception:  # noqa: BLE001 - no alias table: names are asked for as themselves
        return {}


def spawn_refusal(profile_name: str, fetch: Optional[Fetch] = None) -> str:
    """The one call the daemon makes at ``POST /sessions``: "" = start the session."""
    name = (profile_name or "").strip()
    if not name:
        return ""
    try:
        from adk.harnesses.models import load_profile_tool

        tool = load_profile_tool()
        profiles = tool.load_profiles()
    except BaseException:  # noqa: BLE001 - incl. the tool's SystemExit: not ours to report
        return ""
    profile = profiles.get(name)
    if not isinstance(profile, dict) or profile.get("transport") != "bridge":
        return ""
    return profile_refusal(name, profile, spine_models(fetch=fetch),
                           bridge_aliases(tool), profiles)


def annotate_profiles(rows: list, fetch: Optional[Fetch] = None) -> list:
    """Add ``available`` (+ ``unavailable_reason``) to each bridge row of ``/profiles``."""
    if not any(r.get("transport") == "bridge" for r in rows):
        return rows
    models = spine_models(fetch=fetch)
    if not models:
        return rows
    try:
        from adk.harnesses.models import load_profile_tool

        aliases = bridge_aliases(load_profile_tool())
    except BaseException:  # noqa: BLE001
        aliases = {}
    for row in rows:
        if row.get("transport") != "bridge":
            continue
        verdict = model_verdict(upstream_model(str(row.get("model") or ""), aliases), models)
        if verdict["available"] is None:
            continue
        row["available"] = bool(verdict["available"])
        if not verdict["available"]:
            row["unavailable_reason"] = verdict["note"] or "not served now"
    return rows
