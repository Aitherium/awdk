"""search_tools ranks on the gateway first and always returns argument schemas."""

import asyncio
import json

from adk.tools_meta import search_tools, search_tools_ranked

CATALOGUE = [
    {"name": "fleet_runtime_status", "description": "List units with live state.",
     "inputSchema": {"type": "object", "properties": {"only_down": {"type": "boolean"}}}},
    {"name": "web_search", "description": "Search the web.", "inputSchema": {"type": "object"}},
]


class _Gateway:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    async def search_tools(self, query, top_k=8):
        self.calls.append((query, top_k))
        return self.hits


def test_semantic_hits_win_and_carry_the_catalogue_schema():
    gw = _Gateway([{"name": "fleet_runtime_status", "description": "d"}])
    out = json.loads(asyncio.run(
        search_tools_ranked("which units are down", CATALOGUE, 5, gw)))
    assert out["ranking"] == "semantic"
    assert out["results"][0]["name"] == "fleet_runtime_status"
    assert "only_down" in out["results"][0]["parameters"]["properties"]
    assert gw.calls == [("which units are down", 5)]


def test_gateway_failure_falls_back_to_keyword_search():
    class _Down:
        async def search_tools(self, q, k=8):
            raise RuntimeError("offline")

    out = json.loads(asyncio.run(search_tools_ranked("web search", CATALOGUE, 5, _Down())))
    assert "ranking" not in out
    assert out["results"][0]["name"] == "web_search"


def test_empty_gateway_answer_and_no_client_fall_back():
    for client in (_Gateway([]), _Gateway(None), None):
        out = json.loads(asyncio.run(search_tools_ranked("web", CATALOGUE, 5, client)))
        assert out["results"][0]["name"] == "web_search"


def test_keyword_results_now_include_schemas():
    out = json.loads(search_tools("fleet runtime", CATALOGUE, 5))
    assert out["results"][0]["parameters"]["properties"]["only_down"]["type"] == "boolean"
