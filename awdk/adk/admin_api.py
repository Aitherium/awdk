"""Admin/settings API for the built-in awdk web console.

Every route lives under ``/admin/*`` and is registered on the same FastAPI app
that ``adk/server.py`` builds. ``/admin`` is deliberately NOT in the server's
``_skip_auth_paths`` set, so the existing bearer middleware
(``server.py:_auth_middleware``) gates every call — the same callback bearer the
chat page receives in its ``#k=`` fragment. The surface is reachable over the
public trycloudflare tunnel, so the security posture matters:

* provider API keys are NEVER returned in full — only masked previews;
* ``PATCH /admin/config`` writes only an allowlist of safe scalar fields
  (writing e.g. ``aither_toolpack_dirs`` would be arbitrary-code-execution via
  the next pack load, so it is hard-denied);
* adding an external MCP server is a two-step prepare→confirm flow and the URL
  is SSRF-guarded (loopback / private / link-local ranges denied by default) —
  an attacker-supplied MCP server injects tools straight into the ReAct loop;
* the log tail is scrubbed of bearer tokens / API keys before it is returned
  (the token that gates this very API can otherwise leak through it).

The routes are registered via :func:`register_admin_routes`, which receives the
``get_agent`` coroutine and shared ``state`` dict as closures from
``create_app`` (an APIRouter cannot close over those), keeping ``server.py``
thin.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from adk.config import load_saved_config, save_saved_config

# ── Provider-key store (replicated from cli.py to avoid importing the 9k-line
#    CLI module into the server runtime — cli.py imports server.py, so importing
#    it here would be a circular import). Keep in sync with cli.py:3862-3899. ──

_KNOWN_PROVIDERS = {
    "openai": {"env": "OPENAI_API_KEY", "label": "OpenAI"},
    "anthropic": {"env": "ANTHROPIC_API_KEY", "label": "Anthropic"},
    "deepseek": {"env": "DEEPSEEK_API_KEY", "label": "DeepSeek"},
    "google": {"env": "GOOGLE_API_KEY", "label": "Google AI"},
    "openrouter": {"env": "OPENROUTER_API_KEY", "label": "OpenRouter"},
    "groq": {"env": "GROQ_API_KEY", "label": "Groq"},
    "together": {"env": "TOGETHER_API_KEY", "label": "Together AI"},
}


def _keys_path() -> Path:
    d = Path.home() / ".aither"
    d.mkdir(parents=True, exist_ok=True)
    return d / "provider_keys.json"


def _load_provider_keys() -> dict:
    p = _keys_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_provider_keys(data: dict) -> None:
    p = _keys_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    if sys.platform != "win32":
        os.chmod(p, 0o600)


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return key[:4] + "..." + key[-4:]


# ── Config write allowlist (Athena finding #3: PATCH → RCE) ──
# Only these scalar preference fields may be written through the config editor.
# Anything that resolves to a filesystem path, a base URL, or a backend/pack
# search dir is intentionally EXCLUDED — those are the RCE / SSRF vectors and
# must go through the dedicated, validated endpoints (LLM switch, MCP add).
_CONFIG_ALLOWLIST = {
    "model",
    "cloud_mode",
    "prefer_local",
    "phonehome_enabled",
    "json_logging",
    "log_level",
    "temperature",
    "max_tokens",
    "effort_default",
}

# Keys whose VALUES must be masked when read back (never echo secrets).
_SECRET_HINT = re.compile(r"(key|token|secret|password|bearer)", re.IGNORECASE)

# Log-line scrubbing (Athena finding #4: log tail leaks the bearer).
_LOG_REDACTIONS = [
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"), "Bearer [REDACTED]"),
    (re.compile(r"#k=[A-Za-z0-9._\-%]+"), "#k=[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9._\-]+"), "sk-[REDACTED]"),
    (re.compile(r"\baither_sk_[A-Za-z0-9._\-]+"), "aither_sk_[REDACTED]"),
    (re.compile(r"\b(?:ghp|ghs)_[A-Za-z0-9]+"), "[REDACTED]"),
]


def _redact_log_line(line: str) -> str:
    for pat, repl in _LOG_REDACTIONS:
        line = pat.sub(repl, line)
    return line


def _mask_config(cfg: dict) -> dict:
    """Return a copy of a saved-config dict with secret-looking values masked."""
    out: dict = {}
    for k, v in cfg.items():
        if isinstance(v, str) and v and _SECRET_HINT.search(k):
            out[k] = _mask_key(v)
        elif isinstance(v, dict):
            out[k] = _mask_config(v)
        else:
            out[k] = v
    return out


def _json_safe(v: Any) -> Any:
    """Coerce a config field value into something JSON-serializable."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    return str(v)


