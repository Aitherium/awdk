"""AitherSearch (standalone extract) — keyless web search for the research agent.

This is a minimal, dependency-light extract of AitherOS's AitherSearch service
(services/cognition/AitherSearch.py), keeping its DuckDuckGo provider — the path
that needs NO API key and works offline-friendly. It powers the agent's
`web_search` MCP-style tool so the deliverable runs with zero external accounts.

Strategy (in order, with graceful fallback):
  1. `ddgs` library (the maintained DuckDuckGo client) — robust, structured.
  2. `duckduckgo_search` (older name) — same API.
  3. Direct HTML scrape of html.duckduckgo.com — last-resort, no extra dep.

Returns the same result shape AitherSearch's /search emits:
    {"query", "results": [{"title","url","snippet","source"}], "provider", "count"}
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

logger = logging.getLogger("deep_research.aithersearch")


def _ddgs_client():
    """Return a DDGS class from whichever package is installed, or None."""
    try:
        from ddgs import DDGS  # maintained package
        return DDGS
    except ImportError:
        pass
    try:
        from duckduckgo_search import DDGS  # legacy package name
        return DDGS
    except ImportError:
        return None


def _search_ddgs(query: str, limit: int) -> list[dict[str, Any]]:
    """Search via the ddgs/duckduckgo_search library."""
    ddgs_cls = _ddgs_client()
    if ddgs_cls is None:
        return []
    results: list[dict[str, Any]] = []
    with ddgs_cls() as ddgs:
        # .text() yields dicts: {title, href, body}
        for r in ddgs.text(query, max_results=limit):
            results.append({
                "title": (r.get("title") or "").strip(),
                "url": r.get("href") or r.get("url") or "",
                "snippet": (r.get("body") or "").strip()[:400],
                "source": "duckduckgo",
            })
            if len(results) >= limit:
                break
    return results


async def _search_html(query: str, limit: int) -> list[dict[str, Any]]:
    """Last-resort: scrape the DuckDuckGo HTML endpoint (no extra deps)."""
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        resp = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; DeepResearchAgent/1.0)"},
        )
        resp.raise_for_status()
        html = resp.text

    links = re.findall(r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', html, re.DOTALL)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)
    results: list[dict[str, Any]] = []
    for i, (url, title) in enumerate(links[:limit]):
        snippet = snippets[i] if i < len(snippets) else ""
        title = re.sub(r"<[^>]+>", "", title).strip()
        snippet = re.sub(r"<[^>]+>", "", snippet).strip()
        # DuckDuckGo wraps targets in a redirect with ?uddg=<encoded-url>
        if "uddg=" in url:
            try:
                params = parse_qs(urlparse(url).query)
                url = unquote(params.get("uddg", [url])[0])
            except (ValueError, KeyError):
                pass
        results.append({
            "title": title,
            "url": url,
            "snippet": snippet[:400],
            "source": "duckduckgo",
        })
    return results


async def search(query: str, limit: int = 6) -> dict[str, Any]:
    """Run a keyless web search. Returns AitherSearch-shaped results."""
    query = (query or "").strip()
    if not query:
        return {"query": query, "results": [], "provider": "none", "count": 0}

    results: list[dict[str, Any]] = []
    provider = "duckduckgo"
    try:
        # ddgs is synchronous; run it inline (fast). Fall back to HTML scrape.
        results = _search_ddgs(query, limit)
        if not results:
            results = await _search_html(query, limit)
            provider = "duckduckgo-html"
    except Exception as exc:  # noqa: BLE001 — degrade gracefully, never crash the loop
        logger.warning("ddgs search failed (%s); trying HTML fallback", exc)
        try:
            results = await _search_html(query, limit)
            provider = "duckduckgo-html"
        except Exception as exc2:  # noqa: BLE001
            logger.error("web search failed entirely: %s", exc2)
            return {"query": query, "results": [], "provider": "error",
                    "count": 0, "error": str(exc2)}

    # De-dupe by URL, preserve order.
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for r in results:
        u = r.get("url", "")
        if u and u not in seen:
            seen.add(u)
            deduped.append(r)

    return {"query": query, "results": deduped[:limit],
            "provider": provider, "count": len(deduped[:limit])}
