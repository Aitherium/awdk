"""Tests for SQLite memory."""

import pytest
from adk.memory import Memory


@pytest.fixture
def mem(tmp_path):
    return Memory(db_path=tmp_path / "test.db", agent_name="test")


class TestKVStore:
    @pytest.mark.asyncio
    async def test_remember_and_recall(self, mem):
        await mem.remember("key1", "value1")
        result = await mem.recall("key1")
        assert result == "value1"

    @pytest.mark.asyncio
    async def test_recall_missing(self, mem):
        result = await mem.recall("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_overwrite(self, mem):
        await mem.remember("key1", "old")
        await mem.remember("key1", "new")
        assert await mem.recall("key1") == "new"

    @pytest.mark.asyncio
    async def test_forget(self, mem):
        await mem.remember("key1", "value1")
        await mem.forget("key1")
        assert await mem.recall("key1") is None

    @pytest.mark.asyncio
    async def test_search(self, mem):
        await mem.remember("project_name", "AitherOS")
        await mem.remember("project_version", "0.1.0")
        await mem.remember("unrelated", "stuff")
        results = await mem.search("project")
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_search_by_value(self, mem):
        await mem.remember("key1", "AitherOS is cool")
        results = await mem.search("AitherOS")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_list_keys(self, mem):
        await mem.remember("a", "1")
        await mem.remember("b", "2")
        keys = await mem.list_keys()
        assert set(keys) == {"a", "b"}

    @pytest.mark.asyncio
    async def test_list_keys_by_category(self, mem):
        await mem.remember("a", "1", category="config")
        await mem.remember("b", "2", category="data")
        keys = await mem.list_keys(category="config")
        assert keys == ["a"]

    @pytest.mark.asyncio
    async def test_metadata(self, mem):
        await mem.remember("key1", "val", metadata={"source": "test"})
        results = await mem.search("key1")
        assert results[0].metadata == {"source": "test"}


class TestConversationHistory:
    @pytest.mark.asyncio
    async def test_add_and_get(self, mem):
        await mem.add_message("s1", "user", "Hello")
        await mem.add_message("s1", "assistant", "Hi there!")
        history = await mem.get_history("s1")
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_session_isolation(self, mem):
        await mem.add_message("s1", "user", "Session 1")
        await mem.add_message("s2", "user", "Session 2")
        h1 = await mem.get_history("s1")
        h2 = await mem.get_history("s2")
        assert len(h1) == 1
        assert len(h2) == 1

    @pytest.mark.asyncio
    async def test_history_limit(self, mem):
        for i in range(100):
            await mem.add_message("s1", "user", f"Message {i}")
        history = await mem.get_history("s1", limit=10)
        assert len(history) == 10

    @pytest.mark.asyncio
    async def test_clear_session(self, mem):
        await mem.add_message("s1", "user", "Hello")
        await mem.clear_session("s1")
        history = await mem.get_history("s1")
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_history_order(self, mem):
        await mem.add_message("s1", "user", "First")
        await mem.add_message("s1", "assistant", "Second")
        await mem.add_message("s1", "user", "Third")
        history = await mem.get_history("s1")
        assert history[0]["content"] == "First"
        assert history[2]["content"] == "Third"
