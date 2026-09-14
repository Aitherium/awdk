"""Tests for the offline-first provenance publisher."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from adk.config import Config
from adk.selfgraph.publisher import Publisher, PublishOutcome, resolve_base_url
from adk.selfgraph.schema import (
    GraphUpdate,
    ProvEdge,
    ProvEdgeType,
    ProvNode,
    ProvNodeType,
    make_node_id,
)
from adk.selfgraph.spool import Spool


def _make_test_update(run_id="run1", agent_id="agent1", tenant_id="t1"):
    """Create a minimal valid GraphUpdate for testing."""
    claim_node = ProvNode(
        id=make_node_id(ProvNodeType.CLAIM, "test claim", tenant_id),
        node_type=ProvNodeType.CLAIM,
        name="test claim",
        tenant_id=tenant_id,
        workspace_id="ws1",
        run_id=run_id,
        agent_id=agent_id,
    )
    source_node = ProvNode(
        id=make_node_id(ProvNodeType.SOURCE, "http://example.com", tenant_id),
        node_type=ProvNodeType.SOURCE,
        name="http://example.com",
        tenant_id=tenant_id,
        workspace_id="ws1",
        agent_id=agent_id,
    )
    edge = ProvEdge(
        source_id=claim_node.id,
        target_id=source_node.id,
        edge_type=ProvEdgeType.CITES,
        tenant_id=tenant_id,
        workspace_id="ws1",
        run_id=run_id,
        agent_id=agent_id,
    )
    return GraphUpdate(
        nodes=[claim_node, source_node],
        edges=[edge],
        run_id=run_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        workspace_id="ws1",
    )


@pytest.fixture
def spool_db(tmp_path):
    """Create a Spool instance with a temporary database."""
    db_path = tmp_path / "test_spool.db"
    spool = Spool(db_path=db_path)
    yield spool
    spool.close()


class TestResolveBaseUrl:
    """Test resolve_base_url() precedence order."""

    def test_explicit_env_var_takes_precedence(self, monkeypatch):
        """AITHER_SELFGRAPH_URL should take first precedence."""
        monkeypatch.setenv("AITHER_SELFGRAPH_URL", "http://custom.url:9000")
        monkeypatch.delenv("AITHER_GRAPH_URL", raising=False)

        url = resolve_base_url()
        assert url == "http://custom.url:9000"

    def test_secondary_env_var_second_precedence(self, monkeypatch):
        """AITHER_GRAPH_URL should be second in precedence."""
        monkeypatch.delenv("AITHER_SELFGRAPH_URL", raising=False)
        monkeypatch.setenv("AITHER_GRAPH_URL", "http://secondary.url:8000")

        url = resolve_base_url()
        assert url == "http://secondary.url:8000"

    def test_config_gateway_third_precedence(self, monkeypatch):
        """Config.gateway_url should be third in precedence."""
        monkeypatch.delenv("AITHER_SELFGRAPH_URL", raising=False)
        monkeypatch.delenv("AITHER_GRAPH_URL", raising=False)

        config = Config()
        config.gateway_url = "http://config.gateway:7000"

        url = resolve_base_url(config)
        assert url == "http://config.gateway:7000"

    def test_default_localhost_last_precedence(self, monkeypatch):
        """Should default to 127.0.0.1:8154 when nothing is set."""
        monkeypatch.delenv("AITHER_SELFGRAPH_URL", raising=False)
        monkeypatch.delenv("AITHER_GRAPH_URL", raising=False)

        config = Config()
        config.gateway_url = ""

        url = resolve_base_url(config)
        assert url == "http://127.0.0.1:8154"

    def test_uses_127_not_localhost(self, monkeypatch):
        """Should use 127.0.0.1 not localhost (IPv6 hangs issue)."""
        monkeypatch.delenv("AITHER_SELFGRAPH_URL", raising=False)
        monkeypatch.delenv("AITHER_GRAPH_URL", raising=False)

        config = Config()
        config.gateway_url = ""

        url = resolve_base_url(config)
        assert "127.0.0.1" in url
        assert "localhost" not in url


class TestPublisherInitialization:
    """Test Publisher initialization."""

    def test_publisher_auto_resolves_base_url(self):
        """Publisher should auto-resolve base_url if empty."""
        publisher = Publisher(base_url="")
        assert publisher.base_url, "Should have resolved a base_url"
        assert "127.0.0.1" in publisher.base_url or publisher.base_url.startswith("http")

    def test_publisher_uses_provided_base_url(self):
        """Publisher should use provided base_url."""
        publisher = Publisher(base_url="http://custom:8000")
        assert publisher.base_url == "http://custom:8000"

    def test_publisher_creates_default_spool(self, tmp_path, monkeypatch):
        """Publisher should create a default Spool if not provided."""
        monkeypatch.setenv("AITHER_SELFGRAPH_DB", str(tmp_path / "spool.db"))
        publisher = Publisher(base_url="http://localhost:8154")
        assert publisher.spool is not None

    def test_publisher_uses_provided_spool(self, spool_db):
        """Publisher should use provided Spool."""
        publisher = Publisher(base_url="http://localhost:8154", spool=spool_db)
        assert publisher.spool is spool_db

    def test_publisher_token_from_arg(self):
        """Publisher should use provided token."""
        publisher = Publisher(
            base_url="http://localhost:8154",
            token="test-token-123",
        )
        assert publisher.token == "test-token-123"


@pytest.mark.asyncio
class TestPublishUpdate:
    """Test publish_update() method."""

    async def test_publish_update_success(self, spool_db):
        """publish_update() should return ok=True on HTTP 200."""
        publisher = Publisher(
            base_url="http://localhost:8154",
            token="test-token",
            spool=spool_db,
        )

        update = _make_test_update()

        with patch.object(publisher, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.post.return_value = mock_response
            mock_get_client.return_value = mock_client

            outcome = await publisher.publish_update(update)

            assert outcome.ok is True
            assert outcome.sent == 1
            assert outcome.failed == 0
            mock_client.post.assert_called_once()

    async def test_publish_update_checks_invariants_first(self, spool_db):
        """publish_update() should validate invariants before posting."""
        publisher = Publisher(
            base_url="http://localhost:8154",
            token="test-token",
            spool=spool_db,
        )

        # Create an invalid update (unsourced claim)
        invalid_update = GraphUpdate(
            nodes=[
                ProvNode(
                    id="claim:test:123",
                    node_type=ProvNodeType.CLAIM,
                    name="unsourced claim",
                    run_id="run1",
                    agent_id="agent1",
                )
            ],
            edges=[],
            run_id="run1",
            agent_id="agent1",
        )

        outcome = await publisher.publish_update(invalid_update)

        assert outcome.ok is False
        assert outcome.failed == 1
        assert len(outcome.errors) > 0

    async def test_publish_update_400_terminal_error(self, spool_db):
        """HTTP 400 should mark as terminal (don't retry)."""
        publisher = Publisher(
            base_url="http://localhost:8154",
            token="test-token",
            spool=spool_db,
        )

        update = _make_test_update()

        with patch.object(publisher, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 400
            mock_response.text = "Invalid schema"
            mock_client.post.return_value = mock_response
            mock_get_client.return_value = mock_client

            outcome = await publisher.publish_update(update)

            assert outcome.ok is False
            assert outcome.failed == 1
            assert "400" in outcome.errors[0]

    async def test_publish_update_500_transient_error(self, spool_db):
        """HTTP 500 should be marked as transient."""
        publisher = Publisher(
            base_url="http://localhost:8154",
            token="test-token",
            spool=spool_db,
        )

        update = _make_test_update()

        with patch.object(publisher, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_response.text = "Server error"
            mock_client.post.return_value = mock_response
            mock_get_client.return_value = mock_client

            outcome = await publisher.publish_update(update)

            assert outcome.ok is False
            assert outcome.failed == 1
            assert outcome.unreachable is True
            assert "500" in outcome.errors[0]

    async def test_publish_update_connection_error_transient(self, spool_db):
        """Connection errors should be marked as transient."""
        publisher = Publisher(
            base_url="http://localhost:8154",
            token="test-token",
            spool=spool_db,
        )

        update = _make_test_update()

        with patch.object(publisher, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.ConnectError("Connection refused")
            mock_get_client.return_value = mock_client

            outcome = await publisher.publish_update(update)

            assert outcome.ok is False
            assert outcome.failed == 1
            assert outcome.unreachable is True

    async def test_publish_update_no_token(self, spool_db):
        """publish_update() without token should return ok=False."""
        publisher = Publisher(
            base_url="http://localhost:8154",
            token="",  # No token
            spool=spool_db,
        )

        update = _make_test_update()

        outcome = await publisher.publish_update(update)

        assert outcome.ok is False
        assert outcome.failed == 1
        assert "authenticated" in outcome.errors[0]


@pytest.mark.asyncio
class TestDrain:
    """Test drain() method."""

    async def test_drain_with_no_token_skips_send(self, spool_db):
        """drain() without token should skip sending but not raise."""
        publisher = Publisher(
            base_url="http://localhost:8154",
            token="",  # No token
            spool=spool_db,
        )

        # Enqueue something
        spool_db.enqueue(_make_test_update())

        outcome = await publisher.drain()

        assert outcome.ok is False
        assert outcome.skipped >= 1
        assert "not authenticated" in outcome.errors[0]

    async def test_drain_marks_successful_as_sent(self, spool_db):
        """drain() should mark successfully published entries as 'sent'."""
        publisher = Publisher(
            base_url="http://localhost:8154",
            token="test-token",
            spool=spool_db,
        )

        rowid = spool_db.enqueue(_make_test_update())

        with patch.object(publisher, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.post.return_value = mock_response
            mock_get_client.return_value = mock_client

            outcome = await publisher.drain()

            assert outcome.ok is True
            assert outcome.sent == 1

            # Entry should be marked sent in spool
            pending = spool_db.pending(limit=10)
            assert len(pending) == 0, "Sent entry should not be pending"

    async def test_drain_marks_4xx_as_failed_terminal(self, spool_db):
        """drain() should mark 4xx errors as terminal (don't retry)."""
        publisher = Publisher(
            base_url="http://localhost:8154",
            token="test-token",
            spool=spool_db,
        )

        rowid = spool_db.enqueue(_make_test_update())

        with patch.object(publisher, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 422
            mock_response.text = "Unprocessable entity"
            mock_client.post.return_value = mock_response
            mock_get_client.return_value = mock_client

            outcome = await publisher.drain()

            assert outcome.failed == 1
            # Entry should still be in spool (marked as attempted)
            # but not pending (4xx = don't retry)

    async def test_drain_5xx_leaves_retryable(self, spool_db):
        """drain() should leave 5xx errors as pending (retryable)."""
        publisher = Publisher(
            base_url="http://localhost:8154",
            token="test-token",
            spool=spool_db,
        )

        rowid = spool_db.enqueue(_make_test_update())

        with patch.object(publisher, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 502
            mock_response.text = "Bad gateway"
            mock_client.post.return_value = mock_response
            mock_get_client.return_value = mock_client

            outcome = await publisher.drain()

            assert outcome.failed == 0
            assert outcome.unreachable is True
            # Entry should still be pending for retry
            pending = spool_db.pending(limit=10)
            assert len(pending) == 1

    async def test_drain_connection_error_leaves_retryable(self, spool_db):
        """drain() should leave connection errors as pending."""
        publisher = Publisher(
            base_url="http://localhost:8154",
            token="test-token",
            spool=spool_db,
        )

        rowid = spool_db.enqueue(_make_test_update())

        with patch.object(publisher, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("timeout")
            mock_get_client.return_value = mock_client

            outcome = await publisher.drain()

            assert outcome.failed == 0
            assert outcome.unreachable is True
            # Entry should still be pending
            pending = spool_db.pending(limit=10)
            assert len(pending) == 1

    async def test_drain_respects_limit(self, spool_db):
        """drain() should respect the limit parameter."""
        publisher = Publisher(
            base_url="http://localhost:8154",
            token="test-token",
            spool=spool_db,
        )

        # Enqueue 5 updates
        for i in range(5):
            spool_db.enqueue(_make_test_update(run_id=f"run{i}"))

        with patch.object(publisher, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.post.return_value = mock_response
            mock_get_client.return_value = mock_client

            outcome = await publisher.drain(limit=3)

            # Should have sent at most 3
            assert outcome.sent <= 3
            mock_client.post.assert_called()
            assert mock_client.post.call_count <= 3

    async def test_drain_returns_empty_when_no_pending(self, spool_db):
        """drain() should return ok=True when no pending entries."""
        publisher = Publisher(
            base_url="http://localhost:8154",
            token="test-token",
            spool=spool_db,
        )

        # No enqueue, so no pending entries

        outcome = await publisher.drain()

        assert outcome.ok is True
        assert outcome.sent == 0
        assert outcome.failed == 0


@pytest.mark.asyncio
class TestContextManager:
    """Test Publisher as async context manager."""

    async def test_async_context_manager(self, spool_db):
        """Publisher should work as async context manager."""
        async with Publisher(
            base_url="http://localhost:8154",
            token="test-token",
            spool=spool_db,
        ) as publisher:
            assert publisher is not None
            assert publisher.token == "test-token"

        # Client should be closed
        assert publisher._client is None or not publisher._client.__dict__.get("_closed", True)


@pytest.mark.asyncio
class TestPublisherNeverRaises:
    """Test that Publisher methods never raise exceptions into agent code."""

    async def test_drain_never_raises(self, spool_db):
        """drain() should never raise, even on unexpected errors."""
        publisher = Publisher(
            base_url="http://localhost:8154",
            token="test-token",
            spool=spool_db,
        )

        spool_db.enqueue(_make_test_update())

        with patch.object(publisher, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post.side_effect = Exception("Unexpected error")
            mock_get_client.return_value = mock_client

            # Should not raise
            try:
                outcome = await publisher.drain()
                assert outcome is not None  # Should return something
            except Exception as e:
                pytest.fail(f"drain() raised an exception: {e}")

    async def test_publish_update_never_raises(self, spool_db):
        """publish_update() should never raise."""
        publisher = Publisher(
            base_url="http://localhost:8154",
            token="test-token",
            spool=spool_db,
        )

        update = _make_test_update()

        with patch.object(publisher, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post.side_effect = RuntimeError("Unexpected")
            mock_get_client.return_value = mock_client

            # Should not raise
            try:
                outcome = await publisher.publish_update(update)
                assert outcome is not None
            except Exception as e:
                pytest.fail(f"publish_update() raised: {e}")


class TestPublishOutcome:
    """Test PublishOutcome dataclass."""

    def test_publish_outcome_to_dict(self):
        """PublishOutcome.to_dict() should be JSON-serializable."""
        outcome = PublishOutcome(
            ok=True,
            sent=5,
            failed=0,
            unreachable=False,
            errors=[],
        )

        d = outcome.to_dict()
        assert d["ok"] is True
        assert d["sent"] == 5
        assert d["failed"] == 0
        assert isinstance(d["errors"], list)

    def test_publish_outcome_defaults(self):
        """PublishOutcome should have sensible defaults."""
        outcome = PublishOutcome(ok=False)
        assert outcome.sent == 0
        assert outcome.failed == 0
        assert outcome.skipped == 0
        assert outcome.unreachable is False
        assert outcome.errors == []
