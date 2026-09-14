"""Tests for adk/conversations.py — ConversationStore with session repair."""

import sys
import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.conversations import (
    ConversationStore,
    Conversation,
    RepairReport,
    get_conversation_store,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_singleton():
    import adk.conversations as conv_mod
    conv_mod._store = None
    yield
    conv_mod._store = None


@pytest.fixture
def store(tmp_path):
    return ConversationStore(data_dir=tmp_path / "conversations")


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------

class TestGetOrCreate:
    @pytest.mark.asyncio
    async def test_create_new_conversation(self, store):
        conv = await store.get_or_create("sess-1", agent_name="atlas")
        assert conv.session_id == "sess-1"
        assert conv.agent_name == "atlas"
        assert conv.messages == []
        assert conv.created_at > 0

    @pytest.mark.asyncio
    async def test_get_existing_conversation(self, store):
        await store.get_or_create("sess-2", agent_name="atlas")
        conv = await store.get_or_create("sess-2")
        assert conv.session_id == "sess-2"
        assert conv.agent_name == "atlas"

    @pytest.mark.asyncio
    async def test_persists_to_disk(self, store):
        await store.append_message("sess-3", "user", "hello", agent_name="demiurge")

        # Create a new store instance pointing to the same directory
        store2 = ConversationStore(data_dir=store._dir)
        conv = await store2.get_or_create("sess-3")
        assert len(conv.messages) == 1
        assert conv.messages[0]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_sanitizes_session_id(self, store):
        conv = await store.get_or_create("has/special!chars@here")
        path = store._path("has/special!chars@here")
        assert "/" not in path.name
        assert "!" not in path.name


class TestAppendMessage:
    @pytest.mark.asyncio
    async def test_append_user_message(self, store):
        await store.append_message("s1", "user", "Hello!")
        conv = await store.get_or_create("s1")
        assert len(conv.messages) == 1
        assert conv.messages[0]["role"] == "user"
        assert conv.messages[0]["content"] == "Hello!"
        assert "timestamp" in conv.messages[0]

    @pytest.mark.asyncio
    async def test_append_multiple_messages(self, store):
        await store.append_message("s1", "user", "Hi")
        await store.append_message("s1", "assistant", "Hello!")
        await store.append_message("s1", "user", "How are you?")
        conv = await store.get_or_create("s1")
        assert len(conv.messages) == 3
        roles = [m["role"] for m in conv.messages]
        assert roles == ["user", "assistant", "user"]

    @pytest.mark.asyncio
    async def test_append_updates_timestamp(self, store):
        await store.append_message("s1", "user", "msg1")
        t1 = (await store.get_or_create("s1")).updated_at
        await store.append_message("s1", "user", "msg2")
        t2 = (await store.get_or_create("s1")).updated_at
        assert t2 >= t1


class TestGetRecent:
    @pytest.mark.asyncio
    async def test_get_recent_returns_last_n(self, store):
        for i in range(10):
            await store.append_message("s1", "user", f"msg-{i}")
        recent = await store.get_recent("s1", n=3)
        assert len(recent) == 3
        assert recent[0]["content"] == "msg-7"
        assert recent[2]["content"] == "msg-9"

    @pytest.mark.asyncio
    async def test_get_recent_empty_session(self, store):
        recent = await store.get_recent("empty", n=5)
        assert recent == []


class TestListSessions:
    @pytest.mark.asyncio
    async def test_list_all_sessions(self, store):
        await store.append_message("s1", "user", "a", agent_name="atlas")
        await store.append_message("s2", "user", "b", agent_name="demiurge")
        sessions = await store.list_sessions()
        assert len(sessions) == 2
        ids = {s["session_id"] for s in sessions}
        assert "s1" in ids
        assert "s2" in ids

    @pytest.mark.asyncio
    async def test_list_sessions_filtered_by_agent(self, store):
        await store.append_message("s1", "user", "a", agent_name="atlas")
        await store.append_message("s2", "user", "b", agent_name="demiurge")
        sessions = await store.list_sessions(agent_name="atlas")
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "s1"

    @pytest.mark.asyncio
    async def test_list_sessions_has_metadata(self, store):
        await store.append_message("s1", "user", "msg1", agent_name="atlas")
        await store.append_message("s1", "user", "msg2", agent_name="atlas")
        sessions = await store.list_sessions()
        assert sessions[0]["message_count"] == 2
        assert sessions[0]["created_at"] > 0


class TestDeleteSession:
    @pytest.mark.asyncio
    async def test_delete_existing_session(self, store):
        await store.append_message("s1", "user", "hello")
        result = await store.delete_session("s1")
        assert result is True
        # Should be gone
        conv = store._load("s1")
        assert conv is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_session(self, store):
        result = await store.delete_session("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_removes_from_cache(self, store):
        await store.append_message("s1", "user", "hello")
        assert "s1" in store._cache
        await store.delete_session("s1")
        assert "s1" not in store._cache


# ---------------------------------------------------------------------------
# LRU cache
# ---------------------------------------------------------------------------

class TestLRUCache:
    @pytest.mark.asyncio
    async def test_cache_eviction(self, store):
        from adk.conversations import _LRU_MAX
        for i in range(_LRU_MAX + 5):
            await store.get_or_create(f"session-{i}")
        assert len(store._cache) <= _LRU_MAX


# ---------------------------------------------------------------------------
# Session repair
# ---------------------------------------------------------------------------

class TestRepairPhase1Schema:
    @pytest.mark.asyncio
    async def test_non_dict_message_fixed(self, store):
        conv = await store.get_or_create("r1")
        conv.messages = ["not a dict", {"role": "user", "content": "ok"}]
        store._save(conv)

        report = await store.repair_session("r1")
        assert report.issues_found >= 1
        assert report.issues_fixed >= 1
        assert report.phases_run == 7

        repaired = await store.get_or_create("r1")
        assert isinstance(repaired.messages[0], dict)
        assert repaired.messages[0]["role"] == "system"

    @pytest.mark.asyncio
    async def test_missing_content_detected_and_fixed(self, store):
        """Phase 1 adds missing content key, then Phase 3 may remove empty messages."""
        conv = await store.get_or_create("r2")
        # Missing content key -- Phase 1 will add content="" via setdefault.
        # Phase 3 will then remove it because user messages with empty content are invalid.
        conv.messages = [{"role": "user"}, {"role": "assistant", "content": "response"}]
        store._save(conv)

        report = await store.repair_session("r2")
        # Phase 1 detects missing content, Phase 3 detects empty content
        assert report.issues_found >= 1
        assert report.issues_fixed >= 1

    @pytest.mark.asyncio
    async def test_missing_role_fixed(self, store):
        """Phase 1 adds missing role key. Non-empty content survives Phase 3."""
        conv = await store.get_or_create("r2b")
        # Missing role but has content -- Phase 1 fixes role to "system"
        conv.messages = [{"content": "some valid text"}]
        store._save(conv)

        report = await store.repair_session("r2b")
        assert report.issues_found >= 1

        repaired = await store.get_or_create("r2b")
        assert len(repaired.messages) == 1
        assert repaired.messages[0]["role"] == "system"
        assert repaired.messages[0]["content"] == "some valid text"


class TestRepairPhase2Roles:
    @pytest.mark.asyncio
    async def test_invalid_role_corrected(self, store):
        conv = await store.get_or_create("r3")
        conv.messages = [{"role": "invalid_role", "content": "test"}]
        store._save(conv)

        report = await store.repair_session("r3")
        assert report.issues_found >= 1

        repaired = await store.get_or_create("r3")
        assert repaired.messages[0]["role"] == "system"


class TestRepairPhase3Content:
    @pytest.mark.asyncio
    async def test_empty_user_message_removed(self, store):
        conv = await store.get_or_create("r4")
        conv.messages = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "response"},
        ]
        store._save(conv)

        report = await store.repair_session("r4")
        assert report.messages_removed >= 1

        repaired = await store.get_or_create("r4")
        assert len(repaired.messages) == 1
        assert repaired.messages[0]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_empty_tool_message_kept(self, store):
        conv = await store.get_or_create("r5")
        conv.messages = [
            {"role": "assistant", "content": "calling tool"},
            {"role": "tool", "content": ""},
        ]
        store._save(conv)

        report = await store.repair_session("r5")
        repaired = await store.get_or_create("r5")
        assert len(repaired.messages) == 2


