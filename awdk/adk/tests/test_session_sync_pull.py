"""Tests for session sync pull functionality (bi-directional sync)."""

import asyncio
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adk.conversations import ConversationStore, Conversation
from adk.sync.sessions import SessionSyncClient, SessionSyncConfig


@pytest.fixture
def temp_aither_dir():
    """Create a temporary ~/.aither structure for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        aither_root = Path(tmpdir)
        (aither_root / "conversations").mkdir(parents=True)
        yield aither_root


@pytest.fixture
def conversation_store(temp_aither_dir):
    """Create a ConversationStore in temp directory."""
    store = ConversationStore(data_dir=temp_aither_dir / "conversations")
    return store


@pytest.fixture
def sync_client():
    """Create a SessionSyncClient with mock config."""
    config = SessionSyncConfig(
        enabled=True,
        gateway_url="http://localhost:8001",
        debounce_seconds=5.0,
        timeout_seconds=30.0,
    )
    return SessionSyncClient(
        gateway_url="http://localhost:8001",
        auth_token="Bearer test_token",
        config=config,
    )


@pytest.mark.asyncio
async def test_pull_sessions_returns_empty_when_disabled(sync_client):
    """Test pull_sessions returns empty list when sync is disabled."""
    sync_client.config.enabled = False
    result = await sync_client.pull_sessions(watermark=0.0)
    assert result == []


@pytest.mark.asyncio
async def test_pull_sessions_with_mock_response(sync_client):
    """Test pull_sessions fetches and parses remote sessions."""
    # Mock the _request method
    remote_data = {
        "sessions": [
            {
                "session_id": "sess-1",
                "agent_name": "test-agent",
                "messages": [
                    {"role": "user", "content": "Hello", "timestamp": 100.0}
                ],
                "created_at": 100.0,
                "updated_at": 150.0,
                "metadata": {},
            },
            {
                "session_id": "sess-2",
                "agent_name": "test-agent",
                "messages": [
                    {"role": "assistant", "content": "Hi", "timestamp": 200.0}
                ],
                "created_at": 200.0,
                "updated_at": 250.0,
                "metadata": {},
            },
        ],
        "total": 2,
    }

    sync_client._request = AsyncMock(return_value=remote_data)

    result = await sync_client.pull_sessions(watermark=0.0)
    assert len(result) == 2
    assert result[0]["session_id"] == "sess-1"
    assert result[1]["session_id"] == "sess-2"
    sync_client._request.assert_called_once_with("GET", "/v1/sessions?since=0.0")


@pytest.mark.asyncio
async def test_pull_sessions_handles_404_gracefully(sync_client):
    """Test pull_sessions handles 404 error gracefully (fail-soft)."""
    error_response = {"error": True, "status": 404, "detail": "Not found"}
    sync_client._request = AsyncMock(return_value=error_response)

    result = await sync_client.pull_sessions(watermark=0.0)
    assert result == []


@pytest.mark.asyncio
async def test_pull_sessions_handles_403_gracefully(sync_client):
    """Test pull_sessions handles 403 error gracefully."""
    error_response = {"error": True, "status": 403, "detail": "Forbidden"}
    sync_client._request = AsyncMock(return_value=error_response)

    result = await sync_client.pull_sessions(watermark=0.0)
    assert result == []


@pytest.mark.asyncio
async def test_pull_sessions_handles_507_gracefully(sync_client):
    """Test pull_sessions handles 507 error gracefully."""
    error_response = {"error": True, "status": 507, "detail": "Service unavailable"}
    sync_client._request = AsyncMock(return_value=error_response)

    result = await sync_client.pull_sessions(watermark=0.0)
    assert result == []


@pytest.mark.asyncio
async def test_pull_sessions_filters_by_watermark(sync_client):
    """Test pull_sessions uses watermark in query param."""
    remote_data = {"sessions": [], "total": 0}
    sync_client._request = AsyncMock(return_value=remote_data)

    watermark = 1234567890.0
    await sync_client.pull_sessions(watermark=watermark)
    sync_client._request.assert_called_once_with(
        "GET", f"/v1/sessions?since={watermark}"
    )


@pytest.mark.asyncio
async def test_merge_remote_sessions_lww_remote_wins(
    sync_client, conversation_store
):
    """Test merge_remote_sessions: remote wins when significantly newer (LWW)."""
    # Create a local session with old timestamp
    now = time.time()
    local_conv = Conversation(
        session_id="sess-1",
        agent_name="agent",
        messages=[{"role": "user", "content": "old", "timestamp": now - 100}],
        created_at=now - 100,
        updated_at=now - 100,
    )
    conversation_store._touch_cache("sess-1", local_conv)
    conversation_store._save(local_conv)

    # Remote session is much newer (>60s)
    remote_sessions = [
        {
            "session_id": "sess-1",
            "agent_name": "agent",
            "messages": [
                {"role": "user", "content": "new", "timestamp": now - 50}
            ],
            "created_at": now - 100,
            "updated_at": now - 10,  # Much newer than local
            "metadata": {},
        }
    ]

    result = await sync_client.merge_remote_sessions(
        conversation_store, remote_sessions
    )

    assert result["merged"] == 1
    assert result["skipped"] == 0
    assert len(result["errors"]) == 0

    # Verify local was replaced
    updated = await conversation_store.get_or_create("sess-1")
    assert len(updated.messages) == 1
    assert updated.messages[0]["content"] == "new"


@pytest.mark.asyncio
async def test_merge_remote_sessions_lww_local_wins(
    sync_client, conversation_store
):
    """Test merge_remote_sessions: local wins when it's newer (LWW)."""
    # Create a local session with recent timestamp
    now = time.time()
    local_conv = Conversation(
        session_id="sess-1",
        agent_name="agent",
        messages=[{"role": "user", "content": "local", "timestamp": now - 10}],
        created_at=now - 100,
        updated_at=now - 10,  # Recent
    )
    conversation_store._touch_cache("sess-1", local_conv)
    conversation_store._save(local_conv)

    # Remote session is older
    remote_sessions = [
        {
            "session_id": "sess-1",
            "agent_name": "agent",
            "messages": [
                {"role": "user", "content": "remote", "timestamp": now - 50}
            ],
            "created_at": now - 100,
            "updated_at": now - 50,  # Older than local
            "metadata": {},
        }
    ]

    result = await sync_client.merge_remote_sessions(
        conversation_store, remote_sessions
    )

    assert result["merged"] == 0
    assert result["skipped"] == 1
    assert len(result["errors"]) == 0

    # Verify local was NOT overwritten
    updated = await conversation_store.get_or_create("sess-1")
    assert updated.messages[0]["content"] == "local"


