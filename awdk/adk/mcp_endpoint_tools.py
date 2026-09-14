"""Customer MCP "hands" — attach a self-hosted agent's own MCP servers as tools.

The platform (AitherOS) lets a customer register the MCP servers running on THEIR
box, then relays the list to this adk loop in the ``/stream`` body as
``mcp_endpoints``. This module is the client side: for each ``{name, url}`` it
speaks MCP JSON-RPC (``tools/list`` to discover, ``tools/call`` to invoke) and
registers a proxy tool on the agent for every tool the server advertises — so the
ReAct loop can call the customer's own tools. This is the "hands" of the
brain (model) · body (this loop) · hands (these MCP tools) trifecta.

Mirrors :mod:`adk.app_proxy_tools` (HTTP companion-app tools), but for the
JSON-RPC MCP wire format that :mod:`adk.mcp_server` already speaks::

    POST {url}  {"jsonrpc":"2.0","method":"tools/list","id":1}
      -> {"result": {"tools": [{"name","description","inputSchema"}]}}
    POST {url}  {"jsonrpc":"2.0","method":"tools/call",
                 "params":{"name":<tool>,"arguments":{...}},"id":2}
      -> {"result": {"content": [{"type":"text","text":"..."}]}}

Best-effort: an unreachable endpoint registers nothing and never raises, so chat
still works (just without that server's tools).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adk.agent import AitherAgent

logger = logging.getLogger("adk.mcp_endpoint_tools")


def _headers_for(ep: dict) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    extra = ep.get("headers") or {}
    if isinstance(extra, dict):
        h.update({str(k): str(v) for k, v in extra.items()})
    token = ep.get("token") or ep.get("bearer")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _list_tools(url: str, headers: dict[str, str]) -> list[dict]:
    """MCP ``tools/list`` — returns the server's tool descriptors, or [] on failure."""
    import httpx

    try:
        resp = httpx.post(
            url,
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
            headers=headers,
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            return (data.get("result") or {}).get("tools") or []
    except Exception as exc:  # noqa: BLE001
        logger.info("MCP tools/list failed for %s: %s", url, exc)
    return []


def _make_mcp_proxy_fn(url: str, tool_name: str, headers: dict[str, str]):
    """An async tool fn that forwards a ``tools/call`` to the customer's MCP server."""

    async def _proxy(**kwargs: Any) -> str:
        import httpx

        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": kwargs},
            "id": 2,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            if data.get("error"):
                return json.dumps({"error": data["error"]})
            result = data.get("result") or {}
            content = result.get("content") or []
            texts = [
                c.get("text", "")
                for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
            return json.dumps(result)
        except httpx.HTTPStatusError as exc:
            return json.dumps({"error": f"MCP returned {exc.response.status_code}",
                               "detail": exc.response.text[:500]})
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": str(exc)})

    _proxy.__name__ = tool_name
    return _proxy


def register_mcp_endpoint_tools(agent: "AitherAgent", endpoints: list[dict] | None) -> int:
    """Register the customer's self-hosted MCP server tools on ``agent``.

    ``endpoints``: a list of ``{name, url, [headers], [token]}`` (as relayed by the
    platform's SelfHostedAdkBackend). Tool names are namespaced ``{endpoint}__{tool}``
    to avoid collisions across servers. Returns the number of tools registered.
    Idempotent: re-registering the same tool just overwrites it.
    """
    from adk.tools import ToolDef

    count = 0
    for ep in endpoints or []:
        if not isinstance(ep, dict):
            continue
        url = (ep.get("url") or "").rstrip("/")
        if not url:
            continue
        ep_name = (ep.get("name") or "mcp").strip() or "mcp"
        headers = _headers_for(ep)
        for td in _list_tools(url, headers):
            tname = td.get("name")
            if not tname:
                continue
            reg_name = f"{ep_name}__{tname}"
            fn = _make_mcp_proxy_fn(url, str(tname), headers)
            fn.__name__ = reg_name
            tool = ToolDef(
                name=reg_name,
                description=td.get("description", f"MCP tool '{tname}' from '{ep_name}'"),
                parameters=td.get("inputSchema") or {"type": "object", "properties": {}},
                fn=fn,
                is_async=True,
            )
            agent._tools._tools[reg_name] = tool
            count += 1

    if count:
        logger.info(
            "Registered %d customer MCP tool(s) from %d endpoint(s) on agent %s",
            count, len(endpoints or []), agent.name,
        )
    return count


__all__ = ["register_mcp_endpoint_tools"]
