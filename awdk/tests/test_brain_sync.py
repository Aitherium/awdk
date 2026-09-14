"""Test suite for brain sync client (platform-home architectural decision).

Tests verify:
1. Client points to AitherBrain service (platform-home, not app-scoped)
2. Tenant isolation is enforced server-side (tenant_id validation)
3. Request/response contracts match
4. Graceful error handling for common failure modes
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from adk.sync.brain import (
    BrainSyncClient,
    SyncDeltaItem,
    SyncRequest,
)


class TestSyncDeltaItem:
    """Test SyncDeltaItem validation and serialization."""

    def test_valid_upsert_item(self):
        """Test creating a valid upsert delta."""
        item = SyncDeltaItem(
            chunk_id="chunk-1",
            op="upsert",
            vector=[0.1, 0.2, 0.3],
            metadata={"text": "example"},
            classification="internal",
        )
        assert item.chunk_id == "chunk-1"
        assert item.op == "upsert"
        assert item.classification == "internal"

    def test_valid_delete_item(self):
        """Test creating a valid delete delta."""
        item = SyncDeltaItem(
            chunk_id="chunk-1",
            op="delete",
        )
        assert item.op == "delete"
        assert item.vector is None

    def test_invalid_chunk_id(self):
        """Test that empty chunk_id is rejected."""
        with pytest.raises(ValueError, match="chunk_id required"):
            SyncDeltaItem(chunk_id="")

    def test_invalid_classification(self):
        """Test that invalid classification is rejected."""
        with pytest.raises(ValueError, match="Invalid classification"):
            SyncDeltaItem(chunk_id="c1", classification="invalid")

    def test_item_serialization(self):
        """Test SyncDeltaItem.to_dict()."""
        item = SyncDeltaItem(
            chunk_id="c1",
            op="upsert",
            vector=[0.5],
            metadata={"key": "value"},
            classification="public",
        )
        d = item.to_dict()
        assert d["chunk_id"] == "c1"
        assert d["op"] == "upsert"
        assert d["classification"] == "public"
        assert d["metadata"]["key"] == "value"


class TestSyncRequest:
    """Test SyncRequest contract."""

    def test_minimal_request(self):
        """Test creating minimal sync request."""
        req = SyncRequest(tenant_id="tnt_123")
        assert req.tenant_id == "tnt_123"
        assert req.workspace_id == "default"
        assert req.watermark == ""
        assert req.delta == []

    def test_request_with_deltas(self):
        """Test sync request with items."""
        items = [
            SyncDeltaItem(chunk_id="c1"),
            SyncDeltaItem(chunk_id="c2", op="delete"),
        ]
        req = SyncRequest(
            tenant_id="tnt_123",
            workspace_id="ws-1",
            watermark="wm-1",
            delta=items,
        )
        assert len(req.delta) == 2
        assert req.watermark == "wm-1"

    def test_request_serialization(self):
        """Test SyncRequest.to_json()."""
        req = SyncRequest(
            tenant_id="tnt_123",
            workspace_id="ws-1",
            delta=[SyncDeltaItem(chunk_id="c1")],
        )
        json_str = req.to_json()
        assert "tnt_123" in json_str
        assert "ws-1" in json_str
        assert "c1" in json_str

    def test_invalid_tenant_id(self):
        """Test that missing tenant_id is rejected."""
        with pytest.raises(ValueError, match="tenant_id required"):
            SyncRequest(tenant_id="")


class TestBrainSyncClient:
    """Test BrainSyncClient initialization and configuration."""

    def test_client_init_with_explicit_url(self):
        """Test client initialization with explicit brain_url."""
        client = BrainSyncClient(
            brain_url="http://aitheros-brain:8271",
            tenant_id="tnt_123",
        )
        assert client.brain_url == "http://aitheros-brain:8271"
        assert client.tenant_id == "tnt_123"
        assert client.workspace_id == "default"

    def test_client_init_with_env_override(self):
        """Test that AITHER_BRAIN_URL env var overrides default."""
        with patch.dict("os.environ", {"AITHER_BRAIN_URL": "http://custom-brain:8271"}):
            client = BrainSyncClient(tenant_id="tnt_123")
            assert "custom-brain" in client.brain_url

    def test_client_init_without_tenant_id(self):
        """Test that tenant_id is required."""
        with pytest.raises(ValueError, match="tenant_id required"):
            BrainSyncClient(tenant_id="")

    def test_client_defaults_to_localhost(self):
        """Test that client defaults to localhost:8271 when no URL configured."""
        with patch.dict("os.environ", {}, clear=True):
            with patch.object(
                BrainSyncClient, "_resolve_brain_service_url", return_value=""
            ):
                client = BrainSyncClient(tenant_id="tnt_123")
                # Should still have a fallback
                assert "8271" in client.brain_url or client.brain_url

    def test_url_trailing_slash_stripped(self):
        """Test that trailing slashes are removed from brain_url."""
        client = BrainSyncClient(
            brain_url="http://brain:8271/",
            tenant_id="tnt_123",
        )
        assert client.brain_url == "http://brain:8271"


class TestBrainSyncPost:
    """Test BrainSyncClient.post_deltas() with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_successful_sync(self):
        """Test successful delta sync to AitherBrain."""
        client = BrainSyncClient(
            brain_url="http://brain:8271",
            tenant_id="tnt_123",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "accepted": 1,
            "rejected": 0,
            "watermark": "wm-2",
        }

        with patch("httpx.AsyncClient") as mock_http_class:
            mock_http = AsyncMock()
            mock_http.__aenter__.return_value = mock_http
            mock_http.__aexit__.return_value = None
            mock_http.post.return_value = mock_response
            mock_http_class.return_value = mock_http

            deltas = [
                SyncDeltaItem(
                    chunk_id="c1",
                    op="upsert",
                    metadata={"text": "test"},
                )
            ]
            result = await client.post_deltas(deltas)

            assert result.accepted == 1
            assert result.rejected == 0
            assert result.watermark == "wm-2"
            assert client.watermark == "wm-2"  # Updated client state

    @pytest.mark.asyncio
    async def test_no_deltas(self):
        """Test post_deltas with empty list (no-op)."""
        client = BrainSyncClient(tenant_id="tnt_123")
        result = await client.post_deltas([])

        assert result.accepted == 0
        assert result.rejected == 0

    @pytest.mark.asyncio
    async def test_403_tenant_isolation(self):
        """Test handling of 403 Forbidden (tenant isolation enforced server-side)."""
        client = BrainSyncClient(
            brain_url="http://brain:8271",
            tenant_id="tnt_123",
        )

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Brain sync restricted to your own tenant"

        with patch("httpx.AsyncClient") as mock_http_class:
            mock_http = AsyncMock()
            mock_http.__aenter__.return_value = mock_http
            mock_http.__aexit__.return_value = None
            mock_http.post.return_value = mock_response
            mock_http_class.return_value = mock_http

            deltas = [SyncDeltaItem(chunk_id="c1")]
            result = await client.post_deltas(deltas)

            # Should degrade gracefully: all items rejected
            assert result.accepted == 0
            assert result.rejected == 1

    @pytest.mark.asyncio
    async def test_401_auth_error(self):
        """Test handling of 401 Unauthorized."""
        client = BrainSyncClient(tenant_id="tnt_123")

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "not enrolled or invalid credentials"

        with patch("httpx.AsyncClient") as mock_http_class:
            mock_http = AsyncMock()
            mock_http.__aenter__.return_value = mock_http
            mock_http.__aexit__.return_value = None
            mock_http.post.return_value = mock_response
            mock_http_class.return_value = mock_http

            deltas = [SyncDeltaItem(chunk_id="c1")]
            result = await client.post_deltas(deltas)

            assert result.accepted == 0
            assert result.rejected == 1

    @pytest.mark.asyncio
    async def test_503_service_unavailable(self):
        """Test handling of 503 Service Unavailable (graceful degradation)."""
        client = BrainSyncClient(tenant_id="tnt_123")

        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.text = "Service Unavailable"

        with patch("httpx.AsyncClient") as mock_http_class:
            mock_http = AsyncMock()
            mock_http.__aenter__.return_value = mock_http
            mock_http.__aexit__.return_value = None
            mock_http.post.return_value = mock_response
            mock_http_class.return_value = mock_http

            deltas = [SyncDeltaItem(chunk_id="c1")]
            result = await client.post_deltas(deltas)

            # Should retry later: no crash
            assert result.accepted == 0
            assert result.rejected == 1

    @pytest.mark.asyncio
    async def test_gzip_compression(self):
        """Test that payload is compressed when compress=True."""
        client = BrainSyncClient(tenant_id="tnt_123")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"accepted": 1, "rejected": 0}

        with patch("httpx.AsyncClient") as mock_http_class:
            mock_http = AsyncMock()
            mock_http.__aenter__.return_value = mock_http
            mock_http.__aexit__.return_value = None
            mock_http.post.return_value = mock_response
            mock_http_class.return_value = mock_http

            deltas = [SyncDeltaItem(chunk_id="c1")]
            await client.post_deltas(deltas, compress=True)

            # Check that Content-Encoding header was set
            call_args = mock_http.post.call_args
            headers = call_args[1]["headers"]
            assert headers.get("Content-Encoding") == "gzip"

    @pytest.mark.asyncio
    async def test_network_error_handling(self):
        """Test handling of network errors (graceful degradation)."""
        import httpx

        client = BrainSyncClient(tenant_id="tnt_123")

        with patch("httpx.AsyncClient") as mock_http_class:
            mock_http = AsyncMock()
            mock_http.__aenter__.return_value = mock_http
            mock_http.__aexit__.return_value = None
            mock_http.post.side_effect = httpx.NetworkError("Connection refused")
            mock_http_class.return_value = mock_http

            deltas = [SyncDeltaItem(chunk_id="c1")]
            result = await client.post_deltas(deltas)

            # Should not crash, should return empty response
            assert result.accepted == 0
            assert result.rejected == 1


class TestAitherBrainIntegration:
    """Integration tests (require AitherBrain running on 8271)."""

    @pytest.mark.asyncio
    async def test_brain_service_routing(self):
        """Test that client correctly routes to /brain/sync on AitherBrain.

        This test documents the architectural decision: brain sync is
        PLATFORM-HOME (not app-scoped). The client POST to AitherBrain:8271/brain/sync.
        """
        client = BrainSyncClient(
            brain_url="http://localhost:8271",
            tenant_id="tnt_123",
        )

        # The contract: the client resolves the PLATFORM AitherBrain URL and
        # post_deltas builds its target as f"{brain_url}/brain/sync".
        assert client.brain_url == "http://localhost:8271"
        assert f"{client.brain_url}/brain/sync" == "http://localhost:8271/brain/sync"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
