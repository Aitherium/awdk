"""Cloudflare tool pack — register cf_* tools on an adk agent.

Two capability surfaces, one pack:

  * Documentation (keyless): cf_docs_search talks JSON-RPC to Cloudflare's
    PUBLIC documentation MCP server (https://docs.mcp.cloudflare.com/mcp). This
    is the highest-value tool in the pack — it stops the agent inventing
    wrangler flags from stale training data.
  * Account ops (API token): DNS, Workers, routes, tunnels, analytics over the
    Cloudflare REST API v4. The account MCP server (mcp.cloudflare.com) requires
    browser OAuth, which a headless agent cannot do — so we use the REST API.

Registration is unconditional and each tool fails soft with actionable guidance
when the token is missing or under-scoped: a read-only token is a status, not a
broken pack. One bad tool never sinks the pack.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("cloudflare_pack")

PACK_ID = "cloudflare"

_TOOL_NAMES = [
    # docs (keyless — always works)
    "cf_docs_search",
    # identity / capability
    "cf_whoami", "cf_config",
    # zones + DNS
    "cf_zones", "cf_dns_list", "cf_dns_upsert", "cf_dns_delete",
    # workers + routes
    "cf_workers_list", "cf_worker_routes", "cf_worker_deploy",
    # tunnels + analytics
    "cf_tunnel_list", "cf_analytics",
]


def register(registry) -> int:
    """Register every cf_* tool. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools, not a crash
        logger.warning("cloudflare pack unavailable (%s) — 0 tools registered", exc)
        return 0
    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("cloudflare: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool must not sink the pack
            logger.debug("cloudflare: skip tool %s: %s", name, exc)
    logger.info("Cloudflare pack registered %d cf_* tools", n)
    return n
