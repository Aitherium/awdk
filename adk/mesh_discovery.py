"""Mesh agent discovery — enumerate every agent in the mesh with its invoke_url,
inference backend, and A2A skills, so you can chat with / command any of them, each
responding on ITS OWN inference.

Sources (merged; owner-auth for the cloud registry, per the MCP-gateway-first model):

  1. Cloud agent registry (source of truth) — owner-authed, per-tenant:
       GET {portal}/api/genesis/v1/agent/agent-endpoints
     Fields: name, invoke_url, reach (tunnel|mesh), model, provider_hint,
             capabilities, status, last_seen.
  2. Platform A2A fleet — the platform's own service-agents + their skills + Ed25519
     public_key (for signed A2A command/control):
       GET {portal}/api/genesis/a2a/fleet?refresh=true   (best-effort)
  3. Local supplement — ~/.aither/agents.json: a list of manually-added endpoints
     (an OpenAI-compatible inference server, a self-hosted agent whose wrapper isn't
     registered yet, etc.). Lets you `adk chat <name>` a raw endpoint immediately.

Fail-closed: no owner token -> the cloud registry is skipped with a clear warning; we
never fabricate agents. The local + a2a-fleet sources still work.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from adk.config import load_saved_config


@dataclass
class AgentRef:
    """One discoverable agent in the mesh."""

    name: str
    invoke_url: str = ""
    reach: str = "unknown"          # tunnel | mesh | lan | local | unknown
    model: str = ""                 # e.g. Bonsai-27B-Q1_0.gguf
    provider_hint: str = ""         # e.g. llamacpp | ollama | genesis | vllm
    capabilities: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)   # A2A skill ids (for `adk invoke`)
    public_key: str = ""            # Ed25519 pubkey for signed-A2A trust
    status: str = "unknown"         # online | registered | unknown
    source: str = "unknown"         # registry | a2a-fleet | local

    # Explicit chat protocol override from agents.json ("adk" | "openai"); else inferred.
    chat_protocol_override: str = ""

    @property
    def chat_protocol(self) -> str:
        """How to chat with this agent:
          "adk"    -> a full adk agent server, POST {url}/chat/stream (AitherOS SSE).
          "openai" -> a raw OpenAI-compatible inference server (llama.cpp / ollama /
                      vllm), POST {url}/v1/chat/completions.
        Explicit override wins; else inferred from provider_hint (a raw inference
        backend => openai) — a registered adk agent defaults to "adk"."""
        if self.chat_protocol_override in ("adk", "openai"):
            return self.chat_protocol_override
        if self.provider_hint.lower() in ("llamacpp", "ollama", "openai", "vllm", "lmstudio"):
            return "openai"
        return "adk"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "invoke_url": self.invoke_url, "reach": self.reach,
            "model": self.model, "provider_hint": self.provider_hint,
            "capabilities": self.capabilities, "skills": self.skills,
            "public_key": self.public_key, "status": self.status, "source": self.source,
            "chat_protocol": self.chat_protocol,
        }


def _portal_base() -> str:
    # The control plane hosts the agent registry (/api/genesis/v1/agent/*). It is
    # NOT veil.aitherium.com: that host is a GitHub Pages STATIC export and
    # answers 404 to every API path, which surfaces only as
    # "Cloud registry unreachable" and an empty agent list.
    # returns 200 with a valid owner bearer; the login `endpoint`
    # (portal.aitherium.com) 401s on that path — do NOT use it here (verified live
    # 2026-07-16). The registry 401 seen in the field is a TOKEN problem (see
    # _owner_token), not an endpoint one. Env override wins; veil is the default.
    return os.getenv(
        "AITHER_PORTAL_URL",
        load_saved_config().get("portal_url", "") or "https://api.aitherium.com",
    ).rstrip("/")


def _owner_token() -> str:
    saved = load_saved_config()
    return (
        os.getenv("AITHER_API_KEY", "")
        or saved.get("api_key", "")
        or os.getenv("AITHER_PORTAL_TOKEN", "")
    )


def _local_agents_path() -> Path:
    explicit = os.getenv("AITHER_AGENTS_FILE", "").strip()
    if explicit:
        return Path(explicit)
    home = os.environ.get("AITHER_HOME") or os.environ.get("HOME") \
        or os.environ.get("USERPROFILE") or "."
    return Path(home) / ".aither" / "agents.json"


def _load_local_agents() -> list[AgentRef]:
    """Read ~/.aither/agents.json — a list of manually-added endpoints. Never raises."""
    path = _local_agents_path()
    try:
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("agents", data) if isinstance(data, dict) else data
    # agents.json is written by the adk daemon as a DICT keyed by name; also accept
    # a plain list. Normalize to a list of dicts.
    if isinstance(items, dict):
        items = list(items.values())
    out: list[AgentRef] = []
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict) or not it.get("name"):
                continue
            out.append(AgentRef(
                name=str(it["name"]),
                invoke_url=str(it.get("invoke_url", it.get("url", ""))),
                reach=str(it.get("reach", "local")),
                model=str(it.get("model", "")),
                provider_hint=str(it.get("provider_hint", it.get("backend", ""))),
                capabilities=list(it.get("capabilities", []) or []),
                skills=list(it.get("skills", []) or []),
                public_key=str(it.get("public_key", "")),
                status=str(it.get("status", "registered")),
                source="local",
                chat_protocol_override=str(it.get("chat_protocol", "")),
            ))
    return out


async def _fetch_registry(warnings: list[str]) -> list[AgentRef]:
    """Cloud agent registry (owner-auth). Fail-closed: no token -> skip + warn."""
    token = _owner_token()
    if not token:
        warnings.append(
            "No owner token — cloud registry skipped. Run `adk login` (device flow) "
            "or set AITHER_API_KEY to discover fleet-registered agents.")
        return []
    saved = load_saved_config()
    url = f"{_portal_base()}/api/genesis/v1/agent/agent-endpoints"
    headers = {"Authorization": f"Bearer {token}"}
    tid = saved.get("tenant_id", "") or os.getenv("AITHER_TENANT_ID", "")
    if tid:
        headers["X-Tenant-ID"] = tid
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url, headers=headers)
        if r.status_code in (401, 402, 403):
            warnings.append(f"Cloud registry auth/entitlement error ({r.status_code}).")
            return []
        r.raise_for_status()
        body = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        warnings.append(f"Cloud registry unreachable: {exc}")
        return []
    rows = body.get("endpoints", body) if isinstance(body, dict) else body
    out: list[AgentRef] = []
    for e in rows if isinstance(rows, list) else []:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        caps = e.get("capabilities", "")
        caps_list = caps.split(",") if isinstance(caps, str) else list(caps or [])
        out.append(AgentRef(
            name=str(e["name"]),
            invoke_url=str(e.get("invoke_url", e.get("url", ""))),
            reach=str(e.get("reach", "unknown")),
            model=str(e.get("model", "")),
            provider_hint=str(e.get("provider_hint", "")),
            capabilities=[c.strip() for c in caps_list if c.strip()],
            status=str(e.get("status", "registered")),
            source="registry",
        ))
    return out


async def _fetch_a2a_fleet(warnings: list[str]) -> list[AgentRef]:
    """Platform A2A fleet (service-agents + skills + public_key). Best-effort."""
    token = _owner_token()
    url = f"{_portal_base()}/api/genesis/a2a/fleet"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url, headers=headers, params={"refresh": "false"})
        r.raise_for_status()
        body = r.json()
    except (httpx.HTTPError, ValueError):
        return []  # best-effort; not a hard failure
    out: list[AgentRef] = []
    for scope in ("local", "remote"):
        for e in body.get(scope, []) if isinstance(body, dict) else []:
            if not isinstance(e, dict) or not e.get("name"):
                continue
            card = e.get("agent_card", {}) if isinstance(e.get("agent_card"), dict) else {}
            out.append(AgentRef(
                name=str(e["name"]),
                invoke_url=str(e.get("url", card.get("agent_url", ""))),
                reach="mesh" if scope == "remote" else "platform",
                skills=list(e.get("skills", card.get("skills", [])) or []),
                public_key=str(e.get("public_key", "")),
                status="online",
                source="a2a-fleet",
            ))
    return out


def _merge(*groups: list[AgentRef]) -> list[AgentRef]:
    """Merge by name. Registry is authoritative for reach/model/invoke_url; a2a-fleet
    fills skills/public_key; local supplements agents not otherwise present."""
    by_name: dict[str, AgentRef] = {}
    # order: a2a-fleet (weakest) -> local -> registry (strongest for core fields)
    for group in groups:
        for a in group:
            cur = by_name.get(a.name)
            if cur is None:
                by_name[a.name] = a
                continue
            # fill blanks + always carry skills/public_key from whichever has them
            cur.invoke_url = a.invoke_url or cur.invoke_url
            cur.reach = a.reach if a.reach not in ("unknown", "") else cur.reach
            cur.model = a.model or cur.model
            cur.provider_hint = a.provider_hint or cur.provider_hint
            cur.capabilities = cur.capabilities or a.capabilities
            cur.skills = cur.skills or a.skills
            cur.public_key = cur.public_key or a.public_key
            cur.status = a.status if a.status != "unknown" else cur.status
            if a.source == "registry":
                cur.source = "registry"
    return sorted(by_name.values(), key=lambda x: x.name)


async def discover_agents() -> tuple[list[AgentRef], list[str]]:
    """Discover every agent in the mesh. Returns (agents, warnings). Never raises."""
    warnings: list[str] = []
    a2a = await _fetch_a2a_fleet(warnings)
    local = _load_local_agents()
    registry = await _fetch_registry(warnings)
    return _merge(a2a, local, registry), warnings


async def resolve_agent(name: str) -> AgentRef | None:
    """Resolve one agent by name (case-insensitive) for chat/invoke. None if not found."""
    agents, _ = await discover_agents()
    lname = name.strip().lower()
    for a in agents:
        if a.name.lower() == lname:
            return a
    # prefix match convenience (e.g. "opti" -> "optiplex-agent")
    for a in agents:
        if a.name.lower().startswith(lname):
            return a
    return None
