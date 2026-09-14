"""Model-callable harness APIs for event logging and context management.

EventLog provides append-only typed event storage with automatic integer tags,
query/collapse semantics, and context-window-aware compaction.

ContextBlocks manages named content blocks (both static and dynamic) with stable
rendering order optimized for KV-cache.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class _Event:
    """Internal event representation with auto-assigned tag."""

    tag: int
    type: str
    payload: Any


@dataclass
class _CollapseMarker:
    """Marker for a collapsed range of events."""

    tag: int
    range_start: int
    range_end: int
    summary: str
    children_tags: list[int] = field(default_factory=list)


class EventLog:
    """Append-only typed event storage with integer tags and range operations.

    Events are stored with auto-incrementing integer tags. Supports querying by type,
    collapsing ranges into summaries, and accessing individual events by tag.

    Example:
        log = EventLog()
        tag1 = log.append("user_message", "Hello")
        tag2 = log.append("tool_call", {"name": "search", "query": "python"})
        errors = log.query(type="error", limit=10)
        tag_summary = log.collapse("1", "5", "User gave instructions")
    """

    def __init__(self) -> None:
        self._events: dict[int, _Event | _CollapseMarker] = {}
        self._next_tag = 0
        self._type_index: dict[str, list[int]] = {}  # type -> [tag, ...]

    def append(self, event_type: str, payload: Any) -> int:
        """Append a typed event and return its tag.

        Args:
            event_type: String type label (e.g., "user_message", "tool_call")
            payload: Event payload (any Python object)

        Returns:
            Integer tag assigned to this event
        """
        tag = self._next_tag
        self._next_tag += 1

        event = _Event(tag=tag, type=event_type, payload=payload)
        self._events[tag] = event

        # Index by type for fast filtering
        if event_type not in self._type_index:
            self._type_index[event_type] = []
        self._type_index[event_type].append(tag)

        return tag

    def query(
        self,
        type: Optional[str] = None,
        limit: Optional[int] = None,
        since: Optional[int] = None,
    ) -> list[tuple[int, str, Any]]:
        """Query events by type and optional limit.

        Results are returned in chronological order. If limit is specified,
        the most recent events are returned (up to limit).

        Args:
            type: Filter by event type (None = all types)
            limit: Maximum events to return (None = all)
            since: Return events with tag > since (None = all)

        Returns:
            List of (tag, type, payload) tuples
        """
        # Get candidate tags
        if type is not None:
            if type not in self._type_index:
                return []
            candidate_tags = self._type_index[type]
        else:
            candidate_tags = sorted(self._events.keys())

        # Filter by since
        if since is not None:
            candidate_tags = [t for t in candidate_tags if t > since]

        # Apply limit (most recent)
        if limit is not None:
            candidate_tags = candidate_tags[-limit:]

        # Build result
        result = []
        for tag in candidate_tags:
            item = self._events[tag]
            if isinstance(item, _Event):
                result.append((tag, item.type, item.payload))
            elif isinstance(item, _CollapseMarker):
                # For collapsed ranges, return the summary
                result.append((tag, "_collapse", {"summary": item.summary}))

        return result

    def get(self, tag: int | list[int]) -> tuple[int, str, Any] | list[tuple[int, str, Any]] | None:
        """Get a single event by tag, or multiple events by list of tags.

        Args:
            tag: Event tag to retrieve, or list of tags

        Returns:
            (tag, type, payload) tuple for single tag, or list of tuples for list of tags.
            For single tag: None if not found.
            For list of tags: only found events are returned.
        """
        if isinstance(tag, list):
            result = []
            for t in tag:
                if t in self._events:
                    item = self._events[t]
                    if isinstance(item, _Event):
                        result.append((t, item.type, item.payload))
                    elif isinstance(item, _CollapseMarker):
                        result.append((t, "_collapse", {"summary": item.summary}))
            return result

        if tag not in self._events:
            return None
        item = self._events[tag]
        if isinstance(item, _Event):
            return (tag, item.type, item.payload)
        elif isinstance(item, _CollapseMarker):
            return (tag, "_collapse", {"summary": item.summary})
        return None

    def collapse(
        self,
        start_tag: int,
        end_tag: int,
        summary: str,
    ) -> int:
        """Collapse a range of events into a single summary marker.

        Replaces events from start_tag to end_tag (inclusive) with one marker.
        The marker's tag is placed where start_tag was for query ordering.
        Original events are preserved internally but no longer appear in
        standard queries.

        Args:
            start_tag: First tag in range
            end_tag: Last tag in range (inclusive)
            summary: Summary text to store

        Returns:
            Tag of the collapse marker
        """
        # Collect children tags (events being collapsed)
        children_tags = []
        for tag in range(start_tag, end_tag + 1):
            if tag in self._events:
                children_tags.append(tag)

        # Use start_tag as the marker's tag (for query ordering)
        marker_tag = start_tag

        marker = _CollapseMarker(
            tag=marker_tag,
            range_start=start_tag,
            range_end=end_tag,
            summary=summary,
            children_tags=children_tags,
        )

        # Remove original events and add marker
        for tag in children_tags:
            item = self._events[tag]
            if isinstance(item, _Event):
                # Remove from type index
                if item.type in self._type_index:
                    self._type_index[item.type] = [
                        t for t in self._type_index[item.type] if t != tag
                    ]
            del self._events[tag]

        # Add the marker at start_tag position
        self._events[marker_tag] = marker

        return marker_tag

    def keys(self) -> list[int]:
        """Get all event tags (including collapsed ranges).

        Returns:
            Sorted list of tags for all events and collapse markers
        """
        return sorted(self._events.keys())


class ContextBlocks:
    """Named static and dynamic context blocks with stable rendering order.

    Maintains a collection of context blocks, each with a name and value.
    Dynamic blocks hold callables that are re-evaluated at render time.

    Rendering order: static blocks in insertion order, followed by dynamic
    blocks. This ordering is KV-cache-friendly for language models.

    Example:
        blocks = ContextBlocks()
        blocks.set("system", "You are a helpful assistant.")
        blocks.set_dynamic("status", lambda: f"Progress: {self.count}%")
        rendered = blocks.render()
        blocks.delete("status")
    """

    def __init__(self) -> None:
        self._static_blocks: dict[str, str] = {}  # name -> value
        self._dynamic_blocks: dict[str, Callable[[], str]] = {}  # name -> callable
        self._insertion_order: list[str] = []  # track order for stable rendering

    def set(self, name: str, value: str) -> None:
        """Set a static block value.

        Args:
            name: Block name
            value: Block content (string)
        """
        # If this is a new block, track insertion order
        if name not in self._static_blocks and name not in self._dynamic_blocks:
            self._insertion_order.append(name)

        # Remove from dynamic if it was there
        if name in self._dynamic_blocks:
            del self._dynamic_blocks[name]

        self._static_blocks[name] = value

    def set_dynamic(self, name: str, fn: Callable[[], str]) -> None:
        """Set a dynamic block that re-evaluates at render time.

        Args:
            name: Block name
            fn: Callable that returns block content (called at each render)
        """
        # If this is a new block, track insertion order
        if name not in self._static_blocks and name not in self._dynamic_blocks:
            self._insertion_order.append(name)

        # Remove from static if it was there
        if name in self._static_blocks:
            del self._static_blocks[name]

        self._dynamic_blocks[name] = fn

    def delete(self, name: str) -> None:
        """Delete a block by name.

        Args:
            name: Block name to delete
        """
        if name in self._static_blocks:
            del self._static_blocks[name]
        if name in self._dynamic_blocks:
            del self._dynamic_blocks[name]
        if name in self._insertion_order:
            self._insertion_order.remove(name)

    def render(self) -> str:
        """Render all blocks into a single string.

        Static blocks are rendered in insertion order, followed by
        dynamic blocks (also in insertion order). Dynamic blocks are
        re-evaluated on each render call.

        Returns:
            Concatenated block content
        """
        parts = []

        # Render static blocks in insertion order
        for name in self._insertion_order:
            if name in self._static_blocks:
                parts.append(self._static_blocks[name])

        # Render dynamic blocks in insertion order. A dynamic block is
        # caller-supplied code; one that raises must not take the whole
        # context down with it (the agent would lose every other block).
        # Substitute a VISIBLE marker rather than swallowing it silently —
        # the model can then see that a block is missing and react.
        for name in self._insertion_order:
            if name in self._dynamic_blocks:
                fn = self._dynamic_blocks[name]
                try:
                    parts.append(fn())
                except Exception as exc:  # noqa: BLE001 — caller-supplied callable
                    logger.warning("dynamic context block %r failed to render: %s", name, exc)
                    parts.append(f"<block {name!r} unavailable: {type(exc).__name__}: {exc}>")

        return "".join(parts)
