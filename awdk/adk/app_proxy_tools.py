"""App proxy tools — register HTTP-based tools from a companion app container.

When ADK_APP_PROXY_URL is set (e.g., http://localhost:8900), this module:
1. Reads the app manifest (ADK_APP_MANIFEST env or auto-detect from ADK_APP_PROXY_URL/api/manifest)
2. Generates proxy tool functions that forward calls to the app's HTTP endpoints
3. Registers them on the AitherAgent so the LLM can call app-specific tools

This is the bridge between sovereign deployment app containers and the ADK agent's ReAct loop.

Usage:
    # Automatic — called from AitherAgent.__init__ when env vars are set
    from adk.app_proxy_tools import register_app_proxy_tools
    register_app_proxy_tools(agent)

Environment variables:
    ADK_APP_PROXY_URL: Base URL of the companion app (e.g., http://localhost:8900)
    ADK_APP_MANIFEST: JSON string of app_tools definitions (optional)
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adk.agent import AitherAgent

logger = logging.getLogger("adk.app_proxy_tools")

# Default tools to register when no manifest is provided —
# probes the app's /api/tools/list endpoint for self-description
_FALLBACK_TOOLS = [
    {
        "name": "app_query",
        "description": "Send a query to the companion app's AI endpoint",
        "endpoint": "/api/chat",
        "method": "POST",
        "parameters": {
            "message": {"type": "string", "required": True},
        },
    },
]


def _make_proxy_fn(base_url: str, tool_def: dict):
    """Create a sync/async function that proxies an HTTP call to the app."""
    endpoint = tool_def.get("endpoint", "/api/tools/" + tool_def["name"])
    method = tool_def.get("method", "POST").upper()
    url = base_url.rstrip("/") + endpoint

    async def _proxy(**kwargs: Any) -> str:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                if method == "GET":
                    resp = await client.get(url, params=kwargs)
                else:
                    resp = await client.post(url, json=kwargs)
                resp.raise_for_status()
                return resp.text
        except httpx.HTTPStatusError as exc:
            return json.dumps({
                "error": f"App returned {exc.response.status_code}",
                "detail": exc.response.text[:500],
            })
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    _proxy.__name__ = tool_def["name"]
    _proxy.__doc__ = tool_def.get("description", f"Proxy call to {endpoint}")
    return _proxy


def _build_parameters_schema(params_def: dict) -> dict:
    """Convert a simplified parameter definition to JSON Schema."""
    properties = {}
    required = []
    for name, spec in params_def.items():
        if isinstance(spec, dict):
            prop = {"type": spec.get("type", "string")}
            if "enum" in spec:
                prop["enum"] = spec["enum"]
            if "default" in spec:
                prop["default"] = spec["default"]
            if "description" in spec:
                prop["description"] = spec["description"]
            if "minimum" in spec:
                prop["minimum"] = spec["minimum"]
            if "maximum" in spec:
                prop["maximum"] = spec["maximum"]
            properties[name] = prop
            if spec.get("required", False):
                required.append(name)
        else:
            properties[name] = {"type": "string"}

    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _load_manifest_tools(base_url: str) -> list[dict]:
    """Load tool definitions from manifest env or app endpoint."""
    # Try env var first (set by deploy.py from app_manifests/*.yaml)
    manifest_json = os.environ.get("ADK_APP_MANIFEST", "")
    if manifest_json:
        try:
            data = json.loads(manifest_json)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "app_tools" in data:
                return data["app_tools"]
        except json.JSONDecodeError:
            logger.warning("ADK_APP_MANIFEST is not valid JSON, trying app endpoint")

    # Try fetching from app's self-description endpoint
    try:
        import httpx

        resp = httpx.get(
            base_url.rstrip("/") + "/api/tools/list",
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "tools" in data:
                return data["tools"]
    except Exception:
        pass

    return _FALLBACK_TOOLS


def register_app_proxy_tools(agent: AitherAgent) -> int:
    """Register HTTP proxy tools from a companion app container.

    Reads ADK_APP_PROXY_URL and registers tools that forward calls
    to the app's HTTP endpoints.

    Returns:
        Number of tools registered.
    """
    base_url = os.environ.get("ADK_APP_PROXY_URL", "")
    if not base_url:
        return 0

    tool_defs = _load_manifest_tools(base_url)
    count = 0

    for td in tool_defs:
        name = td.get("name")
        if not name:
            continue

        fn = _make_proxy_fn(base_url, td)
        params = _build_parameters_schema(td.get("parameters", {}))

        from adk.tools import ToolDef

        tool = ToolDef(
            name=name,
            description=td.get("description", f"App tool: {name}"),
            parameters=params,
            fn=fn,
            is_async=True,
        )
        agent._tools._tools[name] = tool
        count += 1

    if count:
        logger.info(
            "Registered %d app proxy tools from %s on agent %s",
            count, base_url, agent.name,
        )

    return count
