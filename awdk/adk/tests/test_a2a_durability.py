"""Test A2A task persistence and durability across restarts.

This module verifies that A2A tasks created in one instance survive
a "restart" (loading from persistent JSONL storage) and can be retrieved
by a fresh TaskManager instance.
"""

import json
import tempfile
from pathlib import Path

import pytest

from adk.a2a import TaskManager, Task, TaskState, TaskStatus, A2AMessage


@pytest.fixture
def temp_store():
    """Create a temporary JSONL store file for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = Path(tmpdir) / "tasks.jsonl"
        yield store_path


class TestTaskManagerDurability:
    """Test suite for persistent A2A task storage."""

    def test_create_task_persists_to_file(self, temp_store):
        """Verify that created tasks are written to the JSONL store."""
        tm = TaskManager(store_path=temp_store)
        task = tm.create_task(context_id="test-context", metadata={"user": "alice"})

        # Verify the task exists in memory
        assert tm.get_task(task.id) is not None
        assert tm.get_task(task.id).contextId == "test-context"

        # Verify the task was written to file
        assert temp_store.exists()
        with temp_store.open("r", encoding="utf-8") as f:
            lines = f.readlines()
            assert len(lines) >= 1
            # Last line should contain our task
            obj = json.loads(lines[-1])
            assert obj["id"] == task.id
            assert obj["contextId"] == "test-context"

    def test_task_survives_restart(self, temp_store):
        """Simulate restart: create task, new instance loads it from store."""
        # First instance: create a task
        tm1 = TaskManager(store_path=temp_store)
        task = tm1.create_task(
            context_id="restart-test",
            metadata={"version": "1"}
        )
        task_id = task.id

        # "Restart" — construct fresh TaskManager
        tm2 = TaskManager(store_path=temp_store)

        # Verify the new instance can retrieve the task
        retrieved = tm2.get_task(task_id)
        assert retrieved is not None, "Task should be retrievable after restart"
        assert retrieved.id == task_id
        assert retrieved.contextId == "restart-test"
        assert retrieved.metadata == {"version": "1"}

    def test_multiple_tasks_survive_restart(self, temp_store):
        """Verify that multiple tasks all survive a restart."""
        tm1 = TaskManager(store_path=temp_store)
        task_ids = []
        for i in range(3):
            task = tm1.create_task(context_id=f"context-{i}")
            task_ids.append(task.id)

        # "Restart"
        tm2 = TaskManager(store_path=temp_store)

        # Verify all tasks are retrievable
        for i, task_id in enumerate(task_ids):
            retrieved = tm2.get_task(task_id)
            assert retrieved is not None
            assert retrieved.contextId == f"context-{i}"

    def test_task_status_update_persists(self, temp_store):
        """Verify that status changes are persisted to the store."""
        tm1 = TaskManager(store_path=temp_store)
        task = tm1.create_task(context_id="status-test")
        task_id = task.id

        # Update status
        tm1.update_status(task_id, TaskState.WORKING, message="Processing...")

        # "Restart"
        tm2 = TaskManager(store_path=temp_store)
        retrieved = tm2.get_task(task_id)

        assert retrieved is not None
        assert retrieved.status.state == TaskState.WORKING
        assert retrieved.status.message == "Processing..."

    def test_task_message_history_persists(self, temp_store):
        """Verify that message history is persisted to the store."""
        tm1 = TaskManager(store_path=temp_store)
        task = tm1.create_task(context_id="message-test")
        task_id = task.id

        # Add messages
        user_msg = A2AMessage(
            role="user",
            parts=[{"type": "text", "text": "Hello"}],
            messageId="msg-1",
            taskId=task_id,
        )
        tm1.add_message(task_id, user_msg)

        agent_msg = A2AMessage(
            role="agent",
            parts=[{"type": "text", "text": "Hi there"}],
            messageId="msg-2",
            taskId=task_id,
        )
        tm1.add_message(task_id, agent_msg)

        # "Restart"
        tm2 = TaskManager(store_path=temp_store)
        retrieved = tm2.get_task(task_id)

        assert retrieved is not None
        assert len(retrieved.history) == 2
        assert retrieved.history[0]["parts"][0]["text"] == "Hello"
        assert retrieved.history[1]["parts"][0]["text"] == "Hi there"

    def test_get_nonexistent_task_returns_none(self, temp_store):
        """Verify that getting a nonexistent task returns None."""
        tm = TaskManager(store_path=temp_store)
        assert tm.get_task("nonexistent") is None

    def test_malformed_jsonl_line_skipped(self, temp_store):
        """Verify that malformed JSONL lines are skipped gracefully."""
        # Manually write some mixed valid and invalid lines
        with temp_store.open("w", encoding="utf-8") as f:
            # Valid task
            valid = {
                "id": "valid-1",
                "contextId": "ctx-1",
                "status": {"state": "submitted", "message": "", "timestamp": "2026-01-01T00:00:00Z"},
                "history": [],
                "artifacts": [],
                "metadata": {},
            }
            f.write(json.dumps(valid) + "\n")

            # Invalid JSON
            f.write("not valid json\n")

            # Another valid task
            valid2 = {
                "id": "valid-2",
                "contextId": "ctx-2",
                "status": {"state": "working", "message": "Running", "timestamp": "2026-01-01T00:00:00Z"},
                "history": [],
                "artifacts": [],
                "metadata": {},
            }
            f.write(json.dumps(valid2) + "\n")

        # Load should skip the invalid line and recover
        tm = TaskManager(store_path=temp_store)
        assert tm.get_task("valid-1") is not None
        assert tm.get_task("valid-2") is not None

    def test_concurrent_updates_to_store(self, temp_store):
        """Verify that multiple updates write multiple lines (append-only)."""
        tm = TaskManager(store_path=temp_store)
        task = tm.create_task(context_id="multi-update")
        task_id = task.id

        # Multiple updates
        tm.update_status(task_id, TaskState.WORKING)
        tm.update_status(task_id, TaskState.COMPLETED)

        # Should have 3 lines: create + 2 updates
        with temp_store.open("r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
            assert len(lines) >= 3

        # Fresh instance should have the latest state
        tm2 = TaskManager(store_path=temp_store)
        task2 = tm2.get_task(task_id)
        assert task2.status.state == TaskState.COMPLETED


class TestA2AServerTaskPersistence:
    """Test A2A server integration with persistent tasks."""

    def test_a2a_server_uses_persistent_tasks(self, temp_store):
        """Verify that A2AServer can be configured with persistent task storage."""
        from adk.a2a import A2AServer

        server = A2AServer(
            agent=None,
            base_url="http://localhost:8080",
            task_store_path=temp_store,
        )

        # Create a task via the server
        task = server._tasks.create_task(context_id="a2a-test")

        # "Restart" the server
        server2 = A2AServer(
            agent=None,
            base_url="http://localhost:8080",
            task_store_path=temp_store,
        )

        # Task should be retrievable from the new server instance
        retrieved = server2._tasks.get_task(task.id)
        assert retrieved is not None
        assert retrieved.contextId == "a2a-test"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
