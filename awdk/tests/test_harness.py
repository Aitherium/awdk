"""Tests for adk.harness module (EventLog and ContextBlocks)."""

import pytest

from adk.harness import ContextBlocks, EventLog


class TestEventLog:
    """Test EventLog class."""

    def test_append_single_event(self) -> None:
        """append() should store event and return tag."""
        log = EventLog()
        tag = log.append("message", "hello")
        assert isinstance(tag, int)
        assert tag == 0

    def test_append_multiple_events_sequential_tags(self) -> None:
        """Multiple appends should have sequential tags."""
        log = EventLog()
        tag1 = log.append("message", "hello")
        tag2 = log.append("error", "oops")
        tag3 = log.append("tool_call", {"fn": "search"})

        assert tag1 == 0
        assert tag2 == 1
        assert tag3 == 2

    def test_query_all_events(self) -> None:
        """query() with no type should return all events."""
        log = EventLog()
        log.append("message", "hello")
        log.append("error", "oops")
        log.append("message", "goodbye")

        results = log.query()
        assert len(results) == 3
        assert results[0] == (0, "message", "hello")
        assert results[1] == (1, "error", "oops")
        assert results[2] == (2, "message", "goodbye")

    def test_query_by_type(self) -> None:
        """query(type=...) should filter by event type."""
        log = EventLog()
        log.append("message", "hello")
        log.append("error", "oops")
        log.append("message", "goodbye")

        results = log.query(type="message")
        assert len(results) == 2
        assert results[0] == (0, "message", "hello")
        assert results[1] == (2, "message", "goodbye")

    def test_query_by_type_empty_result(self) -> None:
        """query(type=...) with no matching events should return empty list."""
        log = EventLog()
        log.append("message", "hello")

        results = log.query(type="nonexistent")
        assert results == []

    def test_query_with_limit(self) -> None:
        """query(limit=...) should return most recent N events."""
        log = EventLog()
        for i in range(10):
            log.append("message", f"msg_{i}")

        results = log.query(limit=3)
        assert len(results) == 3
        # Most recent 3
        assert results[0] == (7, "message", "msg_7")
        assert results[1] == (8, "message", "msg_8")
        assert results[2] == (9, "message", "msg_9")

    def test_query_with_type_and_limit(self) -> None:
        """query(type=..., limit=...) should combine filters."""
        log = EventLog()
        log.append("message", "m1")
        log.append("error", "e1")
        log.append("message", "m2")
        log.append("error", "e2")
        log.append("message", "m3")

        results = log.query(type="message", limit=2)
        assert len(results) == 2
        assert results[0] == (2, "message", "m2")
        assert results[1] == (4, "message", "m3")

    def test_query_with_since(self) -> None:
        """query(since=...) should return events after tag."""
        log = EventLog()
        for i in range(5):
            log.append("message", f"msg_{i}")

        results = log.query(since=2)
        assert len(results) == 2
        assert results[0] == (3, "message", "msg_3")
        assert results[1] == (4, "message", "msg_4")

    def test_get_existing_event(self) -> None:
        """get(tag) should return event by tag."""
        log = EventLog()
        tag = log.append("message", "hello")

        result = log.get(tag)
        assert result == (tag, "message", "hello")

    def test_get_nonexistent_event(self) -> None:
        """get(tag) should return None for missing tag."""
        log = EventLog()
        log.append("message", "hello")

        result = log.get(999)
        assert result is None

    def test_get_multiple_tags(self) -> None:
        """get(list) should return events for multiple tags."""
        log = EventLog()
        tag0 = log.append("message", "hello")
        tag1 = log.append("error", "oops")
        tag2 = log.append("message", "goodbye")

        results = log.get([tag0, tag2])
        assert len(results) == 2
        assert results[0] == (tag0, "message", "hello")
        assert results[1] == (tag2, "message", "goodbye")

    def test_keys_returns_all_tags(self) -> None:
        """keys() should return sorted list of all tags."""
        log = EventLog()
        log.append("message", "hello")
        log.append("error", "oops")
        log.append("message", "goodbye")

        keys = log.keys()
        assert keys == [0, 1, 2]

    def test_collapse_single_event(self) -> None:
        """collapse() should replace range with marker."""
        log = EventLog()
        tag0 = log.append("message", "hello")
        tag1 = log.append("error", "oops")
        tag2 = log.append("message", "goodbye")

        # Collapse tag0 only
        marker_tag = log.collapse(tag0, tag0, "User greeting")
        assert isinstance(marker_tag, int)
        assert marker_tag == 0  # Marker reuses start_tag position

        # Original event should no longer be in queries
        results = log.query()
        assert len(results) == 3  # Marker, error, goodbye
        assert results[0][1] == "_collapse"  # Marker at tag0 position
        assert results[1] == (tag1, "error", "oops")
        assert results[2] == (tag2, "message", "goodbye")

    def test_collapse_range(self) -> None:
        """collapse() should handle range of events."""
        log = EventLog()
        for i in range(5):
            log.append("message", f"msg_{i}")

        # Collapse events 1-3
        marker_tag = log.collapse(1, 3, "Earlier messages")

        results = log.query()
        # Should have: tag0, marker(at tag1 position), tag4
        assert len(results) == 3
        assert results[0] == (0, "message", "msg_0")
        assert results[1][1] == "_collapse"  # Marker at tag1 position
        assert results[2] == (4, "message", "msg_4")

    def test_collapse_returns_marker_tag(self) -> None:
        """collapse() should return the marker's tag (reuses start_tag)."""
        log = EventLog()
        log.append("message", "m1")
        log.append("message", "m2")
        log.append("message", "m3")

        marker_tag = log.collapse(0, 2, "Summary")
        assert marker_tag == 0  # Marker reuses start_tag position

    def test_collapse_filters_out_original_events(self) -> None:
        """collapse() should remove original events from type index."""
        log = EventLog()
        log.append("message", "m1")
        log.append("error", "e1")
        log.append("message", "m2")

        # Query messages before collapse
        before = log.query(type="message")
        assert len(before) == 2

        # Collapse message events
        log.collapse(0, 2, "All events")

        # Query messages after collapse
        after = log.query(type="message")
        assert len(after) == 0

    def test_keys_includes_collapse_markers(self) -> None:
        """keys() should include collapse marker tags."""
        log = EventLog()
        tag0 = log.append("message", "m1")
        tag1 = log.append("message", "m2")
        marker_tag = log.collapse(tag0, tag1, "Summary")

        keys = log.keys()
        assert marker_tag in keys

    def test_complex_scenario(self) -> None:
        """Test a complex scenario with multiple operations."""
        log = EventLog()

        # Add diverse events
        for i in range(10):
            log.append("message", f"msg_{i}")
            if i % 3 == 0:
                log.append("error", f"error_{i}")

        # Query all
        all_events = log.query()
        assert len(all_events) > 10

        # Query messages
        messages = log.query(type="message")
        assert len(messages) == 10

        # Query with limit
        recent = log.query(limit=5)
        assert len(recent) == 5

        # Collapse early events
        marker = log.collapse(0, 4, "Early activity")
        assert isinstance(marker, int)

        # Verify collapse worked
        after_collapse = log.query()
        # Should have fewer events than before
        assert len(after_collapse) < len(all_events)

        # Keys should include marker
        keys = log.keys()
        assert marker in keys


