"""Meta-tools for runtime tool retrieval and searching.

Instead of pre-loading all 1227 MCP tools, these meta-tools enable on-demand
discovery and invocation:
  - search_tools(query): Find relevant tools from the full catalogue
  - call_tool(name, arguments): Call any tool by name without pre-registration

This keeps request schemas small and lets agents discover specialized tools
only when needed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adk.client._gateway_mcp import GatewayMCPClient

logger = logging.getLogger("adk.tools_meta")


def _tokenize(text: str) -> list[str]:
    """Split text into lowercase tokens (words, no symbols).

    Matches: /codegraph_fuzzyfind, web-search, get_secret -> split into
    ['codegraph', 'fuzzyfind', 'web', 'search', 'get', 'secret'].
    """
    # Split on non-alphanumeric, keep case-insensitive
    words = re.findall(r"[a-z0-9]+", text.lower())
    return words


def _score_tool(tool: dict, query_tokens: list[str]) -> float:
    """BM25-ish scoring: name matches rank higher than description.

    Args:
        tool: {name, description, ...}
        query_tokens: Pre-tokenized query

    Returns:
        Score (higher = better match)
    """
    if not query_tokens:
        return 0.0

    name = tool.get("name", "").lower()
    desc = tool.get("description", "").lower()

    name_tokens = _tokenize(name)
    desc_tokens = _tokenize(desc)

    # Count matches in name (weight 2x)
    name_matches = sum(1 for q in query_tokens if q in name_tokens)
    # Count matches in description (weight 1x)
    desc_matches = sum(1 for q in query_tokens if q in desc_tokens)

    # Bonus if query is a prefix of the tool name (e.g., "code" in "codegraph_*")
    name_prefix_match = 1.0 if name.startswith(query_tokens[0]) else 0.0

    score = (name_matches * 2.0) + desc_matches + (name_prefix_match * 0.5)
    return score


def search_tools(
    query: str,
    all_tools: list[dict],
    limit: int = 8,
) -> str:
    """Search available tools by name and description.

    Args:
        query: User's search query (e.g., "code search" or "codegraph")
        all_tools: Full tool catalogue from list_tools()
        limit: Max results to return (default 8)

    Returns:
        JSON string with {results: [{name, description}, ...], count: int}
    """
    if not query or not query.strip():
        return json.dumps({"results": [], "count": 0, "error": "empty_query"})

    if not all_tools:
        return json.dumps({"results": [], "count": 0})

    query_tokens = _tokenize(query)
    if not query_tokens:
        return json.dumps({"results": [], "count": 0, "error": "no_valid_tokens"})

    # Score all tools
    scored = []
    for tool in all_tools:
        score = _score_tool(tool, query_tokens)
        if score > 0:  # Only include matches
            scored.append((score, tool))

    # Sort by score descending, then by name (for stability)
    scored.sort(key=lambda x: (-x[0], x[1].get("name", "")))

    # Return top limit
    results = [
        {"name": tool.get("name", ""), "description": tool.get("description", "")}
        for _, tool in scored[:limit]
    ]

    return json.dumps({
        "results": results,
        "count": len(results),
        "query": query,
    })


async def call_tool(
    name: str,
    arguments: dict,
    mcp_client: GatewayMCPClient | None,
) -> str:
    """Call a tool by name without pre-registration.

    Args:
        name: Tool name (e.g., "codegraph_fuzzyfind")
        arguments: Tool arguments as dict
        mcp_client: Gateway MCP client to invoke the tool

    Returns:
        JSON string with result or error
    """
    if not name or not name.strip():
        return json.dumps({
            "error": "invalid_name",
            "message": "Tool name required",
        })

    if not mcp_client:
        return json.dumps({
            "error": "no_mcp_client",
            "message": "MCP client not available (offline mode?)",
        })

    try:
        result = await mcp_client.call_tool(name.strip(), arguments or {})
        if result.get("success"):
            return result.get("text", "")
        else:
            # Tool call failed, return error structure
            return json.dumps({
                "error": result.get("error", "unknown"),
                "message": result.get("message", "Tool call failed"),
            })
    except Exception as exc:
        logger.exception("call_tool(%s) raised: %s", name, exc)
        return json.dumps({
            "error": "exception",
            "message": f"Tool call failed: {exc}",
        })