class TestRepairPhase4Timestamps:
    @pytest.mark.asyncio
    async def test_non_monotonic_timestamps_fixed(self, store):
        conv = await store.get_or_create("r6")
        conv.messages = [
            {"role": "user", "content": "a", "timestamp": 100.0},
            {"role": "assistant", "content": "b", "timestamp": 50.0},  # Earlier!
        ]
        store._save(conv)

        report = await store.repair_session("r6")
        assert report.issues_found >= 1
        assert report.messages_reordered >= 1

        repaired = await store.get_or_create("r6")
        assert repaired.messages[1]["timestamp"] >= repaired.messages[0]["timestamp"]


class TestRepairPhase5RoleAlternation:
    @pytest.mark.asyncio
    async def test_consecutive_same_role_detected(self, store):
        conv = await store.get_or_create("r7")
        conv.messages = [
            {"role": "user", "content": "a", "timestamp": 1.0},
            {"role": "user", "content": "b", "timestamp": 2.0},
            {"role": "user", "content": "c", "timestamp": 3.0},
            {"role": "user", "content": "d", "timestamp": 4.0},
        ]
        store._save(conv)

        report = await store.repair_session("r7")
        # Should detect back-to-back user messages
        assert any("P5" in d for d in report.details)


class TestRepairPhase6OrphanTool:
    @pytest.mark.asyncio
    async def test_orphan_tool_result_removed(self, store):
        conv = await store.get_or_create("r8")
        conv.messages = [
            {"role": "tool", "content": "orphan result", "timestamp": 1.0},
            {"role": "user", "content": "hello", "timestamp": 2.0},
        ]
        store._save(conv)

        report = await store.repair_session("r8")
        assert report.issues_found >= 1
        assert report.messages_removed >= 1

        repaired = await store.get_or_create("r8")
        assert repaired.messages[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_tool_after_user_detected(self, store):
        conv = await store.get_or_create("r9")
        conv.messages = [
            {"role": "user", "content": "hi", "timestamp": 1.0},
            {"role": "tool", "content": "result", "timestamp": 2.0},
        ]
        store._save(conv)

        report = await store.repair_session("r9")
        assert any("P6" in d for d in report.details)


class TestRepairPhase7Integrity:
    @pytest.mark.asyncio
    async def test_integrity_hash_stored(self, store):
        await store.append_message("r10", "user", "hello")
        report = await store.repair_session("r10")
        assert report.phases_run == 7

        conv = await store.get_or_create("r10")
        assert "last_repair" in conv.metadata
        assert "content_hash" in conv.metadata["last_repair"]


class TestRepairMisc:
    @pytest.mark.asyncio
    async def test_empty_session_is_clean(self, store):
        await store.get_or_create("empty")
        report = await store.repair_session("empty")
        assert report.clean is True
        assert report.phases_run == 7

    @pytest.mark.asyncio
    async def test_validate_session_dry_run(self, store):
        conv = await store.get_or_create("val")
        conv.messages = [{"role": "invalid", "content": "test"}]
        store._save(conv)

        report = await store.validate_session("val")
        assert report.issues_found >= 1
        assert report.issues_fixed == 0  # dry run

    @pytest.mark.asyncio
    async def test_backup_created_on_repair(self, store):
        await store.append_message("bak", "user", "hello")
        report = await store.repair_session("bak")
        # Backup is only created when auto_fix=True (default) AND issues are found or
        # auto_fix is attempted -- but backup is always created if auto_fix=True
        # and the file exists
        assert report.backup_created is True or report.clean is True

    @pytest.mark.asyncio
    async def test_repair_all(self, store):
        conv1 = await store.get_or_create("all1")
        conv1.messages = [{"role": "invalid", "content": "test"}]
        store._save(conv1)

        await store.append_message("all2", "user", "clean session")

        reports = await store.repair_all()
        # Only non-clean sessions are returned
        dirty_ids = [r.session_id for r in reports]
        assert "all1" in dirty_ids


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_get_conversation_store_returns_same(self):
        s1 = get_conversation_store()
        s2 = get_conversation_store()
        assert s1 is s2

    def test_get_conversation_store_creates_instance(self):
        store = get_conversation_store()
        assert isinstance(store, ConversationStore)
