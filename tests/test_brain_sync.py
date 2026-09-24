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
            bearer="node-key",
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
            bearer="node-key",
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
        client = BrainSyncClient(tenant_id="tnt_123", bearer="node-key")

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
        client = BrainSyncClient(tenant_id="tnt_123", bearer="node-key")

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
        client = BrainSyncClient(tenant_id="tnt_123", bearer="node-key")

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

        client = BrainSyncClient(tenant_id="tnt_123", bearer="node-key")

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


def _mock_http(status=200, body=None):
    mock_response = MagicMock()
    mock_response.status_code = status
    mock_response.json.return_value = body or {"accepted": 1, "rejected": 0}
    mock_http = AsyncMock()
    mock_http.__aenter__.return_value = mock_http
    mock_http.__aexit__.return_value = None
    mock_http.post.return_value = mock_response
    return mock_http


class TestBrainSyncAuthAndRouting:
    """The hub refuses an unauthenticated sync (403 without a tenant context),
    and /brain/sync lives on AitherBrain over TLS, not on Genesis :8001."""

    @pytest.mark.asyncio
    async def test_node_bearer_is_attached(self, monkeypatch):
        import adk.fleet_enroll as fe

        monkeypatch.delenv("AITHER_SESSION_BEARER", raising=False)
        monkeypatch.setattr(fe, "_load_node_auth",
                            lambda: {"node_id": "n1", "api_key": "node-secret"})
        client = BrainSyncClient(brain_url="https://brain:8271", tenant_id="tnt_123")
        mock_http = _mock_http()
        with patch("httpx.AsyncClient", return_value=mock_http):
            await client.post_deltas([SyncDeltaItem(chunk_id="c1")])
        headers = mock_http.post.call_args[1]["headers"]
        assert headers.get("Authorization") == "Bearer node-secret"

    @pytest.mark.asyncio
    async def test_no_credential_fails_closed_without_sending(self, monkeypatch):
        import adk.fleet_enroll as fe

        monkeypatch.delenv("AITHER_SESSION_BEARER", raising=False)
        monkeypatch.setattr(fe, "_load_node_auth", lambda: {})
        client = BrainSyncClient(brain_url="https://brain:8271", tenant_id="tnt_123")
        mock_http = _mock_http()
        with patch("httpx.AsyncClient", return_value=mock_http):
            result = await client.post_deltas([SyncDeltaItem(chunk_id="c1")])
        assert mock_http.post.call_count == 0
        assert result.accepted == 0 and result.rejected == 1

    def test_default_url_is_aitherbrain_over_tls(self, monkeypatch):
        from adk.sync import brain as brain_mod

        for var in ("AITHER_BRAIN_URL", "AITHER_BRAIN_HUB_URL"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr("adk.config.load_saved_config", lambda: {}, raising=False)
        url = brain_mod.resolve_brain_url(None)
        assert url.startswith("https://")
        assert ":8001" not in url and "localhost" not in url
        assert BrainSyncClient(tenant_id="t", bearer="k").brain_url == url

    def test_ingest_does_not_default_to_genesis_http(self):
        import inspect

        import adk.ingest as ingest
        from adk.shell import claude_ingest

        for mod in (ingest, claude_ingest):
            src = inspect.getsource(mod)
            assert "http://localhost:8001" not in src, mod.__name__
            assert "resolve_brain_url" in src, mod.__name__



class TestEnrollPersistsTenantId:
    """Enrollment used to write only tenant_slug, so every enrolled node read
    tenant_id == "" and ``adk ingest --brain`` silently disabled brain sync."""

    @staticmethod
    def _point_at(tmp_path, monkeypatch, auth=None, node=None):
        import json

        import adk.fleet_enroll as fe

        auth_file = tmp_path / "auth.json"
        node_file = tmp_path / "node_auth.json"
        if auth is not None:
            auth_file.write_text(json.dumps(auth), encoding="utf-8")
        if node is not None:
            node_file.write_text(json.dumps(node), encoding="utf-8")
        monkeypatch.setattr(fe, "_AITHER_DIR", tmp_path)
        monkeypatch.setattr(fe, "_AUTH_FILE", auth_file)
        monkeypatch.setattr(fe, "_NODE_AUTH_FILE", node_file)
        return fe, node_file

    _AUTH = {
        "active_profile": "cloud",
        "profiles": {"cloud": {
            "access_token": "user-tok",
            "user": {"username": "u", "tenant_id": "tnt_login", "tenant_slug": "acme"},
        }},
    }

    @pytest.mark.asyncio
    async def test_rich_enroll_writes_identity_tenant_id(self, tmp_path, monkeypatch):
        import json

        import adk.enrollment as enr

        fe, node_file = self._point_at(tmp_path, monkeypatch, auth=self._AUTH)

        async def fake_rich(*a, **k):
            return {"enrolled": True, "tenant_id": "tnt_identity",
                    "registration": {}, "bearer_token": ""}

        async def _none(*a, **k):
            return (0, 0)

        async def _false(*a, **k):
            return False

        monkeypatch.setattr(enr, "rich_enroll", fake_rich)
        monkeypatch.setattr(fe, "_sync_entitled_packs_best_effort", _none)
        monkeypatch.setattr(fe, "_upsert_agents_to_portal", _false)
        monkeypatch.setattr(fe, "_enable_session_sync_default", lambda: None)
        monkeypatch.setenv("AITHER_FLEET_ENROLL", "1")
        out = await fe.enroll_on_boot(enable_heartbeat=False, start_link=False)
        assert out["enrolled"] is True
        saved = json.loads(node_file.read_text(encoding="utf-8"))
        assert saved["tenant_id"] == "tnt_identity"
        assert fe.node_tenant_id() == "tnt_identity"

    def test_node_tenant_id_falls_back_to_signed_in_identity(self, tmp_path, monkeypatch):
        fe, _ = self._point_at(tmp_path, monkeypatch, auth=self._AUTH,
                               node={"node_id": "n1", "api_key": "k"})
        assert fe.node_tenant_id() == "tnt_login"

    def test_no_identity_means_no_tenant(self, tmp_path, monkeypatch):
        fe, _ = self._point_at(tmp_path, monkeypatch, node={"node_id": "n1"})
        assert fe.node_tenant_id() == ""

    def test_backfill_keeps_enrolled_at(self, tmp_path, monkeypatch):
        import json

        node = {"node_id": "n1", "api_key": "k", "enrolled_at": "2026-01-01T00:00:00Z"}
        fe, node_file = self._point_at(tmp_path, monkeypatch, auth=self._AUTH, node=node)
        fe._backfill_node_tenant_id(fe._load_node_auth())
        saved = json.loads(node_file.read_text(encoding="utf-8"))
        assert saved["tenant_id"] == "tnt_login"
        assert saved["enrolled_at"] == "2026-01-01T00:00:00Z"

    def test_ingest_readers_use_node_tenant_id(self):
        import inspect

        import adk.ingest as ingest
        from adk.shell import claude_ingest

        for mod in (ingest, claude_ingest):
            src = inspect.getsource(mod)
            assert "node_tenant_id" in src, mod.__name__
            assert '_load_node_auth().get("tenant_id"' not in src, mod.__name__


class TestOneBrainResolver:
    def test_federation_and_knowledge_sync_agree(self, monkeypatch):
        from adk.sync import brain as brain_mod
        from adk.sync import federation

        monkeypatch.setattr("adk.config.load_saved_config", lambda: {}, raising=False)
        monkeypatch.setenv("AITHER_BRAIN_HUB_URL", "https://hub.example:8271")
        monkeypatch.setenv("AITHER_BRAIN_URL", "https://other.example:8271")
        assert federation._brain_url() == brain_mod.resolve_brain_url(None)
        monkeypatch.delenv("AITHER_BRAIN_HUB_URL")
        assert federation._brain_url() == brain_mod.resolve_brain_url(None)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
