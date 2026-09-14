"""Tests for PortalClient marketplace browse/discover/apply methods."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.auth import Credentials
from adk.portal import PortalClient, PortalConfig


def _client(api_key: str = "aither_sk_live_test") -> PortalClient:
    config = PortalConfig(
        base_url="https://api.aitheros.ai",
        credentials=Credentials(access_token=api_key, token_type="bearer"),
    )
    return PortalClient(config=config)


def _mock_httpx_client(response_data=None, status_code=200):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = response_data if response_data is not None else {}
    mock_resp.content = b"1" if response_data is not None else b""

    client = MagicMock()
    client.get = AsyncMock(return_value=mock_resp)
    client.post = AsyncMock(return_value=mock_resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client, mock_resp


class TestListPacks:
    @pytest.mark.asyncio
    async def test_list_packs_from_live_catalog(self):
        client = _client()
        mock_httpx, _ = _mock_httpx_client({"packs": [{"id": "demiurge", "name": "Demiurge"}]})
        with patch("httpx.AsyncClient", return_value=mock_httpx):
            packs = await client.list_packs()
        assert packs == [{"id": "demiurge", "name": "Demiurge"}]

    @pytest.mark.asyncio
    async def test_list_packs_falls_back_to_bundled_catalog_on_network_failure(self):
        client = _client()
        with patch("httpx.AsyncClient", side_effect=RuntimeError("network down")):
            packs = await client.list_packs()
        # bundled adk/data/packs_catalog.json is real and non-empty
        assert isinstance(packs, list)
        assert any(p.get("id") == "aitherium" for p in packs)

    @pytest.mark.asyncio
    async def test_search_packs_filters_by_query(self):
        client = _client()
        with patch("httpx.AsyncClient", side_effect=RuntimeError("network down")):
            matches = await client.search_packs("atlas")
        assert matches
        assert all("atlas" in (p.get("id", "") + p.get("name", "") + p.get("description", "")).lower()
                    or any("atlas" in t.lower() for t in p.get("tags", []))
                    for p in matches)


class TestNegotiateAndPurchase:
    @pytest.mark.asyncio
    async def test_negotiate_pack_posts_offer(self):
        client = _client()
        mock_httpx, mock_resp = _mock_httpx_client(
            {"decision": "accept", "agreed_credits": 500, "negotiation_token": "tok123"}
        )
        with patch("httpx.AsyncClient", return_value=mock_httpx):
            result = await client.negotiate_pack("atlas-pack", offer_credits=500, rationale="test")
        assert result["decision"] == "accept"
        assert result["negotiation_token"] == "tok123"
        call_kwargs = mock_httpx.post.call_args
        assert call_kwargs.args[0].endswith("/v1/marketplace/negotiate")
        assert call_kwargs.kwargs["json"]["listing_id"] == "atlas-pack"
        assert call_kwargs.kwargs["json"]["offer_credits"] == 500

    @pytest.mark.asyncio
    async def test_purchase_pack_posts_listing_and_token(self):
        client = _client()
        mock_httpx, mock_resp = _mock_httpx_client({"ok": True})
        with patch("httpx.AsyncClient", return_value=mock_httpx):
            result = await client.purchase_pack("atlas-pack", negotiation_token="tok123")
        assert result["ok"] is True
        assert result["_status_code"] == 200
        call_kwargs = mock_httpx.post.call_args
        assert call_kwargs.kwargs["json"]["negotiation_token"] == "tok123"

    @pytest.mark.asyncio
    async def test_marketplace_headers_include_bearer_and_legacy_key(self):
        client = _client(api_key="aither_sk_live_abc")
        headers = client._marketplace_headers()
        assert headers["Authorization"] == "Bearer aither_sk_live_abc"
        assert headers["X-Aither-Api-Key"] == "aither_sk_live_abc"


class TestInstallPack:
    def test_install_pack_delegates_to_packs_plugin(self):
        client = _client(api_key="aither_sk_live_abc")
        fake_plugin = MagicMock()
        fake_plugin._sync.return_value = "installed atlas-pack"
        with patch(
            "adk.shell.plugins.builtins.packs.PacksPlugin", return_value=fake_plugin
        ):
            result = client.install_pack("atlas-pack")
        assert result == "installed atlas-pack"
        fake_plugin.auth.set_auth.assert_called_once()
        fake_plugin._sync.assert_called_once_with(["atlas-pack"])