class TestContextBlocks:
    """Test ContextBlocks class."""

    def test_set_static_block(self) -> None:
        """set() should store static block."""
        blocks = ContextBlocks()
        blocks.set("system", "You are helpful.")
        rendered = blocks.render()
        assert rendered == "You are helpful."

    def test_set_multiple_static_blocks(self) -> None:
        """set() multiple blocks should preserve insertion order."""
        blocks = ContextBlocks()
        blocks.set("a", "A")
        blocks.set("b", "B")
        blocks.set("c", "C")
        rendered = blocks.render()
        assert rendered == "ABC"

    def test_set_static_block_overwrite(self) -> None:
        """set() should overwrite existing static block."""
        blocks = ContextBlocks()
        blocks.set("key", "old")
        blocks.set("key", "new")
        rendered = blocks.render()
        assert rendered == "new"

    def test_set_dynamic_block(self) -> None:
        """set_dynamic() should store callable block."""
        blocks = ContextBlocks()
        counter = {"value": 0}

        def count() -> str:
            counter["value"] += 1
            return f"Count: {counter['value']}"

        blocks.set_dynamic("counter", count)

        # First render
        result1 = blocks.render()
        assert result1 == "Count: 1"

        # Second render - should re-evaluate
        result2 = blocks.render()
        assert result2 == "Count: 2"

    def test_dynamic_blocks_render_last(self) -> None:
        """Dynamic blocks should render after static blocks."""
        blocks = ContextBlocks()
        blocks.set("static", "[Static]")
        blocks.set_dynamic("dynamic", lambda: "[Dynamic]")
        rendered = blocks.render()
        assert rendered == "[Static][Dynamic]"

    def test_multiple_static_and_dynamic_blocks(self) -> None:
        """Mix of static and dynamic blocks should maintain order."""
        blocks = ContextBlocks()
        blocks.set("s1", "S1")
        blocks.set("s2", "S2")
        blocks.set_dynamic("d1", lambda: "D1")
        blocks.set_dynamic("d2", lambda: "D2")

        rendered = blocks.render()
        # Static in insertion order, then dynamic
        assert rendered == "S1S2D1D2"

    def test_delete_static_block(self) -> None:
        """delete() should remove static block."""
        blocks = ContextBlocks()
        blocks.set("a", "A")
        blocks.set("b", "B")
        blocks.delete("a")
        rendered = blocks.render()
        assert rendered == "B"

    def test_delete_dynamic_block(self) -> None:
        """delete() should remove dynamic block."""
        blocks = ContextBlocks()
        blocks.set_dynamic("d1", lambda: "D1")
        blocks.set_dynamic("d2", lambda: "D2")
        blocks.delete("d1")
        rendered = blocks.render()
        assert rendered == "D2"

    def test_delete_nonexistent_block(self) -> None:
        """delete() of nonexistent block should not raise."""
        blocks = ContextBlocks()
        blocks.set("a", "A")
        blocks.delete("b")  # Should not raise
        rendered = blocks.render()
        assert rendered == "A"

    def test_convert_static_to_dynamic(self) -> None:
        """set_dynamic() should replace static block."""
        blocks = ContextBlocks()
        blocks.set("key", "static")
        blocks.set_dynamic("key", lambda: "dynamic")
        rendered = blocks.render()
        assert rendered == "dynamic"

    def test_convert_dynamic_to_static(self) -> None:
        """set() should replace dynamic block."""
        blocks = ContextBlocks()
        blocks.set_dynamic("key", lambda: "dynamic")
        blocks.set("key", "static")
        rendered = blocks.render()
        assert rendered == "static"

    def test_insertion_order_preserved_after_overwrites(self) -> None:
        """Insertion order should be stable even with overwrites."""
        blocks = ContextBlocks()
        blocks.set("a", "A")
        blocks.set("b", "B")
        blocks.set("c", "C")

        # Overwrite middle element
        blocks.set("b", "B2")

        rendered = blocks.render()
        assert rendered == "AB2C"

    def test_empty_blocks_render_empty(self) -> None:
        """Rendering empty ContextBlocks should return empty string."""
        blocks = ContextBlocks()
        rendered = blocks.render()
        assert rendered == ""

    def test_dynamic_function_exception_is_contained(self) -> None:
        """A raising dynamic function is contained, not propagated.

        This deliberately replaces an earlier assertion that the exception
        propagates: propagating means one bad block destroys the agent's whole
        context. It is contained and surfaced instead — see
        `test_throwing_dynamic_block_does_not_kill_the_render`.
        """

        def error_func() -> str:
            raise ValueError("Test error")

        blocks = ContextBlocks()
        blocks.set_dynamic("error", error_func)

        rendered = blocks.render()
        assert "'error' unavailable" in rendered
        assert "Test error" in rendered

    def test_complex_dynamic_blocks(self) -> None:
        """Dynamic blocks can be complex callables."""
        state = {"count": 0, "items": []}

        def status() -> str:
            state["count"] += 1
            return f"Status: {state['count']} calls"

        def items_list() -> str:
            state["items"].append("item")
            return f", {len(state['items'])} items"

        blocks = ContextBlocks()
        blocks.set_dynamic("status", status)
        blocks.set_dynamic("items", items_list)

        result1 = blocks.render()
        assert result1 == "Status: 1 calls, 1 items"

        result2 = blocks.render()
        assert result2 == "Status: 2 calls, 2 items"

    def test_render_with_multiline_content(self) -> None:
        """Blocks should handle multiline content."""
        blocks = ContextBlocks()
        blocks.set("intro", "Line 1\nLine 2\n")
        blocks.set("outro", "Line 3\nLine 4")
        rendered = blocks.render()
        assert rendered == "Line 1\nLine 2\nLine 3\nLine 4"

    def test_context_blocks_does_not_share_state(self) -> None:
        """Multiple ContextBlocks instances should be independent."""
        blocks1 = ContextBlocks()
        blocks2 = ContextBlocks()

        blocks1.set("key", "value1")
        blocks2.set("key", "value2")

        assert blocks1.render() == "value1"
        assert blocks2.render() == "value2"

    def test_render_stability(self) -> None:
        """Render should be stable (same output for same state)."""
        blocks = ContextBlocks()
        blocks.set("a", "A")
        blocks.set("b", "B")
        blocks.set("c", "C")

        result1 = blocks.render()
        result2 = blocks.render()
        assert result1 == result2 == "ABC"

    def test_throwing_dynamic_block_does_not_kill_the_render(self) -> None:
        """A dynamic block is caller code; one that raises must not lose the rest.

        Regression: `render()` called `fn()` unguarded, so a single bad block
        took down the agent's ENTIRE context, not just its own section.
        """
        blocks = ContextBlocks()
        blocks.set("before", "BEFORE|")

        def boom() -> str:
            raise RuntimeError("upstream is down")

        blocks.set_dynamic("bad", boom)
        blocks.set_dynamic("good", lambda: "|GOOD")

        rendered = blocks.render()

        # Surviving blocks are still present...
        assert "BEFORE|" in rendered
        assert "|GOOD" in rendered
        # ...and the failure is VISIBLE, not silently swallowed.
        assert "'bad' unavailable" in rendered
        assert "RuntimeError" in rendered
        assert "upstream is down" in rendered

    def test_throwing_dynamic_block_is_logged(self, caplog) -> None:
        blocks = ContextBlocks()
        blocks.set_dynamic("bad", lambda: (_ for _ in ()).throw(ValueError("nope")))

        with caplog.at_level("WARNING", logger="adk.harness"):
            blocks.render()

        assert any("failed to render" in r.getMessage() for r in caplog.records)