@pytest.mark.asyncio
async def test_merge_remote_sessions_guard_60s(
    sync_client, conversation_store
):
    """Test 60s guard: recent local not overwritten unless remote is >60s newer."""
    now = time.time()
    local_conv = Conversation(
        session_id="sess-1",
        agent_name="agent",
        messages=[{"role": "user", "content": "local", "timestamp": now - 30}],
        created_at=now - 100,
        updated_at=now - 30,  # Recent (within 60s)
    )
    conversation_store._touch_cache("sess-1", local_conv)
    conversation_store._save(local_conv)

    # Remote is only 30s newer (< 60s guard)
    remote_sessions = [
        {
            "session_id": "sess-1",
            "agent_name": "agent",
            "messages": [
                {"role": "user", "content": "remote", "timestamp": now - 50}
            ],
            "created_at": now - 100,
            "updated_at=": now - 59,  # Only 29s newer than local
            "metadata": {},
        }
    ]

    result = await sync_client.merge_remote_sessions(
        conversation_store, remote_sessions
    )

    # Should skip (not enough newer)
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_merge_remote_sessions_new_session(
    sync_client, conversation_store
):
    """Test merge_remote_sessions creates new session if not local."""
    # No local session exists
    remote_sessions = [
        {
            "session_id": "sess-new",
            "agent_name": "agent",
            "messages": [{"role": "user", "content": "hello", "timestamp": 100.0}],
            "created_at": 100.0,
            "updated_at": 150.0,
            "metadata": {"test": True},
        }
    ]

    result = await sync_client.merge_remote_sessions(
        conversation_store, remote_sessions
    )

    assert result["merged"] == 1
    assert result["skipped"] == 0

    # Verify session was created
    created = await conversation_store.get_or_create("sess-new")
    assert created.agent_name == "agent"
    assert len(created.messages) == 1
    assert created.metadata["test"] is True


@pytest.mark.asyncio
async def test_merge_remote_sessions_invalid_shape_skipped(
    sync_client, conversation_store
):
    """Test merge_remote_sessions skips sessions with invalid message shape."""
    remote_sessions = [
        {
            "session_id": "sess-bad",
            "agent_name": "agent",
            "messages": "not a list",  # Invalid
            "created_at": 100.0,
            "updated_at": 150.0,
            "metadata": {},
        }
    ]

    result = await sync_client.merge_remote_sessions(
        conversation_store, remote_sessions
    )

    # Should error, not merge
    assert result["merged"] == 0
    assert len(result["errors"]) > 0


@pytest.mark.asyncio
async def test_merge_remote_sessions_watermark_updated_on_success(
    sync_client, conversation_store
):
    """Test watermark is updated after successful merge."""
    now = time.time()
    remote_sessions = [
        {
            "session_id": "sess-1",
            "agent_name": "agent",
            "messages": [{"role": "user", "content": "hello", "timestamp": now}],
            "created_at": now,
            "updated_at": now + 100,  # Future timestamp
            "metadata": {},
        }
    ]

    # Initial watermark should be 0
    initial_watermark = await conversation_store.get_pull_watermark()
    assert initial_watermark == 0.0

    result = await sync_client.merge_remote_sessions(
        conversation_store, remote_sessions
    )

    assert result["merged"] == 1

    # Watermark should be updated to max(remote.updated_at)
    updated_watermark = await conversation_store.get_pull_watermark()
    assert updated_watermark == now + 100


