"""The enrollment pack sync calls the host that serves the marketplace API.

2026-10-22: every fresh install printed "Could not fetch entitlements: 404" because
the sync called api.aitherium.com/v1/marketplace/license/mine, which the web portal
answers with a 307 to its login page. The API lives on the gateway.
"""
from __future__ import annotations

import asyncio

import httpx

from adk.shell.plugins.builtins import packs


def test_portal_hosts_map_to_the_gateway(monkeypatch):
    monkeypatch.delenv("ADK_MARKETPLACE_URL", raising=False)
    for url in ("https://api.aitherium.com", "https://app.aitherium.com/",
                "https://aitherium.com"):
        assert packs.marketplace_base_url(url) == "https://gateway.aitherium.com"


def test_other_hosts_and_the_override_are_kept(monkeypatch):
    monkeypatch.delenv("ADK_MARKETPLACE_URL", raising=False)
    assert packs.marketplace_base_url("https://portal.acme.test/") == "https://portal.acme.test"
    monkeypatch.setenv("ADK_MARKETPLACE_URL", "https://mk.example.test/")
    assert packs.marketplace_base_url("https://api.aitherium.com") == "https://mk.example.test"


def test_sync_asks_the_gateway_not_the_portal(monkeypatch):
    monkeypatch.delenv("ADK_MARKETPLACE_URL", raising=False)
    seen = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **k):
            seen.append(url)
            return httpx.Response(200, json={"licenses": []},
                                  request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert asyncio.run(packs.sync_entitled_packs("tok", base_url="https://api.aitherium.com")) \
        == (0, 0)
    assert seen == ["https://gateway.aitherium.com/v1/marketplace/license/mine"]
