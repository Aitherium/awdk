"""FastAPI server wrapping an AitherAgent — OpenAI-compatible + Genesis-compatible.

Supports two modes:
- Single agent: `aither-serve --identity aither`
- Fleet mode:   `aither-serve --fleet fleet.yaml` or `aither-serve --agents aither,lyra,demiurge`
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import re
import socket
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from urllib.parse import quote
from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)

from adk import __version__
from adk._tls import tls_verify
from adk.agent import AitherAgent, AgentResponse
from adk.config import Config
from adk.identity import list_identities, load_identity
from adk.llm import LLMRouter, Message
from adk.metrics import get_metrics
from adk.trace import TraceMiddleware, get_trace_id, new_trace

# The daemon fingerprints its OWN package at launch so a probe can tell whether the code
# on disk is the code that is running. A long-lived process that imported before an edit
# keeps serving old code while every file-level read shows the fix; the watchdog's
# capability check compares this against a fresh compute and replaces a stale daemon.
try:
    from adk.code_fingerprint import STARTUP as _CODE_FINGERPRINT
except Exception as _fp_exc:  # noqa: BLE001 — fingerprinting must never stop the daemon
    _CODE_FINGERPRINT = f"unavailable:{type(_fp_exc).__name__}"
_STARTED_AT = time.time()

logger = logging.getLogger("adk.server")

_WEBUI_CACHE: str | None = None

# Secret names are used to build vault paths, so constrain them rather than
# trusting whatever the browser sends.
_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# Resolved base URL per fleet service, so the probe cost is paid once.
_OPERATOR_BASE_CACHE: dict[str, str] = {}


def _operator_bases(service: str, port: int) -> list[str]:
    """Candidate base URLs for a fleet service, in preference order.

    The operator console runs in BOTH contexts and they need different hosts:
      - inside the fleet network, `aitheros-<service>` resolves
      - on the operator's own machine (the common case — adk is a pip package,
        not a fleet container) it does NOT, and every call dies with
        `getaddrinfo failed`
    Measured 2026-07-31: container-DNS-only endpoints returned ConnectError from
    the host for chronicle/pulse/flux, while `https://127.0.0.1:<port>` returned
    200 for all three. Hardcoding either host alone makes the panel permanently
    inert in the other context — and an always-502 panel is the failure this
    whole surface was supposed to avoid.

    Loopback works because these containers publish to 127.0.0.1. Note the scheme
    is https for both: plain http on these ports fails, which has previously been
    misread as "service unreachable".

    ORDER IS CORRECTNESS, NOT PREFERENCE. Inside a container `127.0.0.1:<port>` is
    that container's OWN port — a different service, or nothing — so loopback must
    not be tried first there. On the host the container name cannot resolve at all,
    and this fleet has a documented flaky resolver, so leading with it makes the
    probe slow AND nondeterministic (measured: chronicle and flux answered 200
    directly on loopback while the endpoint reported them unreachable).
    So decide by context rather than trying to be clever about failures.
    """
    in_container = os.path.exists("/.dockerenv")
    container_url = f"https://aitheros-{service}:{port}"
    loopback_url = f"https://127.0.0.1:{port}"
    return [container_url, loopback_url] if in_container else [loopback_url, container_url]


async def _resolve_operator_base(client, service: str, port: int):
    """First candidate whose /health answers. None when the service is truly down.

    Returning None (rather than a guess) is deliberate: the caller must be able to
    say "unreachable" instead of rendering an empty panel that reads as "no data".
    """
    cached = _OPERATOR_BASE_CACHE.get(service)
    if cached:
        return cached
    for base in _operator_bases(service, port):
        # Two attempts per candidate. This fleet has a MEASURED chronic fault
        # where the resolver drops a large fraction of queries, and these health
        # endpoints are slow under load — so a single attempt made this probe
        # nondeterministic: consecutive runs reported chronicle/watch/flux as
        # alternately fine and unreachable while all four answered 200 when
        # probed directly. One retry converts most of that flapping into a
        # correct answer; a panel that lies half the time is worse than a slow one.
        for attempt in (1, 2):
            try:
                # 8s, not 4s: a tight timeout turns "slow" into "unreachable",
                # which renders as a dead panel for a service that is fine.
                resp = await client.get(f"{base}/health", timeout=8.0)
            except Exception as exc:  # noqa: BLE001 — any transport failure
                logger.debug(
                    "operator base %s attempt %d: %s", base, attempt, type(exc).__name__
                )
                continue
            if resp.status_code < 500:
                _OPERATOR_BASE_CACHE[service] = base
                return base
            logger.debug("operator base %s attempt %d: HTTP %s", base, attempt, resp.status_code)
    return None


def _load_webui() -> str | None:
    """Load the packaged admin-console SPA (adk/webui/index.html).

    Shipped as package data in the wheel; read once and cached. Returns None if
    the asset is missing so callers can fall back to the minimal chat page.
    """
    global _WEBUI_CACHE
    if _WEBUI_CACHE is not None:
        return _WEBUI_CACHE or None
    try:
        from importlib.resources import files
        html = (files("adk") / "webui" / "index.html").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        # Dev fallback: read from the source tree next to this module.
        try:
            html = (os.path.join(os.path.dirname(__file__), "webui", "index.html"))
            with open(html, encoding="utf-8") as fh:
                html = fh.read()
        except OSError:
            _WEBUI_CACHE = ""
            return None
    _WEBUI_CACHE = html
    return html


# ─── Swappable agent UI packs ────────────────────────────────────────────────
# The page an agent serves at "/" is a swappable UI PACK, so you can drop in,
# test, and deploy different chat frontends (the full console, a minimal chat,
# AitherAeon, a company room, a custom brand) without touching the agent.
#
# Selection (first that is set wins): $AITHER_AGENT_UI env, else "console".
# Resolution order for a pack name:
#   1. drop-in dir: $AITHER_UI_PACKS_DIR/<name>/index.html  (default ~/.aither/ui-packs)
#   2. packaged built-in: adk/webui/packs/<name>/index.html
#   3. special built-ins: "console" -> the full admin SPA (adk/webui/index.html),
#      "minimal" -> the tiny streaming chat page (_CHAT_HTML)
# Anything unresolvable falls back to console -> minimal, so "/" is NEVER blank.

_BUILTIN_UI_PACKS = ("console", "minimal")


# ── Central-console admin proxy allowlist (module-level so it is unit-testable) ─
#
# The console proxies each mesh agent's OWN /admin API for owner-authed
# management: OBSERVE routes + reversible controls + CONFIGURATION (switch
# backend local<->cloud, set the model, set provider API keys, edit config).
# The ONE thing never proxied is arbitrary code execution — POST /admin/cli/exec
# and GET /admin/cli/commands are hard-denied (that, not config management, was
# the real RCE surface). Everything here is gated by the owner server bearer and
# forwarded to a single discovered agent (no fan-out).
#
# SECRET BOUNDARY: API-key VALUES flow browser -> proxy -> the agent's own
# admin-save (which persists to its vault). The console never RENDERS a stored
# key — reads return the agent's masked view (admin_api._mask_*). The owner
# supplies key values in the UI; the assistant never handles them.
_MESH_ADMIN_ALLOW = {
    "GET": {
        "config", "meta", "routes",
        "llm/status",
        "packs", "catalog/packs",
        "sessions",
        "logs/tail",
        "mcp/servers",
        "graph/stats",
    },
    "POST": {
        # Reversible controls
        "packs/enable", "packs/disable", "packs/reload",
        # Push+enable a bundled pack to a remote agent (no SSH). Only bundled
        # packs are accepted by the target; no arbitrary uploads.
        "packs/apply",
        # LLM configuration — switch backend (local<->cloud), set provider API
        # key, test the connection. These are owner config actions, not RCE.
        "llm/switch", "llm/keys", "llm/test",
        # MCP server management (add via prepare/confirm)
        "mcp/servers/prepare", "mcp/servers/confirm",
    },
    "PATCH": {
        "config",  # edit allowlisted config fields
    },
    "DELETE": {
        "sessions",       # terminate a session (prefix match: sessions/{id})
        "mcp/servers",    # remove an MCP server (prefix match: mcp/servers/{id})
    },
}
# Prefix allowances for parameterized routes (path starts with these).
_MESH_ADMIN_GET_PREFIXES = ("packs/", "sessions/", "mcp/servers/")
# Routes that are NEVER proxied regardless of method — arbitrary code execution.
_MESH_ADMIN_DENY = ("cli/exec", "cli/commands", "cli")


def _mesh_admin_allowed(method: str, sub: str) -> bool:
    """True only for explicitly-allowed (method, admin-subpath) pairs. Fail-closed:
    anything not named here is refused, and the cli/* exec surface is hard-denied
    regardless of method. Prefix rules cover /{id}-style routes."""
    sub = (sub or "").strip("/")
    if not sub or ".." in sub:
        return False
    # Hard deny: arbitrary code execution is never reachable through the console.
    for bad in _MESH_ADMIN_DENY:
        if sub == bad or sub.startswith(bad + "/"):
            return False
    m = method.upper()
    if sub in _MESH_ADMIN_ALLOW.get(m, set()):
        return True
    if m == "GET":
        for pfx in _MESH_ADMIN_GET_PREFIXES:
            if sub.startswith(pfx) and sub != pfx:
                return True
    if m == "PATCH" and (sub.startswith("packs/") and sub.endswith("/settings")):
        return True  # per-pack settings edit
    if m == "DELETE" and sub.startswith("sessions/") and sub != "sessions/":
        return True
    if m == "DELETE" and sub.startswith("mcp/servers/") and sub != "mcp/servers/":
        return True
    return False


def _ui_packs_dir() -> str:
    """Drop-in directory for custom UI packs (one folder per pack)."""
    explicit = os.getenv("AITHER_UI_PACKS_DIR", "").strip()
    if explicit:
        return explicit
    home = os.environ.get("AITHER_HOME") or os.environ.get("HOME") \
        or os.environ.get("USERPROFILE") or "."
    return os.path.join(home, ".aither", "ui-packs")


def resolve_ui_pack_name() -> str:
    """The selected UI pack name: $AITHER_AGENT_UI, else the persisted
    ``agent_ui`` from ~/.aither/config.json (set by `adk ui set`), else 'llamacpp'."""
    env = os.getenv("AITHER_AGENT_UI", "").strip()
    if env:
        return env
    try:
        from adk.config import load_saved_config
        saved = load_saved_config().get("agent_ui")
        if saved:
            return str(saved).strip()
    except Exception:
        pass
    return "llamacpp"


def list_ui_packs() -> dict[str, str]:
    """Map of available pack name -> source ('builtin' | 'packaged' | drop-in path)."""
    packs: dict[str, str] = {name: "builtin" for name in _BUILTIN_UI_PACKS}
    # packaged built-ins under adk/webui/packs/*
    try:
        pkg = os.path.join(os.path.dirname(__file__), "webui", "packs")
        if os.path.isdir(pkg):
            for name in sorted(os.listdir(pkg)):
                if os.path.isfile(os.path.join(pkg, name, "index.html")):
                    packs.setdefault(name, "packaged")
    except OSError:
        pass
    # drop-in packs (override packaged/builtin of the same name)
    try:
        dropin = _ui_packs_dir()
        if os.path.isdir(dropin):
            for name in sorted(os.listdir(dropin)):
                idx = os.path.join(dropin, name, "index.html")
                if os.path.isfile(idx):
                    packs[name] = idx
    except OSError:
        pass
    return packs


def load_ui_pack(name: str | None = None) -> str | None:
    """Load the selected UI pack's HTML, with a fail-soft fallback chain so "/"
    is never blank. Not cached (a dev swapping packs sees the change on reload)."""
    name = (name or resolve_ui_pack_name()).strip() or "console"
    # 1. drop-in dir wins
    try:
        idx = os.path.join(_ui_packs_dir(), name, "index.html")
        if os.path.isfile(idx):
            with open(idx, encoding="utf-8") as fh:
                return fh.read()
    except OSError:
        pass
    # 2. packaged built-in under adk/webui/packs/<name>/
    try:
        idx = os.path.join(os.path.dirname(__file__), "webui", "packs", name, "index.html")
        if os.path.isfile(idx):
            with open(idx, encoding="utf-8") as fh:
                return fh.read()
    except OSError:
        pass
    # 3. special built-ins
    if name == "minimal":
        return _CHAT_HTML
    if name == "console":
        return _load_webui() or _CHAT_HTML
    # 4. unknown pack -> console -> minimal (never blank)
    logger.warning("UI pack %r not found; falling back to console", name)
    return _load_webui() or _CHAT_HTML


_PACK_SDK_CACHE: str | None = None


def _load_pack_sdk() -> str | None:
    """Load the pack-UI bridge SDK (adk/webui/pack_sdk.js), packaged like the console."""
    global _PACK_SDK_CACHE
    if _PACK_SDK_CACHE is not None:
        return _PACK_SDK_CACHE or None
    try:
        from importlib.resources import files
        js = (files("adk") / "webui" / "pack_sdk.js").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        try:
            path = os.path.join(os.path.dirname(__file__), "webui", "pack_sdk.js")
            with open(path, encoding="utf-8") as fh:
                js = fh.read()
        except OSError:
            _PACK_SDK_CACHE = ""
            return None
    _PACK_SDK_CACHE = js
    return js


# A tiny, self-contained streaming chat page the agent serves at "/" so a person
# who ran `adk up` has somewhere to talk to it with live feedback (a "thinking…"
# indicator + token-by-token streaming) instead of a 30-second blank wait. It
# calls the agent's own gated /chat/stream; the callback bearer is read from the
# URL fragment (#k=…), which the browser never sends to the server or logs, so
# the inference endpoint stays authenticated (not an open, abusable proxy).
_CHAT_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aither Agent</title>
<style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
 background:#0b0d10;color:#e6e8eb;display:flex;flex-direction:column;height:100vh}
@media(prefers-color-scheme:light){body{background:#f6f7f9;color:#12141a}}
header{padding:12px 16px;border-bottom:1px solid #2a2f37;font-weight:600;display:flex;
 gap:8px;align-items:center}
@media(prefers-color-scheme:light){header{border-color:#e2e5ea}}
.dot{width:8px;height:8px;border-radius:50%;background:#3ba55d}
#log{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:760px;padding:10px 13px;border-radius:12px;white-space:pre-wrap;word-wrap:break-word}
.user{align-self:flex-end;background:#2563eb;color:#fff;border-bottom-right-radius:3px}
.assistant{align-self:flex-start;background:#1b2027;border-bottom-left-radius:3px}
@media(prefers-color-scheme:light){.assistant{background:#eceef2}}
.thinking{opacity:.7;font-style:italic}
form{display:flex;gap:8px;padding:12px 16px;border-top:1px solid #2a2f37}
@media(prefers-color-scheme:light){form{border-color:#e2e5ea}}
#in{flex:1;padding:11px 13px;border-radius:10px;border:1px solid #2a2f37;background:#12151a;
 color:inherit;font:inherit;resize:none}
@media(prefers-color-scheme:light){#in{background:#fff;border-color:#cfd4dc}}
button{padding:0 18px;border:0;border-radius:10px;background:#2563eb;color:#fff;font:inherit;
 font-weight:600;cursor:pointer}button:disabled{opacity:.5;cursor:default}
</style></head><body>
<header><span class="dot"></span><span id="name">Aither Agent</span></header>
<div id="log"></div>
<form id="f"><textarea id="in" rows="1" placeholder="Message your agent…" autofocus></textarea>
<button id="send">Send</button></form>
<script>
var log=document.getElementById('log'),input=document.getElementById('in'),
 form=document.getElementById('f'),btn=document.getElementById('send');
var key=(location.hash.match(/[#&]k=([^&]+)/)||[])[1]||'';
var sid=localStorage.getItem('adk_sid')||('web-'+Math.random().toString(36).slice(2));
localStorage.setItem('adk_sid',sid);
function hdrs(h){h=h||{};if(key)h['Authorization']='Bearer '+decodeURIComponent(key);return h;}
function bubble(role,text){var d=document.createElement('div');d.className='msg '+role;
 d.textContent=text;log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}
fetch('/health').then(function(r){return r.json()}).then(function(j){
 if(j&&j.agent)document.getElementById('name').textContent=j.agent;}).catch(function(){});
function thinking(el){var i=0;el.classList.add('thinking');
 var t=setInterval(function(){i=(i+1)%4;el.textContent='thinking'+Array(i+1).join('.');},400);
 return t;}
async function send(text){
 bubble('user',text);
 var el=bubble('assistant','');var tk=thinking(el);var got=false,acc='';
 btn.disabled=true;
 try{
  var res=await fetch('/chat/stream',{method:'POST',headers:hdrs({'Content-Type':'application/json'}),
   body:JSON.stringify({message:text,session_id:sid})});
  if(!res.ok){clearInterval(tk);el.classList.remove('thinking');
   el.textContent='⚠ '+res.status+(res.status===401?' — reopen this page from `adk up`':'');btn.disabled=false;return;}
  var reader=res.body.getReader(),dec=new TextDecoder(),buf='';
  while(true){var c=await reader.read();if(c.done)break;buf+=dec.decode(c.value,{stream:true});
   var idx;while((idx=buf.indexOf('\n\n'))>=0){var frame=buf.slice(0,idx);buf=buf.slice(idx+2);
    var line=frame.split('\n').filter(function(l){return l.indexOf('data:')===0})[0];if(!line)continue;
    var d;try{d=JSON.parse(line.slice(5).trim())}catch(e){continue}
    if(d.type==='token'){if(!got){got=true;clearInterval(tk);el.classList.remove('thinking');el.textContent=''}
     acc+=(d.t||'');el.textContent=acc;log.scrollTop=log.scrollHeight}
    else if(d.type==='answer'){if(!got){clearInterval(tk);el.classList.remove('thinking');got=true}
     acc=d.answer||acc;el.textContent=acc;log.scrollTop=log.scrollHeight}
    else if(d.type==='error'){clearInterval(tk);el.classList.remove('thinking');el.textContent='⚠ '+(d.error||'error')}}}
 }catch(e){clearInterval(tk);el.classList.remove('thinking');el.textContent='⚠ '+e.message}
 clearInterval(tk);el.classList.remove('thinking');if(!acc&&!el.textContent)el.textContent='(no response)';
 btn.disabled=false;input.focus();
}
form.addEventListener('submit',function(e){e.preventDefault();var t=input.value.trim();
 if(!t)return;input.value='';send(t)});
input.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();
 form.requestSubmit()}});
</script></body></html>'''


def create_app(
    agent: AitherAgent | None = None,
    identity: str = "aither",
    config: Config | None = None,
    fleet_path: str | None = None,
    fleet_agents: list[str] | None = None,
) -> FastAPI:
    """Create a FastAPI app wrapping an AitherAgent or a fleet of agents.

    Returns a fully configured app with both OpenAI-compatible and Genesis-compatible endpoints.
    """
    config = config or Config.from_env()

    is_fleet = bool(fleet_path or fleet_agents)

    # ─── Lifespan (replaces deprecated @app.on_event("startup")) ───

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        """Run startup tasks, yield for request handling, then cleanup."""
        # Configure structured logging
        from adk.chronicle import configure_logging
        configure_logging(
            level=os.getenv("AITHER_LOG_LEVEL", "INFO"),
            json_output=config.json_logging,
        )

        # Sovereign/offline mode: skip ALL network registration (gateway, secrets,
        # AitherNet, relays, Elysium, mesh, fleet heartbeat, telemetry flushes).
        # The server still mounts every LOCAL route (incl. FormBridge) + the local
        # MCP/A2A servers — it just never phones home. This is the PHI/sovereign
        # posture AND the FormBridge demo runtime, so there is ONE server, not a
        # separate demo shim. Enable with AITHER_OFFLINE=1.
        offline = os.getenv("AITHER_OFFLINE", "").lower() in ("1", "true", "yes")
        if offline:
            logger.info("AITHER_OFFLINE=1 — sovereign mode: local only, no network registration")

        # Sovereign mode still gets TOOLS. AITHER_OFFLINE means "do not phone home", not
        # "run with 7 built-in tools" — a LOCAL MCP gateway (127.0.0.1:8182) is not the
        # cloud, so connecting to it is fully consistent with the sovereign posture and is
        # what closes the parity gap against genesis's ~1500 platform tools.
        if offline:
            # BIND FIRST. This used to `await` attach attempt #1 right here, ahead of
            # uvicorn serving anything. Measured 2026-09-19 with the gateway mid-rebuild:
            # that attempt took ~30 s to fail, the awnode/LLM probes below added ~10 s more,
            # and the :9001 watchdog task ticks every 60 s and kills any `adk.server` that
            # is not LISTENING -- so every daemon was executed mid-boot: 15 relaunches in
            # 30 min, none ever logging "Application startup complete", and every awsh
            # turn fell through to the cloud sign-in dead end. The supervisor below
            # already owns re-attach; it now owns attempt #1 as well. `mcp_retry_now` is
            # pre-set so that attempt starts immediately in the background instead of
            # after the loop's first 5 s backoff. /health reports `tools.mode` honestly
            # (builtin-only until the attach lands), which is what the capability probe
            # and its 90 s grace were written for.
            _state["mcp_retry_now"] = asyncio.Event()
            _state["mcp_retry_now"].set()
            _state["mcp_supervisor_task"] = asyncio.create_task(_supervise_local_mcp())
        if not offline:
            await _connect_gateway_mcp()
            await _register_with_gateway()
            await _sync_secrets()
            await _join_aithernet()
            await _rich_enroll_identity()
            await _init_chat_relay()
            await _init_mail_relay()
            await _init_relay_client()
        await _init_mcp_server()
        await _init_a2a_server()
        await _connect_service_bridge()
        if not offline:
            await _flush_strata_queue()
            await _flush_chronicle_queue()
            await _start_watch_reporter()
            await _flush_pulse_queue()
        # Eagerly detect LLM backend so /health shows the right provider
        try:
            a = await get_agent()
            await a.llm.get_provider()
        except (ImportError, RuntimeError, OSError):
            pass

        # ── Settings sync: portal profile is the source of truth ──
        # Pull the user's profile settings and apply them over the local cache,
        # then keep the client on _state so admin-console mutations can push
        # updates back up. Fail-soft: never blocks boot.
        if not offline:
            try:
                from adk.sync.settings import build_client
                _settings_sync = build_client()
                if _settings_sync is not None:
                    _state["settings_sync"] = _settings_sync
                    try:
                        a = await get_agent()
                    except (ImportError, RuntimeError, OSError, ConnectionError):
                        a = None
                    result = await _settings_sync.pull_and_apply(a)
                    logger.info("settings sync: pulled portal profile — %s", result)
            except Exception as exc:  # noqa: BLE001 — sync is advisory
                logger.warning("settings sync init failed (using local config): %s", exc)

        if not offline:
            # ── Elysium auto-reconnect + mesh hosting ──
            await _reconnect_elysium()
            await _start_mesh_hosting()

            # ── Fleet endpoint registration + continuous heartbeat ──
            await _register_fleet_endpoint()
            _heartbeat_task = asyncio.create_task(_fleet_heartbeat_loop())
            _state["heartbeat_task"] = _heartbeat_task

            # ── Workflow -> expedition mirror tailer (host-side half) ──
            if os.environ.get("AITHER_WORKFLOW_MIRROR", "1") != "0":
                _state["workflow_mirror_task"] = asyncio.create_task(_workflow_mirror_loop())

        yield
        # ── Shutdown cleanup ──
        for _task_key in ("heartbeat_task", "workflow_mirror_task"):
            _bg_task = _state.get(_task_key)
            if _bg_task and not _bg_task.done():
                _bg_task.cancel()
                try:
                    await _bg_task
                except asyncio.CancelledError:
                    logger.debug("%s cancelled on shutdown", _task_key)
                except Exception as exc:  # noqa: BLE001 -- shutdown must finish
                    logger.debug("%s ended with %s on shutdown", _task_key, exc)
        await _deregister_fleet_endpoint()
        _elysium_relay = _state.get("elysium_relay")
        if _elysium_relay:
            await _elysium_relay.stop_heartbeat()
            await _elysium_relay.disconnect_relay_hub()
        bridge = _state.get("aither_bridge")
        if bridge:
            await bridge.stop()
        chat = _state.get("chat_relay")
        if chat:
            await chat.stop_irc_server()
        relay_client = _state.get("relay_client")
        if relay_client:
            try:
                await relay_client.disconnect()
            except (OSError, RuntimeError) as exc:
                logger.warning(f"relay_client disconnect on shutdown failed: {exc}")

    app = FastAPI(
        title=f"AitherADK — {'Fleet' if is_fleet else identity}",
        version=__version__,
        docs_url="/docs",
        lifespan=_lifespan,
    )

    _cors_origins = os.getenv("AITHER_CORS_ORIGINS", "").split(",")
    _cors_origins = [o.strip() for o in _cors_origins if o.strip()]
    # THE AITHERIUM SURFACES MUST BE HERE OR THE DAEMON IS UNDETECTABLE FROM THE WEB.
    #
    # aitherium.com's Living OS probes the visitor's own loopback for a running node
    # (AitherVeil components/os/use-local-node.ts) — that is the "the page notices the
    # software you just installed" moment. Measured 2026-07-31 on the owner's own box: the
    # daemon answered `GET http://127.0.0.1:9001/health` with 200 and
    # `{"status":"healthy","agent":"adk-daemon","version":"2.25.1"}`, and the page still
    # said "no node", because the default list below was localhost-dev only, so the browser
    # discarded the response for want of an Access-Control-Allow-Origin. Nothing logged on
    # either side. The Awconnect EXTENSION saw the same daemon fine (extensions bypass
    # CORS with host permissions) and displayed "node online" a few pixels away from the
    # page's "no node" — which reads as the page being broken rather than as a CORS policy.
    #
    # Still an explicit allowlist, never a wildcard: this daemon is on loopback with the
    # user's own tools attached, so the set of origins that may talk to it is a security
    # decision. allow_credentials stays OFF, so no ambient cookie rides along.
    # 🚨 THIS LIST MUST COVER EVERY ORIGIN THE ADAPTER RUNS ON, and until 2026-08-24
    # it covered four of twelve. `aither-bonsai-adapter.js` carries its own
    # ALLOWED_HOSTS -- the surfaces where the page may run at all -- and the two were
    # hand-maintained separately, so the page ran on origins this daemon refused.
    #
    # Measured that day on the owner's box: nine local services up, `adk up` answering
    # GET http://127.0.0.1:9001/v1/models with 200, and
    # https://wizzense.github.io/GobboNet/ reporting no node and offering a 545 MB
    # in-browser download instead. The page advertises the opposite in its own banner --
    # "ALREADY RUNNING YOUR OWN? PIP INSTALL AWDK THEN ADK UP WORKS TOO, WITH NO
    # SIGN-IN" -- so the instruction on screen was false for the person reading it.
    # Nothing logs it: the daemon answers, the browser silently discards the response
    # for want of an Access-Control-Allow-Origin, and curl from a shell reports 200
    # because curl does not enforce CORS. Preflight per origin, measured:
    #     aitherium.com          200
    #     wizzense.github.io     400   <- the page the user was actually on
    #     gobbonet.aitherium.com 400
    #
    # This is the SAME defect the comment above records being fixed once for
    # aitherium.com. It recurred because that fix was a LIST, not a rule, and the
    # second list lived in another repo tree.
    #
    # Still an explicit allowlist, never a wildcard, and allow_credentials stays OFF.
    # Verified after the fix: the three above answer 200 and evil.example.com still 400.
    _aitherium_origins = [
        "https://aitherium.com",
        "https://www.aitherium.com",
        "https://api.aitherium.com",
        "https://veil.aitherium.com",
        # GobboNet surfaces. The adapter's ALLOWED_HOSTS is the authority for this set.
        "https://desktop.aitherium.com",
        "https://spaces.aitherium.com",
        "https://gobbonet.aitherium.com",
        # TENANT SUBDOMAINS ARE NOT LISTED HERE, deliberately, twice over.
        #
        # They are matched by the rule at the middleware instead. Naming them
        # here shipped a CLIENT LIST in a package strangers pip install: no
        # secret scanner fires on a hostname, and `pip download awdk` turns it
        # into a roster of who bought what. That is not our secret to spend.
        #
        # It also fixes what the comment above complains about -- the defect
        # 'recurred because that fix was a LIST, not a rule'. A list needs an
        # edit and a release per tenant, so each new customer is undetectable
        # from the web until somebody remembers. A rule covers them the day
        # they exist.
        # Both GitHub Pages demos: our fork, and UPSTREAM's own site. Upstream is
        # included deliberately -- a GobboNet user who never heard of us should still
        # find a node they are already running. That is the local-first promise, and
        # it costs nothing: this is an origin allowlist on a loopback daemon, not a
        # grant of anything.
        "https://wizzense.github.io",
        "https://elodineofficial.github.io",
    ]
    # Tenant surfaces as a RULE. Bounded on purpose: https only, ONE label,
    # our apex and nothing else -- evil.example.com, aitherium.com.evil.test,
    # a.b.aitherium.com and http:// all fail it (verified in both directions).
    #
    # NOT applied when the operator supplied AITHER_CORS_ORIGINS: an explicit
    # list is a decision, and silently widening it back to every subdomain of
    # ours would override the person who made it.
    #
    # Expressible as a rule here specifically because allow_credentials stays
    # OFF -- it is never set, and three comments in this file say so -- so a
    # matching origin still carries no ambient cookie.
    _tenant_origin_rule = (
        None if _cors_origins
        else r"^https://[a-z0-9][a-z0-9-]*\.aitherium\.com$"
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_tenant_origin_rule,
        allow_origins=_cors_origins or [
            *_aitherium_origins,
            "http://localhost:3000",
            "http://localhost:8080",
        ],
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        # Athena: no wildcard — only the headers our clients actually send. A
        # permissive list widens what a whitelisted-origin page can do cross-site.
        allow_headers=["Authorization", "Content-Type", "Accept",
                       "X-Caller-Type", "X-Request-ID", "X-Tenant-ID", "X-Workspace-ID"],
        # PRIVATE NETWORK ACCESS -- without this the allowlist above is necessary and
        # NOT sufficient, and the daemon is undetectable from the web for a SECOND
        # reason that looks identical to the first.
        #
        # Chrome treats an https:// page fetching http://127.0.0.1 as a private-network
        # request and sends `Access-Control-Request-Private-Network: true` on the
        # preflight. Starlette KNOWS about that header and REFUSES by default: measured
        # 2026-08-22 against this daemon, a plain preflight returned 200 while the same
        # preflight plus that one header returned `400 Disallowed CORS private-network`.
        # The browser then discards the response and aitherium.com reports "no node"
        # while `curl http://127.0.0.1:9001/v1/models` answers 200 with a correct
        # Access-Control-Allow-Origin -- so every hand check from a shell says the node
        # is detectable and every real browser disagrees.
        #
        # That is the SAME failure the comment above documents (origin missing from the
        # allowlist), recurring one layer deeper after it was fixed, with the identical
        # symptom and no log line on either side. The extension still sees the daemon
        # fine, because extensions bypass both CORS and PNA with host permissions -- so
        # the side panel can read "node online" a few pixels from the page's "no node".
        #
        # Scoped by construction, not widened: PNA exists to stop a public site probing
        # your LAN, and this grant applies only to the explicit origin allowlist above.
        # allow_credentials stays OFF, so no ambient cookie rides along. A wildcard
        # origin here would hand every website on the internet a port scanner.
        allow_private_network=True,
    )

    # Trace ID middleware — generates/propagates X-Request-ID on every request
    app.add_middleware(TraceMiddleware)

    # State shared across endpoints
    _state: dict[str, Any] = {
        "agent": agent,
        "identity": identity,
        "config": config,
        "fleet": None,
        "is_fleet": is_fleet,
        "service_bridge": None,
        # Per-name agent cache (D-2170) — see get_agent() below. Without this,
        # every /chat/stream for a non-default name (e.g. "aither") rebuilt a
        # brand-new AitherAgent from scratch: pack discovery, all built-in +
        # tool-pack tools, MCP gateway attach, MicroScheduler connect, skill
        # loading. Measured live 2026-08-24: 5-90+ seconds of silent work on
        # EVERY message, not just the first.
        "agents_by_name": {},
    }

    # ─── Auth middleware (optional, enabled via AITHER_API_KEY or --api-key) ───

    _server_api_key = os.getenv("AITHER_SERVER_API_KEY", "")
    # "/" and "/chat" serve only the static chat page (no data); the page then
    # authenticates to the gated /chat/stream with the bearer from its URL fragment.
    # Similarly, "/aeon" serves the group-chat UI; "/aeon/stream" is bearer-gated.
    _skip_auth_paths = {"/", "/chat", "/aeon", "/ui", "/local", "/health", "/docs",
                        "/openapi.json", "/metrics", "/demo", "/redoc"}

    def _is_pack_ui_asset(path: str) -> bool:
        """Pack-UI static assets are unauthenticated like the console shell at "/".

        The console mounts pack UIs in sandboxed iframes with NO token — that is
        the security model: the bearer never enters pack code — so the iframe
        cannot send an Authorization header for its own HTML/JS/CSS. The asset
        route only serves files from an enabled pack's declared assets dir, and
        every privileged action still goes through the bearer-gated
        /admin/packs/*/tools/*/invoke bridge. Deliberately narrow match: the
        pack-UI prefix and the bridge SDK, nothing else under /packs/.
        """
        return path == "/packs/_sdk.js" or (path.startswith("/packs/") and "/ui/" in path)

    # Valid caller types for header validation (prevents spoofing)
    _valid_caller_types = {"PLATFORM", "PUBLIC", "DEMO", "TENANT", "ANONYMOUS"}

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        """Bearer token auth + caller-type header validation.

        Validates X-Caller-Type header to prevent header-spoofing attacks.
        External requests cannot claim PLATFORM caller type.
        """
        if request.url.path in _skip_auth_paths or _is_pack_ui_asset(request.url.path):
            return await call_next(request)

        # Validate X-Caller-Type if present (prevent spoofing)
        caller_type = request.headers.get("x-caller-type", "")
        if caller_type:
            if caller_type not in _valid_caller_types:
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Invalid X-Caller-Type: {caller_type}"},
                )
            # External requests cannot claim PLATFORM — that's internal-only
            if caller_type == "PLATFORM" and _server_api_key:
                auth_header = request.headers.get("authorization", "")
                platform_tok = auth_header[7:] if auth_header.startswith("Bearer ") else ""
                # hmac.compare_digest: constant-time, no early-exit timing leak over the tunnel
                if not platform_tok or not hmac.compare_digest(platform_tok, _server_api_key):
                    return JSONResponse(
                        status_code=403,
                        content={"error": "PLATFORM caller type requires valid API key"},
                    )

        if not _server_api_key:
            return await call_next(request)

        # Genuine local access is trusted — no token needed when you're on the box.
        # A request whose immediate peer is loopback AND which carries no proxy /
        # forwarding headers can only have originated on THIS machine. Cloudflare
        # (and any reverse proxy) hits loopback too, but always adds
        # X-Forwarded-For / Cf-Connecting-Ip / Forwarded, so the PUBLIC tunnel stays
        # bearer-gated. This is what makes "open my own agent" seamless in the mesh
        # without pasting a token into the URL.
        client_host = request.client.host if request.client else ""
        forwarded = (
            request.headers.get("x-forwarded-for")
            or request.headers.get("cf-connecting-ip")
            or request.headers.get("forwarded")
        )
        if client_host in ("127.0.0.1", "::1") and not forwarded:
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(status_code=401, content={"error": "Missing or invalid Authorization header"})
        token = auth_header[7:]
        # hmac.compare_digest: constant-time comparison — the bearer is reachable over
        # the public trycloudflare tunnel, so a byte-by-byte `!=` would leak the token
        # position-by-position to a timing attacker.
        if not hmac.compare_digest(token, _server_api_key):
            return JSONResponse(status_code=401, content={"error": "Invalid API key"})
        return await call_next(request)

    async def _init_fleet():
        """Initialize fleet mode (lazy, on first request)."""
        if _state["fleet"] is not None:
            return _state["fleet"]
        from adk.fleet import load_fleet
        fleet = load_fleet(
            path=fleet_path,
            agent_names=fleet_agents,
            config=config,
        )
        _state["fleet"] = fleet
        return fleet

    async def get_agent(name: str | None = None) -> AitherAgent:
        """Get agent by name. In fleet mode, routes to the right agent."""
        if is_fleet:
            fleet = await _init_fleet()
            if name and name in fleet.registry:
                return fleet.registry.get(name)
            # Default to orchestrator
            orch = fleet.get_orchestrator()
            if orch:
                return orch
            # Fallback to first agent
            if fleet.agents:
                return fleet.agents[0]

        if _state["agent"] is None:
            # Load agent spec with customization overrides
            from adk.pack_discovery import load_agent_spec
            from pathlib import Path

            identity = _state["identity"]
            agent_spec = {}

            # Try to load agent spec from installed pack
            pack_dir = Path.home() / ".aither" / "agents" / identity
            if pack_dir.exists():
                agent_yaml = pack_dir / "agent.yaml"
                if agent_yaml.exists():
                    agent_spec = load_agent_spec(agent_yaml) or {}

            # Build AitherAgent with optional system_prompt override
            kwargs = {
                "name": identity,
                "identity": identity,
                "config": _state["config"],
            }
            if agent_spec.get("system_prompt"):
                kwargs["system_prompt"] = agent_spec["system_prompt"]

            _state["agent"] = AitherAgent(**kwargs)
        agent = _state["agent"]

        # If a different agent is requested in single mode, reuse it if we
        # already built one (D-2170). Rebuilding on every call redid pack
        # discovery + tool registration + MCP/MicroScheduler reattach on
        # every single message — this cache is what makes the 2nd+ message
        # to the same named agent fast instead of paying that tax again.
        if name and name != agent.name:
            cache: dict[str, AitherAgent] = _state["agents_by_name"]
            cached = cache.get(name)
            if cached is not None:
                return cached

            # Load agent spec for the requested agent
            from adk.pack_discovery import load_agent_spec
            from pathlib import Path

            agent_spec = {}
            pack_dir = Path.home() / ".aither" / "agents" / name
            if pack_dir.exists():
                agent_yaml = pack_dir / "agent.yaml"
                if agent_yaml.exists():
                    agent_spec = load_agent_spec(agent_yaml) or {}

            kwargs = {
                "name": name,
                "identity": name,
                "config": _state["config"],
            }
            if agent_spec.get("system_prompt"):
                kwargs["system_prompt"] = agent_spec["system_prompt"]

            built = AitherAgent(**kwargs)
            cache[name] = built
            return built

        return agent

    # ─── Metrics (Prometheus) ───

    @app.get("/metrics")
    async def metrics_endpoint(request: Request):
        """Prometheus-compatible metrics export. Requires auth token or localhost."""
        # Allow localhost and container-internal access without auth
        client_host = request.client.host if request.client else ""
        is_local = client_host in ("127.0.0.1", "::1", "localhost", "")
        if not is_local:
            auth = request.headers.get("authorization", "")
            metrics_token = os.getenv("AITHER_METRICS_TOKEN", "")
            if metrics_token and auth != f"Bearer {metrics_token}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return PlainTextResponse(get_metrics().export(), media_type="text/plain; version=0.0.4")

    # ─── Health ───

    # -- Components: what is installed on THIS device, for hosts that render it ------
    # The Living Desktop's "On this device" lane, awdesk's tray, awsh packs and the
    # aitheros launcher all ask this one question; before 2026-09-06 each kept its own
    # list and none derived from the registry. Loopback peer + first-party Origin
    # only: this enumerates local services and their ports, which a stranger's page
    # must not be able to read. 127.0.0.1 answers; a tab on youtube.com does not.
    _components_origins = frozenset({
        "https://aitherium.com", "https://www.aitherium.com",
        "http://localhost:3000", "http://127.0.0.1:3000",
    })
    _components_origin_rule = re.compile(r"^https://[a-z0-9][a-z0-9-]*\.aitherium\.com$")
    _components_loopback = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

    def _components_guard(request: Request) -> None:
        peer = getattr(getattr(request, "client", None), "host", None)
        if peer not in _components_loopback:
            raise HTTPException(status_code=403, detail="components are loopback-only")
        origin = request.headers.get("origin", "")
        # No Origin = a native client on this machine (curl, awsh, awdesk); allowed.
        if origin and origin not in _components_origins \
                and not _components_origin_rule.match(origin):
            raise HTTPException(status_code=403, detail="origin not allowed for components")

    @app.get("/components")
    async def components(request: Request):
        """Every component manifest known here + its live state (see addon_manager)."""
        _components_guard(request)
        from adk.addon_manager import AddonManager
        mgr = AddonManager()
        return {
            "host": "awdk",
            "port": int(os.getenv("AITHER_PORT") or os.getenv("AITHER_DAEMON_PORT") or 0),
            "components": mgr.components_inventory(),
        }

    @app.get("/health")
    async def health():
        try:
            a = await get_agent()
            provider = a.llm.provider_name or "detecting..."
            agent_name = a.name
        except ConnectionError:
            provider = "none"
            agent_name = _state["identity"]

        result = {
            "status": "healthy",
            "agent": agent_name,
            "llm_backend": provider,
            "version": __version__,
            "gateway_connected": _state.get("gateway_connected", False),
            "gateway_mcp_connected": _state.get("gateway_mcp_connected", False),
            # Code currency: what is RUNNING, not what is on disk (see code_fingerprint).
            "code_fingerprint": _CODE_FINGERPRINT,
            "started_at": _STARTED_AT,
        }

        # Capability, not liveness. `status: healthy` is TRUE of a daemon serving
        # turns with 7 built-in tools — and useless, because the interesting failure is
        # that it lost the other 1,227 to a gateway flap and will never notice. A probe
        # that cannot distinguish those two states is the reason two verification runs
        # returned false "silently non-functional" verdicts. So say which one this is.
        # "Attached" means TOOLS ARE CALLABLE, not "a socket opened". A client that
        # connected but registered nothing is the inert-feature state, and reporting it as
        # `platform` is exactly the lie this block exists to prevent.
        _attached = (
            bool(_state.get("gateway_mcp_connected"))
            and _state.get("mcp_tools_registered", 0) > 0
            and _state.get("mcp_tools_catalogue", 0) > 0
        )
        result["tools"] = {
            "mode": "platform" if _attached else "builtin-only",
            "registered": _state.get("mcp_tools_registered", 0),
            "catalogue": _state.get("mcp_tools_catalogue", 0),
            "attach_attempts": _state.get("mcp_attach_attempts", 0),
            "last_error": _state.get("mcp_last_error", ""),
            "retrying": bool(
                _state.get("mcp_supervisor_task")
                and not _state.get("mcp_attach_permanent_failure")
                and not _attached
            ),
        }
        if not _attached and _state.get("mcp_attach_attempts", 0):
            result["degraded"] = ["mcp-gateway-detached: running with built-in tools only"]

        if is_fleet and _state["fleet"]:
            fleet = _state["fleet"]
            result["fleet"] = {
                "name": fleet.name,
                "agents": fleet.registry.agent_names,
                "orchestrator": fleet.orchestrator_name,
            }

        return result

    # ─── Autonomous-X session seeding (loopback → no manual step) ───
    @app.post("/x-session/import")
    async def x_session_import(request: Request):
        """Seed the autonomous X poster's session from a browser that holds it.

        The Awconnect extension POSTs THIS browser's logged-in x.com cookies
        here. Loopback is already trusted by the auth middleware (no token), and
        the daemon runs as the owner — so we forward the cookies to the fleet's
        verify-and-store endpoint with the owner's own credentials. No download,
        no `adk x-session import`, no manual step. Cookies never leave the box.
        """
        import httpx
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid JSON"})
        cookies = (body or {}).get("cookies") or []
        names = {c.get("name") for c in cookies if isinstance(c, dict)}
        if "auth_token" not in names or "ct0" not in names:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "missing auth_token/ct0 — log into x.com in this browser first"})

        api_key = ""
        try:
            from adk.cli import load_saved_config
            api_key = load_saved_config().get("api_key", "") or ""
        except Exception:
            try:
                import json as _json
                with open(os.path.expanduser("~/.aither/config.json"), encoding="utf-8") as fh:
                    api_key = (_json.load(fh) or {}).get("api_key", "") or ""
            except Exception:
                api_key = ""

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {"storage_state": {"cookies": cookies, "origins": []}}
        # Genesis LB (plain HTTP) first, then the worker (TLS); the route lives on
        # both. A 404 means that host doesn't serve it — fall through, don't report.
        # The old bases are dead: :8001 was the retired nginx LB (2026-08-13)
        # and :8159 the worker, which has no host port. The awnode gateway is
        # the mcp gateway (:8182, the app that bakes mcp_gateway.py, host-reachable
        # with the loopback-trusted daemon client; route added 2026-08-26).
        # Plain HTTP by design: the quadlet serves 8182 http-only (TLS at the
        # tunnel edge) — its own unit comment says internal callers must use http.
        bases = [("http://127.0.0.1:8182", False)]
        last = None
        # The client is created PER BASE with that base's verify flag: the
        # loopback base (https://127.0.0.1:8182) cannot match the gateway
        # cert's SAN (aitheros-mcpgateway), so CA-verification would fail on
        # the hostname even though the cert is trusted. The daemon is
        # loopback-trusted by design (see the module docstring), so verify is
        # off for the loopback hop only.
        for base, verify in bases:
            try:
                async with httpx.AsyncClient(timeout=120.0, verify=verify) as client:
                    r = await client.post(
                        f"{base}/social/x/import-session",
                        headers=headers, json=payload)
                if r.status_code == 404:
                    last = (404, (r.text or "")[:200])
                    continue
                data = r.json()
                return JSONResponse(status_code=(200 if data.get("ok") else 502), content=data)
            except Exception as e:  # noqa: BLE001 — try the next base
                last = (0, str(e)[:200])
        return JSONResponse(status_code=502, content={
            "ok": False, "error": f"no fleet endpoint reachable: {last}"})

    # ─── Onboarding status endpoints (Awconnect extension) ───

    # ── Browser handoff: let aitherium.com sign in as the user on THIS box ──
    #
    # The page that detected this daemon (Living OS, under the visitor's
    # local-node opt-in) may ask WHO is signed in here and, on a click, for a
    # one-time ticket it can redeem for a browser session. The daemon's own
    # bearer never leaves this process: Identity `/auth/handoff/mint` is called
    # from here, and only the 60-second ticket goes back to the page.
    #
    # Three gates, each independent of the CORS middleware above:
    #   * TCP peer must be loopback. A header cannot forge a source address.
    #   * Origin must be a first-party surface: the apex, or exactly one label
    #     under it (idp., a tenant subdomain, the hosted sign-in page a tenant app lands
    #     on). The Origin is returned and becomes the ticket's AUDIENCE: the
    #     ticket Identity mints is stamped with it and redeems for that origin
    #     only, so a tenant page cannot obtain a ticket for the apex and a
    #     stranger's site cannot obtain one at all.
    #   * AITHER_BROWSER_HANDOFF=0 turns the whole surface off (404).
    # Note the auth middleware already lets loopback-without-forwarding-headers
    # through, which is exactly what a browser fetch to 127.0.0.1 is.
    _handoff_origins = frozenset({
        "https://aitherium.com",
        "https://www.aitherium.com",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    })
    _handoff_origin_rule = re.compile(r"^https://[a-z0-9][a-z0-9-]*\.aitherium\.com$")
    _handoff_loopback = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

    def _handoff_guard(request: Request) -> str:
        """Refuse anything but a loopback peer on a first-party origin; return the origin."""
        if os.getenv("AITHER_BROWSER_HANDOFF", "1").strip().lower() in {"0", "false", "no", "off"}:
            raise HTTPException(status_code=404, detail="browser handoff disabled on this node")
        peer = getattr(getattr(request, "client", None), "host", None)
        if peer not in _handoff_loopback:
            raise HTTPException(status_code=403, detail="identity handoff is loopback-only")
        origin = request.headers.get("origin", "")
        if origin not in _handoff_origins and not _handoff_origin_rule.match(origin):
            raise HTTPException(status_code=403, detail="origin not allowed for identity handoff")
        return origin

    def _handoff_profile() -> dict[str, Any] | None:
        """The active ~/.aither/auth.json profile, or None if absent/expired."""
        try:
            from adk.auth import AuthStore, resolve_credentials
            store = AuthStore()
            prof = store.get_active_profile()
            if not prof or not prof.get("access_token"):
                return None
            try:
                if resolve_credentials(store=store).is_expired:
                    return None
            except (TypeError, ValueError) as exc:
                # A naive/odd timestamp is not "signed out"; Identity is the
                # authority on expiry and will refuse the mint if it is stale.
                logger.debug("handoff profile expiry unparseable, deferring to Identity: %s", exc)
            return prof
        except Exception as exc:  # noqa: BLE001 — a broken auth.json is "not signed in"
            logger.debug("handoff profile unreadable: %s", exc)
            return None

    def _handoff_identity_base(prof: dict[str, Any]) -> str:
        # 🚩 "local" IS A SENTINEL, NOT A URL, AND IT IS TRUTHY.
        # This guard listed two loopback PREFIXES, so the profile written by a local
        # login -- endpoint "local" -- fell through every branch and the IdP fallback
        # never fired. The daemon then POSTed to "local/auth/handoff/mint", which is
        # not a URL at all: httpx raises, and the browser handoff answers 502.
        # Measured 2026-09-20 on the owner's box, where auth.json's active profile is
        # exactly {"endpoint": "local"} -- so the path that tells aitherium.com who
        # you are has never worked here, and the page offered "Continue as root".
        #
        # Test for what a usable base IS (an absolute http(s) URL), not for the two
        # unusable spellings someone happened to think of.
        base = (prof.get("endpoint") or "").rstrip("/")
        if (not base.startswith("http://") and not base.startswith("https://")) \
                or base.startswith("http://127.0.0.1") or base.startswith("http://localhost"):
            base = os.getenv(
                "AITHER_IDP_URL", os.getenv("AITHER_IDP_BASE_URL", "https://idp.aitherium.com"),
            ).rstrip("/")
        # The IdP mounts Identity under /identity on the public host.
        if "idp.aitherium.com" in base and not base.endswith("/identity"):
            base += "/identity"
        return base

    @app.get("/identity/whoami")
    async def identity_whoami(request: Request):
        """Who is signed in on this device — a NAME, never a credential."""
        _handoff_guard(request)
        prof = _handoff_profile()
        if not prof:
            return {"logged_in": False, "handoff": True}
        user = prof.get("user") or {}
        return {
            "logged_in": True,
            "handoff": True,
            "username": user.get("username") or user.get("id") or "",
            "display_name": user.get("display_name") or user.get("username") or "",
            "tenant_slug": user.get("tenant_slug") or user.get("tenant_id") or "",
        }

    @app.post("/identity/handoff")
    async def identity_handoff(request: Request):
        """Mint a one-time browser ticket for the signed-in local user.

        Called from a click on the apex. The daemon's bearer stays here; the
        page gets a 60-second single-use ticket to redeem at Veil.
        """
        audience = _handoff_guard(request)
        prof = _handoff_profile()
        if not prof:
            raise HTTPException(
                status_code=401, detail="no signed-in identity on this device — run `adk login`",
            )
        base = _handoff_identity_base(prof)
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    f"{base}/auth/handoff/mint",
                    json={"client": "adk-daemon", "audience": audience},
                    headers={"Authorization": f"Bearer {prof['access_token']}"},
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"identity unreachable: {exc.__class__.__name__}",
            )
        if resp.status_code == 401:
            raise HTTPException(
                status_code=401, detail="this device's sign-in has expired — run `adk login`",
            )
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502, detail=f"identity refused handoff ({resp.status_code})",
            )
        data = resp.json() if resp.content else {}
        user = prof.get("user") or {}
        return {
            "ticket": data.get("ticket"),
            "expires_in": data.get("expires_in", 60),
            "audience": audience,
            "username": user.get("username") or user.get("id") or "",
            "display_name": user.get("display_name") or user.get("username") or "",
        }

    # ── One-click mesh enrollment from the desktop ──────────────────────────
    #
    # "Create my own mesh and enroll it into AitherNet." Three things already
    # existed and never met: Identity issues a tenant-scoped headscale key to
    # any signed-in user (POST /v1/mesh-keys/issue — tenant derived
    # server-side, never caller-supplied), adk.mesh.join does the overlay join
    # + Conductor onboard, and enrollment.rich_enroll registers the node on the
    # identity spine. The only assembled chain was `adk join`, which was broken
    # at step 6 and needs a terminal anyway.
    #
    # Same guard as /identity/handoff: loopback peer + first-party Origin +
    # AITHER_BROWSER_HANDOFF kill switch. The user's OWN device-flow bearer is
    # the only credential — no admin role, no paid-tier gate, no root, no
    # AITHER_SELF_SERVICE flag, and the fleet internal secret is never touched.
    @app.post("/mesh/join")
    async def mesh_join_from_desktop(request: Request):
        """Join the signed-in user's mesh on AitherNet and register this node."""
        _handoff_guard(request)
        prof = _handoff_profile()
        if not prof:
            raise HTTPException(
                status_code=401, detail="no signed-in identity on this device — run `adk login`",
            )
        token = prof["access_token"]
        idp = _handoff_identity_base(prof)
        # conductor.aitherium.com, NOT gateway.aitherium.com: gateway is genesis +
        # the MCP gateway and answers 404 for /v1/mesh/onboard (measured 2026-09-02).
        conductor_url = os.getenv("AITHER_CONDUCTOR_URL", "https://conductor.aitherium.com").rstrip("/")
        import platform as _platform
        relay = _state.get("relay")
        node_id = (
            (relay.node_id if relay else "")
            or os.getenv("AITHER_NODE_NAME", "")
            or _platform.node()
        )
        steps: list[dict[str, Any]] = []

        # 1. mesh key — Identity refuses (403) when the caller has no tenant.
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    f"{idp}/v1/mesh-keys/issue",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"identity unreachable: {exc.__class__.__name__}",
            )
        if r.status_code == 403:
            raise HTTPException(
                status_code=403,
                detail="your account has no workspace yet — create or join one first",
            )
        if r.status_code == 401:
            raise HTTPException(
                status_code=401, detail="this device's sign-in has expired — run `adk login`",
            )
        if r.status_code != 200:
            raise HTTPException(
                status_code=502, detail=f"mesh key issuance failed ({r.status_code})",
            )
        mesh_key = (r.json() or {}).get("mesh_key", "")
        if not mesh_key:
            raise HTTPException(status_code=502, detail="identity returned no mesh key")
        steps.append({"step": "mesh_key", "ok": True})

        # 2. identity-spine registration FIRST: /v1/nodes/register hands back a
        #    tenant-scoped capability token (endpoint:mesh) — the ONLY bearer the
        #    conductor's /v1/mesh/onboard accepts from a node. Measured 2026-09-02:
        #    with the overlay step first, the conductor answered 401 every time.
        tenant_id = ""
        node_bearer = ""
        try:
            from adk import enrollment as _enrollment
            enroll = await asyncio.wait_for(
                _enrollment.rich_enroll(idp, token, node_id, enable_heartbeat=True),
                timeout=60,
            )
            tenant_id = str(enroll.get("tenant_id") or "")
            node_bearer = str(enroll.get("bearer_token") or "")
            steps.append({
                "step": "identity_enroll", "ok": bool(enroll.get("enrolled")),
                "error": enroll.get("error", ""),
            })
        except (asyncio.TimeoutError, RuntimeError, OSError, httpx.HTTPError) as exc:
            steps.append({"step": "identity_enroll", "ok": False, "error": str(exc)[:200]})
        if not node_bearer:
            raise HTTPException(
                status_code=502,
                detail="identity enrollment returned no node capability token; "
                       "the conductor cannot admit this node without one",
            )

        # 3. overlay join (headscale/tailscale transport, key auto-applied) + Conductor
        #    onboard, authenticated with the node's own capability token.
        overlay_ip = ""
        transport = "headscale"
        try:
            from adk import mesh as _mesh
            mesh_result = await asyncio.wait_for(
                _mesh.join(
                    conductor_url, node_id, role="worker",
                    headscale=True, headscale_auth_key=mesh_key, psk=node_bearer,
                    tenant_id=tenant_id,
                    # Explicit control-plane URL: the conductor's onboard response
                    # still advertises headscale.aitherium.com (its baked default),
                    # which is blocked by hostname on at least one ISP and answers
                    # plain HTTP on :443 at the edge — `tailscale up` sat in
                    # "not a TLS handshake" until its 30s timeout (2026-09-02).
                    headscale_url=os.getenv("AITHER_HEADSCALE_URL", "https://hs.aitherium.com"),
                ),
                timeout=150,
            )
            overlay_ip = str(mesh_result.get("overlay_ip") or mesh_result.get("aithernet_ip") or "")
            transport = str(mesh_result.get("transport") or transport)
            steps.append({"step": "overlay_join", "ok": True, "overlay_ip": overlay_ip})
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="mesh join timed out (150s)")
        except (RuntimeError, OSError, ValueError, httpx.HTTPError) as exc:
            # Most common cause on a fresh desktop: no tailscale/wireguard binary.
            raise HTTPException(
                status_code=502,
                detail=f"mesh join failed: {str(exc)[:300]}",
            )

        return {
            "ok": True,
            "node_id": node_id,
            "overlay_ip": overlay_ip,
            "transport": transport,
            "tenant_id": tenant_id,
            "steps": steps,
        }

    # ── Signed Spaces: this device as the Space's origin of record ──────────
    #
    # Two routes, both opening with the SAME `_handoff_guard` the identity and
    # mesh routes use: loopback peer + first-party Origin + the
    # AITHER_BROWSER_HANDOFF kill switch. Neither half is decoration.
    #
    #   * The peer check cannot be forged by a header. `request.client.host` is
    #     the socket's own source address, so a page on some other machine
    #     cannot reach this at all.
    #   * The Origin check is the one that stops a STRANGER'S SITE writing here.
    #     A loopback peer is not scarce -- every page the user opens runs on
    #     this machine -- so without it any tab on the internet could POST a
    #     document to the node and repoint what this device claims to serve.
    #
    # They live on the daemon rather than the standalone launcher for cause:
    # the daemon is the only local process that has already solved BOTH browser
    # gates (the CORS allowlist above, and `allow_private_network=True` for the
    # https-page-to-loopback preflight). The launcher has no CORS handling of
    # any kind, and its `/api` default is a logged 501 behind a fixed prefix
    # allowlist -- adding a rival CORS + private-network lane there would be new
    # unmeasured browser surface for no extra capability.
    #
    # NO NEW REQUEST HEADER is introduced. `allow_headers` above is explicit
    # with no wildcard, and a header missing from it fails the PREFLIGHT --
    # which is not an HTTP error and logs nothing on either side, so the symptom
    # would be a route that simply never gets called. `Content-Type:
    # application/json` is already allowed, and that is all these routes need.
    _space_sign_prefix = b"aitherspace.v1."
    _space_max_body_bytes = 256 * 1024
    _space_raw_sig_bytes = 64
    _space_handle_re = re.compile(r"^s-[0-9a-f]{16}$")
    # The challenge is OPAQUE to this node: it is minted elsewhere, echoed back
    # here, and judged elsewhere. It is bounded and character-checked only so an
    # unbounded blob cannot be parked in the file under the name of a nonce.
    _space_challenge_re = re.compile(r"^[A-Za-z0-9._~+/=-]{8,512}$")

    def _space_path():
        """Where this device's Space document lives (``~/.aither/space.json``).

        The env override exists so the guard and cap arms can be exercised
        without writing to a real home directory. The DEFAULT is absolute --
        a relative default resolves against whatever the process's working
        directory happened to be, which is how state gets written somewhere
        nothing ever reads it back from.
        """
        from pathlib import Path
        override = os.getenv("AITHER_SPACE_FILE", "").strip()
        if override:
            return Path(override)
        return Path.home() / ".aither" / "space.json"

    def _space_b64d(value: Any) -> bytes:
        """STANDARD padded base64 only.

        ``validate=True`` is load-bearing and not a style choice: it REJECTS the
        base64url alphabet, which is the encoding the rest of this contract
        refuses on the platform side too. Accepting it here would let a document
        be written that no verifier downstream can read, and the rejection there
        is indistinguishable from a wrong key.
        """
        import base64
        if not isinstance(value, str) or not value:
            raise ValueError("not a base64 string")
        return base64.b64decode(value, validate=True)

    def _space_verify_signature(pubkey_b64: str, message: bytes, sig_b64: str) -> bool:
        """ECDSA P-256 over ``message``, by the key ``pubkey_b64`` names.

        The browser's ``exportKey('raw', ...)`` is the 65-byte uncompressed
        point, and WebCrypto emits a raw ``r||s`` signature, never DER -- so the
        signature is re-encoded before verification and anything that is not
        exactly 64 bytes is refused before any crypto runs.

        Returns False on ANY exception, the library being absent included. A
        verifier that fails open is not a verifier, and this one is the only
        thing standing between a page that passed CORS and the file this device
        serves from.
        """
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives.asymmetric import utils as asym_utils

            pubkey_bytes = _space_b64d(pubkey_b64)
            sig_bytes = _space_b64d(sig_b64)
            if len(sig_bytes) != _space_raw_sig_bytes:
                return False
            r = int.from_bytes(sig_bytes[:32], "big")
            s = int.from_bytes(sig_bytes[32:], "big")
            der_sig = asym_utils.encode_dss_signature(r, s)
            public_key = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), pubkey_bytes
            )
            public_key.verify(der_sig, message, ec.ECDSA(hashes.SHA256()))
            return True
        except Exception:  # noqa: BLE001 -- every failure is "not verified"
            return False

    def _space_verify_envelope(envelope: Any):
        """Verify a Space envelope with NO directory row to compare against.

        Returns ``(ok, doc, status)``; ``doc`` is non-None only when ok.

        This node holds no directory, so it cannot ask "is this the key the
        platform expects for this handle". It asks the question it CAN answer,
        which turns out to be the same one: an anonymous Space handle is a
        FINGERPRINT of its signing key (``s-`` + the first 16 hex of
        sha256 over the raw public key), so recomputing the handle from the key
        in the document proves the document names the handle its own key owns.
        Whoever holds the key owns that handle; nobody else can produce a
        document that passes both that check and the signature.
        """
        import hashlib
        import json as _json

        if isinstance(envelope, (bytes, bytearray)):
            try:
                envelope = envelope.decode("utf-8")
            except Exception:
                return (False, None, "malformed")
        if isinstance(envelope, str):
            try:
                envelope = _json.loads(envelope)
            except Exception:
                return (False, None, "malformed")
        if not isinstance(envelope, dict) or envelope.get("v") != 1:
            return (False, None, "malformed")

        payload_b64 = envelope.get("payload")
        sig_b64 = envelope.get("sig")
        pubkey_b64 = envelope.get("pubkey")
        try:
            payload_bytes = _space_b64d(payload_b64)
            # Decoded for its SIDE EFFECT: the alphabet check. A base64url
            # signature must be refused here, in the same direction and for the
            # same reason the platform refuses it, rather than reaching the
            # verifier and coming back as an unexplainable wrong-key rejection.
            _space_b64d(sig_b64)
            pubkey_bytes = _space_b64d(pubkey_b64)
        except Exception:
            return (False, None, "malformed")

        # A WRONG-LENGTH SIGNATURE IS A BAD SIGNATURE, not a malformed envelope,
        # and the distinction is not pedantry: a DER-encoded signature is 70-72
        # bytes and is the shape a future signer gets wrong. The platform calls
        # it `bad_signature`; refusing it as `malformed` here would describe the
        # same defect with two different words on two sides of one wire. The
        # length check lives inside the verifier below, which returns False on
        # anything but raw r||s before any crypto runs.
        # THE SIGNATURE IS CHECKED BEFORE THE PAYLOAD IS PARSED, and the order is
        # the point rather than a preference. It matches the platform verifier
        # step for step, so both answer the SAME word for the same envelope: a
        # swapped payload under a good signature is `bad_signature` here too,
        # not `malformed`. Two verifiers that refuse for different stated reasons
        # send whoever reads the log looking for two different defects.
        message = _space_sign_prefix + str(payload_b64).encode("ascii")
        if not _space_verify_signature(pubkey_b64, message, sig_b64):
            return (False, None, "bad_signature")

        try:
            doc = _json.loads(payload_bytes.decode("utf-8"))
        except Exception:
            return (False, None, "malformed")
        if not isinstance(doc, dict) or doc.get("v") != 1:
            return (False, None, "malformed")

        # A document may NEVER nominate a key other than the one it is signed by.
        if doc.get("pubkey") != pubkey_b64:
            return (False, None, "pubkey_mismatch")

        handle = doc.get("handle")
        if not isinstance(handle, str) or not _space_handle_re.match(handle):
            return (False, None, "handle_mismatch")
        derived = "s-" + hashlib.sha256(pubkey_bytes).hexdigest()[:16]
        if derived != handle:
            return (False, None, "handle_pubkey_mismatch")

        issued_at = doc.get("issued_at")
        if isinstance(issued_at, bool) or not isinstance(issued_at, int):
            return (False, None, "malformed")
        if not isinstance(doc.get("config"), dict):
            return (False, None, "schema")

        return (True, doc, "ok")

    @app.get("/space")
    async def space_get(request: Request):
        """What this device is serving, if anything.

        ``{state: 'none'|'serving', doc?, handle?, updated_at?, challenge_echo?}``

        ``challenge_echo`` is the nonce the registering page handed this node on
        the way in, read straight back. That echo is the whole proof this route
        exists to produce, and it proves exactly one thing: a process on the
        registering browser's own loopback, reachable only behind the guard
        above, accepted that nonce. It says nothing about hardware, ownership
        or uptime, and the surface that shows it must not imply otherwise.
        """
        _handoff_guard(request)
        path = _space_path()
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {"state": "none"}
        except OSError as exc:
            logger.debug("space record unreadable: %s", exc)
            return {"state": "none"}
        try:
            record = json.loads(raw.decode("utf-8"))
        except Exception:
            # A corrupt record is NOT "serving". Reporting it as such would put
            # a Space in the directory that this device cannot produce.
            logger.warning("space record at %s is unparseable", path)
            return {"state": "none"}
        if not isinstance(record, dict):
            return {"state": "none"}
        envelope = record.get("envelope")
        if not isinstance(envelope, dict):
            return {"state": "none"}
        return {
            "state": "serving",
            "doc": envelope,
            "handle": record.get("handle"),
            "updated_at": record.get("updated_at"),
            "challenge_echo": record.get("challenge"),
        }

    @app.post("/space")
    async def space_post(request: Request):
        """Accept a signed Space document from a first-party page on this box.

        Body: ``{"envelope": {...}, "challenge": "<opaque nonce>"}``.

        The signature is verified HERE, before anything is written. Passing CORS
        is not authorisation to overwrite what this device serves -- the guard
        proves the request came from a first-party page on this machine, and
        the signature proves the document came from the key that owns the
        handle. Both, or the file is untouched.
        """
        _handoff_guard(request)

        # Cap BEFORE reading where the client declares a size, and again after,
        # because Content-Length is caller-supplied and may be absent or lie.
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > _space_max_body_bytes:
            raise HTTPException(status_code=413, detail="space document too large")
        raw = await request.body()
        if len(raw) > _space_max_body_bytes:
            raise HTTPException(status_code=413, detail="space document too large")

        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail="body must be JSON")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")

        challenge = body.get("challenge")
        if not isinstance(challenge, str) or not _space_challenge_re.match(challenge):
            raise HTTPException(status_code=400, detail="a challenge is required")

        ok, doc, status = _space_verify_envelope(body.get("envelope"))
        if not ok:
            # The file is NOT touched on this path, and that is asserted rather
            # than assumed: an unverified document must not be able to change
            # what this device serves, not even by truncating the old one.
            raise HTTPException(status_code=400, detail=f"envelope refused: {status}")

        path = _space_path()
        record = {
            "v": 1,
            "envelope": body.get("envelope"),
            "handle": doc.get("handle"),
            "issued_at": doc.get("issued_at"),
            "challenge": challenge,
            "updated_at": int(time.time()),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(json.dumps(record, indent=2).encode("utf-8"))
            # Atomic: a reader sees the old record or the new one, never a
            # half-written one. A plain truncate-then-write loses the Space on
            # any crash between the two.
            os.replace(tmp, path)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"could not write space record: {exc}")

        return {
            "ok": True,
            "state": "serving",
            "handle": doc.get("handle"),
            "updated_at": record["updated_at"],
            "challenge_echo": challenge,
        }

    @app.get("/onboard/status")
    async def onboard_status():
        """Report onboarding status for the Awconnect extension.

        Returns: {logged_in, username, tenant, vault_secrets, enrolled, node_id, agents, hub_url}
        Never errors (treats missing files as logged_out/not_enrolled/0-secrets).
        """
        import json as _json

        # Resolve auth via the CANONICAL loader (merges config.json + auth.json +
        # env), so 'logged_in' matches what the rest of adk sees — a raw config.json
        # read reported logged_out on a machine that was actually signed in.
        logged_in = False
        username = None
        tenant = None
        api_key = ""
        try:
            from adk.cli import load_saved_config
            cfg = load_saved_config() or {}
            api_key = cfg.get("api_key", "") or ""
            username = cfg.get("username")
            tenant = cfg.get("tenant_id")
            logged_in = bool(api_key)
        except Exception:  # noqa: BLE001 — fall back to a direct read
            try:
                with open(os.path.expanduser("~/.aither/config.json"), encoding="utf-8") as fh:
                    cfg = _json.load(fh) or {}
                    api_key = cfg.get("api_key", "") or ""
                    username = cfg.get("username")
                    tenant = cfg.get("tenant_id")
                    logged_in = bool(api_key)
            except (OSError, ValueError):
                pass

        # Read auth.json for tenant_slug if not in config
        if not tenant:
            try:
                auth_path = os.path.expanduser("~/.aither/auth.json")
                with open(auth_path, encoding="utf-8") as fh:
                    auth = _json.load(fh) or {}
                    tenant = auth.get("tenant_slug")
            except (OSError, ValueError):
                pass

        # Count vault secrets (secrets.enc file)
        vault_secrets = 0
        try:
            secrets_path = os.path.expanduser("~/.aither/secrets.enc")
            if os.path.isfile(secrets_path):
                # Estimate: encrypted secrets file size / avg secret size (~256 bytes per secret)
                size = os.path.getsize(secrets_path)
                vault_secrets = max(1, size // 256) if size > 0 else 0
        except (OSError, ValueError):
            pass

        # Check enrollment
        enrolled = False
        node_id = None
        hub_url = None
        try:
            node_auth_path = os.path.expanduser("~/.aither/node_auth.json")
            if os.path.isfile(node_auth_path):
                with open(node_auth_path, encoding="utf-8") as fh:
                    na = _json.load(fh) or {}
                    node_id = na.get("node_id")
                    hub_url = na.get("hub_url")
                    if node_id:
                        enrolled = True
        except (OSError, ValueError):
            pass

        # Load agents from agents.json
        agents = []
        try:
            agents_path = os.path.expanduser("~/.aither/agents.json")
            if os.path.isfile(agents_path):
                with open(agents_path, encoding="utf-8") as fh:
                    agents_data = _json.load(fh) or {}
                    agents_map = agents_data.get("agents", {})
                    for name, data in agents_map.items():
                        if isinstance(data, dict):
                            agents.append({
                                "name": name,
                                "url": data.get("url", ""),
                                "status": data.get("status", "unknown"),
                            })
        except (OSError, ValueError):
            pass

        return JSONResponse({
            "logged_in": logged_in,
            "username": username,
            "tenant": tenant,
            "vault_secrets": vault_secrets,
            "enrolled": enrolled,
            "node_id": node_id,
            "agents": agents,
            "hub_url": hub_url,
        })

    @app.post("/onboard/sync")
    async def onboard_sync():
        """Run secrets sync in-process via SecretsSync.

        Returns: {ok:true, synced:number} or {ok:false, error}
        """
        import json as _json

        # Read api_key from config
        api_key = ""
        try:
            config_path = os.path.expanduser("~/.aither/config.json")
            with open(config_path, encoding="utf-8") as fh:
                cfg = _json.load(fh) or {}
                api_key = cfg.get("api_key", "") or ""
        except (OSError, ValueError):
            pass

        if not api_key:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "not logged in (no api_key in config)"}
            )

        try:
            from adk.sync.secrets import SecretsSync
            syncer = SecretsSync(api_key=api_key)
            synced = await syncer.sync()
            return JSONResponse({"ok": True, "synced": synced or 0})
        except Exception as e:
            return JSONResponse(
                status_code=502,
                content={"ok": False, "error": str(e)[:200]}
            )

    @app.post("/onboard/enroll")
    async def onboard_enroll():
        """Run enrollment as a subprocess with ~120s timeout.

        Returns: {ok:true, node_id} or {ok:false, error:<stderr tail>}
        """
        import json as _json
        import subprocess
        import sys

        try:
            # Run enrollment subprocess
            result = subprocess.run(
                [sys.executable, '-m', 'adk', 'enroll', '--yes'],
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=120,
            )

            # Read node_id from node_auth.json after enroll
            node_id = None
            try:
                node_auth_path = os.path.expanduser("~/.aither/node_auth.json")
                if os.path.isfile(node_auth_path):
                    with open(node_auth_path, encoding="utf-8") as fh:
                        na = _json.load(fh) or {}
                        node_id = na.get("node_id")
            except (OSError, ValueError):
                pass

            if node_id:
                return JSONResponse({"ok": True, "node_id": node_id})

            # Enroll ran but didn't produce node_id
            stderr_tail = (result.stderr or "")[-200:]
            return JSONResponse(
                status_code=502,
                content={
                    "ok": False,
                    "error": f"enroll completed but no node_id: {stderr_tail}"
                }
            )
        except subprocess.TimeoutExpired:
            return JSONResponse(
                status_code=504,
                content={"ok": False, "error": "enrollment timed out (>120s)"}
            )
        except Exception as e:
            return JSONResponse(
                status_code=502,
                content={"ok": False, "error": str(e)[:200]}
            )

    # ─── Zero-Knowledge user backup (loopback → encryption local, ciphertext-only to GitHub) ───
    # The passphrase is NEVER persisted — it is required on every /backup/now|list|restore
    # call and lives only in the user's head. The GitHub token is stored ENCRYPTED with the
    # passphrase-derived key, so ~/.aither/backup_config.json holds nothing usable on its own
    # (repo name + salt + an encrypted token). Encryption happens on-daemon; GitHub receives
    # ONLY base64 Fernet ciphertext. Aitherium never sees plaintext or any key.
    # NOTE: the daemon is loopback-trusted (no bearer, like /onboard/* and /x-session/import);
    # a same-user local process could still overwrite the config, but cannot decrypt any
    # existing backup or read the stored token without the passphrase.

    def _backup_config_path() -> str:
        """Local config file path for backup settings."""
        home = os.path.expanduser("~")
        return os.path.join(home, ".aither", "backup_config.json")

    def _load_backup_config() -> dict:
        """Load backup config from ~/.aither/backup_config.json. Returns empty dict if unconfigured."""
        import json as _json
        config_path = _backup_config_path()
        if os.path.isfile(config_path):
            try:
                with open(config_path, encoding="utf-8") as fh:
                    return _json.load(fh) or {}
            except (OSError, ValueError):
                pass
        return {}

    def _save_backup_config(cfg: dict) -> None:
        """Save backup config to ~/.aither/backup_config.json with restrictive permissions."""
        import json as _json
        config_path = _backup_config_path()
        # Ensure dir exists
        os.makedirs(os.path.dirname(config_path), exist_ok=True, mode=0o700)
        # Write with restrictive perms (intent; actual depends on umask)
        with open(config_path, "w", encoding="utf-8") as fh:
            _json.dump(cfg, fh, indent=2)
        # Try to chmod 0600 on the file itself
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            pass

    def _backup_derive_key(passphrase: str, salt_hex: str) -> bytes:
        """Derive a Fernet key locally (PBKDF2-HMAC-SHA256, 600k iters). The
        passphrase is never persisted; the key is re-derived per operation."""
        import base64 as _b64
        from cryptography.hazmat.primitives import hashes as _hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC as _PBKDF2HMAC
        kdf = _PBKDF2HMAC(algorithm=_hashes.SHA256(), length=32,
                          salt=bytes.fromhex(salt_hex), iterations=600000)
        return _b64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))

    def _backup_unlock_token(cfg: dict, passphrase: str):
        """Return (github_token, error). The token is stored ENCRYPTED with the
        passphrase-derived key, so a wrong/absent passphrase yields no token and no
        on-disk secret is usable on its own. Doubles as passphrase validation."""
        from cryptography.fernet import Fernet as _Fernet, InvalidToken as _InvalidToken
        token_enc = cfg.get("token_enc", "")
        if not token_enc:
            return None, "backup config missing encrypted token — reconfigure"
        try:
            key = _backup_derive_key(passphrase, cfg.get("salt", ""))
            return _Fernet(key).decrypt(token_enc.encode("ascii")).decode("utf-8"), None
        except _InvalidToken:
            return None, "wrong passphrase"
        except Exception as _e:  # noqa: BLE001
            return None, f"token unlock failed: {str(_e)[:80]}"

    @app.post("/backup/config")
    async def backup_config_set(request: Request):
        """Configure backup target (GitHub). Stores token + repo + salt locally only."""
        import json as _json
        import secrets
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid JSON"})

        target = body.get("target", "").strip()
        github_repo = body.get("github_repo", "").strip()
        github_token = body.get("github_token", "").strip()
        passphrase = body.get("passphrase", "").strip()

        if not target or target != "github":
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "target must be 'github'"})
        if not github_repo or "/" not in github_repo:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "github_repo must be 'owner/repo'"})
        if not github_token:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "github_token is required"})
        if not passphrase or len(passphrase) < 8:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "passphrase must be at least 8 characters"})

        # Encrypt the GitHub token with the passphrase-derived key and persist the
        # CIPHERTEXT only — never the passphrase, never the plaintext token.
        from cryptography.fernet import Fernet as _Fernet
        salt = secrets.token_hex(16)  # 32 hex chars = 16 bytes
        key = _backup_derive_key(passphrase, salt)
        token_enc = _Fernet(key).encrypt(github_token.encode("utf-8")).decode("ascii")

        cfg = {
            "target": "github",
            "github_repo": github_repo,
            "salt": salt,
            "token_enc": token_enc,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _save_backup_config(cfg)
        return JSONResponse({"ok": True})

    # ── Secrets vault ────────────────────────────────────────────────────────
    # Backed by adk.vault_lockbox (the local encrypted store at
    # ~/.aither/secrets.enc, or the platform vault in remote mode) so the bundled
    # console manages the SAME vault the CLI (`aither vault`) does. No second
    # store, no drift.
    #
    # SECRET BOUNDARY (the console's existing policy, upheld here): this NEVER
    # returns a stored value. Listing is names + metadata only. Writes flow
    # browser -> here -> vault. If you need a value, `aither vault get <name>`
    # prints it in your terminal, where it is your shell's scrollback rather
    # than a long-lived panel in a browser.
    #
    # Auth: the global middleware above already gates every route (loopback, or
    # the server bearer). No extra dependency needed — and adding a weaker one
    # here would only create a second, softer door.

    @app.get("/secrets")
    async def secrets_list():
        """Names + metadata for every secret. Never values."""
        try:
            from adk import vault_lockbox
        except Exception as exc:  # noqa: BLE001 — report, never 500 silently
            return JSONResponse(
                status_code=503,
                content={"error": f"vault unavailable: {type(exc).__name__}"},
            )
        try:
            items = vault_lockbox.list_secrets() or []
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content={"error": f"vault read failed: {type(exc).__name__}: {exc}"},
            )
        remote, where = (False, "local")
        try:
            remote, where = vault_lockbox.remote_mode()
        except Exception:  # noqa: BLE001 — mode is informational only
            pass
        out = []
        for it in items:
            if isinstance(it, str):
                out.append({"name": it})
            elif isinstance(it, dict):
                # Strip anything value-shaped defensively: list_secrets() is not
                # supposed to carry values, and if that ever changes upstream the
                # console must not start leaking them into a browser payload.
                out.append({
                    k: v for k, v in it.items()
                    if k not in ("value", "secret", "plaintext")
                })
        return JSONResponse({
            "count": len(out),
            "secrets": out,
            "remote": remote,
            "source": where,
        })

    @app.post("/secrets")
    async def secrets_set(request: Request):
        """Create or replace a secret. Body: {name, value, secret_type?}"""
        try:
            from adk import vault_lockbox
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=503,
                content={"error": f"vault unavailable: {type(exc).__name__}"},
            )
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=400, content={"error": "invalid JSON body"})

        name = str(body.get("name") or body.get("key") or "").strip()
        value = body.get("value")
        if not name:
            return JSONResponse(status_code=400, content={"error": "name is required"})
        if not isinstance(value, str) or value == "":
            # An empty write would overwrite a working credential with nothing and
            # still look like success.
            return JSONResponse(
                status_code=400,
                content={"error": "value is required and must be a non-empty string"},
            )
        if not _SECRET_NAME_RE.match(name):
            return JSONResponse(
                status_code=400,
                content={"error": "name must be 1-128 chars of A-Z a-z 0-9 _ . -"},
            )
        try:
            ok = vault_lockbox.set_secret(
                name, value, str(body.get("secret_type") or "generic")
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content={"error": f"vault write failed: {type(exc).__name__}: {exc}"},
            )
        if not ok:
            return JSONResponse(
                status_code=502, content={"error": "vault refused the write"}
            )
        # Confirm it PERSISTED. set_secret returning True is not proof the value
        # is readable back, and "wrote successfully but stored nothing" is the
        # exact class this codebase keeps producing.
        try:
            stored = vault_lockbox.get_secret(name)
        except Exception:  # noqa: BLE001
            stored = None
        if not stored:
            return JSONResponse(
                status_code=502,
                content={"error": "write reported success but the secret did not persist"},
            )
        return JSONResponse({"ok": True, "name": name, "length": len(stored)})

    @app.get("/backup/status")
    async def backup_status():
        """Report backup configuration. Does NOT contact GitHub — the token is
        encrypted at rest and only unlocks with the passphrase, so a restore-point
        count needs an unlocked call (POST /backup/list with the passphrase).
        last_backup comes from a NON-secret local marker written by /backup/now."""
        cfg = _load_backup_config()
        if not cfg:
            return JSONResponse({
                "configured": False,
                "target": None,
                "github_repo": None,
                "restore_points": None,
                "last_backup": None,
            })

        last_backup = None
        try:
            marker = os.path.join(os.path.expanduser("~"), ".aither", ".backup_last")
            if os.path.isfile(marker):
                with open(marker, encoding="utf-8") as fh:
                    last_backup = fh.read().strip() or None
        except OSError:
            pass

        return JSONResponse({
            "configured": True,
            "target": cfg.get("target", "github"),
            "github_repo": cfg.get("github_repo"),
            "restore_points": None,  # unknown until unlocked with the passphrase
            "last_backup": last_backup,
        })

    @app.post("/backup/now")
    async def backup_now(request: Request):
        """Create a backup: tar ~/.aither, encrypt locally, upload to GitHub."""
        import tarfile
        import base64
        import io
        import httpx
        from datetime import datetime
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes

        cfg = _load_backup_config()
        if not cfg:
            return JSONResponse({"ok": False, "error": "backup not configured"},
                               status_code=400)

        try:
            body = await request.json()
        except Exception:
            body = {}

        passphrase = body.get("passphrase", "").strip()
        if not passphrase:
            return JSONResponse({"ok": False, "error": "passphrase required"},
                               status_code=400)

        # Prepare backup — the destination comes from the STORED config (never the
        # request), and the token unlocks only with the correct passphrase.
        home = os.path.expanduser("~")
        aither_dir = os.path.join(home, ".aither")
        github_repo = cfg.get("github_repo", "")
        salt_hex = cfg.get("salt", "")
        github_token, _tok_err = _backup_unlock_token(cfg, passphrase)
        if _tok_err:
            return JSONResponse({"ok": False, "error": _tok_err}, status_code=400)

        if not os.path.isdir(aither_dir):
            return JSONResponse({"ok": False, "error": "~/.aither not found"},
                               status_code=400)

        # Curated allowlist of the USER's own data — identity, secrets, history.
        # We deliberately do NOT tar the whole ~/.aither: frameworks/, bin/, llamacpp/,
        # graph/ are installed tooling / re-derivable (2.8GB measured), not user data,
        # and GitHub rejects >100MB blobs anyway.
        include = [
            "config.json", "auth.json", "node_auth.json", "agents.json",
            "identities.json", "secrets.enc",
            "memory", "conversations", "sessions",
        ]

        def _build_encrypted_blob():
            """Tar the allowlisted user-data paths, then PBKDF2-derive + Fernet-encrypt.
            Runs in a worker THREAD — tar+encrypt would otherwise block the daemon's
            single asyncio loop for the whole operation."""
            tar_bytes = io.BytesIO()
            with tarfile.open(fileobj=tar_bytes, mode="w:gz") as tar:
                for name in include:
                    p = os.path.join(aither_dir, name)
                    if os.path.exists(p):
                        try:
                            tar.add(p, arcname=os.path.join(".aither", name))
                        except OSError:
                            pass  # skip unreadable
            tar_data = tar_bytes.getvalue()
            kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                             salt=bytes.fromhex(salt_hex), iterations=600000)
            key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
            ct = Fernet(key).encrypt(tar_data)
            return base64.b64encode(ct).decode("ascii"), len(tar_data)

        try:
            import asyncio as _asyncio
            ciphertext_b64, tar_size = await _asyncio.to_thread(_build_encrypted_blob)

            # Upload to GitHub
            backup_id = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            filename = f"{backup_id}.enc"
            github_path = f"aither-backups/{filename}"

            headers = {"Authorization": f"token {github_token}"}
            url = f"https://api.github.com/repos/{github_repo}/contents/{github_path}"
            payload = {
                "message": f"Backup: {backup_id}",
                "content": ciphertext_b64,
            }

            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.put(url, headers=headers, json=payload)
                if r.status_code not in (201, 200):
                    return JSONResponse({
                        "ok": False,
                        "error": f"GitHub upload failed: {r.status_code}",
                    }, status_code=502)

            # Non-secret local marker so /backup/status can show the latest
            # backup without needing the passphrase.
            try:
                with open(os.path.join(home, ".aither", ".backup_last"),
                          "w", encoding="utf-8") as _fh:
                    _fh.write(backup_id)
            except OSError:
                pass

            return JSONResponse({
                "ok": True,
                "backup_id": backup_id,
                "size": tar_size,
                "note": "ciphertext-only — Aitherium/GitHub never see plaintext",
            })
        except Exception as e:
            logger.exception("backup_now failed")
            return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=502)

    @app.post("/backup/list")
    async def backup_list(request: Request):
        """List backups on GitHub. Requires the passphrase (the token is encrypted
        at rest and only unlocks with it)."""
        import httpx

        cfg = _load_backup_config()
        if not cfg:
            return JSONResponse({"ok": False, "error": "backup not configured"},
                               status_code=400)

        try:
            body = await request.json()
        except Exception:
            body = {}
        passphrase = body.get("passphrase", "").strip()
        if not passphrase:
            return JSONResponse({"ok": False, "error": "passphrase required"},
                               status_code=400)
        github_token, _tok_err = _backup_unlock_token(cfg, passphrase)
        if _tok_err:
            return JSONResponse({"ok": False, "error": _tok_err}, status_code=400)

        try:
            github_repo = cfg.get("github_repo", "")
            headers = {"Authorization": f"token {github_token}"}
            url = f"https://api.github.com/repos/{github_repo}/contents/aither-backups"

            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(url, headers=headers)
                if r.status_code == 404:
                    return JSONResponse({"ok": True, "backups": []})
                if r.status_code != 200:
                    return JSONResponse({"ok": False, "error": f"GitHub error: {r.status_code}"},
                                       status_code=502)

                items = r.json()
                backups = []
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict) and item.get("name", "").endswith(".enc"):
                            backup_id = item["name"].replace(".enc", "")
                            backups.append({
                                "backup_id": backup_id,
                                "ts": backup_id,
                                "size": item.get("size", 0),
                            })
                backups.sort(key=lambda x: x["backup_id"], reverse=True)
                return JSONResponse({"ok": True, "backups": backups})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=502)

    @app.post("/backup/restore")
    async def backup_restore(request: Request):
        """Restore a backup from GitHub and decrypt locally."""
        import tarfile
        import base64
        import io
        import httpx
        from cryptography.fernet import Fernet, InvalidToken
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes

        cfg = _load_backup_config()
        if not cfg:
            return JSONResponse({"ok": False, "error": "backup not configured"},
                               status_code=400)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid JSON"})

        backup_id = body.get("backup_id", "").strip()
        passphrase = body.get("passphrase", "").strip()

        if not backup_id:
            return JSONResponse({"ok": False, "error": "backup_id required"}, status_code=400)
        if not passphrase:
            return JSONResponse({"ok": False, "error": "passphrase required"}, status_code=400)

        try:
            github_repo = cfg.get("github_repo", "")
            github_token, _tok_err = _backup_unlock_token(cfg, passphrase)
            if _tok_err:
                return JSONResponse({"ok": False, "error": _tok_err}, status_code=400)
            salt_hex = cfg.get("salt", "")
            filename = f"{backup_id}.enc"
            github_path = f"aither-backups/{filename}"

            # Fetch encrypted backup from GitHub
            headers = {"Authorization": f"token {github_token}"}
            url = f"https://api.github.com/repos/{github_repo}/contents/{github_path}"

            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.get(url, headers=headers)
                if r.status_code == 404:
                    # 200-with-ok:false (not HTTP 404) so the extension's port-probe,
                    # which treats 404 as "route not on this port", isn't misled.
                    return JSONResponse({"ok": False, "error": "backup not found"})
                if r.status_code != 200:
                    return JSONResponse({"ok": False, "error": f"GitHub error: {r.status_code}"},
                                       status_code=502)

                data = r.json()
                ciphertext_b64 = data.get("content", "")

            # Decode base64
            try:
                ciphertext = base64.b64decode(ciphertext_b64)
            except Exception:
                return JSONResponse({"ok": False, "error": "invalid base64 in backup"},
                                   status_code=400)

            # Derive key
            salt_bytes = bytes.fromhex(salt_hex)
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt_bytes,
                iterations=600000,
            )
            key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))

            # Decrypt
            cipher = Fernet(key)
            try:
                tar_data = cipher.decrypt(ciphertext)
            except InvalidToken:
                return JSONResponse({"ok": False, "error": "decryption failed (wrong passphrase?)"},
                                   status_code=400)

            # Extract to ~/.aither-restore/<backup_id>/
            home = os.path.expanduser("~")
            restore_dir = os.path.join(home, ".aither-restore", backup_id)
            os.makedirs(restore_dir, exist_ok=True, mode=0o700)

            tar_io = io.BytesIO(tar_data)
            file_count = 0
            try:
                with tarfile.open(fileobj=tar_io, mode="r:gz") as tar:
                    # filter="data" blocks path-traversal / absolute members (defense
                    # in depth — the tar is the user's own passphrase-authenticated backup).
                    try:
                        tar.extractall(path=restore_dir, filter="data")
                    except TypeError:  # Python < 3.12 has no filter kwarg
                        tar.extractall(path=restore_dir)
                    file_count = len(tar.getnames())
            except Exception as e:
                return JSONResponse({"ok": False, "error": f"extraction failed: {str(e)[:200]}"},
                                   status_code=400)

            return JSONResponse({
                "ok": True,
                "restored_to": restore_dir,
                "files": file_count,
            })
        except Exception as e:
            logger.exception("backup_restore failed")
            return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=502)

    # ─── Operator service proxies (fleet monitoring dashboard) ───
    # Read-only aggregated endpoints for the bundled console's operator panels.
    # Each service endpoint is independent: a timeout/failure in one panel does
    # not blank others. Auth: global middleware (loopback or bearer). Never
    # returns secret values; errors are distinguishable (503 module missing,
    # 502 upstream failed, 504 timeout).

    @app.get("/operator/chronicle")
    async def operator_chronicle():
        """Aggregate Chronicle logs, search, stats for the operator dashboard.
        Returns: {logs, search, stats, services, error?}
        Never logs secret/credential values."""
        try:
            import httpx
        except ImportError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": f"http client unavailable: {type(exc).__name__}",
                    "logs": None, "search": None, "stats": None, "services": None,
                },
            )

        result = {"logs": None, "search": None, "stats": None, "services": None}
        errors: list[str] = []

        # tls_verify(), never verify=False: it returns the AitherNet CA bundle so
        # self-signed internal certs are trusted WITH verification, and only an
        # explicit AITHER_TLS_VERIFY=false disables it (loudly). adk/_tls.py exists
        # precisely to stop this call site hardcoding a bypass.
        async with httpx.AsyncClient(timeout=10.0, verify=tls_verify()) as client:
            base_url = await _resolve_operator_base(client, "chronicle", 8121)
            if base_url is None:
                return JSONResponse(
                    status_code=502,
                    content={
                        "error": "chronicle unreachable on "
                                 "aitheros-chronicle:8121 or 127.0.0.1:8121",
                        "tried": _operator_bases("chronicle", 8121),
                    },
                )
            # Recent logs (default 50 entries)
            try:
                r = await client.get(f"{base_url}/logs?count=50")
                if r.status_code == 200:
                    result["logs"] = r.json()

                else:
                    errors.append(f"tools: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001 - one dead sub-call
                # must not blank the whole panel, but it must not vanish either
                errors.append(f"tools: {type(exc).__name__}")

            # Statistics (buffer, service counts, level counts)
            try:
                r = await client.get(f"{base_url}/stats")
                if r.status_code == 200:
                    result["stats"] = r.json()

                else:
                    errors.append(f"stats: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001 - one dead sub-call
                # must not blank the whole panel, but it must not vanish either
                errors.append(f"stats: {type(exc).__name__}")

            # Services list (for filter dropdowns)
            try:
                r = await client.get(f"{base_url}/services")
                if r.status_code == 200:
                    result["services"] = r.json()

                else:
                    errors.append(f"services: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001 - one dead sub-call
                # must not blank the whole panel, but it must not vanish either
                errors.append(f"services: {type(exc).__name__}")

            # Search (example: recent errors)
            try:
                r = await client.get(f"{base_url}/search?q=level:error&limit=20")
                if r.status_code == 200:
                    result["search"] = r.json()

                else:
                    errors.append(f"search: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001 - one dead sub-call
                # must not blank the whole panel, but it must not vanish either
                errors.append(f"search: {type(exc).__name__}")

        if all(v is None for v in result.values()):
            return JSONResponse(
                status_code=502,
                content={"error": "chronicle service unreachable", **result},
            )
        return JSONResponse({**result, "errors": errors, "degraded": bool(errors)})

    @app.get("/operator/pulse")
    async def operator_pulse():
        """Aggregate Pulse alerts, pain signals, system metrics for operator
        console. Returns: {alerts, pain, stats, disk, error?}
        Never returns secret alert values."""
        try:
            import httpx
        except ImportError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": f"http client unavailable: {type(exc).__name__}",
                    "alerts": None, "pain": None, "stats": None, "disk": None,
                },
            )

        result = {"alerts": None, "pain": None, "stats": None, "disk": None}
        errors: list[str] = []

        async with httpx.AsyncClient(
            timeout=8.0, verify=tls_verify(),
            http2=False,  # Pulse may not support h2; stick to h1
        ) as client:
            base_url = await _resolve_operator_base(client, "pulse", 8081)
            if base_url is None:
                return JSONResponse(
                    status_code=502,
                    content={
                        "error": "pulse unreachable on aitheros-pulse:8081 "
                                 "or 127.0.0.1:8081",
                        "tried": _operator_bases("pulse", 8081),
                    },
                )
            # Active alerts (aggregated, with severity)
            try:
                r = await client.get(f"{base_url}/alerts")
                if r.status_code == 200:
                    result["alerts"] = r.json()

                else:
                    errors.append(f"alerts: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001 - one dead sub-call
                # must not blank the whole panel, but it must not vanish either
                errors.append(f"alerts: {type(exc).__name__}")

            # Pain signal plane (infrastructure pressure)
            try:
                r = await client.get(f"{base_url}/pain")
                if r.status_code == 200:
                    result["pain"] = r.json()

                else:
                    errors.append(f"pain: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001 - one dead sub-call
                # must not blank the whole panel, but it must not vanish either
                errors.append(f"pain: {type(exc).__name__}")

            # Stats (uptime, metrics, active signal count)
            try:
                r = await client.get(f"{base_url}/stats")
                if r.status_code == 200:
                    result["stats"] = r.json()

                else:
                    errors.append(f"stats: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001 - one dead sub-call
                # must not blank the whole panel, but it must not vanish either
                errors.append(f"stats: {type(exc).__name__}")

            # Disk pressure (reclaimable space, current usage)
            try:
                r = await client.get(f"{base_url}/disk/status")
                if r.status_code == 200:
                    result["disk"] = r.json()

                else:
                    errors.append(f"disk: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001 - one dead sub-call
                # must not blank the whole panel, but it must not vanish either
                errors.append(f"disk: {type(exc).__name__}")

        if all(v is None for v in result.values()):
            return JSONResponse(
                status_code=502,
                content={"error": "pulse service unreachable", **result},
            )
        return JSONResponse({**result, "errors": errors, "degraded": bool(errors)})

    @app.get("/operator/watch")
    async def operator_watch():
        """Aggregate AitherWatch component status for operator console.
        Returns: {components, health, agents, error?}
        Currently may timeout due to fleet network issues."""
        try:
            import httpx
        except ImportError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": f"http client unavailable: {type(exc).__name__}",
                    "components": None, "health": None, "agents": None,
                },
            )

        result = {"components": None, "health": None, "agents": None}
        errors: list[str] = []

        # Was hardcoded to http://127.0.0.1:8082. Two defects: plain HTTP fails on
        # these ports (measured — they serve HTTPS, and an http probe returns a
        # transport error that reads as "service down"), and a loopback-only URL
        # is wrong whenever adk runs inside the fleet network. The resolver tries
        # both hosts over https.
        async with httpx.AsyncClient(timeout=5.0, verify=tls_verify()) as client:
            base_url = await _resolve_operator_base(client, "watch", 8082)
            if base_url is None:
                return JSONResponse(
                    status_code=502,
                    content={
                        "error": "watch unreachable on aitheros-watch:8082 "
                                 "or 127.0.0.1:8082",
                        "tried": _operator_bases("watch", 8082),
                        "components": None, "health": None, "agents": None,
                    },
                )
            # Component status (process/service health)
            try:
                r = await client.get(f"{base_url}/status")
                if r.status_code == 200:
                    result["components"] = r.json()

                else:
                    errors.append(f"components: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001 - one dead sub-call
                # must not blank the whole panel, but it must not vanish either
                errors.append(f"components: {type(exc).__name__}")

            # Health check (service liveness)
            try:
                r = await client.get(f"{base_url}/health")
                if r.status_code == 200:
                    result["health"] = r.json()

                else:
                    errors.append(f"health: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001 - one dead sub-call
                # must not blank the whole panel, but it must not vanish either
                errors.append(f"health: {type(exc).__name__}")

            # Agent heartbeats (fleet member status)
            try:
                r = await client.get(f"{base_url}/agents")
                if r.status_code == 200:
                    result["agents"] = r.json()

                else:
                    errors.append(f"agents: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001 - one dead sub-call
                # must not blank the whole panel, but it must not vanish either
                errors.append(f"agents: {type(exc).__name__}")

        if all(v is None for v in result.values()):
            return JSONResponse(
                status_code=502,
                content={"error": "watch service unreachable", **result},
            )
        return JSONResponse({**result, "errors": errors, "degraded": bool(errors)})

    @app.get("/operator/flux")
    async def operator_flux():
        """Aggregate AitherFlux mailbox/routing/cognitive budget state for
        operator console. Returns: {mailboxes, routes, services, stats,
        cognitive_budgets, error?}"""
        try:
            import httpx
        except ImportError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": f"http client unavailable: {type(exc).__name__}",
                    "mailboxes": None, "routes": None, "services": None,
                    "stats": None, "cognitive_budgets": None,
                },
            )

        # Flux requires X-Internal-Key header for auth (internal mesh boundary)
        internal_key = os.getenv("AITHER_INTERNAL_SECRET", "")
        headers = {}
        if internal_key:
            headers["X-Internal-Key"] = internal_key

        result = {
            "mailboxes": None, "routes": None, "services": None,
            "stats": None, "cognitive_budgets": None,
        }
        errors: list[str] = []

        async with httpx.AsyncClient(
            timeout=8.0, verify=tls_verify(), http2=False,
        ) as client:
            base_url = await _resolve_operator_base(client, "flux", 8117)
            if base_url is None:
                return JSONResponse(
                    status_code=502,
                    content={
                        "error": "flux unreachable on aitheros-flux:8117 "
                                 "or 127.0.0.1:8117",
                        "tried": _operator_bases("flux", 8117),
                    },
                )
            # Mailbox registry (services + queue depths)
            try:
                r = await client.get(f"{base_url}/mailboxes", headers=headers)
                if r.status_code == 200:
                    result["mailboxes"] = r.json()

                else:
                    errors.append(f"mailboxes: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001 - one dead sub-call
                # must not blank the whole panel, but it must not vanish either
                errors.append(f"mailboxes: {type(exc).__name__}")

            # Routing table (service → node → port mapping)
            try:
                r = await client.get(f"{base_url}/routes", headers=headers)
                if r.status_code == 200:
                    result["routes"] = r.json()

                else:
                    errors.append(f"routes: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001 - one dead sub-call
                # must not blank the whole panel, but it must not vanish either
                errors.append(f"routes: {type(exc).__name__}")

            # Service registry (all known services)
            try:
                r = await client.get(f"{base_url}/services", headers=headers)
                if r.status_code == 200:
                    result["services"] = r.json()

                else:
                    errors.append(f"services: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001 - one dead sub-call
                # must not blank the whole panel, but it must not vanish either
                errors.append(f"services: {type(exc).__name__}")

            # Statistics (packets/sec, success rate, latency)
            try:
                r = await client.get(f"{base_url}/stats", headers=headers)
                if r.status_code == 200:
                    result["stats"] = r.json()

                else:
                    errors.append(f"stats: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001 - one dead sub-call
                # must not blank the whole panel, but it must not vanish either
                errors.append(f"stats: {type(exc).__name__}")

            # Cognitive resource budgets (service allocations)
            try:
                r = await client.get(f"{base_url}/cognitive/budgets", headers=headers)
                if r.status_code == 200:
                    result["cognitive_budgets"] = r.json()

                else:
                    errors.append(f"cognitive_budgets: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001 - one dead sub-call
                # must not blank the whole panel, but it must not vanish either
                errors.append(f"cognitive_budgets: {type(exc).__name__}")

        if all(v is None for v in result.values()):
            return JSONResponse(
                status_code=502,
                content={"error": "flux service unreachable", **result},
            )
        return JSONResponse({**result, "errors": errors, "degraded": bool(errors)})

    # ─── Built-in streaming chat page (so a human has somewhere to talk) ───

    def _pack_html(name: str | None = None) -> HTMLResponse:
        """A pack page, served so a browser can never hold a stale copy.

        load_ui_pack() re-reads the file on every request, so the ORIGIN is
        always current -- but these routes sent no Cache-Control, ETag or
        Last-Modified, which lets a browser heuristically cache the HTML and
        keep showing an older build of the page long after the file changed.
        That is indistinguishable, to whoever is looking at it, from the demo
        having reverted. no-cache still permits a cached copy; it forbids
        using one without revalidating.
        """
        return HTMLResponse(
            load_ui_pack(name),
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    @app.get("/", response_class=HTMLResponse)
    async def console_page():
        # The LANDPAGE is the "switcher" pack — one grid over every surface of
        # the platform (the Local AI app, the tenant apps, the products), so a
        # visitor lands somewhere instead of a blank console. The selected UI
        # pack (the app itself) lives at /local; `adk ui set <pack>` still
        # chooses it. Never blank — falls back console -> minimal.
        return _pack_html("switcher")

    @app.get("/local", response_class=HTMLResponse)
    async def console_page_local():
        # The SELECTED UI pack ($AITHER_AGENT_UI, default "console" = the full
        # admin SPA). Swap it with `adk ui set <pack>` or drop a folder in
        # ~/.aither/ui-packs/. Never blank — falls back console -> minimal.
        return _pack_html()

    @app.get("/ui", response_class=HTMLResponse)
    async def console_page_alias():
        return _pack_html()

    @app.get("/chat", response_class=HTMLResponse)
    async def chat_page_minimal():
        # Back-compat: the original lightweight streaming chat page (the
        # "minimal" pack), regardless of the selected pack.
        return _pack_html("minimal")

    @app.get("/aeon", response_class=HTMLResponse)
    async def aeon_page():
        # Aeon group-chat UI pack — multi-agent discussion.
        return _pack_html("aeon")

    # ─── Pack UI assets + bridge SDK (sandboxed-iframe plugin system) ───
    # A pack may declare a `ui:` block in its .toolpack.yaml; the console then
    # mounts its page as <iframe sandbox="allow-scripts allow-forms"> (opaque
    # origin, NO token). These static routes are auth-skipped like "/" itself;
    # everything privileged goes through the bearer-gated invoke bridge.

    _PACK_ASSET_TYPES = {
        ".html": "text/html; charset=utf-8", ".js": "text/javascript",
        ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon",
        ".woff": "font/woff", ".woff2": "font/woff2", ".txt": "text/plain",
        ".map": "application/json",
    }

    @app.get("/packs/_sdk.js")
    async def pack_sdk_js():
        js = _load_pack_sdk()
        if js is None:
            return JSONResponse(status_code=404, content={"error": "sdk_asset_missing"})
        return PlainTextResponse(js, media_type="text/javascript",
                                 headers={"X-Content-Type-Options": "nosniff"})

    @app.get("/packs/{pack_id}/ui/{asset_path:path}")
    async def pack_ui_asset(pack_id: str, asset_path: str):
        """Serve a static file from an ENABLED pack's declared ui assets dir.

        Fail-closed everywhere: unknown pack → 404; pack not enabled → 403;
        no ui block → 404; any path that resolves outside the assets dir
        (traversal, absolute, drive-qualified, symlink escape) → 403.
        """
        from adk.config import load_saved_config as _lsc
        from adk.pack_scope import valid_pack_id

        if not valid_pack_id(pack_id):
            return JSONResponse(status_code=400, content={"error": "invalid_pack_id"})
        try:
            from adk.tool_pack_loader import get_tool_pack_loader
            manifest = get_tool_pack_loader().discover().get(pack_id)
        except (ImportError, RuntimeError):
            manifest = None
        if manifest is None:
            return JSONResponse(status_code=404, content={"error": "pack_not_found"})
        if pack_id not in (_lsc().get("required_packs") or []):
            return JSONResponse(status_code=403, content={"error": "pack_not_enabled"})
        assets_dir = manifest.ui_assets_dir
        if assets_dir is None or not assets_dir.is_dir():
            return JSONResponse(status_code=404, content={"error": "pack_has_no_ui"})

        rel = (asset_path or "index.html").replace("\\", "/")
        if rel.endswith("/") or rel == "":
            rel += "index.html"
        # Reject traversal/absolute forms before touching the filesystem…
        if rel.startswith("/") or ".." in rel.split("/") or ":" in rel:
            return JSONResponse(status_code=403, content={"error": "invalid_asset_path"})
        try:
            target = (assets_dir / rel).resolve()
            # …and verify the RESOLVED path (catches symlink escapes).
            if not target.is_relative_to(assets_dir.resolve()):
                return JSONResponse(status_code=403, content={"error": "invalid_asset_path"})
        except (OSError, ValueError):
            return JSONResponse(status_code=403, content={"error": "invalid_asset_path"})
        if not target.is_file():
            return JSONResponse(status_code=404, content={"error": "asset_not_found"})
        if target.stat().st_size > 5_000_000:
            return JSONResponse(status_code=413, content={"error": "asset_too_large"})
        media = _PACK_ASSET_TYPES.get(target.suffix.lower(), "application/octet-stream")
        from fastapi.responses import FileResponse
        return FileResponse(
            target, media_type=media,
            headers={
                "X-Content-Type-Options": "nosniff",
                # Only the console (same origin) may frame pack pages.
                "Content-Security-Policy": "frame-ancestors 'self'",
                "Cache-Control": "no-cache",
            })

    @app.get("/packs/{pack_name}/{asset_path:path}", response_class=FileResponse)
    async def pack_asset(pack_name: str, asset_path: str):
        """Static assets for a UI pack (e.g. the on-device Bonsai worker).

        The pack's index.html is served as a STRING (load_ui_pack); its sibling
        files were served by nothing, which is why the old on-device backend
        inlined its runtime as a blob. Drop-in dir wins, then the packaged dir.
        Traversal is refused, and an asset that does not exist is a 404 — never
        a fallback to the index (a missing worker must be loud, not a page).
        """
        if not asset_path or ".." in asset_path or "\\" in asset_path or asset_path.startswith("/"):
            raise HTTPException(status_code=404, detail="no such asset")
        base = _ui_packs_dir()
        cand = os.path.join(base, pack_name, asset_path)
        if not os.path.isfile(cand):
            cand = os.path.join(os.path.dirname(__file__), "webui", "packs", pack_name, asset_path)
        if not os.path.isfile(cand):
            raise HTTPException(status_code=404, detail="no such asset")
        return FileResponse(cand)


    # ─── Admin/settings console API (all under /admin/*, bearer-gated) ───
    # Registered as a function (not an APIRouter) so the handlers can close over
    # get_agent + _state and operate on the LIVE agent — backend swap, pack
    # reload, and MCP registration all take effect without a restart. /admin is
    # intentionally absent from _skip_auth_paths so _auth_middleware gates it.
    try:
        from adk.admin_api import register_admin_routes
        register_admin_routes(app, get_agent=get_agent, state=_state)
    except ImportError as exc:
        logger.warning("admin console API unavailable: %s", exc)

    # ISO Factory bridge (/admin/factory/*) — shells to the monorepo factory CLI
    # across the wheel boundary; no-op where the factory isn't present.
    try:
        from adk.admin_factory import register_admin_factory_routes
        register_admin_factory_routes(app, state=_state)
    except ImportError as exc:
        logger.warning("factory bridge unavailable: %s", exc)

    # ─── No-backend handler ───

    @app.get("/demo")
    async def demo_redirect():
        """Redirect to demo.aitherium.com when no local backend is available."""
        from fastapi.responses import RedirectResponse
        return RedirectResponse("https://demo.aitherium.com")

    @app.exception_handler(ConnectionError)
    async def _no_backend_handler(request: Request, exc: ConnectionError):
        return JSONResponse(
            status_code=503,
            content={
                "error": "no_backend",
                "message": "No LLM backend available. Set AITHER_API_KEY to use the gateway, or install Ollama locally.",
                "demo": "https://demo.aitherium.com",
                "gateway": "https://gateway.aitherium.com",
                "docs": "https://github.com/Aitherium/awdk/blob/main/docs/GETTING_STARTED.md",
            },
        )

    # ─── Fleet endpoints ───

    @app.get("/agents")
    async def list_agents_endpoint():
        """List all agents in the fleet (or the single agent)."""
        if is_fleet:
            fleet = await _init_fleet()
            return {
                "fleet": fleet.name,
                "orchestrator": fleet.orchestrator_name,
                "agents": fleet.registry.list(),
            }
        a = await get_agent()
        return {
            "fleet": None,
            "orchestrator": a.name,
            "agents": [{
                "name": a.name,
                "identity": a._identity.name,
                "description": a._identity.description,
                "skills": a._identity.skills,
                "tools": [t.name for t in a._tools.list_tools()],
                "status": "running",
            }],
        }

    @app.post("/agents/{agent_name}/chat")
    async def agent_chat(agent_name: str, request: Request):
        """Chat with a specific agent in the fleet."""
        body = await request.json()
        message = body.get("message", body.get("content", ""))
        session_id = body.get("session_id")
        request_id = get_trace_id()

        a = await get_agent(agent_name)
        start = time.time()
        resp = await a.chat(message, session_id=session_id)
        latency_ms = (time.time() - start) * 1000

        # Record metrics (safe — latency_ms may be MagicMock in tests)
        try:
            _metrics = get_metrics()
            _metrics.record_request(latency_ms=latency_ms, status_code=200)
            _metrics.record_llm_call(
                model=str(resp.model or ""), latency_ms=float(resp.latency_ms or 0),
                tokens=int(resp.tokens_used or 0),
            )
        except (TypeError, ValueError):
            pass

        # Fire-and-forget Strata ingest
        asyncio.ensure_future(_strata_ingest(
            agent=a.name, session_id=resp.session_id,
            user_message=message, assistant_response=resp.content,
            model=resp.model, tokens_used=resp.tokens_used,
            latency_ms=resp.latency_ms, tool_calls=resp.tool_calls_made,
        ))

        # Fire-and-forget Chronicle log
        asyncio.ensure_future(_chronicle_log_chat(
            agent=a.name, session_id=resp.session_id,
            model=resp.model, tokens_used=resp.tokens_used,
            latency_ms=resp.latency_ms, request_id=request_id,
        ))

        return {
            "response": resp.content,
            "agent": a.name,
            "model": resp.model,
            "tokens_used": resp.tokens_used,
            "session_id": resp.session_id,
            "tool_calls": resp.tool_calls_made,
            "artifacts": resp.artifacts,
            "request_id": request_id,
        }

    @app.get("/agents/{agent_name}/sessions")
    async def agent_sessions(agent_name: str):
        """List conversation sessions for an agent."""
        from adk.conversations import get_conversation_store
        store = get_conversation_store()
        sessions = await store.list_sessions(agent_name=agent_name)
        return {"agent": agent_name, "sessions": sessions}

    @app.post("/agent/packs/reload")
    async def reload_agent_packs():
        """Hot-reload agent packs without restarting the process.

        Rediscovers installed packs and rebuilds the ToolRegistry to pick up
        any new skills or tools that were installed. Called when pack.applied
        Flux events arrive, or manually via the endpoint.
        """
        try:
            # Get the current agent
            agent = await get_agent()

            original_tool_count = len(agent._tools.list_tools())
            logger.info("Pack reload: re-registering discovered packs for agent %s", agent.name)

            # Actually rebuild: re-run pack discovery + tool-pack registration so
            # newly-installed packs' tools land in agent._tools. _load_discovered_packs
            # scans the discovery dirs and registers licensed packs into the live
            # registry; fall back to register_tool_packs directly if unavailable.
            reloaded = False
            if hasattr(agent, "_load_discovered_packs"):
                agent._load_discovered_packs()
                reloaded = True
            else:
                try:
                    from adk.builtin_tools import register_tool_packs
                    register_tool_packs(agent)
                    reloaded = True
                except ImportError:
                    logger.warning("register_tool_packs unavailable — reload is a no-op")

            new_tool_count = len(agent._tools.list_tools())

            return {
                "status": "reloaded" if reloaded else "noop",
                "agent": agent.name,
                "tools_before": original_tool_count,
                "tools_after": new_tool_count,
                "tools_added": max(0, new_tool_count - original_tool_count),
                "message": "Packs reloaded" if reloaded else "No reload mechanism available",
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("Pack reload failed: %s", e)
            return {
                "status": "failed",
                "error": str(e),
                "message": "Pack reload encountered an error",
            }

    @app.post("/forge/dispatch")
    async def forge_dispatch(request: Request):
        """Dispatch a task via AgentForge."""
        from adk.forge import ForgeSpec, get_forge
        body = await request.json()
        spec = ForgeSpec(
            agent_type=body.get("agent", body.get("agent_type", "auto")),
            task=body.get("task", body.get("message", "")),
            timeout=body.get("timeout", 120.0),
            effort=body.get("effort", 5),
            context=body.get("context", ""),
        )
        forge = get_forge()
        result = await forge.dispatch(spec)
        return {
            "content": result.content,
            "agent": result.agent,
            "tokens_used": result.tokens_used,
            "tool_calls": result.tool_calls,
            "status": result.status,
            "latency_ms": result.latency_ms,
            "error": result.error,
        }

    @app.post("/v1/forge/dispatch")
    async def v1_forge_dispatch(request: Request):
        """Accept forge dispatch from sovereign AitherOS nodes (canonical path)."""
        return await forge_dispatch(request)

    # ─── Genesis-compatible chat ───

    @app.post("/chat")
    async def chat(request: Request):
        _nudge_mcp_attach()
        body = await request.json()
        message = body.get("message", body.get("content", ""))
        session_id = body.get("session_id")
        agent_name = body.get("agent")

        # An empty prompt runs the FULL agentic pipeline on nothing: the model has
        # no user question in context, burns turns calling tools aimlessly, and
        # answers with a first-contact greeting, which reads as the agent being
        # broken rather than as the request being empty. Refuse before any of that
        # starts -- measured on the platform's own /agent lane: 7 turns, 7 tools,
        # 133.8s, ending in a greeting to a question that never reached the model.
        if not (message or "").strip():
            raise HTTPException(status_code=400, detail="empty_message")
        request_id = get_trace_id()

        # Inference controls (null=auto pattern — only pass if explicitly set)
        chat_kwargs: dict[str, Any] = {}
        if body.get("effort") is not None:
            chat_kwargs["effort"] = int(body["effort"])
        if body.get("temperature") is not None:
            chat_kwargs["temperature"] = float(body["temperature"])
        if body.get("top_p") is not None:
            chat_kwargs["top_p"] = float(body["top_p"])
        if body.get("repetition_penalty") is not None:
            chat_kwargs["repetition_penalty"] = float(body["repetition_penalty"])
        if body.get("max_tokens") is not None:
            chat_kwargs["max_tokens"] = int(body["max_tokens"])
        if body.get("model") is not None:
            chat_kwargs["model"] = body["model"]
        if body.get("tool_choice") is not None:
            chat_kwargs["tool_choice"] = body["tool_choice"]

        a = await get_agent(agent_name)
        start = time.time()
        resp = await a.chat(message, session_id=session_id, **chat_kwargs)
        latency_ms = (time.time() - start) * 1000

        # Record metrics (safe — latency_ms may be MagicMock in tests)
        try:
            _metrics = get_metrics()
            _metrics.record_request(latency_ms=latency_ms, status_code=200)
            _metrics.record_llm_call(
                model=str(resp.model or ""), latency_ms=float(resp.latency_ms or 0),
                tokens=int(resp.tokens_used or 0),
            )
        except (TypeError, ValueError):
            pass

        # Fire-and-forget Strata ingest (training loop)
        asyncio.ensure_future(_strata_ingest(
            agent=a.name, session_id=resp.session_id,
            user_message=message, assistant_response=resp.content,
            model=resp.model, tokens_used=resp.tokens_used,
            latency_ms=resp.latency_ms, tool_calls=resp.tool_calls_made,
        ))

        # Fire-and-forget Chronicle log
        asyncio.ensure_future(_chronicle_log_chat(
            agent=a.name, session_id=resp.session_id,
            model=resp.model, tokens_used=resp.tokens_used,
            latency_ms=resp.latency_ms, request_id=request_id,
        ))

        return {
            "response": resp.content,
            "agent": a.name,
            "model": resp.model,
            "tokens_used": resp.tokens_used,
            "prompt_tokens": resp.prompt_tokens,
            "completion_tokens": resp.completion_tokens,
            "session_id": resp.session_id,
            "tool_calls": resp.tool_calls_made,
            "artifacts": resp.artifacts,
            "request_id": request_id,
            "finish_reason": resp.finish_reason,
            "effort_level": resp.effort_level,
            "cache_status": resp.cache_status,
        }

    # ─── AitherOS-typed SSE streaming ───

    @app.post("/stream")
    async def stream_chat(request: Request):
        """SSE stream using AitherOS event protocol.

        Emits typed events: session_start, thinking, tool_call, tool_result,
        token, answer, complete — matching the Genesis/MicroScheduler protocol
        so shell-core's useAitherStream works identically against ADK and Genesis.
        """
        body = await request.json()
        message = body.get("message", body.get("content", ""))
        session_id = body.get("session_id") or f"adk-{uuid.uuid4().hex[:8]}"
        agent_name = body.get("agent")
        reasoning = body.get("reasoning", False)
        mcp_endpoints = body.get("mcp_endpoints")
        system_additions = body.get("system_additions")

        return StreamingResponse(
            _aitheros_stream(get_agent, message, session_id, agent_name, reasoning, mcp_endpoints,
                             system_additions=system_additions),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/chat/stream")
    async def stream_chat_genesis_compat(request: Request):
        """Genesis-compatible SSE stream — alias for /stream.

        AitherShell sends POST /chat/stream with {message, persona, ...}.
        Maps the Genesis body shape to the ADK handler so `adk shell`
        and any AitherShell pointing at an ADK server works out of the box.
        """
        body = await request.json()
        message = body.get("message", body.get("content", ""))
        session_id = body.get("session_id") or f"adk-{uuid.uuid4().hex[:8]}"
        agent_name = body.get("agent") or body.get("persona")

        # An empty prompt runs the FULL agentic pipeline on nothing: the model has
        # no user question in context, burns turns calling tools aimlessly, and
        # answers with a first-contact greeting, which reads as the agent being
        # broken rather than as the request being empty. Refuse before any of that
        # starts -- measured on the platform's own /agent lane: 7 turns, 7 tools,
        # 133.8s, ending in a greeting to a question that never reached the model.
        if not (message or "").strip():
            raise HTTPException(status_code=400, detail="empty_message")
        reasoning = body.get("reasoning", False)
        mcp_endpoints = body.get("mcp_endpoints")
        # Extra SYSTEM content for this turn — the same `system_additions` list
        # genesis honours. AitherShell sends a pack prompt and its live
        # [USER'S SHELL] block (clock, cwd, shell) here. Until 2026-08-23 this
        # handler silently DROPPED the field, so the shell's pack persona and
        # situation reached genesis and never the daemon — the body looked right
        # in every log and changed nothing. Bounded in adk.situation.
        system_additions = body.get("system_additions")
        # `session_context.summary` is the genesis-protocol channel AitherShell
        # uses to replay a resumed transcript ("Conversation so far"). Until
        # 2026-08-23 this handler dropped it too, so every --resume'd or
        # omnibox follow-up line reached the agent cold: the owner typed
        # "what time is it", then "how do you know that?", and got an essay on
        # epistemology. Folded into the system additions, after the caller's.
        _sc = body.get("session_context") or {}
        _summary = str(_sc.get("summary") or "").strip() if isinstance(_sc, dict) else ""
        if _summary:
            system_additions = list(system_additions or []) + [
                "Conversation so far in this terminal (most recent last); the new "
                "line may be a follow-up to it:\n" + _summary[:6000]
            ]

        # Optional per-turn generation params (honored by the OpenAI-compatible
        # providers). Only forward what the caller actually set + what is valid,
        # so a UI's settings drawer really takes effect instead of being decorative.
        gen_params: dict[str, Any] = {}
        try:
            if body.get("temperature") is not None:
                gen_params["temperature"] = float(body["temperature"])
            if body.get("max_tokens") is not None:
                gen_params["max_tokens"] = int(body["max_tokens"])
        except (TypeError, ValueError):
            gen_params = {}

        return StreamingResponse(
            _aitheros_stream(get_agent, message, session_id, agent_name, reasoning,
                             mcp_endpoints, gen_params=gen_params or None,
                             system_additions=system_additions),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/chat/steer")
    async def chat_steer(request: Request):
        """Inject a steering message into an active chat session.

        Clients call this while a /chat/stream SSE is open. The steering message
        is forwarded into the session's queue so the agent pipeline sees it
        immediately and can react before the next LLM call.

        Body:
            session_id: str     — the session to steer
            message: str        — steering text (e.g. "focus on X", "stop", "@abort")
            action: str         — "append" (default), "cancel", or "hint"

        Actions:
            append — user follow-up message, visible in chat and injected as
                     a user message in the conversation history.
            hint   — invisible system-level context nudge. NOT shown in
                     conversation history. Injected as a [STEERING HINT]
                     system block that guides the agent's next reasoning step
                     without interrupting flow.
            cancel — abort the running generation (not yet implemented).
        """
        body = await request.json()
        session_id = body.get("session_id", "")
        message = body.get("message", "")
        action = body.get("action", "append")

        if not session_id:
            return JSONResponse({"ok": False, "error": "session_id required"}, status_code=400)

        from adk.steering import queue_steering_message
        queued = await queue_steering_message(session_id, action, message)

        if not queued:
            return JSONResponse(
                {"ok": False, "error": "no active session", "session_id": session_id},
                status_code=404,
            )

        return JSONResponse(
            {
                "ok": True,
                "session_id": session_id,
                "queued": True,
                "action": action,
            },
            status_code=200,
        )

    @app.post("/aeon/stream")
    async def stream_aeon_group_chat(request: Request):
        """Aeon group-chat SSE stream — multi-agent discussion.

        Body: {message, preset?, agents?, rounds?, session_id?, temperature?, max_tokens?}
        Response: SSE with agent_message events per participant + synthesis.
        """
        body = await request.json()
        message = body.get("message", "")
        preset = body.get("preset", "balanced")
        agents = body.get("agents")
        rounds = body.get("rounds", 1)
        session_id = body.get("session_id") or f"aeon-{uuid.uuid4().hex[:8]}"

        # NOTE: no per-turn temperature/max_tokens here — AeonSession.chat() runs a
        # multi-agent round and does not thread per-call generation params, so
        # accepting them would be a silent no-op. Omitted deliberately.

        return StreamingResponse(
            _aeon_stream(message, preset, agents, rounds, session_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ─── Artifact endpoints ───

    @app.get("/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str):
        """Get artifact metadata by ID."""
        from adk.artifacts import get_registry
        art = get_registry().get_by_id(artifact_id)
        if not art:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return art.to_dict()

    @app.get("/sessions/{session_id}/artifacts")
    async def get_session_artifacts(session_id: str):
        """List artifacts produced in a session."""
        from adk.artifacts import get_registry
        arts = get_registry().get(session_id)
        return {"session_id": session_id, "artifacts": [a.to_dict() for a in arts]}

    @app.post("/sessions/{session_id}/confirm")
    async def confirm_tools(session_id: str, request: Request):
        """Resume a turn paused for tool approval (human-in-the-loop).

        Body: ``{decisions: [{tool_use_id|tool, result: "allow"|"deny", deny_message?}]}``.
        Records the decisions then re-runs the paused turn, streaming the continuation as
        the same SSE trace (``session_start`` → … → ``complete``). 409 if nothing paused."""
        from adk.approval import get_approval_store
        body = await request.json()
        decisions = body.get("decisions", []) or []
        store = get_approval_store()
        paused = store.get(session_id)
        if not paused:
            return JSONResponse(
                {"error": "no_paused_turn",
                 "message": "no turn is awaiting approval for this session"},
                status_code=409,
            )
        store.record_decisions(session_id, decisions)
        message = paused.get("user_message", "")
        agent_name = paused.get("agent")
        return StreamingResponse(
            _aitheros_stream(get_agent, message, session_id, agent_name, False),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ─── OpenAI-compatible endpoints ───

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        messages_raw = body.get("messages", [])
        model = body.get("model")
        temperature = body.get("temperature", 0.7)
        max_tokens = body.get("max_tokens", 4096)
        stream = body.get("stream", False)

        a = await get_agent()

        # ── Plain mode: direct LLM completion, no agent loop ──
        # The local UI pack's Ask tab sends plain:true. The agent loop decides
        # tool use and can hang mid-fleet-churn, timing out after 60s with
        # "took too long to process" — a direct completion cannot tool-loop
        # and is the reliable path for casual questions. Agentic paths
        # (Tasks/Build) keep the loop. OpenAI shape on purpose, like the rest
        # of this route.
        if body.get("plain"):
            msgs = [Message(role=m["role"], content=m.get("content", ""))
                    for m in messages_raw]

            if stream:
                async def plain_stream():
                    async for chunk in a.llm.chat_stream(msgs, model=model or None):
                        if chunk.content:
                            yield "data: " + json.dumps({
                                "id": "chatcmpl-plain", "object": "chat.completion.chunk",
                                "created": int(time.time()), "model": chunk.model or "",
                                "choices": [{"index": 0, "delta": {"content": chunk.content},
                                             "finish_reason": None}],
                            }) + "\n\n"
                    yield "data: " + json.dumps({
                        "id": "chatcmpl-plain", "object": "chat.completion.chunk",
                        "created": int(time.time()), "model": "",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }) + "\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(plain_stream(), media_type="text/event-stream")

            resp = await a.llm.chat(msgs, model=model or None)
            return {
                "id": "chatcmpl-plain",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": resp.model or "",
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": resp.content},
                             "finish_reason": resp.finish_reason}],
                "usage": {"prompt_tokens": resp.prompt_tokens,
                          "completion_tokens": resp.completion_tokens,
                          "total_tokens": resp.prompt_tokens + resp.completion_tokens},
            }

        # Convert to Message objects
        messages = [Message(role=m["role"], content=m.get("content", "")) for m in messages_raw]

        if stream:
            # Extract last user message for agent.chat_stream()
            last_user_msg = ""
            history_for_stream = []
            for m in messages_raw:
                if m.get("role") == "user":
                    last_user_msg = m.get("content", "")
                if m.get("role") in ("user", "assistant"):
                    history_for_stream.append({"role": m["role"], "content": m.get("content", "")})
            # Remove last user message from history (chat_stream takes it separately)
            if history_for_stream and history_for_stream[-1]["role"] == "user":
                history_for_stream = history_for_stream[:-1]

            return StreamingResponse(
                _stream_agent_response(a, last_user_msg, history_for_stream, model),
                media_type="text/event-stream",
            )

        resp = await a.llm.chat(
            messages, model=model, temperature=temperature, max_tokens=max_tokens
        )

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": resp.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": resp.content},
                    "finish_reason": resp.finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": resp.prompt_tokens,
                "completion_tokens": resp.completion_tokens,
                "total_tokens": resp.tokens_used,
            },
        }

    @app.get("/v1/models")
    async def list_models_endpoint():
        import inspect as _inspect

        a = await get_agent()
        # HONESTY (2026-09-13). This listing hardcoded `owned_by: local` for every id the
        # provider returned -- including, that afternoon, an orchestrator whose unit was
        # masked and whose every answer came from DeepSeek-flash. When the provider is an
        # OpenAI-compatible upstream (MicroScheduler), forward ITS listing, which carries
        # the `aither` availability block; fall back to the flat list only when it cannot.
        # get_provider is async on the real LLM facade and a plain MagicMock under
        # test; a fake provider's base_url is not a str. Neither may 500 the listing.
        try:
            provider = a.llm.get_provider()
            if _inspect.isawaitable(provider):
                provider = await provider
        except (RuntimeError, OSError, ConnectionError, AttributeError, TypeError):
            provider = None
        base = getattr(provider, "base_url", None)
        if isinstance(base, str) and base:
            try:
                async with httpx.AsyncClient(timeout=10.0, verify=tls_verify()) as _c:
                    r = await _c.get(base.rstrip("/").removesuffix("/v1") + "/v1/models")
                if r.status_code == 200 and isinstance(r.json().get("data"), list):
                    return {"object": "list", "data": r.json()["data"],
                            "aither_source": base}
            except Exception as exc:  # noqa: BLE001 - fall through to the flat list
                logger.debug("upstream /v1/models unavailable, serving the flat "
                             "list without availability: %s", exc)
        try:
            models = await a.llm.list_models()
        except (RuntimeError, OSError, ConnectionError):
            models = []

        return {
            "object": "list",
            "data": [
                {
                    "id": m,
                    "object": "model",
                    "created": 0,
                    "owned_by": "local",
                    "aither": {"available": None, "probed": False,
                               "note": "flat list; upstream availability not consulted"},
                }
                for m in models
            ],
        }

    # ── local image generation ────────────────────────────────────────────────
    #: Where media-forge listens. It is a HOST process, so from this daemon (also a
    #: host process) that is plain loopback -- the container indirection that
    #: AITHER_MEDIAFORGE_URL exists for does not apply here.
    _mediaforge_url = os.environ.get(
        "AITHER_MEDIAFORGE_URL", "http://127.0.0.1:8200").rstrip("/")

    #: Where a SELF-SERVICE install lands. `imagegen setup` deploys the
    #: `cuda-comfyui-*` recipe, whose deployment.port is 8188 -- so the person this
    #: whole path exists for ends up with ComfyUI and NO media-forge. A bridge that
    #: only spoke to media-forge would answer "media-forge unreachable" to exactly the
    #: stranger it was built to serve, which is the defect this constant exists to
    #: prevent.
    _comfy_url = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")

    async def _comfy_checkpoint(client, want: str = "") -> str:
        """The checkpoint to use, ASKED of the server rather than hardcoded.

        A hardcoded name is wrong on every machine but the one it was written on --
        `imagegen setup` installs whatever the recipe pulled, and a user may have
        added their own. Prefers an SDXL-looking name because the sampler defaults
        below are SDXL's; falls back to whatever is first rather than failing, since
        a working render with an unexpected model beats a correct refusal.
        """
        r = await client.get(f"{_comfy_url}/object_info/CheckpointLoaderSimple")
        r.raise_for_status()
        names = (r.json()["CheckpointLoaderSimple"]["input"]["required"]
                 ["ckpt_name"][0]) or []
        if not names:
            raise RuntimeError("ComfyUI has no checkpoints installed")
        if want and want in names:
            return want
        for n in names:
            low = n.lower()
            if "xl" in low and "refiner" not in low:
                return n
        return names[0]

    def _comfy_workflow(ckpt: str, prompt: str, width: int, height: int,
                        steps: int, cfg: float, seed: int) -> dict:
        """A minimal SDXL txt2img graph in ComfyUI's API format.

        Node ids are strings because that is what /prompt expects; each `inputs` link
        is [node_id, output_index]. Kept to the seven nodes a text-to-image actually
        needs -- loading a template from disk would be one more thing to ship and to
        drift.
        """
        return {
            "1": {"class_type": "CheckpointLoaderSimple",
                  "inputs": {"ckpt_name": ckpt}},
            "2": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": prompt, "clip": ["1", 1]}},
            "3": {"class_type": "CLIPTextEncode",
                  # A negative prompt is not optional for SDXL at low step counts;
                  # without one the output is noticeably muddier.
                  "inputs": {"text": "blurry, low quality, watermark, text",
                             "clip": ["1", 1]}},
            "4": {"class_type": "EmptyLatentImage",
                  "inputs": {"width": width, "height": height, "batch_size": 1}},
            "5": {"class_type": "KSampler",
                  "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                             "sampler_name": "euler", "scheduler": "normal",
                             "denoise": 1.0, "model": ["1", 0],
                             "positive": ["2", 0], "negative": ["3", 0],
                             "latent_image": ["4", 0]}},
            "6": {"class_type": "VAEDecode",
                  "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
            "7": {"class_type": "SaveImage",
                  "inputs": {"filename_prefix": "adk", "images": ["6", 0]}},
        }

    async def _generate_via_comfy(prompt: str, width: int, height: int,
                                  budget: float, req: dict):
        """Drive ComfyUI directly. Returns {"images": [...]} or a JSONResponse error."""
        steps = int((req or {}).get("steps") or 24)
        cfg = float((req or {}).get("cfg") or 7.0)
        seed = int((req or {}).get("seed") or int(time.time() * 1000) % 2**31)
        async with httpx.AsyncClient(timeout=30.0) as client:
            ckpt = await _comfy_checkpoint(client, str((req or {}).get("checkpoint") or ""))
            wf = _comfy_workflow(ckpt, prompt, width, height, steps, cfg, seed)
            r = await client.post(f"{_comfy_url}/prompt", json={"prompt": wf})
            if r.status_code != 200:
                # ComfyUI reports a rejected GRAPH here, with the offending node --
                # far more useful than this daemon guessing, so pass it through.
                return JSONResponse(status_code=502, content={
                    "error": f"ComfyUI rejected the workflow (HTTP {r.status_code})",
                    "detail": r.text[:400], "checkpoint": ckpt})
            pid = (r.json() or {}).get("prompt_id")
            if not pid:
                return JSONResponse(status_code=502, content={
                    "error": "ComfyUI accepted the prompt but returned no prompt_id"})

            deadline = time.time() + budget
            while time.time() < deadline:
                h = await client.get(f"{_comfy_url}/history/{pid}")
                if h.status_code == 200 and (h.json() or {}).get(pid):
                    entry = h.json()[pid]
                    # A FAILED run also lands in history. Reporting its outputs as
                    # success would return an empty list and read as "nothing matched".
                    status = (entry.get("status") or {})
                    if status.get("status_str") == "error":
                        return JSONResponse(status_code=502, content={
                            "error": "ComfyUI failed to execute the workflow",
                            "detail": str(status.get("messages"))[:400]})
                    urls = []
                    for out in (entry.get("outputs") or {}).values():
                        for img in (out.get("images") or []):
                            fn = img.get("filename")
                            if not fn:
                                continue
                            sub = img.get("subfolder", "")
                            typ = img.get("type", "output")
                            urls.append(f"{_comfy_url}/view?filename={quote(fn)}"
                                        f"&subfolder={quote(sub)}&type={quote(typ)}")
                    if urls:
                        return {"images": urls, "checkpoint": ckpt, "steps": steps}
                    # In history, no error, no images: say so rather than looping to a
                    # timeout that blames the deadline.
                    return JSONResponse(status_code=502, content={
                        "error": "ComfyUI finished with no images",
                        "checkpoint": ckpt})
                await asyncio.sleep(2)
        return JSONResponse(status_code=504, content={
            "error": f"ComfyUI did not finish within {int(budget)}s",
            "hint": "the run is still queued in ComfyUI; it is not lost"})

    @app.post("/v1/generate")
    async def generate_image_endpoint(req: dict):
        """Generate an image on the USER'S OWN hardware, for a browser that cannot.

        WHY THIS LIVES HERE and not in media-forge. The browser tool's contract is
        `POST {localImageBase}/v1/generate -> {images: [...]}`, and the obvious place
        to serve it is media-forge, which owns the generator. It cannot: measured
        2026-08-24, media-forge sends NO CORS headers at all (`OPTIONS
        /api/studio/txt2img` -> 405, no Access-Control-Allow-Origin on any GET), so a
        page on aitherium.com or wizzense.github.io cannot call it from a browser.
        This daemon already has the right origin allowlist, is already discovered by
        that page, and already routes its chat -- so bridging here is one place to fix
        instead of two, and media-forge stays a loopback-only service, which is the
        correct posture for something with no auth.

        THE SHAPE MISMATCH IS THE WORK. media-forge's txt2img returns a JOB, not an
        image: dispatch, then poll `/api/jobs/{id}` until `status == "done"`, then read
        `result.images`. The browser tool wants one synchronous answer. So this waits.
        Measured on the reference box, a 512x512 `fast` preset took ~380s -- that is a
        real number and it is why `timeout` is generous and why the failure below says
        which stage it died in rather than just "failed".

        IMAGES COME BACK AS LOOPBACK URLs, deliberately, not base64. media-forge
        answers `result.images[0] = "/media/<sha>.png"`, and that file serves 200 with
        `image/png` (458 KB for the measured one). Returning the URL instead of
        inlining ~600 KB of base64 per image keeps this response small, and an <img>
        tag needs no CORS to load it -- only `fetch()` does, which is why the render
        path works while the API path did not. The gobbonet image mod accepts a
        loopback URL for exactly this reason.
        """
        prompt = str((req or {}).get("prompt") or "").strip()
        if not prompt:
            return JSONResponse(status_code=400,
                                content={"error": "prompt is required"})
        width = int((req or {}).get("width") or 1024)
        height = int((req or {}).get("height") or 1024)
        # `fast` unless asked otherwise: this is a browser waiting on a person, and the
        # difference between presets here is minutes.
        preset = str((req or {}).get("preset") or "fast")
        style = str((req or {}).get("style") or "photoreal")
        budget = float((req or {}).get("timeout") or 600)

        body = {"prompt": prompt, "style": style, "count": 1, "width": width,
                "height": height, "preset": preset, "timeout": int(budget)}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # The dispatch itself can outlive its own HTTP call on this route --
                # media-forge runs the job inside the request. A read timeout here is
                # therefore NOT a failure; the job is already tracked, and the poll
                # below finds it. Treating it as an error would report a failure for a
                # render that is running.
                try:
                    await client.post(f"{_mediaforge_url}/api/studio/txt2img", json=body)
                except httpx.ReadTimeout:
                    # Deliberate, and deliberately NOT silent: media-forge runs the job
                    # inside the request, so a read timeout here means it STARTED, not
                    # that it failed -- the poll below is the real result. Logging keeps
                    # a genuine dispatch failure distinguishable from this expected one.
                    logger.debug("txt2img dispatch read-timeout; polling for the job")

                deadline = time.time() + budget
                while time.time() < deadline:
                    r = await client.get(f"{_mediaforge_url}/api/jobs")
                    r.raise_for_status()
                    jobs = [j for j in (r.json().get("jobs") or [])
                            if j.get("kind") == "txt2img"]
                    if not jobs:
                        await asyncio.sleep(2)
                        continue
                    job = jobs[-1]
                    status = str(job.get("status") or "")
                    if status in ("done", "complete", "finished"):
                        res = job.get("result") or {}
                        paths = [x for x in (res.get("images") or []) if x]
                        if not paths:
                            return JSONResponse(status_code=502, content={
                                "error": "media-forge finished with no images",
                                "detail": str(res.get("error") or "")[:300]})
                        return {"images": [
                            p if str(p).startswith("http") else f"{_mediaforge_url}{p}"
                            for p in paths]}
                    if status == "error":
                        res = job.get("result") or {}
                        return JSONResponse(status_code=502, content={
                            "error": "media-forge could not generate",
                            # Its own note names the missing engine when there is one,
                            # which is far more useful than this daemon guessing.
                            "detail": str(res.get("error") or job.get("error") or "")[:300],
                            "note": str(res.get("note") or "")[:300]})
                    await asyncio.sleep(3)
            return JSONResponse(status_code=504, content={
                "error": f"image generation did not finish within {int(budget)}s",
                "hint": "the job is still running in media-forge; it is not lost"})
        except httpx.HTTPError:
            # media-forge is absent. That is the NORMAL case for someone who ran
            # `imagegen setup`, which installs ComfyUI and nothing else -- so fall
            # through to it rather than reporting a failure that names a service the
            # user was never told to install.
            pass
        try:
            return await _generate_via_comfy(prompt, width, height, budget, req)
        except (httpx.HTTPError, RuntimeError, KeyError) as e:
            return JSONResponse(status_code=503, content={
                "error": "no local image backend answered",
                "tried": [_mediaforge_url, _comfy_url],
                "detail": f"{type(e).__name__}: {e}"[:200],
                "hint": "run `python -m adk.toolpacks.image_bootstrap setup` to install "
                        "one, or set COMFYUI_URL / AITHER_MEDIAFORGE_URL"})

    @app.get("/v1/generate")
    async def generate_image_capability():
        """Is local image generation actually available? A capability probe, not a health
        check: the browser needs to know whether to offer the tool at all, and
        `media-forge is up` does not imply `it can render` -- it needs a diffusion
        backend behind it."""
        async with httpx.AsyncClient(timeout=6.0) as client:
            for name, url, path in (("media-forge", _mediaforge_url, "/api/studio/loras"),
                                    ("comfyui", _comfy_url, "/system_stats")):
                try:
                    r = await client.get(f"{url}{path}")
                    if r.status_code == 200:
                        return {"available": True, "engine": url, "kind": name}
                except httpx.HTTPError:
                    continue
        return {"available": False, "tried": [_mediaforge_url, _comfy_url],
                "hint": "run `python -m adk.toolpacks.image_bootstrap setup`"}

    @app.get("/v1/identities")
    async def list_identities_endpoint():
        """List available agent identities."""
        return {"identities": list_identities()}

    # ─── Strata ingest helper (fire-and-forget) ───

    async def _strata_ingest(**kwargs):
        """Send chat data to Strata for training/analytics. Never blocks or raises."""
        try:
            from adk.strata import get_strata_ingest
            strata = get_strata_ingest()
            await strata.ingest_chat(**kwargs)
        except (ImportError, RuntimeError, OSError):
            pass  # Truly fire-and-forget

    # ─── MCP server (every node is also an MCP server) ───

    async def _init_mcp_server():
        """Initialize the MCP server so this node SERVES tools, not just consumes them."""
        try:
            from adk.mcp_server import MCPServer

            a = _state.get("agent")
            if a is None and not is_fleet:
                a = AitherAgent(
                    name=_state["identity"],
                    identity=_state["identity"],
                    config=_state["config"],
                )
                _state["agent"] = a

            # Build a merged registry: agent tools + fleet tools
            if is_fleet and _state.get("fleet"):
                from adk.tools import ToolRegistry
                merged = ToolRegistry()
                for fleet_agent in _state["fleet"].agents:
                    for td in fleet_agent._tools.list_tools():
                        # Prefix with agent name to avoid collisions
                        prefixed_name = f"{fleet_agent.name}__{td.name}"
                        merged._tools[prefixed_name] = td._replace(name=prefixed_name) if hasattr(td, '_replace') else td
                        # Also keep original name from first agent that has it
                        if td.name not in merged._tools:
                            merged._tools[td.name] = td
                mcp = MCPServer(tool_registry=merged, server_name=_state["fleet"].name)
            elif a:
                mcp = MCPServer(tool_registry=a._tools, server_name=a.name)
            else:
                mcp = MCPServer(server_name=_state["identity"])

            mcp.mount(app)
            _state["mcp_server"] = mcp

            # Wire relay → MCP server so inbound mesh tool calls are handled locally
            relay_obj = _state.get("relay")
            if relay_obj:
                relay_obj.set_local_mcp_server(mcp)

            logger.info("MCP server initialized (%d tools)", len(mcp.registry.list_tools()))
        except (ImportError, RuntimeError, OSError) as exc:
            logger.debug("MCP server init failed (non-fatal): %s", exc)

    # ─── A2A protocol server (Google A2A v0.3.0) ───

    async def _init_a2a_server():
        """Initialize the A2A protocol server for cross-agent interop."""
        try:
            from adk.a2a import A2AServer

            a = _state.get("agent")
            base_url = f"http://localhost:{config.server_port}"

            a2a = A2AServer(
                agent=a,
                base_url=base_url,
                server_name=a.name if a else _state.get("identity", "adk-agent"),
            )
            a2a.mount(app)
            _state["a2a_server"] = a2a
            logger.info("A2A server initialized (protocol v0.3.0)")
        except (ImportError, RuntimeError, OSError) as exc:
            logger.debug("A2A server init failed (non-fatal): %s", exc)

    async def _connect_service_bridge():
        """Connect ServiceBridge to discover AitherOS services (non-fatal)."""
        try:
            from adk.services import ServiceBridge
            bridge = ServiceBridge()
            status = await bridge.connect()
            _state["service_bridge"] = bridge

            if status.mode != "standalone":
                # Register MCP tools on fleet agents or single agent
                if is_fleet and _state["fleet"]:
                    for a in _state["fleet"].agents:
                        await bridge.register_on_agent(a)
                elif _state["agent"]:
                    await bridge.register_on_agent(_state["agent"])
            else:
                # Visible warning when AitherOS is not detected
                import sys
                agent = _state.get("agent")
                builtin_count = len(agent._tools.list_tools()) if agent else 0
                print(
                    "\n\033[33m\u26a0  STANDALONE MODE \u2014 AitherOS not detected\033[0m\n"
                    "   awnode (localhost:8080) and Genesis (localhost:8001) "
                    "are unreachable.\n"
                    f"   Only {builtin_count} built-in tools available "
                    f"(vs 449+ with AitherOS).\n"
                    "   Start AitherOS or set AITHER_NODE_URL to connect.\n",
                    file=sys.stderr,
                )
                # Start background reconnect so we auto-upgrade when
                # AitherOS services come online
                await bridge.start_background_reconnect()

            logger.info("ServiceBridge mode: %s (tools: %d)",
                        status.mode, status.tools_count)
        except (ImportError, RuntimeError, OSError) as exc:
            logger.debug("ServiceBridge startup failed (non-fatal): %s", exc)

    async def _flush_strata_queue():
        """Flush any queued Strata entries from previous sessions."""
        try:
            from adk.strata import get_strata_ingest
            strata = get_strata_ingest()
            flushed = await strata.flush_queue()
            if flushed:
                logger.info("Flushed %d queued Strata entries", flushed)
        except (ImportError, RuntimeError, OSError):
            pass

    async def _chronicle_log_chat(**kwargs):
        """Send chat event to Chronicle. Never blocks or raises."""
        try:
            import inspect
            from adk.chronicle import get_chronicle
            chronicle = get_chronicle()
            # Drop kwargs log_llm_call doesn't accept (e.g. session_id) — callers
            # pass a richer set; signature drift must not crash this fire-and-forget.
            accepted = set(inspect.signature(chronicle.log_llm_call).parameters)
            await chronicle.log_llm_call(**{k: v for k, v in kwargs.items() if k in accepted})
        except Exception:
            pass  # Truly fire-and-forget

    async def _flush_chronicle_queue():
        """Flush any queued Chronicle entries from previous sessions."""
        try:
            from adk.chronicle import get_chronicle
            chronicle = get_chronicle()
            flushed = await chronicle.flush_queue()
            if flushed:
                logger.info("Flushed %d queued Chronicle entries", flushed)
        except (ImportError, RuntimeError, OSError):
            pass

    async def _start_watch_reporter():
        """Start the background Watch health reporter."""
        try:
            from adk.watch import get_watch_reporter
            reporter = get_watch_reporter()

            # Register a collector that reports fleet/agent state
            def _collect_health():
                data = {"version": __version__}
                try:
                    if is_fleet and _state["fleet"]:
                        fleet = _state["fleet"]
                        data["agents"] = fleet.registry.agent_names
                        data["agent_count"] = len(fleet.agents)
                    elif _state["agent"]:
                        data["agents"] = [_state["agent"].name]
                        data["agent_count"] = 1
                except (KeyError, AttributeError):
                    pass
                return data

            reporter.register_collector(_collect_health)
            await reporter.start()
        except (ImportError, RuntimeError, OSError) as exc:
            logger.debug("Watch reporter startup failed (non-fatal): %s", exc)

    async def _flush_pulse_queue():
        """Flush any queued Pulse pain signals from previous sessions."""
        try:
            from adk.pulse import get_pulse
            pulse = get_pulse()
            flushed = await pulse.flush_queue()
            if flushed:
                logger.info("Flushed %d queued Pulse pain signals", flushed)
        except (ImportError, RuntimeError, OSError):
            pass

    async def _register_fleet_endpoint():
        """Register invoke_url with portal fleet so sovereign can dispatch to us."""
        from adk.config import load_saved_config
        saved = load_saved_config()
        api_key = saved.get("api_key", "") or config.aither_api_key
        if not api_key:
            logger.debug("Fleet endpoint registration skipped (no API key)")
            return
        # Determine reach mode and invoke_url (tunnel vs mesh overlay)
        reach_mode = "tunnel"
        invoke_url = os.getenv("AITHER_INVOKE_URL", "")
        if not invoke_url:
            # Check for mesh mode (overlay IP registration)
            mesh_overlay_ip = os.getenv("AITHER_MESH_OVERLAY_IP", "").strip()
            if mesh_overlay_ip:
                reach_mode = "mesh"
                invoke_url = f"http://{mesh_overlay_ip}:{config.server_port}"
            else:
                invoke_url = f"http://localhost:{config.server_port}"
        tenant_id = saved.get("tenant_id", "") or os.getenv("AITHER_TENANT_ID", "")
        agent_name = _state.get("identity", identity)
        # Advertise this agent's Ed25519 A2A public key so a peer verifying a
        # signed inbound request can look it up (a2a_trust._is_key_trusted) from
        # the AUTHORITATIVE fleet directory instead of a static allowlist. Same
        # deterministic keypair the agent signs with (load_or_generate_keypair).
        a2a_public_key = ""
        try:
            from adk.a2a_client import load_or_generate_keypair
            _, a2a_public_key = load_or_generate_keypair(agent_name)
        except Exception as _pk_exc:
            logger.debug("A2A pubkey unavailable for fleet registration: %s", _pk_exc)
        try:
            portal_url = os.getenv("AITHER_PORTAL_URL", "https://app.aitherium.com")
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(
                    f"{portal_url}/v1/agents/upsert",
                    json={
                        "name": agent_name,
                        "scope": {"visibility": "workspace", "tenant_id": tenant_id},
                        "invoke_url": invoke_url,
                        "reach": reach_mode,
                        "status": "online",
                        # Advertise what inference this agent runs, so discovery
                        # (`adk agents ls`) shows "optiplex → bonsai (llamacpp)".
                        "model": getattr(config, "model", "") or "",
                        "provider_hint": getattr(config, "llm_backend", "") or "",
                        "public_key": a2a_public_key,
                    },
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            _state["fleet_registered"] = True
            logger.info("Registered fleet endpoint: %s -> %s", agent_name, invoke_url)
        except (ImportError, RuntimeError, OSError, ConnectionError, httpx.HTTPError) as exc:
            _state["fleet_registered"] = False
            logger.warning("Fleet endpoint registration failed (non-fatal): %s", exc)

        # Register with new fleet API (separate try/except for resilience)
        workspace_id = saved.get("workspace_id", "") or os.getenv("AITHER_WORKSPACE_ID", "")
        try:
            portal_url = os.getenv("AITHER_PORTAL_URL", "https://app.aitherium.com")
            import httpx
            billing_email = saved.get("billing_email", "")
            async with httpx.AsyncClient(timeout=15) as client:
                fleet_resp = await client.post(
                    f"{portal_url}/api/fleet/endpoints/register",
                    json={
                        "name": agent_name,
                        "url": invoke_url,
                        "reach": reach_mode,
                        "agent_type": "adk-agent",
                        "capabilities": ["chat", "tools"],
                        "billing_email": billing_email,
                        # Inference backend advertised for mesh discovery.
                        "model": getattr(config, "model", "") or "",
                        "provider_hint": getattr(config, "llm_backend", "") or "",
                    },
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "X-Tenant-ID": tenant_id,
                        "X-Workspace-ID": workspace_id,
                    },
                )
                if fleet_resp.status_code == 200:
                    data = fleet_resp.json()
                    _state["fleet_endpoint_id"] = data.get("endpoint_id", "")
                    _state["fleet_api_key"] = data.get("api_key", "")
                    logger.info("Registered fleet API endpoint: %s (id=%s)", agent_name, _state.get("fleet_endpoint_id"))
                else:
                    logger.debug("Fleet API registration returned %d", fleet_resp.status_code)
        except (ImportError, RuntimeError, OSError, ConnectionError, httpx.HTTPError) as exc:
            logger.debug("Fleet API registration failed (non-fatal): %s", exc)

        # Register with AitherFleet service (central roster for cycle dispatch)
        try:
            fleet_svc_url = os.getenv("AITHER_FLEET_URL", "http://localhost:8162")
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{fleet_svc_url}/fleet/agents",
                    json={
                        "name": agent_name,
                        "visibility": "tenant",
                        "status": "active",
                        "card": {
                            "agent_type": "adk-agent",
                            "capabilities": ["chat", "tools", "forge"],
                            "invoke_url": invoke_url,
                            "reach": reach_mode,
                        },
                    },
                    headers={
                        "X-Tenant-ID": tenant_id,
                        "X-Workspace-ID": workspace_id,
                    },
                )
            logger.info("Registered with AitherFleet service: %s", agent_name)
        except (ImportError, RuntimeError, OSError, ConnectionError, httpx.HTTPError) as exc:
            logger.debug("AitherFleet registration failed (non-fatal): %s", exc)

    async def _deregister_fleet_endpoint():
        """Mark agent as offline in portal fleet on shutdown."""
        if not _state.get("fleet_registered"):
            return
        from adk.config import load_saved_config
        saved = load_saved_config()
        api_key = saved.get("api_key", "") or config.aither_api_key
        if not api_key:
            return
        agent_name = _state.get("identity", identity)
        tenant_id = saved.get("tenant_id", "") or os.getenv("AITHER_TENANT_ID", "")
        try:
            portal_url = os.getenv("AITHER_PORTAL_URL", "https://app.aitherium.com")
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{portal_url}/v1/agents/upsert",
                    json={
                        "name": agent_name,
                        "scope": {"visibility": "workspace", "tenant_id": tenant_id},
                        "status": "offline",
                    },
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            logger.info("Deregistered fleet endpoint: %s", agent_name)
        except (ImportError, RuntimeError, OSError, ConnectionError, httpx.HTTPError) as exc:
            logger.debug("Fleet endpoint deregistration failed (non-fatal): %s", exc)

        # Deregister from fleet API (separate try/except for resilience)
        fleet_endpoint_id = _state.get("fleet_endpoint_id")
        if fleet_endpoint_id:
            try:
                portal_url = os.getenv("AITHER_PORTAL_URL", "https://app.aitherium.com")
                import httpx
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.delete(
                        f"{portal_url}/api/fleet/endpoints/{fleet_endpoint_id}",
                        headers={"Authorization": f"Bearer {api_key}"},
                    )
                logger.info("Deregistered fleet API endpoint: %s (id=%s)", agent_name, fleet_endpoint_id)
            except (ImportError, RuntimeError, OSError, ConnectionError, httpx.HTTPError) as exc:
                logger.debug("Fleet API deregistration failed (non-fatal): %s", exc)

    async def _workflow_mirror_loop():
        """Stream Claude Code Workflow journals into expeditions every 15s.

        The host half of the workflow -> expedition mirror (adk.workflow_mirror):
        the hooks record and bind each run, this loop tails the journals. One
        bad pass never ends the loop; errors are in the pass summary.
        """
        from adk.workflow_mirror import scan_and_mirror
        interval = max(5, int(os.environ.get("AITHER_WORKFLOW_MIRROR_INTERVAL", "15") or 15))
        while True:
            try:
                summary = await asyncio.to_thread(scan_and_mirror)
                if summary.get("errors"):
                    logger.debug("workflow mirror: %s", summary["errors"])
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 -- the loop must outlive one bad pass
                logger.debug("workflow mirror pass failed: %s", exc)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _fleet_heartbeat_loop():
        """Continuously heartbeat to portal every 60s using FederationLiteClient.

        Reports: status, CPU/memory, active agents, inference backend, tokens processed.
        On failure, queues locally and retries next cycle.
        """
        from adk.config import load_saved_config
        saved = load_saved_config()
        api_key = saved.get("api_key", "") or config.aither_api_key
        if not api_key:
            logger.debug("Fleet heartbeat loop skipped (no API key)")
            return

        tenant_id = saved.get("tenant_id", "") or os.environ.get("AITHER_TENANT_ID", "")
        portal_url = os.environ.get("AITHER_PORTAL_URL", "https://app.aitherium.com")
        agent_name = _state.get("identity", identity)
        instance_id = os.environ.get("AITHER_INSTANCE_ID", "")
        invoke_url = os.environ.get("AITHER_INVOKE_URL", f"http://localhost:{config.server_port}")

        try:
            from adk.federation_lite import FederationLiteClient
            fed_client = FederationLiteClient(
                hub_url=portal_url,
                api_key=api_key,
                node_id=instance_id or agent_name,
            )
        except ImportError:
            fed_client = None

        consecutive_failures = 0

        while True:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break

            try:
                # Collect metrics
                metrics_data = {}
                try:
                    m = get_metrics()
                    metrics_data = {
                        "tokens_processed": getattr(m, "tokens_processed", 0),
                        "requests_total": getattr(m, "requests_total", 0),
                        "uptime_seconds": getattr(m, "uptime_seconds", 0),
                    }
                except (RuntimeError, AttributeError):
                    pass

                # System resource metrics
                try:
                    import psutil
                    metrics_data["cpu_percent"] = psutil.cpu_percent(interval=0)
                    mem = psutil.virtual_memory()
                    metrics_data["memory_percent"] = mem.percent
                except ImportError:
                    pass

                # Inference backend info
                metrics_data["inference_mode"] = config.cloud_mode or config.llm_backend
                metrics_data["invoke_url"] = invoke_url

                # Agent list
                agents_list = [{
                    "name": agent_name,
                    "invoke_url": invoke_url,
                    "status": "online",
                    "tenant_id": tenant_id,
                }]

                if fed_client:
                    result = await fed_client.heartbeat(
                        status="online",
                        metrics=metrics_data,
                        agents=agents_list,
                    )
                    if result.get("error"):
                        consecutive_failures += 1
                        logger.debug("Fleet heartbeat failed (%d): %s", consecutive_failures, result)
                    else:
                        consecutive_failures = 0
                        logger.debug("Fleet heartbeat OK")
                else:
                    # Fallback: direct HTTP heartbeat
                    import httpx
                    async with httpx.AsyncClient(timeout=15) as client:
                        await client.post(
                            f"{portal_url}/v1/agents/upsert",
                            json={
                                "name": agent_name,
                                "scope": {"visibility": "workspace", "tenant_id": tenant_id},
                                "invoke_url": invoke_url,
                                "status": "online",
                                "metrics": metrics_data,
                            },
                            headers={"Authorization": f"Bearer {api_key}"},
                        )
                    consecutive_failures = 0
                    logger.debug("Fleet heartbeat OK (direct)")

            except asyncio.CancelledError:
                break
            except (RuntimeError, OSError, ConnectionError) as exc:
                consecutive_failures += 1
                if consecutive_failures <= 3 or consecutive_failures % 10 == 0:
                    logger.warning("Fleet heartbeat error (%d): %s", consecutive_failures, exc)

    async def _sync_secrets():
        """Pull secrets from platform vault into local ADK store on startup."""
        try:
            from adk.sync.secrets import sync_secrets
            synced = await sync_secrets()
            if synced:
                logger.info("Synced %d secrets from vault", len(synced))
        except (ImportError, RuntimeError, OSError, ConnectionError, httpx.HTTPError) as exc:
            logger.debug("Secrets sync failed (non-fatal): %s", exc)

    # Prefix -> intent categories, using the SAME vocabulary the built-in tools use
    # (builtin_tools.py TOOL_INTENT_CATEGORIES): code, file, analysis, command, research,
    # web_research, question. Tagging is what lets _filter_tools_by_intent actually exclude
    # a tool — an untagged tool matches every intent and defeats the filter.
    _MCP_INTENT_PREFIXES = (
        (("codegraph_", "repowise_", "git_", "graph_code", "scope_", "acc_"), ["code", "analysis"]),
        (("fs_", "file_"), ["code", "file"]),
        (("web_", "search_", "research_", "fetch_", "context7_"), ["research", "web_research", "question"]),
        (("recall", "remember", "memory", "knowledge_", "graph_", "rag_", "query_"), ["analysis", "question"]),
        (("http_", "cf_", "cloudflare_", "docker", "k3s_", "hetzner_", "ring_"), ["command"]),
    )

    def _mcp_intent_categories(name: str) -> list:
        """Best-effort intent tags for a gateway tool, by name prefix."""
        lowered = name.lower()
        for prefixes, cats in _MCP_INTENT_PREFIXES:
            if lowered.startswith(prefixes):
                return list(cats)
        return ["analysis"]  # a real tag beats none: an untagged tool bypasses filtering

    def _prioritise_mcp_tools(specs: list) -> list:
        """Order gateway tools so the eager cap keeps the broadly-useful ones.

        This is a deliberate stopgap, not the end state: the right answer is runtime
        selection (tool search over the tool graph / MCTS planning / intent), so the agent
        retrieves from all ~1200 on demand instead of pre-loading any fixed slice.
        """
        preferred = (
            "codegraph_", "repowise_", "fs_", "git_", "web_", "search_", "recall",
            "remember", "query_", "knowledge_", "graph_", "http_",
        )
        head = [s for s in specs if str(s.get("name", "")).lower().startswith(preferred)]
        tail = [s for s in specs if s not in head]
        return head + tail

    def _nudge_mcp_attach() -> None:
        """A turn is starting while we have no platform tools — retry NOW, don't block.

        Closes most of the recovery window without putting the gateway on the turn's
        critical path. Measured before this existed: the live daemon sat detached for
        **115s** after the gateway came back, purely because it was mid-backoff, and every
        turn in that window silently ran on 7 built-in tools instead of 1,227.

        Deliberately does NOT await the attach. The failure being recovered from is a
        gateway that HANGS rather than refuses, so awaiting here would add that
        hang to every single turn — trading a degraded answer for no answer at all.
        """
        if _state.get("mcp_attach_permanent_failure"):
            return
        if _state.get("gateway_mcp_connected") and _state.get("mcp_tools_catalogue", 0) > 0:
            return
        ev = _state.get("mcp_retry_now")
        if ev is not None:
            ev.set()

    def _mark_mcp_detached(reason: str) -> None:
        """Record that platform tools are NOT usable right now.

        Both flags must move together. Leaving `mcp_tools_registered` at its last good
        value while the gateway is gone made /health report `mode: platform, registered:
        18` against a dead gateway for 140s in the flap test — a stale success is
        worse than no signal, because it is the signal a probe trusts.
        """
        _state["gateway_mcp_connected"] = False
        _state["mcp_tools_registered"] = 0
        _state["mcp_tools_catalogue"] = 0
        _state["mcp_last_error"] = reason
        # Drop the stale CATALOGUE too. Leaving it meant `search_tools` kept returning
        # 1,227 real-looking names against a dead gateway, so the model would pick one,
        # `call_tool` would fail at invoke time, and the model would burn its tool budget
        # retrying plausible-looking names that could not work. An empty catalogue makes
        # search_tools say "nothing available", which the model handles correctly by
        # answering from what it knows. The eager tool closures stay registered on the
        # agent by design — they re-point at a fresh client on the next attach, and
        # unregistering/re-registering 16 closures on every flap is churn for no gain.
        _state["all_mcp_tools"] = []

    async def _connect_local_mcp_if_available() -> bool:
        """Sovereign-mode tool access: attach to a LOCAL MCP gateway only.

        Offline mode must not mean tool-less. This targets a loopback gateway
        (AITHER_MCP_GATEWAY, default 127.0.0.1:8182) and REFUSES anything non-loopback, so
        turning on sovereign mode can never silently start talking to the cloud. Entirely
        fail-soft: if the local gateway is down the daemon still serves with its built-in
        tools, and says so.

        Returns True when the gateway is attached and tools are registered. The caller
        (`_supervise_local_mcp`) retries on False: this used to run exactly
        once at boot, so a daemon that started during a gateway flap ran with 7 built-in
        tools for its ENTIRE lifetime and looked merely "not very capable".
        """
        _state["mcp_attach_attempts"] = _state.get("mcp_attach_attempts", 0) + 1
        target = os.getenv("AITHER_MCP_GATEWAY", "127.0.0.1:8182").strip()
        if not target:
            _state["mcp_last_error"] = "AITHER_MCP_GATEWAY is empty"
            return False
        if "://" not in target:
            target = f"http://{target}"
        host = target.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
        if host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
            logger.info(
                "Sovereign mode: refusing non-loopback MCP gateway %s — built-in tools only",
                target,
            )
            # A config choice, not a transient fault — retrying can never fix it.
            _state["mcp_last_error"] = f"non-loopback gateway refused: {target}"
            _state["mcp_attach_permanent_failure"] = True
            return False
        try:
            from adk.client._gateway_mcp import create_gateway_mcp_client

            mcp_client = await create_gateway_mcp_client(
                gateway_url=target,
                api_key=config.aither_api_key or os.getenv("AITHER_INTERNAL_KEY", ""),
            )
            if not mcp_client:
                logger.info("Sovereign mode: local MCP gateway %s unavailable — built-in tools only", target)
                _mark_mcp_detached(f"gateway {target} unavailable")
                return False
            _state["gateway_mcp_client"] = mcp_client
            # NOTE: `gateway_mcp_connected` is deliberately NOT set here. Listing and
            # registering 1,227 tools takes ~11s, and flipping the flag on connect made
            # /health report `mode: platform, registered: 0` for that whole window —
            # measured 2026-07-29 during the flap test. The flag is set at the
            # bottom, once tools are actually usable, so "attached" never means
            # "connected but inert".
            tools = await mcp_client.list_tools()
            # An EMPTY catalogue is a FAILED attach, not a small one. `list_tools()` returns
            # [] for unreachable/401/402/4xx alike, and the two meta-tools below register
            # unconditionally — so without this guard the daemon reported
            # `mode: platform, registered: 2, catalogue: 0`: attached to nothing, offering a
            # `search_tools` that searches an empty list. Measured in the flap test.
            if not tools:
                logger.info(
                    "Sovereign mode: local MCP gateway %s listed 0 tools — built-in tools only",
                    target,
                )
                _mark_mcp_detached(f"gateway {target} listed 0 tools")
                return False
            # REGISTER them with the agent. Listing alone is worthless: the first version of
            # this logged "1192 tools" while the agent answered with tool_calls=0, because
            # nothing was ever wired into the tool registry — a textbook inert feature that
            # reads as parity in the logs.
            #
            # But registering them FLAT is just as broken, and measurably so: 7 built-in
            # tools answered a trivial turn in 4.4s, while 1227 flat-registered tools made
            # the SAME turn time out at 230s — every request carries every schema.
            # `_filter_tools_by_intent` (agent.py:146) already narrows at runtime, but it
            # treats a tool with NO intent_categories as always-matching, so untagged tools
            # defeat it entirely. Two things are therefore required:
            #   1. TAG each MCP tool so intent filtering can actually exclude it.
            #   2. CAP the eager set, because that filter FAILS OPEN when intent is None —
            #      without a cap a no-intent turn is back to shipping every schema.
            # Cache the full tool catalogue for runtime retrieval (meta-tools).
            # This enables search_tools and call_tool to access all ~1227 tools on demand
            # without shipping every schema in every request.
            _state["all_mcp_tools"] = tools

            # Register meta-tools first: search_tools and call_tool enable on-demand
            # retrieval instead of pre-loading all ~1227 schemas (latency cliff:
            # 64 eager tools = 29.9s, 1227 eager = 230s timeout).
            registered = 0
            a = await get_agent()
            if a:
                # Register search_tools: find tools by query
                from adk.tools_meta import search_tools as search_tools_impl

                async def search_tools(query: str = "", limit: int = 8) -> str:
                    return search_tools_impl(query, _state.get("all_mcp_tools", []), limit)

                search_tools.__doc__ = (
                    "Search available tools by name and description. Returns "
                    "top matches with name and description. Use this to discover "
                    "specialized tools for your task."
                )
                try:
                    a._tools.register(
                        search_tools,
                        name="search_tools",
                        description=(
                            "Search for MCP tools by query. Returns name, description "
                            "of up to 8 matching tools. Essential for finding tools not "
                            "in the eager-loaded core."
                        ),
                        intent_categories=["analysis", "code", "research", "question"],
                    )
                    registered += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning("Failed to register search_tools: %s", e)

                # Register call_tool: invoke any tool by name
                from adk.tools_meta import call_tool as call_tool_impl

                async def call_tool(name: str, arguments: dict | None = None) -> str:
                    return await call_tool_impl(
                        name, arguments or {}, _state.get("gateway_mcp_client")
                    )

                call_tool.__doc__ = (
                    "Call a tool by name with arguments. Use search_tools first to "
                    "find the tool name and signature."
                )
                try:
                    a._tools.register(
                        call_tool,
                        name="call_tool",
                        description=(
                            "Call any MCP tool by name without pre-registration. "
                            "Pair with search_tools to find and invoke tools on demand."
                        ),
                        intent_categories=["analysis", "code", "research", "question", "command"],
                    )
                    registered += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning("Failed to register call_tool: %s", e)

            # Register eager-loaded core tools (reduced from 64 to 16 based on
            # latency measurements: knee point is ~16 tools before curve flattens).
            max_tools = int(os.getenv("ADK_MCP_MAX_TOOLS", "16"))
            if a:
                for tool_spec in _prioritise_mcp_tools(tools)[:max_tools]:
                    tool_name = tool_spec.get("name") or ""
                    if not tool_name:
                        continue

                    async def _local_tool_call(tn=tool_name, **kwargs) -> str:
                        result = await mcp_client.call_tool(tn, kwargs)
                        if result.get("success"):
                            return result.get("text", "")
                        return f"Error: {result.get('message', 'unknown')}"

                    _local_tool_call.__name__ = tool_name
                    _local_tool_call.__doc__ = tool_spec.get("description", "")
                    try:
                        a._tools.register(
                            _local_tool_call,
                            name=tool_name,
                            description=tool_spec.get("description", ""),
                            intent_categories=_mcp_intent_categories(tool_name),
                        )
                        registered += 1
                    except Exception:  # noqa: BLE001 — one bad spec must not drop the rest
                        continue
            logger.info(
                "Sovereign mode: local MCP gateway %s connected — %d tools listed, "
                "%d REGISTERED (%d eager + 2 meta-tools for runtime retrieval)",
                target, len(tools), registered, max_tools,
            )
            _state["mcp_tools_registered"] = registered
            _state["mcp_tools_catalogue"] = len(tools)
            # Registering zero tools is NOT an attachment: it is the inert-feature failure
            # (security-review-patterns #5) wearing a success log. Make the supervisor retry.
            if registered <= 0:
                _mark_mcp_detached(f"gateway {target} listed {len(tools)} tools, registered 0")
                return False
            _state["gateway_mcp_connected"] = True
            _state["mcp_last_error"] = ""
            return True
        except Exception as exc:  # noqa: BLE001 — tools are optional, serving is not
            logger.info("Sovereign mode: local MCP gateway unreachable (%s) — built-in tools only", exc)
            _mark_mcp_detached(f"{type(exc).__name__}: {exc}")
            return False

    async def _supervise_local_mcp():
        """Keep re-attempting the local MCP attachment until it succeeds, then watch it.

        The gateway FLAPS — measured 2026-07-29, `curl -m 8` returned nothing and
        four minutes later the identical endpoint answered 200 in 0.0026s, while the
        container read `Up 2 hours (healthy)` throughout. A one-shot attach at boot turns
        that momentary flap into a permanent capability loss for the daemon's whole
        lifetime: 1,227 platform tools silently become 7 built-in ones. That produced two
        FALSE "silently non-functional" verdicts in one verification run, because the
        feature under test was fine and the attachment was not.

        This is ONE loop covering both lifecycle halves, deliberately. It was briefly two
        (a fast retry task + a slow watchdog task) and they raced: the watchdog attached at
        16:41:25, the retry task woke from its backoff sleep at 16:42:39 and re-attached
        against a by-then-dead gateway, which tore down a working attachment and left
        /health asserting `platform, registered: 18` at nothing. Two loops sharing one
        piece of mutable state is the bug; one loop cannot race itself.

        Backoff while detached is bounded and then CONSTANT rather than giving up: the
        failure being recovered from is "the gateway is down right now", which has no
        deadline. One `initialize` round-trip every 2 minutes is negligible against a
        loopback service.
        """
        detached_delay = 5.0
        # 30s, not the 300s a list_tools()-based probe forced: an 8ms/36-byte ping means the
        # stale window costs nothing to shrink by 10x, and /health can no longer assert
        # `platform` at a gateway that died four minutes ago.
        attached_interval = float(os.getenv("ADK_MCP_WATCH_INTERVAL", "30"))
        while True:
            if _state.get("mcp_attach_permanent_failure"):
                return  # misconfiguration, not a flap — retrying cannot help

            # Key on the ATTACH VERDICT, not on `gateway_mcp_connected` alone: that flag is
            # also written by the cloud `_connect_gateway_mcp` path, and keying this on
            # someone else's flag is how "connected but zero tools" reads as a success.
            attached = (
                bool(_state.get("gateway_mcp_connected"))
                and _state.get("mcp_tools_registered", 0) > 0
                and _state.get("mcp_tools_catalogue", 0) > 0
            )

            if attached:
                await asyncio.sleep(attached_interval)
                client = _state.get("gateway_mcp_client")
                if not client:
                    _mark_mcp_detached("client handle vanished")
                    continue
                # Probe with MCP `ping`, NOT `list_tools()` and NOT the gateway's /health:
                #   - /health answers 200 while /mcp is unresponsive on this very gateway —
                #     that lie is how the flap stayed invisible;
                #   - list_tools() costs 3.2s and 366 KB (1,227 schemas), too heavy to run
                #     on a watch interval, which forced a 300s window in which /health could
                #     assert `platform` at a gateway that had already died;
                #   - ping measures 8ms / 36 bytes on the same session and endpoint.
                # `ping()` returns False rather than raising on every failure path, so the
                # `except` here is belt-and-braces, not the detection mechanism — the first
                # version of this watchdog wrapped `list_tools()` in a try/except that could
                # NEVER fire (it returns [] instead of raising) and was therefore itself
                # inert: security-review-patterns #5, committed by the check meant to catch it.
                try:
                    alive = await client.ping()
                except Exception as exc:  # noqa: BLE001 — a dead client must not kill the loop
                    alive = False
                    _state["mcp_last_error"] = f"probe raised: {type(exc).__name__}: {exc}"
                if not alive:
                    logger.warning("Sovereign mode: MCP gateway went away — re-attaching")
                    _mark_mcp_detached(
                        _state.get("mcp_last_error") or "gateway ping failed"
                    )
                    detached_delay = 5.0  # recover fast from a flap, as at boot
                continue

            # Interruptible backoff: a turn arriving while detached sets this event and we
            # retry immediately instead of serving it tool-less for up to the full delay.
            ev = _state.get("mcp_retry_now")
            if ev is not None:
                try:
                    await asyncio.wait_for(ev.wait(), timeout=detached_delay)
                    detached_delay = 5.0  # a live caller wants tools — retry eagerly again
                except asyncio.TimeoutError:
                    detached_delay = min(detached_delay * 2, 30.0)
                ev.clear()
            else:
                await asyncio.sleep(detached_delay)
                detached_delay = min(detached_delay * 2, 30.0)
            if await _connect_local_mcp_if_available():
                logger.info(
                    "Sovereign mode: MCP gateway attached on attempt #%d — %d tools registered",
                    _state.get("mcp_attach_attempts", 0),
                    _state.get("mcp_tools_registered", 0),
                )

    async def _connect_gateway_mcp():
        """Connect MCP client to gateway for platform tool access.

        Self-hosted agents use this to access platform tools (code search, memory,
        secrets, etc.) without running the full AitherOS stack locally.

        Authentication hierarchy:
          1. AITHER_API_KEY (device-flow token, ACTA key, or Identity bearer)
          2. Local AitherSecrets self-mint (fallback via fleet_enroll)

        Fail-soft: no token or connection failure logs warning, agent runs offline.
        """
        if not config.aither_api_key:
            logger.debug("Gateway MCP: no API key configured (running offline)")
            return

        try:
            from adk.client._gateway_mcp import create_gateway_mcp_client

            gateway_url = config.gateway_url or os.getenv("AITHER_GATEWAY_URL", "")
            if not gateway_url:
                gateway_url = "https://mcp.aitherium.com"

            # Create and test connection (never crashes startup)
            mcp_client = await create_gateway_mcp_client(
                gateway_url=gateway_url,
                api_key=config.aither_api_key,
            )

            if mcp_client:
                _state["gateway_mcp_client"] = mcp_client
                _state["gateway_mcp_connected"] = True
                logger.info("Gateway MCP client connected: %s", gateway_url)

                # Register gateway tools in agent (best-effort, async)
                # Use the same meta-tools + eager-core strategy as sovereign mode
                try:
                    a = await get_agent()
                    if a:
                        tools = await mcp_client.list_tools()
                        # Cache the full tool catalogue for runtime retrieval
                        _state["all_mcp_tools"] = tools

                        # Register meta-tools (search_tools, call_tool)
                        from adk.tools_meta import search_tools as search_tools_impl

                        async def search_tools(query: str = "", limit: int = 8) -> str:
                            return search_tools_impl(
                                query, _state.get("all_mcp_tools", []), limit
                            )

                        search_tools.__doc__ = (
                            "Search available tools by name and description. Returns "
                            "top matches. Essential for finding tools not in the eager core."
                        )
                        try:
                            a._tools.register(
                                search_tools,
                                name="search_tools",
                                description=(
                                    "Search for MCP tools by query. Returns name, "
                                    "description of up to 8 matches."
                                ),
                                intent_categories=[
                                    "analysis", "code", "research", "question"
                                ],
                            )
                        except Exception as e:  # noqa: BLE001
                            logger.debug("Failed to register search_tools: %s", e)

                        from adk.tools_meta import call_tool as call_tool_impl

                        async def call_tool(
                            name: str, arguments: dict | None = None
                        ) -> str:
                            return await call_tool_impl(name, arguments or {}, mcp_client)

                        call_tool.__doc__ = (
                            "Call a tool by name. Pair with search_tools to find tools."
                        )
                        try:
                            a._tools.register(
                                call_tool,
                                name="call_tool",
                                description=(
                                    "Call any MCP tool by name without pre-registration."
                                ),
                                intent_categories=[
                                    "analysis", "code", "research", "question", "command"
                                ],
                            )
                        except Exception as e:  # noqa: BLE001
                            logger.debug("Failed to register call_tool: %s", e)

                        # Register eager-loaded core (reduced to 16)
                        max_tools = int(os.getenv("ADK_MCP_MAX_TOOLS", "16"))
                        registered = 2  # search_tools + call_tool
                        for tool_spec in tools[:max_tools]:
                            tool_name = tool_spec.get("name", "").strip()
                            if not tool_name:
                                continue

                            async def _gateway_tool_call(
                                tn=tool_name, **kwargs
                            ) -> str:
                                result = await mcp_client.call_tool(tn, kwargs)
                                if result.get("success"):
                                    return result.get("text", "")
                                return f"Error: {result.get('message', 'unknown')}"

                            _gateway_tool_call.__name__ = tool_name
                            _gateway_tool_call.__doc__ = tool_spec.get("description", "")
                            try:
                                a._tools.register(
                                    _gateway_tool_call,
                                    name=tool_name,
                                    description=tool_spec.get("description", ""),
                                    intent_categories=_mcp_intent_categories(tool_name),
                                )
                                registered += 1
                            except Exception:  # noqa: BLE001
                                continue

                        logger.info(
                            "Registered %d gateway tools with agent %s "
                            "(%d eager + 2 meta-tools)",
                            registered, a.name, max_tools,
                        )
                except Exception as exc:  # noqa: BLE001 — tool registration is advisory
                    logger.debug("Gateway tool registration failed (non-fatal): %s", exc)
            else:
                _state["gateway_mcp_connected"] = False
                logger.debug("Gateway MCP connection failed (running offline)")
        except ImportError:
            logger.debug("Gateway MCP unavailable (continuing offline)")
        except Exception as exc:  # noqa: BLE001 — must never crash startup
            _state["gateway_mcp_connected"] = False
            logger.warning("Gateway MCP init failed (continuing offline): %s", exc)

    async def _register_with_gateway():
        if not config.gateway_url or not config.aither_api_key:
            logger.debug("Gateway auto-registration skipped (not configured)")
            return
        if not config.register_agent:
            logger.debug("Gateway auto-registration skipped (AITHER_REGISTER_AGENT not set)")
            _state["gateway_connected"] = False
            return
        try:
            from adk.client import GatewayClient
            gw = GatewayClient(gateway_url=config.gateway_url, api_key=config.aither_api_key)
            ident = load_identity(identity)

            # Get owner email for registration (required by gateway contract)
            owner_email = os.getenv("AITHER_OWNER_EMAIL", "").strip()
            if not owner_email:
                # Try to extract from saved config
                from adk.config import load_saved_config
                saved = load_saved_config()
                owner_email = saved.get("billing_email", "") or saved.get("email", "")

            if not owner_email:
                logger.warning(
                    "Gateway registration skipped: AITHER_OWNER_EMAIL not set "
                    "(required by gateway contract)"
                )
                _state["gateway_connected"] = False
                return

            result = await gw.register_agent(
                name=ident.name,
                owner_email=owner_email,
                description=ident.description,
                framework="adk",
            )
            _state["gateway_connected"] = True
            logger.info("Registered with gateway %s: %s", config.gateway_url, result)
        except Exception as exc:  # noqa: BLE001 — registration is ALWAYS best-effort;
            # a bad signature / gateway hiccup must never take down the agent server.
            _state["gateway_connected"] = False
            logger.warning("Gateway registration failed (non-fatal): %s", exc)

    async def _join_aithernet():
        """Auto-join the AitherNet mesh relay if API key is configured."""
        if not config.aither_api_key:
            return
        try:
            from adk.relay import get_relay
            agent_names = []
            if is_fleet and _state.get("fleet"):
                agent_names = [a.name for a in _state["fleet"].agents]
            elif _state.get("agent"):
                agent_names = [_state["agent"].name]

            relay = get_relay(
                api_key=config.aither_api_key,
                gateway_url=config.gateway_url or "",
                node_name=os.getenv("AITHER_NODE_NAME", ""),
                agents=agent_names,
                capabilities=_detect_node_capabilities(),
                port=config.server_port,
            )
            result = await relay.register()
            if result.get("ok") is not False:
                _state["relay"] = relay
                await relay.start_heartbeat(interval=60)
                logger.info(
                    "Joined AitherNet mesh as %s (node_id=%s, agents=%s)",
                    relay.node_name, relay.node_id[:12], agent_names,
                )
        except (ImportError, RuntimeError, OSError, ConnectionError, httpx.HTTPError) as exc:
            logger.debug("AitherNet join failed (non-fatal): %s", exc)

    async def _rich_enroll_identity():
        """Register with AitherIdentity's rich node spine (separate from the AitherNet
        mesh relay joined by _join_aithernet, above — that's presence/messaging; this
        is identity + capability-token issuance). On success, mints a tenant-scoped
        bearer_token and self-services a real avk_... gateway key via AitherSecrets,
        replacing the node's reliance on the user's own access token for follow-up
        calls. Non-fatal: enrollment.rich_enroll() never raises.
        """
        if not config.aither_api_key:
            return
        try:
            import platform
            from adk import enrollment
            from adk import fleet_enroll

            idp_url = os.getenv("AITHER_IDP_URL", os.getenv("AITHER_IDP_BASE_URL", "https://idp.aitherium.com"))
            relay = _state.get("relay")
            node_id = relay.node_id if relay else os.getenv("AITHER_NODE_NAME", "") or platform.node()

            result = await enrollment.rich_enroll(
                idp_url, config.aither_api_key, node_id, enable_heartbeat=True,
            )
            if not result.get("enrolled"):
                logger.debug("Identity enrollment skipped/failed (non-fatal): %s", result.get("error"))
                return

            logger.info(
                "Identity-enrolled with AitherNet as %s (tenant=%s)",
                node_id[:12], result.get("tenant_id", ""),
            )

            bearer_token = result.get("bearer_token", "")
            if bearer_token:
                await fleet_enroll._self_mint_gateway_key(bearer_token, node_id)
        except (ImportError, RuntimeError, OSError, ConnectionError, httpx.HTTPError) as exc:
            logger.debug("Identity enrollment failed (non-fatal): %s", exc)

    def _detect_node_capabilities() -> list[str]:
        """Detect what this node can do."""
        caps = ["chat", "tools", "mcp", "a2a", "irc", "smtp"]
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                caps.append("inference")
                caps.append("gpu")
        except (FileNotFoundError, OSError):
            pass
        if is_fleet:
            caps.append("fleet")
        return caps

    # ─── Elysium reconnect + mesh hosting ───

    async def _reconnect_elysium():
        """Re-join desktop mesh on startup if previously connected via `adk connect --elysium`."""
        from adk.config import load_saved_config  # noqa: F811
        try:
            saved = load_saved_config()
            elysium_url = saved.get("elysium_url", "")
            if not elysium_url:
                return

            node_token = saved.get("node_token", "")
            mesh_url = saved.get("mesh_url", "")

            # Set env vars for LLM router dual-mode
            core_llm = saved.get("core_llm_url", "")
            if core_llm:
                os.environ.setdefault("AITHER_CORE_LLM_URL", core_llm)
            if node_token:
                os.environ.setdefault("AITHER_NODE_TOKEN", node_token)

            # Re-join mesh
            if mesh_url:
                import httpx
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        await client.post(
                            f"{mesh_url}/heartbeat",
                            json={"node_id": saved.get("node_id", ""), "status": "online"},
                            headers={
                                "Authorization": f"Bearer {node_token}" if node_token else "",
                                "Content-Type": "application/json",
                            },
                        )
                    logger.info("Reconnected to desktop mesh at %s", mesh_url)
                except (httpx.HTTPError, OSError) as e:
                    logger.debug("Desktop mesh reconnect failed (non-fatal): %s", e)

        except (OSError, ValueError) as e:
            logger.debug("Elysium reconnect skipped: %s", e)

    async def _start_mesh_hosting():
        """Start mesh hosting if --mesh flag or config mesh_enabled is set."""
        from adk.config import load_saved_config  # noqa: F811
        mesh_enabled = os.getenv("AITHER_MESH_ENABLED", "").lower() in ("true", "1", "yes")
        if not mesh_enabled:
            try:
                saved = load_saved_config()
                mesh_enabled = saved.get("mesh_enabled", False)
            except (OSError, ValueError):
                pass

        if not mesh_enabled:
            return

        try:
            from adk.relay import AitherNetRelay  # noqa: F401

            saved = load_saved_config()
            base_host = saved.get("elysium_base_host", "")
            node_token = saved.get("node_token", "")

            # Create relay pointed at desktop (not cloud gateway)
            relay_kwargs = {
                "node_name": os.getenv("AITHER_NODE_NAME", ""),
                "capabilities": _detect_node_capabilities(),
                "port": config.server_port,
            }
            if base_host:
                relay_kwargs["gateway_url"] = f"{base_host}:8001"
            if node_token:
                relay_kwargs["api_key"] = node_token

            relay = AitherNetRelay(**relay_kwargs)
            result = await relay.register()

            if result.get("ok") is not False:
                # Wire MCP server for inbound tool calls
                mcp = _state.get("mcp_server")
                if mcp:
                    relay.set_local_mcp_server(mcp)

                await relay.start_heartbeat(interval=60)
                await relay.connect_relay_hub()
                _state["elysium_relay"] = relay
                logger.info(
                    "Mesh hosting active: node=%s, capabilities=%s",
                    relay.node_id[:12], relay.capabilities,
                )
        except (ImportError, OSError, ConnectionError, ValueError) as e:
            logger.debug("Mesh hosting startup failed (non-fatal): %s", e)

    # ─── Chat relay startup + endpoints ───

    async def _init_chat_relay():
        """Initialize the chat relay and wire federation handlers."""
        try:
            from adk.chat import get_chat_relay
            relay_obj = _state.get("relay")
            node_id = relay_obj.node_id if relay_obj else ""
            chat = get_chat_relay(node_id=node_id)
            _state["chat_relay"] = chat

            # Register agents as chat participants
            if is_fleet and _state.get("fleet"):
                for a in _state["fleet"].agents:
                    chat.register_agent(a.name)
            elif _state.get("agent"):
                chat.register_agent(_state["agent"].name)

            # Wire federation: relay mesh "chat" messages → local chat
            if relay_obj:
                relay_obj.on("chat", chat.handle_federated_message)
                relay_obj.on("mail", lambda data: _handle_mesh_mail(data))

            # Start raw IRC protocol server (opt-in via AITHER_IRC_PORT)
            irc_port_env = os.getenv("AITHER_IRC_PORT", "")
            if irc_port_env:
                try:
                    irc_port = int(irc_port_env)
                    await chat.start_irc_server(port=irc_port)
                    logger.info("IRC server listening on port %d", irc_port)
                except (RuntimeError, OSError) as irc_exc:
                    logger.debug("IRC server startup failed (non-fatal): %s", irc_exc)

                # Start Aither bridge only when IRC is enabled
                try:
                    from adk.aither_bridge import init_aither_bridge
                    bridge = await init_aither_bridge(chat)
                    if bridge:
                        _state["aither_bridge"] = bridge
                        logger.info("Aither bridge active")
                except (ImportError, RuntimeError, OSError) as bridge_exc:
                    logger.debug("Aither bridge startup failed (non-fatal): %s", bridge_exc)

            logger.info("Chat relay initialized (channels=%d)", len(chat._channels))
        except (ImportError, RuntimeError, OSError) as exc:
            logger.debug("Chat relay init failed (non-fatal): %s", exc)

    async def _init_mail_relay():
        """Initialize the mail relay."""
        try:
            from adk.smtp import get_mail_relay
            relay_obj = _state.get("relay")
            node_id = relay_obj.node_id if relay_obj else ""
            mail = get_mail_relay(node_id=node_id)
            _state["mail_relay"] = mail

            # Auto-provision mailboxes for agents
            if is_fleet and _state.get("fleet"):
                for a in _state["fleet"].agents:
                    mail.provision_mailbox(a.name)
            elif _state.get("agent"):
                mail.provision_mailbox(_state["agent"].name)

            # Start inbound SMTP listener (non-fatal)
            smtp_port = int(os.getenv("AITHER_SMTP_PORT", "2525"))
            try:
                started = await mail.start_inbound_server(port=smtp_port)
                if started:
                    logger.info("Inbound SMTP server started on port %d", smtp_port)
            except (RuntimeError, OSError) as smtp_exc:
                logger.debug("Inbound SMTP server startup failed (non-fatal): %s", smtp_exc)

            logger.info("Mail relay initialized (configured=%s)", mail.is_configured)
        except (ImportError, RuntimeError, OSError) as exc:
            logger.debug("Mail relay init failed (non-fatal): %s", exc)

    async def _init_relay_client():
        """Initialize ChatRelayClient so this node can be a first-class relay
        participant. Requires RELAY_URL and AITHER_BEARER env vars.
        Registers agents as mention handlers that dispatch to agent.chat().
        """
        relay_url = os.getenv("RELAY_URL", "")
        aither_bearer = os.getenv("AITHER_BEARER", "")
        if not relay_url or not aither_bearer:
            logger.debug(
                "Relay client skipped (RELAY_URL or AITHER_BEARER not set)"
            )
            return

        try:
            from adk.relay_client import get_relay_client

            workspace_id = os.getenv("RELAY_WORKSPACE_ID", "")
            client = get_relay_client(
                base_url=relay_url,
                aither_bearer=aither_bearer,
                workspace_id=workspace_id or None,
                user_id=_state.get("identity", identity),
                nick=_state.get("identity", identity),
            )

            # Register mention handlers to dispatch to agents
            if is_fleet and _state.get("fleet"):
                for a in _state["fleet"].agents:

                    def _make_handler(agent):
                        async def handle(msg):
                            try:
                                # Strip @mentions for natural input
                                txt = msg.content
                                for nick in (ag.name for ag in _state["fleet"].agents):
                                    txt = txt.replace(f"@{nick}", "").strip()
                                resp = await agent.chat(
                                    txt,
                                    session_id=f"relay:{msg.channel}:{msg.nick}",
                                )
                                return resp.content
                            except Exception as exc:
                                logger.error(
                                    "Mention handler error for @%s: %s",
                                    agent.name, exc,
                                )
                                return None

                        return handle

                    client.register_mention_handler(a.name, _make_handler(a))
            elif _state.get("agent"):
                a = _state["agent"]

                async def _default_mention_handler(msg):
                    try:
                        # Strip @nick from message
                        txt = msg.content.replace(f"@{a.name}", "").strip()
                        resp = await a.chat(
                            txt,
                            session_id=f"relay:{msg.channel}:{msg.nick}",
                        )
                        return resp.content
                    except Exception as exc:
                        logger.error(
                            "Mention handler error for @%s: %s",
                            a.name, exc,
                        )
                        return None

                client.register_mention_handler(a.name, _default_mention_handler)

            # Connect and store on app state
            if await client.connect():
                _state["relay_client"] = client
                logger.info("Relay client connected as %s", client.nick)
            else:
                logger.warning("Relay client connection failed (non-fatal)")
        except (ImportError, RuntimeError, OSError, ConnectionError) as exc:
            logger.debug("Relay client init failed (non-fatal): %s", exc)

    def _handle_mesh_mail(data: dict):
        """Handle incoming mail from the mesh relay."""
        try:
            mail = _state.get("mail_relay")
            if mail:
                mail.receive_mesh_mail(data)
        except (RuntimeError, OSError) as exc:
            logger.debug("Mesh mail handler error: %s", exc)

    # ── Chat WebSocket ──

    @app.websocket("/ws/chat")
    async def ws_chat(websocket: WebSocket):
        """WebSocket endpoint for real-time chat (IRC-compatible)."""
        chat = _state.get("chat_relay")
        if not chat:
            await websocket.close(code=4000, reason="Chat relay not initialized")
            return

        await websocket.accept()
        nick = f"user_{uuid.uuid4().hex[:6]}"

        try:
            # Wait for join message with nick
            init_data = await asyncio.wait_for(websocket.receive_json(), timeout=10)
            if init_data.get("type") == "join":
                nick = init_data.get("nick", nick)
                channel = init_data.get("channel", "#general")
            else:
                channel = "#general"

            chat.connect_ws(nick, websocket)
            chat.join(channel, nick)

            # Send channel history
            history = chat.history(channel, limit=50)
            await websocket.send_json({"type": "history", "channel": channel, "messages": history})

            # Message loop
            while True:
                data = await websocket.receive_json()
                await chat.handle_ws_message(nick, data)

        except WebSocketDisconnect:
            pass
        except asyncio.TimeoutError:
            pass
        except (RuntimeError, OSError, ConnectionError) as exc:
            logger.debug("WebSocket chat error: %s", exc)
        finally:
            chat.disconnect_ws(nick)
            # Part all channels
            user = chat._users.get(nick)
            if user:
                for ch in list(user.channels):
                    chat.part(ch, nick)

    # ── Chat REST endpoints ──

    @app.get("/chat/channels")
    async def chat_channels():
        """List available chat channels."""
        chat = _state.get("chat_relay")
        if not chat:
            return {"channels": []}
        return {"channels": chat.list_channels()}

    @app.get("/chat/channels/{channel}/history")
    async def chat_channel_history(channel: str, limit: int = 50):
        """Get message history for a channel."""
        chat = _state.get("chat_relay")
        if not chat:
            return {"messages": []}
        ch = f"#{channel}" if not channel.startswith("#") else channel
        return {"channel": ch, "messages": chat.history(ch, limit=limit)}

    @app.get("/chat/channels/{channel}/users")
    async def chat_channel_users(channel: str):
        """List users in a channel."""
        chat = _state.get("chat_relay")
        if not chat:
            return {"users": []}
        ch = f"#{channel}" if not channel.startswith("#") else channel
        return {"channel": ch, "users": chat.who(ch)}

    @app.post("/chat/channels/{channel}/message")
    async def chat_post_message(channel: str, request: Request):
        """Post a message to a channel (REST alternative to WebSocket)."""
        chat = _state.get("chat_relay")
        if not chat:
            return JSONResponse({"error": "Chat relay not initialized"}, status_code=503)
        body = await request.json()
        nick = body.get("nick", body.get("from", "api"))
        content = body.get("content", body.get("message", ""))
        if not content:
            return JSONResponse({"error": "content is required"}, status_code=400)

        ch = f"#{channel}" if not channel.startswith("#") else channel
        msg = chat.post(ch, nick, content)

        # Federate to mesh
        if msg and _state.get("relay"):
            asyncio.ensure_future(chat.federate_message(msg))

        return {"ok": bool(msg), "msg_id": msg.msg_id if msg else None}

    @app.get("/chat/users")
    async def chat_online_users():
        """List all online users across channels."""
        chat = _state.get("chat_relay")
        if not chat:
            return {"users": []}
        return {"users": chat.online_users()}

    @app.get("/chat/status")
    async def chat_status():
        """Chat relay status."""
        chat = _state.get("chat_relay")
        if not chat:
            return {"active": False, "message": "Chat relay not initialized"}
        return {**chat.status(), "active": True}

    @app.get("/bridge/status")
    async def bridge_status():
        """Aither ↔ IRC bridge status."""
        bridge = _state.get("aither_bridge")
        if not bridge:
            return {"active": False, "message": "Aither bridge not running"}
        return {**bridge.status(), "active": True}

    # ── Mail REST endpoints ──

    @app.post("/mail/send")
    async def mail_send(request: Request):
        """Send an email (queued for delivery)."""
        mail = _state.get("mail_relay")
        if not mail:
            return JSONResponse({"error": "Mail relay not initialized"}, status_code=503)
        body = await request.json()
        result = await mail.send(
            to=body.get("to", ""),
            subject=body.get("subject", ""),
            body=body.get("body", ""),
            html=body.get("html", ""),
            from_addr=body.get("from", ""),
            agent=body.get("agent", ""),
            attachments=body.get("attachments"),
        )
        return result

    @app.get("/mail/inbox")
    async def mail_inbox(agent: str = "", limit: int = 50):
        """Get received emails."""
        mail = _state.get("mail_relay")
        if not mail:
            return {"emails": []}
        return {"emails": mail.inbox(agent=agent, limit=limit)}

    @app.get("/mail/sent")
    async def mail_sent(agent: str = "", limit: int = 50):
        """Get sent/queued emails."""
        mail = _state.get("mail_relay")
        if not mail:
            return {"emails": []}
        return {"emails": mail.sent(agent=agent, limit=limit)}

    @app.get("/mail/email/{email_id}")
    async def mail_get_email(email_id: str):
        """Get email by ID."""
        mail = _state.get("mail_relay")
        if not mail:
            return JSONResponse({"error": "not_found"}, status_code=404)
        email_obj = mail.get_email(email_id)
        if not email_obj:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return email_obj

    @app.post("/mail/config")
    async def mail_configure(request: Request):
        """Configure SMTP settings."""
        mail = _state.get("mail_relay")
        if not mail:
            return JSONResponse({"error": "Mail relay not initialized"}, status_code=503)
        body = await request.json()
        mail.configure(**body)
        return {"ok": True, "config": mail.get_config()}

    @app.get("/mail/config")
    async def mail_get_config():
        """Get SMTP configuration (password redacted)."""
        mail = _state.get("mail_relay")
        if not mail:
            return {"configured": False}
        return mail.get_config()

    @app.get("/mail/providers")
    async def mail_providers():
        """List available SMTP provider presets."""
        from adk.smtp import PROVIDER_PRESETS
        return {"providers": PROVIDER_PRESETS}

    @app.post("/mail/mailbox/provision")
    async def mail_provision_mailbox(request: Request):
        """Provision a mailbox for a user or agent."""
        mail = _state.get("mail_relay")
        if not mail:
            return JSONResponse({"error": "Mail relay not initialized"}, status_code=503)
        body = await request.json()
        return mail.provision_mailbox(
            username=body.get("username", ""),
            email_address=body.get("email_address", ""),
            display_name=body.get("display_name", ""),
            domain=body.get("domain", ""),
        )

    @app.get("/mail/mailboxes")
    async def mail_list_mailboxes():
        """List all provisioned mailboxes."""
        mail = _state.get("mail_relay")
        if not mail:
            return {"mailboxes": []}
        return {"mailboxes": mail.list_mailboxes()}

    @app.get("/mail/mailbox/{username}/inbox")
    async def mail_user_inbox(username: str, limit: int = 50):
        """Get inbox for a specific user/agent."""
        mail = _state.get("mail_relay")
        if not mail:
            return {"emails": []}
        return {"emails": mail.inbox(agent=username, limit=limit)}

    @app.get("/mail/status")
    async def mail_status():
        """Mail relay status."""
        mail = _state.get("mail_relay")
        if not mail:
            return {"active": False, "message": "Mail relay not initialized"}
        return {**mail.status(), "active": True}

    # ─── Fleet power control (/fleet/power/*) ───
    #
    # Registered HERE, in the unconditional app builder, and deliberately not
    # beside the workspace routers. That block lives inside
    # _mount_workspace_routers, which RETURNS EARLY when portal-kit-backend is
    # absent and is itself called conditionally -- so mounting there put fleet
    # stop/start behind an unrelated feature gate and it silently 404'd. Fleet
    # power must be reachable exactly when the rest of the fleet is not.
    #
    # Imported rather than vendored: the implementation names host paths and
    # container conventions that must never ship to PyPI with adk, so an absent
    # module (a stranger's machine) is skipped and the daemon is unchanged.
    try:
        try:
            from lib.fleet.power_control import create_fleet_power_router
        except ImportError:
            # DERIVED, never typed. Until 2026-09-04 this was an absolute path
            # literal for one developer's checkout -- six lines under the comment
            # above saying host paths must never ship to PyPI, in the file that
            # ships to PyPI. It was also wrong on every machine but that one: the
            # same tree is mounted under a different prefix inside the fleet
            # distro, so the literal silently missed on the hosts this feature
            # exists for. Walk instead: server.py -> adk -> package -> repo root.
            import pathlib as _pl
            import sys as _sys
            _root = str(_pl.Path(__file__).resolve().parents[2] / "AitherOS")
            if _root not in _sys.path:
                _sys.path.insert(0, _root)
            from lib.fleet.power_control import create_fleet_power_router
        app.include_router(create_fleet_power_router())
        logging.getLogger("adk.server").info("Mounted fleet power control (/fleet/power/*)")
    except ImportError as _fp_exc:
        logging.getLogger("adk.server").debug("fleet power control unavailable: %s", _fp_exc)

    # ─── Mesh relay endpoints ───

    @app.get("/mesh/status")
    async def mesh_status():
        """AitherNet mesh relay status."""
        relay = _state.get("relay")
        if not relay:
            return {"joined": False, "message": "Set AITHER_API_KEY to join AitherNet"}
        return relay.status()

    @app.get("/mesh/nodes")
    async def mesh_nodes(capability: str = "", limit: int = 50):
        """Discover other nodes on the mesh."""
        relay = _state.get("relay")
        if not relay:
            return {"nodes": [], "message": "Not connected to AitherNet"}
        nodes = await relay.discover(capability=capability, limit=limit)
        return {"nodes": [n.__dict__ for n in nodes], "total": len(nodes)}

    @app.post("/mesh/relay")
    async def mesh_relay(request: Request):
        """Relay a message to another node."""
        relay = _state.get("relay")
        if not relay:
            return JSONResponse({"error": "Not connected to AitherNet"}, status_code=503)
        body = await request.json()
        to_node = body.get("to_node", "")
        msg_type = body.get("msg_type", "chat")
        payload = body.get("payload", {})
        if not to_node:
            return JSONResponse({"error": "to_node is required"}, status_code=400)
        ok = await relay.send(to_node, msg_type, payload)
        return {"ok": ok, "relayed_to": to_node, "msg_type": msg_type}

    @app.get("/mesh/messages")
    async def mesh_messages():
        """Poll for relay messages addressed to this node."""
        relay = _state.get("relay")
        if not relay:
            return {"messages": []}
        messages = await relay.poll_messages()
        return {"messages": [m.__dict__ for m in messages], "count": len(messages)}

    @app.get("/mesh/agents")
    async def mesh_agents():
        """Discover every agent in the mesh (name + invoke_url + inference backend +
        skills). Auth-gated: the server queries the owner registry with ITS token and
        returns the list to the authenticated caller — the browser never sees the
        owner token (it auths to this endpoint with the server bearer instead)."""
        from adk.mesh_discovery import discover_agents
        agents, warnings = await discover_agents()
        return {"agents": [a.to_dict() for a in agents], "warnings": warnings,
                "total": len(agents)}

    def _is_safe_proxy_url(url: str) -> bool:
        """Guard the mesh proxy targets. invoke_url values come from owner-authed
        discovery (registry / local agents.json / a2a-fleet), NOT from caller input,
        so this is defense-in-depth against a poisoned registry entry — not the
        primary control. We deliberately ALLOW private/LAN IPs because real mesh
        agents live there (e.g. the OptiPlex on 192.168.x.x); we only reject
        non-HTTP(S) schemes and the cloud-metadata address."""
        try:
            from urllib.parse import urlparse
            u = urlparse(url)
            if u.scheme not in ("http", "https"):
                return False
            host = (u.hostname or "").lower()
            if not host:
                return False
            # Block cloud instance-metadata endpoints (link-local 169.254.169.254).
            if host in ("169.254.169.254", "metadata", "metadata.google.internal"):
                return False
            if host.startswith("169.254."):
                return False
            return True
        except Exception:
            return False

    async def _proxy_agent_stream(ref, message: str):
        """SSE generator that forwards a chat to a mesh agent (protocol-aware) and
        re-emits frames in the pack format (token/answer/error), so the web packs can
        talk to a REMOTE agent through this server without holding its credentials."""
        try:
            if ref.chat_protocol == "openai":
                from adk.llm.base import Message
                from adk.llm.openai_compat import OpenAIProvider
                base = ref.invoke_url.rstrip("/")
                if not base.endswith("/v1"):
                    base += "/v1"
                prov = OpenAIProvider(base_url=base, default_model=ref.model or "")
                got = False
                async for chunk in prov.chat_stream([Message(role="user", content=message)],
                                                    max_tokens=1024):
                    text = getattr(chunk, "content", "") or ""
                    if text:
                        got = True
                        yield f"data: {json.dumps({'type': 'token', 't': text})}\n\n"
                if not got:
                    resp = await prov.chat([Message(role="user", content=message)], max_tokens=1024)
                    yield f"data: {json.dumps({'type': 'answer', 'answer': getattr(resp, 'content', '') or ''})}\n\n"
            else:
                from adk.shell.genesis_client import GenesisClient
                client = GenesisClient(base_url=ref.invoke_url)
                async for chunk in client.chat_stream(message):
                    yield f"data: {json.dumps({'type': 'token', 't': chunk})}\n\n"
        except Exception as exc:  # noqa: BLE001 - surface as an SSE error frame, never 500 mid-stream
            # Log the full exception server-side; emit a generic frame so remote
            # agent internals / URLs / secrets never leak to the browser.
            logger.warning("mesh chat proxy to %s failed: %s", getattr(ref, "name", "?"), exc)
            yield f"data: {json.dumps({'type': 'error', 'error': 'upstream agent error'})}\n\n"

    @app.post("/mesh/agents/{name}/chat/stream")
    async def mesh_agent_chat(name: str, request: Request):
        """Proxy a streaming chat to the named mesh agent — it replies on its OWN
        inference. Auth-gated (server bearer); the remote agent's URL/creds stay
        server-side."""
        from adk.mesh_discovery import resolve_agent
        ref = await resolve_agent(name)
        if ref is None or not ref.invoke_url:
            return JSONResponse({"error": f"agent '{name}' not found or has no invoke_url"},
                                status_code=404)
        if not _is_safe_proxy_url(ref.invoke_url):
            logger.warning("mesh chat proxy: refusing unsafe invoke_url for %s", name)
            return JSONResponse({"error": "agent has an unsafe invoke_url"}, status_code=502)
        body = await request.json()
        message = body.get("message", body.get("content", ""))
        return StreamingResponse(
            _proxy_agent_stream(ref, message),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Central console: aggregate each agent's own /admin API through an ALLOWLIST
    # (module-level _mesh_admin_allowed). See its definition for the rationale —
    # cli/exec + credential writes are never reachable through this proxy.
    @app.api_route("/mesh/agents/{name}/admin/{path:path}",
                   methods=["GET", "POST", "PATCH", "DELETE"])
    async def mesh_agent_admin(name: str, path: str, request: Request):
        """Owner-authed proxy to a mesh agent's OWN /admin API, restricted to an
        allowlist (observe + narrow safe controls). Auth-gated by the server
        bearer; the remote agent's URL stays server-side; secret values are never
        surfaced (remote admin masks them). cli/exec + credential writes are not
        reachable through here."""
        import httpx
        from adk.mesh_discovery import resolve_agent

        if not _mesh_admin_allowed(request.method, path):
            logger.warning("mesh admin proxy: refusing %s /admin/%s for %s",
                           request.method, path, name)
            return JSONResponse(
                {"error": "admin route not permitted through the mesh console",
                 "method": request.method, "path": path},
                status_code=403,
            )

        ref = await resolve_agent(name)
        if ref is None or not ref.invoke_url:
            return JSONResponse({"error": f"agent '{name}' not found or has no invoke_url"},
                                status_code=404)
        if not _is_safe_proxy_url(ref.invoke_url):
            logger.warning("mesh admin proxy: refusing unsafe invoke_url for %s", name)
            return JSONResponse({"error": "agent has an unsafe invoke_url"}, status_code=502)

        target = ref.invoke_url.rstrip("/") + "/admin/" + path.strip("/")
        # Forward the owner's bearer to the remote agent (mesh-shared server key);
        # a mismatch surfaces as the remote's own 401, never a silent success.
        fwd_headers = {}
        auth = request.headers.get("authorization")
        if auth:
            fwd_headers["Authorization"] = auth
        body_bytes = await request.body()
        if request.headers.get("content-type"):
            fwd_headers["Content-Type"] = request.headers["content-type"]

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.request(
                    request.method, target,
                    params=dict(request.query_params),
                    content=body_bytes or None,
                    headers=fwd_headers,
                )
        except httpx.RequestError as exc:
            logger.warning("mesh admin proxy to %s failed: %s", name, exc)
            return JSONResponse({"error": "upstream agent unreachable"}, status_code=502)

        media = resp.headers.get("content-type", "application/json")
        return Response(content=resp.content, status_code=resp.status_code,
                        media_type=media)

    @app.post("/mesh/tools/call")
    async def mesh_tool_call(request: Request):
        """Call an MCP tool on a remote mesh node."""
        relay = _state.get("relay")
        if not relay:
            return JSONResponse({"error": "Not connected to AitherNet"}, status_code=503)
        body = await request.json()
        node_id = body.get("node_id", "")
        tool_name = body.get("name", body.get("tool", ""))
        arguments = body.get("arguments", {})
        if not node_id or not tool_name:
            return JSONResponse({"error": "node_id and name are required"}, status_code=400)
        result = await relay.call_remote_tool(node_id, tool_name, arguments)
        return result

    @app.get("/mesh/tools")
    async def mesh_discover_tools(node_id: str = ""):
        """Discover MCP tools on mesh nodes."""
        relay = _state.get("relay")
        if not relay:
            return {"tools": [], "message": "Not connected to AitherNet"}
        if node_id:
            tools = await relay.list_remote_tools(node_id)
            return {"node_id": node_id, "tools": tools}
        all_tools = await relay.discover_mesh_tools()
        return {"tools": all_tools, "total": len(all_tools)}

    @app.post("/mesh/agent/call")
    async def mesh_agent_call(request: Request):
        """Call an agent on a remote mesh node."""
        relay = _state.get("relay")
        if not relay:
            return JSONResponse({"error": "Not connected to AitherNet"}, status_code=503)
        body = await request.json()
        agent_name = body.get("agent", "")
        message = body.get("message", body.get("content", ""))
        target_node = body.get("node_id", "")
        if not agent_name or not message:
            return JSONResponse({"error": "agent and message are required"}, status_code=400)
        result = await relay.call_remote_agent(agent_name, message, target_node=target_node)
        return result

    # ─── MCP server status ───

    @app.get("/mcp/status")
    async def mcp_status():
        """MCP server status."""
        mcp = _state.get("mcp_server")
        if not mcp:
            return {"active": False, "message": "MCP server not initialized"}
        return {**mcp.status(), "active": True}

    # ─── Aeon — Multi-Agent Group Chat ───

    _state["aeon_sessions"] = {}

    @app.post("/aeon/chat")
    async def aeon_chat(request: Request):
        """Multi-agent group chat. Creates or reuses an AeonSession."""
        from adk.aeon import AeonSession, AEON_PRESETS

        body = await request.json()
        message = body.get("message", "")
        if not message:
            return JSONResponse({"error": "message is required"}, status_code=400)

        session_id = body.get("session_id")
        preset = body.get("preset", "balanced")
        participants = body.get("participants")
        rounds = body.get("rounds", 1)
        synthesize = body.get("synthesize", True)

        # Reuse existing session or create new
        sessions = _state["aeon_sessions"]
        if session_id and session_id in sessions:
            session = sessions[session_id]
        else:
            session = AeonSession(
                participants=participants,
                preset=preset,
                rounds=rounds,
                synthesize=synthesize,
                config=config,
            )
            sessions[session.session_id] = session

        response = await session.chat(message)
        return {
            "session_id": response.session_id,
            "messages": [m.to_dict() for m in response.messages],
            "synthesis": response.synthesis.to_dict() if response.synthesis else None,
            "total_tokens": response.total_tokens,
            "total_latency_ms": response.total_latency_ms,
            "round_number": response.round_number,
            "participants": session.participants,
        }

    @app.get("/aeon/presets")
    async def aeon_presets():
        """List available group chat presets."""
        from adk.aeon import AEON_PRESETS
        return {"presets": AEON_PRESETS}

    @app.get("/aeon/sessions/{session_id}")
    async def aeon_session_detail(session_id: str):
        """Get history and stats for an Aeon session."""
        sessions = _state["aeon_sessions"]
        if session_id not in sessions:
            return JSONResponse({"error": "session not found"}, status_code=404)
        session = sessions[session_id]
        return {
            "session_id": session.session_id,
            "participants": session.participants,
            "history": [m.to_dict() for m in session.history],
            "rounds": session._round_counter,
            "total_messages": len(session.history),
        }

    # ─── Slash-command manifest for AitherShell auto-discovery ───

    @app.get("/slash-commands")
    async def slash_commands():
        """Return structured manifest of all ADK CLI commands.

        AitherShell queries this on startup and auto-registers each command
        as a /name slash command with tab-completion for arguments.
        """
        from adk.cli import build_command_manifest
        manifest = build_command_manifest()
        return {
            "commands": manifest,
            "version": __version__,
            "total": len(manifest),
        }

    @app.post("/cli/execute")
    async def cli_execute(request: Request):
        """Execute an ADK CLI command from AitherShell.

        Body: {"command": "train", "args": ["status"]}
        or:   {"command": "backend", "args": ["list"]}

        Returns the command's stdout/stderr as text.
        This lets AitherShell run any CLI command without shelling out.
        """
        import asyncio
        import subprocess

        body = await request.json()
        command = body.get("command", "")
        args = body.get("args", [])

        if not command:
            return JSONResponse({"error": "command is required"}, status_code=400)

        # Security: only allow known ADK commands, not arbitrary shell execution
        from adk.cli import build_command_manifest
        valid_commands = {c["name"] for c in build_command_manifest()}
        if command not in valid_commands:
            return JSONResponse(
                {"error": f"Unknown command: {command}", "valid": sorted(valid_commands)},
                status_code=400,
            )

        cmd = ["python", "-m", "adk.cli", command] + [str(a) for a in args]
        try:
            result = await asyncio.to_thread(
                subprocess.run, cmd,
                capture_output=True, text=True, timeout=60,
            )
            return {
                "command": command,
                "args": args,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Command timed out (60s)"}, status_code=504)
        except FileNotFoundError:
            return JSONResponse({"error": "Python not found"}, status_code=500)

    # ── FormBridge (local form automation; routes are loopback-guarded) ──
    if os.getenv("AITHER_FORMBRIDGE_ROUTES", "1").lower() not in ("0", "false"):
        try:
            from adk.formbridge.routes import create_formbridge_router

            app.include_router(create_formbridge_router())
            logger.info("FormBridge routes mounted (/formbridge/*)")
        except ImportError as e:
            # The public wheel excludes adk/formbridge; an engine installed for a
            # FormBridge office without the private distribution has NO routes.
            # Say so loudly -- a debug line here hid a dead install.
            logger.warning(
                "FormBridge routes NOT mounted (adk.formbridge missing -- install the "
                "FormBridge engine distribution, not the public awdk wheel): %s", e,
            )

    # ── Local AI UI routes (the `local` UI pack: tasks, images, mail) ──
    # Mounted on the main app so the pack served at "/" has ONE origin and
    # ONE auth plane. Reuses the awrun queue wrappers and adk.images.
    try:
        from adk.local_routes import router as _local_router

        app.include_router(_local_router)
        logger.info("Local AI routes mounted (/api/local/*)")
    except ImportError as e:
        logger.warning("Local AI routes not available: %s", e)

    return app


async def _strata_ingest_bg(**kwargs):
    """Module-level fire-and-forget Strata ingest for _aitheros_stream.

    _aitheros_stream is module-level and cannot see create_app's _strata_ingest
    closure — referencing it raised NameError and silently truncated the SSE
    stream after the answer. Never blocks or raises.
    """
    try:
        from adk.strata import get_strata_ingest
        strata = get_strata_ingest()
        await strata.ingest_chat(**kwargs)
    except Exception:
        pass



def _tool_result_ok(result) -> bool:
    """Did this tool actually succeed?

    Derived from the payload rather than assumed. A JSON object carrying an
    `error` key is a failure; everything else keeps the previous answer, so
    this can only turn a wrong True into a right False -- it can never invent
    a failure for a tool that worked.

    Deliberately narrow. A broader rule (scanning for the word 'error'
    anywhere) would mark a successful web_search for 'python error handling'
    as failed, and a tool result that lies in the other direction is worse:
    it makes the agent abandon work that succeeded.
    """
    text = result if isinstance(result, str) else str(result)
    stripped = text.strip()
    if not stripped.startswith('{'):
        return True
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        parsed = None  # unparseable -> keep the previous answer (see docstring)
    if isinstance(parsed, dict) and parsed.get('error'):
        return False
    return True

async def _aitheros_stream(
    get_agent_fn, message: str, session_id: str, agent_name: str | None, reasoning: bool,
    mcp_endpoints: list | None = None, gen_params: dict | None = None,
    system_additions: list | None = None,
):
    """SSE generator emitting AitherOS-typed events.

    Uses the app-level get_agent() for shared memory/tools/fleet support.
    Emits heartbeat during sync tool execution to prevent frontend timeout.

    Protocol:
      event: session_start  -> {session_id, agent, model}
      event: heartbeat      -> {} (every 2s during tool execution)
      event: tool_call      -> {tools: [{name, args}]}
      event: tool_result    -> {results: [{tool, success, output}]}
      event: token          -> {t: "chunk", n: count}
      event: answer         -> {answer: "full response"}
      event: complete       -> {duration_ms, tokens_used}
    """
    start = time.time()
    try:
        agent = await get_agent_fn(agent_name)

        # Layer-3 supervised reasoning (gated): a parallel watcher escalates a
        # stuck tool loop by calling the agent's deep_reasoning tool ON ITS
        # BEHALF and injecting the conclusion via the steering queue — a fast
        # router does not self-diagnose being stuck (measured: deep_reasoning
        # registered AND advertised on 12/12 instances, called 0 times).
        # Conservative here: escalate on repeated tool failures only
        # (drift_steps=999). AITHER_ADK_SUPERVISOR=0 disables. The daemon has no
        # ReasoningSession of its own, so the supervisor steers through
        # queue_steering_message — the same channel POST /chat/steer uses.
        _sup_task: Optional[asyncio.Task] = None
        _sup_sess = None
        if os.environ.get("AITHER_ADK_SUPERVISOR", "1") not in ("0", "false", "False"):
            try:
                from adk.reasoning_session import ReasoningSession as _RS
                from adk.supervisor import Supervisor as _Sup, Goal as _SupGoal
                from adk.steering import queue_steering_message as _qsm
                _sup_sess = _RS(session_id)
                _sup_sess.mark_running()
                _sup_reasoner = None
                _sup_td = agent._tools.get("deep_reasoning")
                if _sup_td is not None:
                    _sup_fn = _sup_td.fn

                    async def _forced_reasoner(problem: str) -> str:
                        try:
                            return str((await _sup_fn(problem)) or "").strip()
                        except Exception:  # noqa: BLE001 — advisory fallback
                            return ""
                    _sup_reasoner = _forced_reasoner
                _sup = _Sup(_sup_sess, _SupGoal(message, session_id),
                            reasoning_tool_name="deep_reasoning",
                            fail_threshold=2, drift_steps=999, emit_events=True,
                            reasoner=_sup_reasoner,
                            steer_fn=lambda m: _qsm(session_id, "hint", m))
                _sup_task = asyncio.ensure_future(_sup.run(tick_s=0.5))
            except Exception:  # noqa: BLE001 — supervisor is best-effort
                _sup_task = None
                _sup_sess = None

        # Attach the customer's self-hosted MCP "hands" relayed by the platform in
        # the /stream body (brain/body/HANDS). Best-effort; never blocks the turn.
        if mcp_endpoints:
            try:
                from adk.mcp_endpoint_tools import register_mcp_endpoint_tools
                register_mcp_endpoint_tools(agent, mcp_endpoints)
            except Exception:
                pass
        model_name = getattr(agent.llm, "provider_name", "unknown")

        # session_start
        yield f"event: session_start\ndata: {json.dumps({'type': 'session_start', 'session_id': session_id, 'agent': agent.name, 'model': model_name})}\n\n"

        _gp = gen_params or {}
        # If agent has tools, use stream_react to emit events (KG, steering, tokens)
        if agent._tools.list_tools():
            from adk.steering import register_steering_queue, unregister_steering_queue

            # LIVE RELAY — not collect-then-replay.
            #
            # Until 2026-08-23 this branch appended every stream_react event to a
            # list and yielded the list AFTER the whole turn returned. Measured
            # from AitherShell: session_start at +0.4 s, then NOTHING for 17.4 s,
            # then one token blob + answer + complete. The agent was genuinely
            # streaming its <think> and its answer the entire time; the daemon
            # was holding every byte. A user sees that as "hung for 18 s, then
            # dumped" — and the spinner the shell runs on heartbeats never got
            # one, because nothing was yielded to carry it.
            #
            # So: stream_react runs as a task, on_event drops each event on a
            # queue, and THIS generator yields as they land. A 2 s quiet period
            # yields a heartbeat so the client can show liveness during a slow
            # tool or a long model stall. The steering queue is registered around
            # the task exactly as before (clients may inject mid-turn).
            _q: asyncio.Queue = asyncio.Queue()
            _done = object()
            tool_calls_for_kg = []

            def _on_stream_event(ev: dict) -> None:
                """Relay one stream_react event to the SSE loop below, live."""
                if ev.get("type") == "tool":
                    tool_calls_for_kg.append(ev.get("name", "?"))
                if _sup_sess is not None:
                    try:
                        _sup_sess.emit(ev)   # the supervisor observes this stream
                    except Exception:  # noqa: BLE001
                        pass
                _q.put_nowait(ev)

            async def _run_turn():
                try:
                    await register_steering_queue(session_id)
                    return await agent.stream_react(
                        message,
                        on_event=_on_stream_event,
                        session_id=session_id,
                        system_additions=system_additions,
                    )
                finally:
                    unregister_steering_queue(session_id)
                    if _sup_sess is not None:
                        _sup_sess.mark_done()   # ends the supervisor's observe loop
                    _q.put_nowait(_done)

            _turn_task = asyncio.ensure_future(_run_turn())

            while True:
                try:
                    ev = await asyncio.wait_for(_q.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    _hb = {'type': 'heartbeat', 'elapsed_ms': int((time.time() - start) * 1000)}
                    yield f"event: heartbeat\ndata: {json.dumps(_hb)}\n\n"
                    continue
                if ev is _done:
                    break
                ev_type = ev.get("type", "")

                if ev_type == "thinking":
                    text = ev.get("text", "")
                    if text:
                        yield f"event: token\ndata: {json.dumps({'type': 'token', 't': text})}\n\n"
                elif ev_type == "token":
                    text = ev.get("text", "")
                    if text:
                        yield f"event: token\ndata: {json.dumps({'type': 'token', 't': text})}\n\n"
                elif ev_type == "tool":
                    tool_name = ev.get("name", "?")
                    yield f"event: tool_call\ndata: {json.dumps({'type': 'tool_call', 'tools': [{'name': tool_name, 'args': ev.get('args', {})}]})}\n\n"
                elif ev_type == "tool_result":
                    tool_name = ev.get("name", "?")
                    result = ev.get("result", "")
                    yield f"event: tool_result\ndata: {json.dumps({'type': 'tool_result', 'results': [{'tool': tool_name, 'success': True, 'output': str(result)[:500]}]})}\n\n"
                elif ev_type == "knowledge_graph":
                    # Emit KG event directly (gap B fix)
                    yield f"event: knowledge_graph\ndata: {json.dumps({'type': 'knowledge_graph', 'nodes': ev.get('nodes', []), 'edges': ev.get('edges', [])})}\n\n"
                elif ev_type == "error":
                    error_msg = ev.get("error", "Unknown error")
                    yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': error_msg})}\n\n"
                # Skip "done" events - we emit complete separately

            # Re-raises a stream_react failure into the outer handler, which
            # emits a typed error + terminal complete (never a truncated stream).
            resp = await _turn_task

            # Let a mid-flight reasoner steer land, then stop watching — the
            # stream is ending and nothing will drain new steers after it.
            if _sup_task is not None:
                try:
                    await asyncio.wait_for(_sup_task, timeout=2.0)
                except asyncio.TimeoutError:
                    _sup_task.cancel()

            # No silent empty answers: an empty completion must surface as a typed
            # error, not a blank answer that reads as "the model said nothing".
            if not (resp.content or "").strip():
                logger.warning("AitherOS stream: empty completion from %s (model %s)",
                               agent.name, model_name)
                _err_data = json.dumps({"type": "error",
                                        "error": "model returned an empty completion"})
                yield f"event: error\ndata: {_err_data}\n\n"
                _end_ms = int((time.time() - start) * 1000)
                yield (f"event: complete\ndata: "
                       f"{json.dumps({'type': 'complete', 'duration_ms': _end_ms})}\n\n")
                return

            yield f"event: answer\ndata: {json.dumps({'type': 'answer', 'answer': resp.content})}\n\n"

            duration_ms = int((time.time() - start) * 1000)
            yield f"event: complete\ndata: {json.dumps({'type': 'complete', 'duration_ms': duration_ms, 'tokens_used': resp.tokens_used, 'model': resp.model, 'session_id': session_id})}\n\n"

            # Fire-and-forget Strata + Chronicle
            asyncio.ensure_future(_strata_ingest_bg(
                agent=agent.name, session_id=session_id,
                user_message=message, assistant_response=resp.content,
                model=resp.model, tokens_used=resp.tokens_used,
                latency_ms=resp.latency_ms, tool_calls=tool_calls_for_kg,
            ))
            return

        # Streaming path — no tools
        full_content = ""
        token_count = 0
        async for chunk in agent.chat_stream(message, session_id=session_id,
                                             system_additions=system_additions, **_gp):
            if chunk:
                full_content += chunk
                token_count += 1
                yield f"event: token\ndata: {json.dumps({'type': 'token', 't': chunk, 'n': token_count})}\n\n"

        # No silent empty answers (same rule as the tool branch above).
        if not full_content.strip():
            logger.warning("AitherOS stream: empty completion from %s (no-tools path)",
                           agent.name)
            _err_data = json.dumps({"type": "error",
                                    "error": "model returned an empty completion"})
            yield f"event: error\ndata: {_err_data}\n\n"
            _end_ms = int((time.time() - start) * 1000)
            yield (f"event: complete\ndata: "
                   f"{json.dumps({'type': 'complete', 'duration_ms': _end_ms})}\n\n")
            return

        yield f"event: answer\ndata: {json.dumps({'type': 'answer', 'answer': full_content})}\n\n"

        duration_ms = int((time.time() - start) * 1000)
        yield f"event: complete\ndata: {json.dumps({'type': 'complete', 'duration_ms': duration_ms, 'tokens_used': token_count, 'model': model_name, 'session_id': session_id})}\n\n"

        asyncio.ensure_future(_strata_ingest_bg(
            agent=agent.name, session_id=session_id,
            user_message=message, assistant_response=full_content,
            model=model_name, tokens_used=token_count,
            latency_ms=duration_ms, tool_calls=[],
        ))

    except Exception as exc:
        # Broad on purpose: an unhandled exception inside an SSE generator silently
        # truncates the stream (client sees session_start + heartbeats then EOF, with
        # no error/complete). Surface it as a typed error + terminal complete so the
        # caller always gets a clean end, and log the full traceback for diagnosis.
        logger.exception("AitherOS stream error: %s", exc)
        yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"
        duration_ms = int((time.time() - start) * 1000)
        yield f"event: complete\ndata: {json.dumps({'type': 'complete', 'duration_ms': duration_ms})}\n\n"


async def _aeon_stream(message: str, preset: str, agents: list[str] | None,
                       rounds: int, session_id: str):
    """SSE generator for Aeon group-chat — multi-agent discussion with synthesis.

    Protocol:
      event: session_start  -> {type, session_id, orchestrator, participants, model}
      event: agent_message  -> {type, agent, content, tokens_used, latency_ms, round_number}
      event: synthesis      -> {type, agent, content, tokens_used, latency_ms} (if enabled)
      event: error          -> {type, error}
      event: complete       -> {type, total_tokens, total_latency_ms, session_id}
    """
    start = time.time()
    try:
        from adk.aeon import AeonSession

        # Create the session with the given preset/agents/rounds
        session = AeonSession(
            participants=agents,
            preset=preset,
            rounds=rounds,
            synthesize=True,
        )
        model_name = getattr(session._shared_llm, "provider_name", "unknown")

        # Emit session_start
        yield f"event: session_start\ndata: {json.dumps({'type': 'session_start', 'session_id': session_id, 'orchestrator': session.orchestrator, 'participants': session.participants, 'model': model_name})}\n\n"

        # Run the group chat
        response = await session.chat(message)

        # Emit each agent message (excluding synthesis)
        for msg in response.messages:
            yield f"event: agent_message\ndata: {json.dumps({'type': 'agent_message', 'agent': msg.agent, 'content': msg.content, 'tokens_used': msg.tokens_used, 'latency_ms': msg.latency_ms, 'round_number': msg.round_number})}\n\n"

        # Emit synthesis if present
        if response.synthesis:
            yield f"event: synthesis\ndata: {json.dumps({'type': 'synthesis', 'agent': response.synthesis.agent, 'content': response.synthesis.content, 'tokens_used': response.synthesis.tokens_used, 'latency_ms': response.synthesis.latency_ms})}\n\n"

        # Emit complete
        total_ms = int((time.time() - start) * 1000)
        yield f"event: complete\ndata: {json.dumps({'type': 'complete', 'total_tokens': response.total_tokens, 'total_latency_ms': response.total_latency_ms, 'session_id': session_id})}\n\n"

    except Exception as exc:
        logger.exception("Aeon stream error: %s", exc)
        yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"
        duration_ms = int((time.time() - start) * 1000)
        yield f"event: complete\ndata: {json.dumps({'type': 'complete', 'duration_ms': duration_ms})}\n\n"


async def _stream_agent_response(agent, message: str, history: list[dict], model: str | None):
    """SSE stream generator using agent.chat_stream() — full pipeline.

    Routes through the agent's tool loop, safety, context manager, memory,
    and events — NOT a raw LLM stream bypass.

    If the agent has tools and the LLM requests a tool call, chat_stream()
    falls back to sync chat() and yields the full response as one chunk.
    """
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    try:
        async for text_chunk in agent.chat_stream(
            message, history=history or None, model=model,
        ):
            data = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model or getattr(agent.llm, "provider_name", ""),
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": text_chunk},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(data)}\n\n"
    except Exception as exc:  # noqa: BLE001 — this is the terminal SSE error
        # boundary; any uncaught exception here (observed live: httpx.HTTPStatusError
        # from a backend rejecting a tool-calling request, e.g. an Ollama model that
        # doesn't support the tools schema) must surface as a clear error chunk to
        # the client, not crash the generator and leave the HTTP response hanging
        # with zero bytes sent ("(no response)" client-side, indistinguishable from
        # a real hang). A narrower (RuntimeError, OSError, ConnectionError) catch
        # here previously let exactly this class of error through uncaught.
        logger.error("Streaming error: %s", exc)
        data = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model or "",
            "choices": [{"index": 0, "delta": {"content": f"Error: {exc}"}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(data)}\n\n"

    # Final stop chunk
    stop_data = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model or "",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(stop_data)}\n\n"
    yield "data: [DONE]\n\n"


def _resolve_portal_kit_backend() -> bool:
    """Add portal-kit-backend to sys.path if running in the monorepo.

    Search order:
    1. Already importable (pip install / Docker mount) — no action needed
    2. PORTAL_KIT_BACKEND_PATH env var
    3. Monorepo sibling: AitherOS/apps/packages/portal-kit-backend/
    4. Relative from ADK: ../../AitherOS/apps/packages/portal-kit-backend/

    Returns True if portal_kit_backend is importable after this call.
    """
    import importlib
    import sys
    from pathlib import Path

    # Already available?
    try:
        importlib.import_module("portal_kit_backend")
        return True
    except ImportError:
        pass

    candidates = []

    # Env override
    env_path = os.getenv("PORTAL_KIT_BACKEND_PATH")
    if env_path:
        candidates.append(Path(env_path))

    # Monorepo: ADK is at <root>/awdk/, backend at <root>/AitherOS/apps/packages/
    adk_root = Path(__file__).resolve().parent.parent  # awdk/
    monorepo_root = adk_root.parent  # project root
    candidates.append(monorepo_root / "AitherOS" / "apps" / "packages")
    # Also try if portal-kit-backend parent is directly at packages/
    candidates.append(monorepo_root / "AitherOS" / "apps" / "packages" / "portal-kit-backend")

    for cand in candidates:
        # The package dir must contain portal_kit_backend/__init__.py or be the
        # parent that contains portal_kit_backend/ as a subdirectory.
        pkg_init = cand / "portal_kit_backend" / "__init__.py"
        parent_init = cand / "__init__.py"
        if pkg_init.exists():
            # cand is the parent we need on sys.path
            path_str = str(cand)
            if path_str not in sys.path:
                sys.path.insert(0, path_str)
        elif parent_init.exists() and cand.name == "portal-kit-backend":
            # portal-kit-backend IS the package (uses portal_kit_backend as import name)
            # Add its parent so `import portal_kit_backend` works
            path_str = str(cand.parent)
            if path_str not in sys.path:
                sys.path.insert(0, path_str)

        try:
            importlib.import_module("portal_kit_backend")
            return True
        except ImportError:
            continue

    return False


def _load_capability_domains_config() -> dict:
    """Load capability_domains.yaml from AitherOS config.

    Returns parsed dict or {} if not found.
    """
    from pathlib import Path

    candidates = [
        Path(os.getenv("CAPABILITY_DOMAINS_PATH", "")),
        Path(__file__).resolve().parent.parent.parent / "AitherOS" / "config" / "capability_domains.yaml",
        Path("/app/AitherOS/config/capability_domains.yaml"),
    ]
    for p in candidates:
        if p.exists():
            try:
                import yaml
                return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception:
                pass
    return {}


def _mount_workspace_routers(app: FastAPI, port: int) -> None:
    """Mount portal-kit-backend routers for workspace mode.

    Makes the ADK server function like a full WorkspaceRuntime with
    calendar, mail, social, executive, workspace intelligence, documents,
    onboarding, file sync, contacts, and config endpoints.

    Resolves portal-kit-backend from the monorepo if not pip-installed,
    initialises the SQLite workspace store, and wires portal registration
    + Proton auto-connect into the server lifespan.
    """
    _ws_log = logging.getLogger("adk.workspace")
    mounted = 0

    # ── Resolve portal-kit-backend package ──
    if not _resolve_portal_kit_backend():
        _ws_log.error(
            "portal-kit-backend not found. Install it or set PORTAL_KIT_BACKEND_PATH. "
            "Workspace routers will not be available."
        )
        return

    # ── Set up workspace data directory ──
    from pathlib import Path
    data_dir = Path(os.getenv("AITHER_DATA_DIR", os.path.expanduser("~/.aither")))
    store_path = data_dir / "workspace" / "aitherchat.db"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("AITHER_CHAT_STORE_PATH", str(store_path))

    # ── Discover brain pack + agent.yaml via pack_discovery ──
    from adk.pack_discovery import discover_agent_yaml, discover_brain_pack, discover_pack_dir
    import yaml

    agent_yaml_path = discover_agent_yaml()
    brain_pack_path = discover_brain_pack()
    pack_dir = discover_pack_dir()

    # Set env vars so portal-kit-backend picks them up
    if brain_pack_path:
        os.environ.setdefault("AGENT_BRAIN_PACK", str(brain_pack_path))
        _ws_log.info("Brain pack: %s", brain_pack_path)
    if pack_dir:
        _ws_log.info("Pack directory: %s", pack_dir)

    # Read agent spec
    enabled_domains: list[str] = []
    agent_spec: dict = {}
    if agent_yaml_path and agent_yaml_path.exists():
        try:
            agent_spec = yaml.safe_load(agent_yaml_path.read_text(encoding="utf-8")) or {}
            enabled_domains = agent_spec.get("enabled_domains", [])
            _ws_log.info("Agent spec: %s (domains=%s)", agent_yaml_path, enabled_domains or "all")
        except Exception:
            pass

    # ── Load structured domain definitions for API ──
    domain_config = _load_capability_domains_config()
    domain_defs = domain_config.get("capability_domains", {})

    def _domain_ok(domain: str) -> bool:
        return not enabled_domains or domain in enabled_domains

    # ── Router mount helper ──
    def _try_mount(import_fn, label: str, domain: str = ""):
        nonlocal mounted
        if domain and not _domain_ok(domain):
            _ws_log.debug("Skipped %s (domain '%s' not enabled)", label, domain)
            return
        try:
            router = import_fn()
            app.include_router(router)
            mounted += 1
            _ws_log.info("Workspace router: %s", label)
        except ImportError as e:
            _ws_log.debug("Workspace router %s not available: %s", label, e)
        except Exception as e:
            _ws_log.warning("Failed to mount %s: %s", label, e)

    app_id = os.getenv("APP_ID", "aither-local")

    # ── Mount portal-kit-backend routers (domain-gated) ──

    # Calendar + mail
    def _cal():
        from portal_kit_backend.routers.calendar import create_calendar_router
        return create_calendar_router(app_id=app_id)
    _try_mount(_cal, "calendar (/api/calendar/*)", "calendar_mail")

    def _mail():
        from portal_kit_backend.routers.workspace_mail import create_workspace_mail_router
        return create_workspace_mail_router(app_id=app_id)
    _try_mount(_mail, "mail (/api/mail/*)", "calendar_mail")

    # People + directory
    def _dir():
        from portal_kit_backend.routers.workspace_directory import create_workspace_directory_router
        return create_workspace_directory_router()
    _try_mount(_dir, "directory (/api/directory/*)", "people")

    def _contacts():
        from portal_kit_backend.routers.contacts import create_contacts_router
        return create_contacts_router()
    _try_mount(_contacts, "contacts (/api/contacts/*)", "people")

    # Proton suite / file sync
    def _fs():
        from portal_kit_backend.routers.file_sync import router
        return router
    _try_mount(_fs, "file-sync (/api/file-sync/*)", "proton_suite")

    # Social + marketing
    def _soc():
        from portal_kit_backend.routers.social import create_social_router
        return create_social_router()
    _try_mount(_soc, "social (/api/social/*)", "social_marketing")

    # Executive assistant
    def _exec():
        from portal_kit_backend.routers.executive_briefing import create_executive_briefing_router
        return create_executive_briefing_router(app_id=app_id)
    _try_mount(_exec, "executive (/api/executive/*)", "executive_assistant")

    # Workspace intelligence
    def _wi():
        from portal_kit_backend.routers.workspace_intelligence import create_workspace_intelligence_router
        return create_workspace_intelligence_router(app_id=app_id)
    _try_mount(_wi, "workspace-intelligence (/api/workspace-intelligence/*)", "workspace_intelligence")

    # Documents
    def _docs():
        from portal_kit_backend.routers.documents import create_documents_router
        return create_documents_router()
    _try_mount(_docs, "documents (/api/documents/*)", "documents")

    # Onboarding (no domain gate — always available)
    def _onb():
        from portal_kit_backend.routers.onboarding import create_onboarding_router
        return create_onboarding_router(app_id=app_id)
    _try_mount(_onb, "onboarding (/api/onboarding/*)")


    # ── Workspace config endpoints (portal iframe + domain info) ──

    @app.get("/api/config/embed")
    async def config_embed():
        return {
            "app_id": app_id,
            "name": agent_spec.get("name", "Aither"),
            "embed": True,
            "url": f"http://localhost:{port}",
            "embed_url": f"http://localhost:{port}/?embedded=true",
        }

    @app.get("/api/config/tabs")
    async def config_tabs():
        portal = agent_spec.get("portal", {})
        return {"tabs": portal.get("capabilities", []), "app_id": app_id}

    @app.get("/api/config/domains")
    async def config_domains():
        """Return structured domain definitions with enabled state."""
        domains_out = []
        for did, ddef in domain_defs.items():
            domains_out.append({
                "id": did,
                "label": ddef.get("label", did),
                "description": ddef.get("description", ""),
                "panels": ddef.get("panels", []),
                "routers": ddef.get("routers", []),
                "tools": ddef.get("tools", []),
                "enabled": _domain_ok(did),
            })
        return {
            "domains": domains_out,
            "enabled_domains": enabled_domains,
            "app_id": app_id,
        }

    # ── Wire lifespan: store init, portal registration, Proton auto-connect ──

    _orig_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _workspace_lifespan(a):
        async with _orig_lifespan(a):
            # Initialise SQLite workspace store
            try:
                from portal_kit_backend.aither_store import _ensure_init
                await _ensure_init()
                _ws_log.info("Workspace store initialised at %s", store_path)
            except Exception as e:
                _ws_log.warning("Workspace store init failed: %s", e)

            # Register with portal
            try:
                from adk.registration import start_registration
                await start_registration(
                    agent_spec=agent_spec,
                    server_url=f"http://localhost:{port}",
                )
            except Exception as e:
                _ws_log.debug("Portal registration skipped: %s", e)

            # Auto-connect Proton if detected
            try:
                from adk.proton_setup import detect_proton_bridge, auto_connect_mail
                detection = detect_proton_bridge()
                if detection["bridge_running"]:
                    _ws_log.info("Proton Bridge detected — attempting auto-connect")
                    result = await auto_connect_mail(
                        api_base=f"http://localhost:{port}",
                    )
                    if result.get("ok"):
                        _ws_log.info("Proton mail connected: %s", result.get("email"))
                    else:
                        _ws_log.info(
                            "Proton auto-connect: %s (run --setup or configure secrets)",
                            result.get("reason", "unknown"),
                        )
            except Exception:
                pass

            yield

            # Cleanup
            try:
                from adk.registration import stop_registration
                await stop_registration()
            except Exception:
                pass

    app.router.lifespan_context = _workspace_lifespan

    # ── Serve workspace frontend (catch-all, MUST be last) ──
    # Search order: custom frontend-dist, WorkspaceRuntime frontend, bundled fallback
    from pathlib import Path
    from fastapi.staticfiles import StaticFiles

    _frontend_candidates = [
        Path(os.getenv("AITHER_FRONTEND_DIR", "")),                    # explicit override
        Path.cwd() / "frontend-dist",                                  # local build output
        Path.cwd() / "frontend",                                       # local dev frontend
        Path(__file__).resolve().parent / "workspace-frontend",        # bundled with ADK
    ]
    for fdir in _frontend_candidates:
        if fdir.exists() and (fdir / "index.html").exists():
            app.mount("/", StaticFiles(directory=str(fdir), html=True), name="frontend")
            _ws_log.info("Workspace frontend: %s", fdir)
            break
    else:
        _ws_log.warning("No workspace frontend found — API-only mode")

    _ws_log.info("Workspace mode: %d portal-kit routers mounted, store=%s", mounted, store_path)


def main():
    """CLI entry point: aither-serve"""
    parser = argparse.ArgumentParser(description="AitherADK Agent Server")
    parser.add_argument("--identity", "-i", default="aither", help="Agent identity to load (single-agent mode)")
    parser.add_argument("--port", "-p", type=int, default=None, help="Port (default: 8080)")
    parser.add_argument("--host", default=None, help="Host (default: 0.0.0.0)")
    parser.add_argument("--backend", "-b", help="LLM backend: ollama, openai, anthropic")
    parser.add_argument("--model", "-m", help="Model name override")
    parser.add_argument("--fleet", "-f", default=None, help="Fleet YAML config file for multi-agent mode")
    parser.add_argument("--agents", "-a", default=None, help="Comma-separated agent identities for fleet mode (e.g. aither,lyra,demiurge)")
    parser.add_argument("--invoke-url", default=None, help="Publicly reachable URL for fleet dispatch (e.g. http://192.168.1.50:8900)")
    parser.add_argument("--workspace", action="store_true", help="Workspace mode: mount portal-kit-backend routers, register with portal")
    parser.add_argument("--setup", action="store_true", help="Run first-time setup wizard (Proton auto-detect, etc.)")
    args = parser.parse_args()

    config = Config.from_env()
    if args.backend:
        config.llm_backend = args.backend
    if args.model:
        config.model = args.model

    port = args.port or config.server_port
    host = args.host or config.server_host
    # Write the resolved port/host back so lifespan helpers (_join_aithernet,
    # invoke_url, gateway/fleet registration) all see the actual bound port —
    # not the config default. Without this, --port diverges from config.server_port
    # (and _join_aithernet referenced an undefined `port`, crashing startup).
    config.server_port = port
    config.server_host = host

    if args.invoke_url:
        os.environ["AITHER_INVOKE_URL"] = args.invoke_url

    # Determine mode
    fleet_path = args.fleet
    fleet_agents = args.agents.split(",") if args.agents else None
    is_fleet = bool(fleet_path or fleet_agents)

    # Workspace mode flag (env var or CLI)
    workspace_mode = args.workspace or os.getenv("AITHER_WORKSPACE_MODE", "").lower() in ("true", "1")
    if workspace_mode:
        os.environ["AITHER_WORKSPACE_MODE"] = "true"

    # First-run setup wizard (--setup)
    if args.setup:
        from adk.proton_setup import print_detection_summary
        print("\n  AitherADK Workspace Setup\n")
        print_detection_summary()
        print("  Setup complete. Start the workspace server with:")
        print(f"    aither serve --workspace --port {port}")
        return

    app = create_app(
        identity=args.identity,
        config=config,
        fleet_path=fleet_path,
        fleet_agents=fleet_agents,
    )

    # ── Workspace mode: mount portal-kit-backend routers ──
    if workspace_mode:
        _mount_workspace_routers(app, port)

    import uvicorn

    if config.gateway_url and config.aither_api_key:
        gateway_line = f"  Gateway: {config.gateway_url} (will register on startup)"
    else:
        gateway_line = (
            "  Gateway: not configured — set AITHER_API_KEY to connect\n"
            "  Demo:    https://demo.aitherium.com"
        )

    if is_fleet:
        agents_str = fleet_agents if fleet_agents else f"from {fleet_path}"
        print(f"Starting AitherADK fleet server — agents: {agents_str}, port: {port}")
        print(f"  Fleet:  GET  http://localhost:{port}/agents")
        print(f"  Chat:   POST http://localhost:{port}/agents/<name>/chat")
        print(f"  Forge:  POST http://localhost:{port}/forge/dispatch")
    elif workspace_mode:
        print(f"Starting AitherADK workspace server — identity: {args.identity}, port: {port}")
        print(f"  Portal: http://localhost:{port} (workspace UI)")
        print(f"  Embed:  http://localhost:{port}/?embedded=true")
    else:
        print(f"Starting AitherADK server — identity: {args.identity}, port: {port}")

    print(f"  Chat:   POST http://localhost:{port}/chat")
    print(f"  OpenAI: POST http://localhost:{port}/v1/chat/completions")
    print(f"  WS:     WS   ws://localhost:{port}/ws/chat")
    irc_port_env = os.getenv("AITHER_IRC_PORT", "")
    if irc_port_env:
        print(f"  IRC:    TCP  localhost:{irc_port_env} (mIRC, WeeChat, HexChat, irssi)")
    print(f"  MCP:    POST http://localhost:{port}/mcp (JSON-RPC 2.0)")
    print(f"  A2A:    POST http://localhost:{port}/a2a (Google A2A v0.3.0)")
    print(f"  Card:   GET  http://localhost:{port}/.well-known/agent.json")
    print(f"  Mail:   POST http://localhost:{port}/mail/send")
    print(f"  Health: GET  http://localhost:{port}/health")
    print(f"  Docs:   GET  http://localhost:{port}/docs")
    print(gateway_line)
    # Publish where we actually bound so consumers (MCP delegation, AitherShell) DISCOVER
    # the daemon instead of hardcoding a port that can silently drift out of agreement.
    try:
        from adk.daemon_endpoint import clear_daemon_url, publish_daemon_url

        # Prove we can actually BIND before advertising ourselves. Publishing first is how
        # a daemon that never starts still overwrites the live daemon's entry: on
        # 2026-07-29 one launched on a Windows excluded port range, published, failed to
        # bind with WinError 10013, and took the healthy :9001 daemon's endpoint down with
        # it on the way out. An advertised address that was never listened on is worse than
        # no advertisement, because consumers trust the file over their default.
        _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            _probe.bind((host, port))
        finally:
            _probe.close()
        published = publish_daemon_url(host, port)
        print(f"  Endpoint published: {published} (~/.aither/daemon.json)")
    except OSError as exc:
        print(f"  Endpoint NOT published — cannot bind {host}:{port} ({exc})")
        clear_daemon_url = None
    except Exception:  # noqa: BLE001 — discovery is an optimisation, never fatal
        clear_daemon_url = None
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        if clear_daemon_url:
            clear_daemon_url()


def main_workspace():
    """CLI entry point: adk-workspace — shortcut for `adk-serve --workspace`."""
    import sys
    if "--workspace" not in sys.argv:
        sys.argv.insert(1, "--workspace")
    main()


if __name__ == "__main__":
    main()