@pytest.mark.asyncio
async def test_merge_remote_sessions_watermark_not_updated_on_error(
    sync_client, conversation_store
):
    """Test watermark is NOT updated if merge has errors."""
    now = time.time()
    remote_sessions = [
        {
            "session_id": "sess-1",
            "agent_name": "agent",
            "messages": [{"role": "user", "content": "hello", "timestamp": now}],
            "created_at": now,
            "updated_at": now + 100,
            "metadata": {},
        },
        {
            "session_id": "sess-bad",
            "agent_name": "agent",
            "messages": "invalid",  # Will error
            "created_at": now,
            "updated_at": now + 200,
            "metadata": {},
        },
    ]

    result = await sync_client.merge_remote_sessions(
        conversation_store, remote_sessions
    )

    # Should have errors
    assert len(result["errors"]) > 0

    # Watermark should NOT be updated (partial failure safe)
    watermark = await conversation_store.get_pull_watermark()
    assert watermark == 0.0


@pytest.mark.asyncio
async def test_get_pull_watermark_default_zero(conversation_store):
    """Test get_pull_watermark returns 0.0 when not set."""
    watermark = await conversation_store.get_pull_watermark()
    assert watermark == 0.0


@pytest.mark.asyncio
async def test_set_pull_watermark_persists(conversation_store, temp_aither_dir):
    """Test set_pull_watermark persists to disk."""
    ts = 1234567890.0
    await conversation_store.set_pull_watermark(ts)

    # Load directly from file
    watermark_file = temp_aither_dir / "pull_watermark.json"
    assert watermark_file.exists()
    data = json.loads(watermark_file.read_text(encoding="utf-8"))
    assert data["last_updated_at"] == ts


@pytest.mark.asyncio
async def test_set_pull_watermark_monotonic(conversation_store):
    """Test set_pull_watermark is monotonic (doesn't go backwards)."""
    # Set initial watermark
    await conversation_store.set_pull_watermark(1000.0)

    # Try to set backwards
    await conversation_store.set_pull_watermark(500.0)

    # Should keep the higher value
    watermark = await conversation_store.get_pull_watermark()
    assert watermark == 1000.0


@pytest.mark.asyncio
async def test_merge_remote_session_replaces_local(
    conversation_store, temp_aither_dir
):
    """Test merge_remote_session atomically replaces local session."""
    now = time.time()

    # Create local session
    local_conv = Conversation(
        session_id="sess-1",
        agent_name="old-agent",
        messages=[{"role": "user", "content": "old", "timestamp": now - 100}],
        created_at=now - 100,
        updated_at=now - 100,
    )
    await conversation_store.get_or_create("sess-1")  # Prime the store
    conversation_store._touch_cache("sess-1", local_conv)
    conversation_store._save(local_conv)

    # Remote session
    remote = {
        "session_id": "sess-1",
        "agent_name": "new-agent",
        "messages": [
            {"role": "user", "content": "new1", "timestamp": now - 50},
            {"role": "assistant", "content": "new2", "timestamp": now - 40},
        ],
        "created_at": now - 100,
        "updated_at": now - 10,
        "metadata": {"version": 2},
    }

    # Merge
    await conversation_store.merge_remote_session("sess-1", remote)

    # Load and verify
    merged = await conversation_store.get_or_create("sess-1")
    assert merged.agent_name == "new-agent"
    assert len(merged.messages) == 2
    assert merged.messages[0]["content"] == "new1"
    assert merged.messages[1]["content"] == "new2"
    assert merged.metadata["version"] == 2


@pytest.mark.asyncio
async def test_enroll_enables_session_sync_config(temp_aither_dir):
    """Test fleet_enroll._enable_session_sync_default sets config."""
    from adk.fleet_enroll import _enable_session_sync_default

    # Mock the config file path
    config_file = temp_aither_dir / "config.json"

    # Monkey-patch the global to use temp dir
    with patch("adk.fleet_enroll._CONFIG_FILE", config_file):
        _enable_session_sync_default()

    # Verify config was written
    assert config_file.exists()
    config = json.loads(config_file.read_text(encoding="utf-8"))
    assert config.get("session_sync", {}).get("enabled") is True


@pytest.mark.asyncio
async def test_enroll_respects_opt_out(temp_aither_dir):
    """Test fleet_enroll._enable_session_sync_default respects opt-out."""
    from adk.fleet_enroll import _enable_session_sync_default

    config_file = temp_aither_dir / "config.json"

    # Pre-write config with opt-out
    config_file.write_text(
        json.dumps({"session_sync": {"enabled": False}}),
        encoding="utf-8"
    )

    with patch("adk.fleet_enroll._CONFIG_FILE", config_file):
        _enable_session_sync_default()

    # Verify opt-out was respected (still False)
    config = json.loads(config_file.read_text(encoding="utf-8"))
    assert config.get("session_sync", {}).get("enabled") is False
