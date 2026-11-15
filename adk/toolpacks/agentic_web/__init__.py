"""Agentic Web tool pack: make a business's domain usable by AI agents, end to end.

Four parts, one SiteProfile:

* ``scanner``    -- isitagentready.com client + offline checks (reuses agent_ready)
* ``generators`` -- one generator per check (A2A+AP2, MCP card, skills index, OAuth,
  WebMCP, Web Bot Auth directory, DNS-AID, ACP, UCP, x402 test, MPP fragment)
* ``signer``     -- RFC 9421 Ed25519 signing with Signature-Agent (Web Bot Auth)
* ``deployers``  -- plan/apply per target (static_dir, pages_repo_pr, veil_public,
  cf_worker, cf_dns); hard gates in ``gates`` become decision cards.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("agentic_web_pack")

PACK_ID = "agentic_web"

_TOOL_NAMES = ["agentic_web_scan", "agentic_web_plan", "agentic_web_apply",
               "agentic_web_verify", "agentic_web_generate"]


def register(registry) -> int:
    """Register every agentic_web_* tool. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 - import failure = 0 tools, not a crash
        logger.warning("agentic_web pack unavailable (%s) -- 0 tools registered", exc)
        return 0
    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 - one bad tool must not sink the pack
            logger.debug("agentic_web: skip tool %s: %s", name, exc)
    logger.info("Agentic Web pack registered %d agentic_web_* tools", n)
    return n