def _dump_effective_config(cfg: Any) -> dict:
    """Dump the full live Config (all fields) with secret values masked.

    Surfaces the *effective* runtime configuration — every field, not just the
    subset that was written to config.yaml — so the console can show a human the
    complete picture. Secret-looking fields are masked; writable ones are flagged
    by the caller against _CONFIG_ALLOWLIST.
    """
    if cfg is None:
        return {}
    fields = getattr(cfg, "__dataclass_fields__", None)
    names = list(fields.keys()) if fields else [
        k for k in vars(cfg) if not k.startswith("_")
    ]
    out: dict = {}
    for name in names:
        try:
            val = getattr(cfg, name)
        except AttributeError:
            continue
        if isinstance(val, str) and val and _SECRET_HINT.search(name):
            out[name] = _mask_key(val)
        else:
            out[name] = _json_safe(val)
    return out


# ── SSRF guard for user-supplied MCP server URLs (Athena finding #2) ──


def _url_is_safe(url: str) -> tuple[bool, str]:
    """Reject MCP URLs that resolve to loopback / private / link-local hosts.

    Returns (ok, reason). Override for trusted local dev with
    ``AITHER_ALLOW_PRIVATE_MCP=1`` (e.g. a co-located MCP node on localhost).
    """
    if os.getenv("AITHER_ALLOW_PRIVATE_MCP", "").lower() in ("1", "true", "yes"):
        return True, ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "unparseable URL"
    if parsed.scheme not in ("http", "https"):
        return False, f"scheme '{parsed.scheme}' not allowed (use http/https)"
    host = parsed.hostname
    if not host:
        return False, "URL has no host"
    # Resolve every A/AAAA record — a hostname can point at a private IP.
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        return False, f"DNS resolution failed: {exc}"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False, (
                f"host resolves to non-public address {addr} "
                "(loopback/private/link-local blocked; set "
                "AITHER_ALLOW_PRIVATE_MCP=1 to allow local servers)"
            )
    return True, ""


def _sanitize_tool_text(text: str) -> str:
    """Strip common prompt-injection markers from advertised MCP tool text."""
    if not text:
        return ""
    cleaned = re.sub(
        r"(?i)\b(ignore (all )?previous instructions|system prompt|you are now)\b",
        "[filtered]",
        text,
    )
    return cleaned[:600]


def _node_to_dict(node: Any) -> dict:
    """Best-effort serialization of a GraphMemory GraphNode."""
    for attr in ("model_dump", "_asdict", "dict"):
        fn = getattr(node, attr, None)
        if callable(fn):
            try:
                return fn()
            except (TypeError, ValueError):
                pass
    if hasattr(node, "__dict__"):
        return {k: v for k, v in vars(node).items() if not k.startswith("_")}
    return {
        "id": getattr(node, "id", None),
        "name": getattr(node, "name", None) or getattr(node, "label", None),
        "type": getattr(node, "node_type", None) or getattr(node, "type", None),
    }


# ── MCP external-server persistence (in saved config under mcp.external_servers) ──


def _load_mcp_servers() -> list[dict]:
    cfg = load_saved_config()
    return list((cfg.get("mcp") or {}).get("external_servers") or [])


def _save_mcp_servers(servers: list[dict]) -> None:
    cfg = load_saved_config()
    mcp = dict(cfg.get("mcp") or {})
    mcp["external_servers"] = servers
    save_saved_config({"mcp": mcp})


def _agent_log_path() -> Path:
    return Path.home() / ".aither" / "logs" / "agent.log"


# ── Per-pack settings (namespaced under packs_settings.<id> in saved config) ──
# A pack UI sees ONLY its own settings through the bridge — never the global
# config, sessions, or other packs' settings (owner requirement: pack scoping).


def _load_pack_settings(pack_id: str) -> dict:
    cfg = load_saved_config()
    all_settings = cfg.get("packs_settings") or {}
    s = all_settings.get(pack_id) if isinstance(all_settings, dict) else None
    return dict(s) if isinstance(s, dict) else {}


def _save_pack_settings(pack_id: str, settings: dict) -> None:
    cfg = load_saved_config()
    all_settings = dict(cfg.get("packs_settings") or {})
    all_settings[pack_id] = dict(settings)
    save_saved_config({"packs_settings": all_settings})


AgentGetter = Callable[..., Awaitable[Any]]


def register_admin_routes(
    app: FastAPI,
    *,
    get_agent: AgentGetter,
    state: dict[str, Any],
) -> None:
    """Register every ``/admin/*`` route on *app*.

    ``get_agent`` and ``state`` are the same closures ``create_app`` uses, so the
    admin surface operates on the live agent (backend swap, pack reload, and MCP
    registration all take effect without a restart).
    """

    # Pending MCP additions awaiting confirmation (prepare→confirm), by token.
    _pending_mcp: dict[str, dict] = {}

    async def _push_settings() -> None:
        """Best-effort mirror of the current settings to the portal profile.

        Wired lazily so a missing/older settings_sync module never breaks a
        mutation. Portal is the source of truth; local edits push up.
        """
        sync = state.get("settings_sync")
        if sync is None:
            return
        try:
            await sync.push_snapshot()
        except Exception:  # noqa: BLE001 — sync is advisory, never fail a mutation
            pass

    # ── Config ────────────────────────────────────────────────────────────

    @app.get("/admin/config")
    async def admin_get_config():
        saved = load_saved_config()
        effective = _dump_effective_config(state.get("config"))
        return {
            # Values persisted to ~/.aither/config.yaml (masked).
            "config": _mask_config(saved),
            # The FULL effective runtime Config — every field, not just saved
            # ones — so the console surfaces the complete picture.
            "effective": effective,
            "writable_fields": sorted(_CONFIG_ALLOWLIST),
            "note": (
                "Only writable_fields may be PATCHed. Backend base URLs, keys, "
                "and pack dirs are managed via /admin/llm/* and /admin/mcp/* — "
                "not free-form config — for safety. Non-writable fields are shown "
                "read-only; change them via env/startup."
            ),
        }

    @app.patch("/admin/config")
    async def admin_patch_config(request: Request):
        body = await request.json()
        patch = body.get("config", body) if isinstance(body, dict) else {}
        if not isinstance(patch, dict):
            return JSONResponse(status_code=400, content={"error": "expected an object"})
        rejected = [k for k in patch if k not in _CONFIG_ALLOWLIST]
        if rejected:
            return JSONResponse(
                status_code=403,
                content={
                    "error": "field_not_writable",
                    "rejected": rejected,
                    "writable_fields": sorted(_CONFIG_ALLOWLIST),
                    "hint": (
                        "These fields can execute code or redirect traffic at "
                        "startup and cannot be set through the config editor."
                    ),
                },
            )
        accepted = {k: patch[k] for k in patch}
        save_saved_config(accepted)
        await _push_settings()
        return {
            "ok": True,
            "applied": accepted,
            "effect": "restart-required for llm_backend-class fields; "
            "model/cloud_mode/prefer_local take effect on the next turn",
        }

    # ── LLM backend ───────────────────────────────────────────────────────

    @app.get("/admin/llm/status")
    async def admin_llm_status():
        agent = await get_agent()
        keys = _load_provider_keys()
        providers = []
        for pid, meta in _KNOWN_PROVIDERS.items():
            saved = keys.get(pid) or os.environ.get(meta["env"], "")
            providers.append({
                "id": pid,
                "label": meta["label"],
                "has_key": bool(saved),
                "key_preview": _mask_key(saved) if saved else "",
            })
        try:
            from adk.shell_launcher import _preflight_check
            have_local, local_desc = _preflight_check()
        except (ImportError, RuntimeError):
            have_local, local_desc = False, ""
        return {
            "active_backend": agent.llm.provider_name or "detecting...",
            "model": getattr(agent.llm, "_model", None),
            "local_backend_detected": have_local,
            "local_backend": local_desc,
            "providers": providers,
        }

    @app.post("/admin/llm/switch")
    async def admin_llm_switch(request: Request):
        body = await request.json()
        provider = (body.get("provider") or "").strip()
        if not provider:
            return JSONResponse(status_code=400, content={"error": "provider required"})
        model = body.get("model") or None
        base_url = body.get("base_url") or None
        # Prefer a stored key over any key echoed in the request.
        api_key = _load_provider_keys().get(provider) or body.get("api_key") or None
        agent = await get_agent()
        try:
            agent.llm.switch_backend(provider, base_url=base_url, api_key=api_key, model=model)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=400,
                content={"error": "switch_failed", "detail": _scrub(str(exc))},
            )
        persist = body.get("persist", True)
        if persist:
            save_saved_config({"llm_provider": provider, **({"model": model} if model else {})})
            await _push_settings()
        return {
            "ok": True,
            "active_backend": agent.llm.provider_name,
            "model": getattr(agent.llm, "_model", None),
            "persisted": bool(persist),
        }

    @app.post("/admin/llm/keys")
    async def admin_llm_set_key(request: Request):
        body = await request.json()
        provider = (body.get("provider") or "").strip()
        key = (body.get("api_key") or "").strip()
        if provider not in _KNOWN_PROVIDERS:
            return JSONResponse(
                status_code=400,
                content={"error": f"unknown provider '{provider}'",
                         "known": sorted(_KNOWN_PROVIDERS)},
            )
        if not key:
            return JSONResponse(status_code=400, content={"error": "api_key required"})
        keys = _load_provider_keys()
        keys[provider] = key
        _save_provider_keys(keys)
        os.environ[_KNOWN_PROVIDERS[provider]["env"]] = key
        # Provider keys are device-local secrets — deliberately NOT pushed to
        # the portal profile.
        return {"ok": True, "provider": provider, "key_preview": _mask_key(key)}

    @app.post("/admin/llm/test")
    async def admin_llm_test(request: Request):
        body = await request.json()
        provider = (body.get("provider") or "").strip()
        agent = await get_agent()
        target = provider or agent.llm.provider_name
        try:
            from adk.llm import Message
            provider_obj = await agent.llm.get_provider()
            resp = await provider_obj.chat([Message(role="user", content="Say OK")], max_tokens=8)
            text = (getattr(resp, "content", "") or "").strip()[:100]
            return {"ok": True, "provider": target, "response_preview": _scrub(text)}
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content={"ok": False, "provider": target, "error": _scrub(str(exc))},
            )

    # ── Packs ─────────────────────────────────────────────────────────────

    def _discover_manifests() -> dict[str, Any]:
        """Discovered pack manifests keyed by id ({} when the loader is absent)."""
        try:
            from adk.tool_pack_loader import get_tool_pack_loader
            return dict(get_tool_pack_loader().discover())
        except (ImportError, RuntimeError, AttributeError):
            return {}

    def _pack_summary(m: Any, enabled: set[str], active_tools: list[str]) -> dict:
        ui_tabs = getattr(m, "ui_tabs", []) or []
        return {
            "id": m.id,
            "name": m.name,
            "version": m.version,
            "category": m.category,
            "tier": m.tier,
            "description": m.description,
            "icon": getattr(m, "icon", ""),
            "tags": list(getattr(m, "tags", []) or []),
            "skills": list(getattr(m, "skills", []) or []),
            "mcp_tools": list(m.mcp_tools or []),
            "entitlements": list(m.entitlements or []),
            "min_tier": m.min_tier,
            "deprecated": bool(getattr(m, "deprecated", False)),
            "redirect_to": getattr(m, "redirect_to", ""),
            "has_ui": bool(ui_tabs),
            "ui_tabs": ui_tabs,
            "enabled": m.id in enabled,
            "live_tool_count": sum(1 for t in active_tools if m.tool_matches(t)),
        }

    @app.get("/admin/packs")
    async def admin_packs_list():
        agent = await get_agent()
        active_tools = [t.name for t in agent._tools.list_tools()]
        saved = load_saved_config()
        enabled = list(saved.get("required_packs") or [])
        enabled_set = set(enabled)
        packs = [
            _pack_summary(m, enabled_set, active_tools)
            for m in _discover_manifests().values()
        ]
        packs.sort(key=lambda p: ((0 if p["enabled"] else 1), str(p["id"])))
        return {"active_tool_count": len(active_tools), "enabled_packs": enabled, "available": packs}

    @app.post("/admin/packs/enable")
    async def admin_packs_enable(request: Request):
        body = await request.json()
        pack = (body.get("pack") or body.get("id") or "").strip()
        if not pack:
            return JSONResponse(status_code=400, content={"error": "pack required"})
        saved = load_saved_config()
        enabled = list(saved.get("required_packs") or [])
        if pack not in enabled:
            enabled.append(pack)
        save_saved_config({"required_packs": enabled})
        result = await _reload_packs()
        await _push_settings()
        return {"ok": True, "enabled_packs": enabled, "reload": result}

    @app.post("/admin/packs/disable")
    async def admin_packs_disable(request: Request):
        body = await request.json()
        pack = (body.get("pack") or body.get("id") or "").strip()
        saved = load_saved_config()
        enabled = [p for p in (saved.get("required_packs") or []) if p != pack]
        save_saved_config({"required_packs": enabled})
        await _push_settings()
        # Note: tools already registered stay until restart; disabling removes it
        # from the enabled set so it won't re-register on the next reload/boot.
        return {"ok": True, "enabled_packs": enabled,
                "note": "already-loaded tools persist until restart"}

    @app.post("/admin/packs/reload")
    async def admin_packs_reload():
        return await _reload_packs()

    @app.post("/admin/packs/apply")
    async def admin_packs_apply(request: Request):
        """Materialize a bundled pack into THIS agent's ~/.aither/agents, enable
        it, and reload — so a pack can be pushed to any registered agent
        (including remotely, through the /mesh/agents/{name}/admin proxy) with no
        SSH. Only packs bundled with the agent's own adk build are accepted;
        arbitrary file uploads are deliberately not (moat/safety)."""
        import shutil
        from pathlib import Path

        body = await request.json()
        pack = (body.get("pack") or body.get("id") or "").strip()
        if not pack or "/" in pack or "\\" in pack or pack.startswith("."):
            return JSONResponse(status_code=400, content={"error": "valid pack name required"})
        packs_dir = Path(__file__).parent / "packs"
        available = ([p.name for p in packs_dir.glob("*") if (p / "agent.yaml").exists()]
                     if packs_dir.exists() else [])
        bundled = packs_dir / pack
        if not (bundled / "agent.yaml").exists():
            return JSONResponse(status_code=404,
                                content={"error": "pack_not_found", "pack": pack,
                                         "available": available})
        dest = Path.home() / ".aither" / "agents" / pack
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(bundled, dest)
        except OSError as exc:
            return JSONResponse(status_code=500,
                                content={"error": "copy_failed", "detail": _scrub(str(exc))})
        saved = load_saved_config()
        enabled = list(saved.get("required_packs") or [])
        if pack not in enabled:
            enabled.append(pack)
        save_saved_config({"required_packs": enabled})
        result = await _reload_packs()
        await _push_settings()
        return {"ok": True, "pack": pack, "installed_to": str(dest),
                "enabled": True, "enabled_packs": enabled, "reload": result}

    async def _reload_packs() -> dict:
        agent = await get_agent()
        before = len(agent._tools.list_tools())
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
                pass
        after = len(agent._tools.list_tools())
        return {"status": "reloaded" if reloaded else "noop",
                "tools_before": before, "tools_after": after,
                "tools_added": max(0, after - before)}

    # ── Pack detail / settings / pack-scoped tool invocation ─────────────
    # NOTE: static /admin/packs/* routes above are registered BEFORE these
    # parameterized ones (FastAPI matches in registration order).

    @app.get("/admin/packs/{pack_id}")
    async def admin_pack_detail(pack_id: str):
        m = _discover_manifests().get(pack_id)
        if m is None:
            return JSONResponse(status_code=404, content={"error": "pack_not_found"})
        agent = await get_agent()
        saved = load_saved_config()
        enabled_set = set(saved.get("required_packs") or [])
        live_tools = [
            {"name": t.name, "description": (t.description or "").strip()[:300]}
            for t in agent._tools.list_tools() if m.tool_matches(t.name)
        ]
        out = _pack_summary(m, enabled_set, [t["name"] for t in live_tools])
        out.update({
            "live_tools": live_tools,
            "persona_fragment_count": len(m.persona_fragments or []),
            "pricing": dict(getattr(m, "pricing", {}) or {}),
            "settings": _mask_config(_load_pack_settings(pack_id)),
        })
        return out

    @app.get("/admin/packs/{pack_id}/settings")
    async def admin_pack_settings_get(pack_id: str):
        if _discover_manifests().get(pack_id) is None:
            return JSONResponse(status_code=404, content={"error": "pack_not_found"})
        return {"pack": pack_id, "settings": _mask_config(_load_pack_settings(pack_id))}

    @app.patch("/admin/packs/{pack_id}/settings")
    async def admin_pack_settings_patch(pack_id: str, request: Request):
        if _discover_manifests().get(pack_id) is None:
            return JSONResponse(status_code=404, content={"error": "pack_not_found"})
        body = await request.json()
        patch = body.get("settings", body) if isinstance(body, dict) else None
        if not isinstance(patch, dict):
            return JSONResponse(status_code=400, content={"error": "expected an object"})
        current = _load_pack_settings(pack_id)
        for k, v in patch.items():
            key = str(k)[:64]
            if v is None:
                current.pop(key, None)
            elif isinstance(v, str) and len(v) > 2000:
                return JSONResponse(
                    status_code=400,
                    content={"error": "value_too_long", "field": key, "max": 2000},
                )
            elif isinstance(v, (str, int, float, bool)):
                # Pack settings are scalars only — no nested config, no lists —
                # so a pack UI cannot smuggle structured payloads into config.yaml.
                current[key] = v
            else:
                return JSONResponse(
                    status_code=400,
                    content={"error": "scalar_values_only", "field": key},
                )
        if len(current) > 50:
            return JSONResponse(status_code=400, content={"error": "too_many_settings (max 50)"})
        _save_pack_settings(pack_id, current)
        await _push_settings()
        return {"ok": True, "pack": pack_id, "settings": _mask_config(current)}

    @app.post("/admin/packs/{pack_id}/tools/{tool_name}/invoke")
    async def admin_pack_tool_invoke(pack_id: str, tool_name: str, request: Request):
        """Invoke one of *pack_id*'s own tools — the pack-UI bridge's only door.

        The sandboxed pack iframe never holds the bearer; the console parent
        calls this with its token. Ownership (tool ∈ pack's mcp_tools patterns)
        is enforced HERE, server-side — the parent's client-side check is UX
        only. The call runs inside the pack's data scope (see adk.pack_scope):
        file tools are jailed to ~/.aither/packs/<id>/data and any session_id
        is namespaced so pack activity never touches the owner's chat sessions.
        """
        import asyncio

        from adk.pack_scope import pack_scope, valid_pack_id

        if not valid_pack_id(pack_id):
            return JSONResponse(status_code=400, content={"error": "invalid_pack_id"})
        m = _discover_manifests().get(pack_id)
        if m is None:
            return JSONResponse(status_code=404, content={"error": "pack_not_found"})
        saved = load_saved_config()
        if pack_id not in (saved.get("required_packs") or []):
            return JSONResponse(status_code=403,
                                content={"error": "pack_not_enabled", "pack": pack_id})
        if not m.tool_matches(tool_name):
            return JSONResponse(status_code=403,
                                content={"error": "tool_not_in_pack",
                                         "tool": tool_name, "pack": pack_id})
        # Athena gate: a hostile manifest could declare broad globs (e.g.
        # "file_*") to claim adk BUILT-IN tools and reach owner data through
        # the bridge. Packs may only bridge-invoke tools they brought — the
        # built-in surface is never pack-invokable, regardless of manifest.
        try:
            from adk import builtin_tools as _bt
            if callable(getattr(_bt, tool_name, None)):
                return JSONResponse(
                    status_code=403,
                    content={"error": "builtin_tool_not_bridgeable", "tool": tool_name})
        except ImportError:
            pass
        try:
            body = await request.json()
        except (ValueError, RuntimeError):
            body = {}
        args = body.get("args", {}) if isinstance(body, dict) else {}
        if not isinstance(args, dict):
            return JSONResponse(status_code=400, content={"error": "args must be an object"})
        # Long-running research tools need more than a web timeout; cap at 10 min.
        try:
            timeout = min(600.0, max(1.0, float(body.get("timeout", 120))))
        except (TypeError, ValueError):
            timeout = 120.0
        sid = args.get("session_id")
        if isinstance(sid, str) and sid and not sid.startswith(f"pack-{pack_id}-"):
            args["session_id"] = f"pack-{pack_id}-{sid}"

        agent = await get_agent()
        if not any(t.name == tool_name for t in agent._tools.list_tools()):
            return JSONResponse(
                status_code=404,
                content={"error": "tool_not_registered", "tool": tool_name,
                         "hint": "pack tools may be entitlement-gated or not yet reloaded"})
        try:
            with pack_scope(pack_id):
                result = await asyncio.wait_for(
                    agent._tools.execute(tool_name, args), timeout=timeout)
        except asyncio.TimeoutError:
            return JSONResponse(status_code=504,
                                content={"error": "tool_timeout", "timeout_seconds": timeout})
        except Exception as exc:  # noqa: BLE001 — surface, never crash the console
            return JSONResponse(status_code=500,
                                content={"error": "tool_failed", "detail": _scrub(str(exc))[:400]})
        truncated = False
        if isinstance(result, str) and len(result) > 262_144:  # 256 KiB cap
            result = result[:262_144]
            truncated = True
        parsed: Any = result
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except (ValueError, TypeError):
                parsed = result
        return {"ok": True, "pack": pack_id, "tool": tool_name,
                "result": parsed, "truncated": truncated}

    # ── Aitherium pack catalog (server-side proxy, fail-soft offline) ─────
    # The console's "Available from Aitherium" section. Proxied here so the
    # browser needs no CORS exception and the portal token never reaches the
    # page. Service packs (e.g. media-forge) only exist in this catalog — the
    # local loader can never discover them.

    _catalog_cache: dict[str, Any] = {"data": None, "at": 0.0}
    _CATALOG_TTL = 3600.0
    # The catalog lives on Genesis behind the portal; deployments differ in
    # which proxy prefix is mounted, so probe the known shapes in order.
    _CATALOG_PATHS = (
        "/api/packs/catalog",
        "/v1/packs/catalog",
        "/api/bridge/genesis/api/v1/catalog/packs",
    )

    @app.get("/admin/catalog/packs")
    async def admin_catalog_packs():
        import httpx as _httpx

        now = time.time()
        if _catalog_cache["data"] is not None and now - _catalog_cache["at"] < _CATALOG_TTL:
            return _catalog_cache["data"]
        # Lazy import — settings_sync imports this module at load time.
        try:
            from adk.sync.settings import _default_portal_url, _resolve_token
            token = _resolve_token()
            portal = _default_portal_url()
        except ImportError:
            token, portal = "", ""
        if not token or not portal:
            return {"packs": [], "offline": True,
                    "note": "no portal token — sign in with `adk login` to browse the catalog"}
        installed = set(_discover_manifests())
        last_err = ""
        try:
            async with _httpx.AsyncClient(timeout=10) as client:
                for path in _CATALOG_PATHS:
                    try:
                        resp = await client.get(
                            f"{portal}{path}",
                            headers={"Authorization": f"Bearer {token}"})
                    except _httpx.HTTPError as exc:
                        last_err = str(exc)
                        continue
                    if resp.status_code != 200:
                        last_err = f"HTTP {resp.status_code} on {path}"
                        continue
                    try:
                        data = resp.json()
                    except ValueError:
                        last_err = f"non-JSON on {path}"
                        continue
                    raw = data.get("packs") if isinstance(data, dict) else None
                    if not isinstance(raw, list):
                        last_err = f"unexpected shape on {path}"
                        continue
                    packs = []
                    for p in raw:
                        if not isinstance(p, dict):
                            continue
                        packs.append({
                            "id": p.get("id"),
                            "name": p.get("name"),
                            "type": p.get("type") or p.get("category"),
                            "version": p.get("version"),
                            "description": (str(p.get("description") or ""))[:400],
                            "tier": p.get("tier"),
                            "icon": p.get("icon"),
                            "pricing": p.get("pricing") if isinstance(p.get("pricing"), dict) else {},
                            "licensed": bool(p.get("licensed", False)),
                            "installed": bool(p.get("installed", False)) or p.get("id") in installed,
                            "local": p.get("id") in installed,
                        })
                    out = {"packs": packs, "offline": False, "source": path,
                           "portal": portal, "count": len(packs)}
                    _catalog_cache["data"] = out
                    _catalog_cache["at"] = now
                    return out
        except Exception as exc:  # noqa: BLE001 — catalog is advisory, never raise
            last_err = str(exc)
        return {"packs": [], "offline": True,
                "note": _scrub(f"catalog unreachable ({last_err[:120]})")}

    # ── Sessions ──────────────────────────────────────────────────────────

    @app.get("/admin/sessions")
    async def admin_sessions_list():
        from adk.conversations import get_conversation_store
        store = get_conversation_store()
        return {"sessions": await store.list_sessions()}

    @app.get("/admin/sessions/{session_id}")
    async def admin_session_history(session_id: str):
        from adk.conversations import get_conversation_store
        store = get_conversation_store()
        return {"session_id": session_id, "messages": await store.load_full_history(session_id)}

    @app.delete("/admin/sessions/{session_id}")
    async def admin_session_delete(session_id: str):
        from adk.conversations import get_conversation_store
        store = get_conversation_store()
        return {"ok": await store.delete_session(session_id), "session_id": session_id}

    # ── Logs (redacted) ───────────────────────────────────────────────────

    @app.get("/admin/logs/tail")
    async def admin_logs_tail(request: Request):
        try:
            n = int(request.query_params.get("n", "200"))
        except ValueError:
            n = 200
        n = max(1, min(n, 2000))
        path = _agent_log_path()
        if not path.exists():
            return {"lines": [], "path": str(path), "note": "no log file yet"}
        try:
            raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return JSONResponse(status_code=500, content={"error": str(exc)})
        tail = raw[-n:]
        return {"lines": [_redact_log_line(ln) for ln in tail], "count": len(tail)}

    # ── Knowledge graph ───────────────────────────────────────────────────

    @app.get("/admin/graph/search")
    async def admin_graph_search(request: Request):
        query = request.query_params.get("q", "").strip()
        try:
            limit = int(request.query_params.get("limit", "10"))
        except ValueError:
            limit = 10
        agent = await get_agent()
        if agent._graph is None:
            return {"nodes": [], "note": "graph memory not enabled for this agent"}
        if not query:
            return {"nodes": []}
        nodes = await agent._graph.search(query, limit=max(1, min(limit, 50)))
        return {"nodes": [_node_to_dict(node) for node in nodes]}

    @app.get("/admin/graph/neighborhood")
    async def admin_graph_neighborhood(request: Request):
        entity = request.query_params.get("entity", "").strip()
        try:
            depth = int(request.query_params.get("depth", "2"))
        except ValueError:
            depth = 2
        agent = await get_agent()
        if agent._graph is None:
            return {"related": {}, "note": "graph memory not enabled for this agent"}
        if not entity:
            return {"related": {}}
        return {"entity": entity, "related": await agent._graph.get_related(entity, depth=max(1, min(depth, 4)))}

    @app.get("/admin/graph/stats")
    async def admin_graph_stats():
        agent = await get_agent()
        if agent._graph is None:
            return {"stats": {}, "note": "graph memory not enabled for this agent"}
        return {"stats": await agent._graph.get_stats()}

    # ── MCP external servers (prepare → confirm, SSRF-guarded) ─────────────

    @app.get("/admin/mcp/servers")
    async def admin_mcp_list():
        return {"servers": _load_mcp_servers()}

    @app.post("/admin/mcp/servers/prepare")
    async def admin_mcp_prepare(request: Request):
        body = await request.json()
        url = (body.get("url") or "").strip()
        if not url:
            return JSONResponse(status_code=400, content={"error": "url required"})
        safe, reason = _url_is_safe(url)
        if not safe:
            return JSONResponse(status_code=400, content={"error": "unsafe_url", "detail": reason})
        headers = body.get("headers") or None
        try:
            from adk.core.mcp import mcp_tools
            tools = await mcp_tools(url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content={"error": "mcp_unreachable", "detail": _scrub(str(exc))},
            )
        token = f"mcp-{int(time.time() * 1000)}-{len(_pending_mcp)}"
        preview = [
            {"name": getattr(t, "name", "?"),
             "description": _sanitize_tool_text(getattr(t, "description", ""))}
            for t in tools
        ]
        _pending_mcp[token] = {"url": url, "headers": headers,
                               "name": body.get("name") or url, "tools": preview}
        return {
            "confirm_token": token,
            "url": url,
            "tool_count": len(preview),
            "tools": preview,
            "warning": (
                "These tools will be injected into the agent's reasoning loop and "
                "can be invoked automatically. Only confirm servers you trust."
            ),
        }

    @app.post("/admin/mcp/servers/confirm")
    async def admin_mcp_confirm(request: Request):
        body = await request.json()
        token = (body.get("confirm_token") or "").strip()
        pending = _pending_mcp.pop(token, None)
        if not pending:
            return JSONResponse(status_code=400,
                                content={"error": "unknown or expired confirm_token"})
        agent = await get_agent()
        try:
            from adk.core.mcp import mcp_tools
            tools = await mcp_tools(pending["url"], headers=pending["headers"])
            for t in tools:
                agent._tools.register(t.__call__ if hasattr(t, "__call__") else t,
                                      name=getattr(t, "name", None),
                                      description=getattr(t, "description", ""))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=502,
                                content={"error": "register_failed", "detail": _scrub(str(exc))})
        servers = _load_mcp_servers()
        servers.append({"id": token, "url": pending["url"], "name": pending["name"],
                        "headers": pending["headers"], "added_at": time.time()})
        _save_mcp_servers(servers)
        await _push_settings()
        return {"ok": True, "id": token, "tools_registered": len(tools),
                "note": "tools available on the next chat turn"}

    @app.delete("/admin/mcp/servers/{server_id}")
    async def admin_mcp_delete(server_id: str):
        servers = _load_mcp_servers()
        remaining = [s for s in servers if s.get("id") != server_id]
        _save_mcp_servers(remaining)
        await _push_settings()
        return {"ok": True, "removed": server_id,
                "note": "registered tools persist until restart"}

    # ── Console meta (capabilities the UI adapts to) ──────────────────────

    @app.get("/admin/meta")
    async def admin_meta():
        from adk import __version__
        agent = None
        try:
            agent = await get_agent()
        except (ImportError, RuntimeError, OSError, ConnectionError):
            pass
        return {
            "version": __version__,
            "agent": getattr(agent, "name", None),
            "cli_enabled": _cli_enabled(),
            "settings_sync": state.get("settings_sync") is not None,
        }

    # ── API explorer route list ──────────────────────────────────────────
    # Introspects the live route table instead of /openapi.json — the server
    # uses `from __future__ import annotations`, which leaves `request: Request`
    # params as unresolved ForwardRefs and makes FastAPI's schema generation
    # 500. The route table is always available and is all the explorer needs.

    @app.get("/admin/routes")
    async def admin_routes():
        from fastapi.routing import APIRoute
        out = []
        for r in app.routes:
            if not isinstance(r, APIRoute):
                continue
            summary = r.summary or ((getattr(r.endpoint, "__doc__", "") or "").strip().split("\n")[0])
            for m in sorted(r.methods or []):
                if m in ("HEAD", "OPTIONS"):
                    continue
                out.append({"method": m, "path": r.path, "summary": summary})
        out.sort(key=lambda x: (x["path"], x["method"]))
        return {"routes": out, "total": len(out)}

    # ── Interactive CLI (bearer-gated + manifest-restricted + disable-able) ──
    # An interactive CLI reachable over the public tunnel is powerful, so it is
    # (a) gated by the same bearer as everything else, (b) restricted to the ADK
    # command manifest — NEVER arbitrary shell (args are passed as a list, no
    # shell=True), and (c) independently disable-able via AITHER_ADMIN_CLI=0.

    @app.get("/admin/cli/commands")
    async def admin_cli_commands():
        if not _cli_enabled():
            return JSONResponse(status_code=403, content=_cli_disabled_body())
        try:
            from adk.cli import build_command_manifest
            manifest = build_command_manifest()
        except (ImportError, RuntimeError) as exc:
            return JSONResponse(status_code=500, content={"error": _scrub(str(exc))})
        return {"commands": manifest, "total": len(manifest)}

    @app.post("/admin/cli/exec")
    async def admin_cli_exec(request: Request):
        if not _cli_enabled():
            return JSONResponse(status_code=403, content=_cli_disabled_body())
        import asyncio
        import subprocess

        body = await request.json()
        command = (body.get("command") or "").strip()
        args = body.get("args") or []
        if not command:
            return JSONResponse(status_code=400, content={"error": "command is required"})
        if not isinstance(args, list):
            return JSONResponse(status_code=400, content={"error": "args must be a list"})
        try:
            from adk.cli import build_command_manifest
            valid = {c["name"] for c in build_command_manifest()}
        except (ImportError, RuntimeError) as exc:
            return JSONResponse(status_code=500, content={"error": _scrub(str(exc))})
        if command not in valid:
            return JSONResponse(
                status_code=400,
                content={"error": f"unknown command '{command}'", "valid": sorted(valid)},
            )
        # List args, no shell=True → no shell injection; only manifest commands run.
        cmd = [sys.executable, "-m", "adk.cli", command] + [str(a) for a in args]
        try:
            result = await asyncio.to_thread(
                subprocess.run, cmd, capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            return JSONResponse(status_code=504, content={"error": "command timed out (120s)"})
        except FileNotFoundError:
            return JSONResponse(status_code=500, content={"error": "python not found"})
        return {
            "command": command,
            "args": args,
            "stdout": _scrub(result.stdout),
            "stderr": _scrub(result.stderr),
            "returncode": result.returncode,
        }


def _scrub(text: str) -> str:
    """Scrub secrets from any free-text (error strings, model previews)."""
    return _redact_log_line(text or "")


def _cli_enabled() -> bool:
    """The console CLI is on by default; set AITHER_ADMIN_CLI=0 to disable it."""
    return os.getenv("AITHER_ADMIN_CLI", "1").strip().lower() not in ("0", "false", "off", "no")


def _cli_disabled_body() -> dict:
    return {
        "error": "cli_disabled",
        "detail": "The admin console CLI is disabled on this agent (AITHER_ADMIN_CLI=0).",
    }
