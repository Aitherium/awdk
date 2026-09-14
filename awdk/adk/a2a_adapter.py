"""A2A (Agent-to-Agent) protocol adapter — map A2A tasks to AitherEvents.

Bridges Google A2A v0.3.0 task lifecycle onto AitherEvent for room participation.
Remote A2A agents appear in the room as actor_kind='a2a' with pillars mapped from
task state transitions.

Usage:
    from adk.a2a_adapter import A2AAdapter

    adapter = A2AAdapter(room_id="main", remote_agent_id="foo")

    # On remote task submit:
    events = adapter.on_task_submitted(task_id, "user asks question")

    # On state transition:
    events = adapter.on_task_state_changed(task_id, "working")

    # On completion:
    events = adapter.on_task_completed(task_id, "the answer")
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("adk.a2a_adapter")

#: Delivery counters. A silent drop is the failure this whole subsystem exists to end,
#: so every outcome is countable: `stats()` is how you tell "no A2A traffic" from
#: "every event was thrown away".
_stats = {"sent": 0, "failed": 0, "last_error": None}


def emit(event_type: str, **kwargs: Any) -> bool:
    """POST one event to the harness daemon. Returns True only when it was ACCEPTED.

    This deliberately does NOT import AitherOS's AeonEmit. awdk ships to PyPI and
    must never depend on the monorepo — an `from AitherOS.lib...` import fails on every
    install outside this repo, and on Windows it can even bind to the AitherOS DIRECTORY
    and "succeed" while resolving nothing.

    The previous version fell back to a stub that logged at DEBUG and returned **True**.
    That is the exact defect that made three separate producers report success while
    emitting nothing earlier today: a caller cannot distinguish a delivered event from a
    discarded one, so the room stays empty and nothing anywhere says why. Failures are
    now counted and surfaced, and the return value means what it says.
    """
    import json
    import urllib.error
    import urllib.request

    base = os.environ.get("AITHER_HARNESS_URL", "http://127.0.0.1:8362").rstrip("/")
    token = os.environ.get("AITHER_HARNESS_TOKEN", "") or _token_from_disk()

    # Translate AeonEmit's flat keyword style into the daemon's wire shape. The daemon
    # takes a NESTED `actor: {kind, id, name}`; passing actor_kind/actor_id as
    # top-level fields is a 422 on every call. Callers keep the ergonomic signature.
    payload_env = {
        "type": event_type,
        "actor": {
            "kind": kwargs.pop("actor_kind", "a2a"),
            "id": kwargs.pop("actor_id", "a2a-peer"),
            "name": kwargs.pop("actor_name", "") or kwargs.get("actor_id", "a2a-peer"),
        },
    }
    for key in ("room", "tier", "session", "stage", "payload",
                "correlation_id", "causation_id", "pillar"):
        if key in kwargs and kwargs[key] is not None:
            payload_env[key] = kwargs[key]
    body = json.dumps(payload_env).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(f"{base}/events", data=body,
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            resp.read()
        _stats["sent"] += 1
        _stats["last_error"] = None
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        _stats["failed"] += 1
        _stats["last_error"] = str(exc)
        logger.warning(f"A2A event {event_type} NOT delivered: {exc}")
        return False


def _token_from_disk() -> str:
    """The daemon writes its bearer here on first start."""
    try:
        return (Path.home() / ".aither" / "harness_token").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return ""


def stats() -> dict:
    """Delivery counters, so a quiet A2A lane is diagnosable rather than mysterious."""
    return dict(_stats)

# A2A task states mapped to AitherEvent pillars + flux codes
# Reference: adk/a2a.py TaskState enum + AitherEventSpine FLUX_PILLARS
_STATE_TO_PILLAR = {
    "submitted": "orchestration",  # a2a.s (submit)
    "working": "orchestration",    # a2a.u — FLUX_PILLARS maps a2a.u to
                               # orchestration; "cognition" is NOT one of
                               # the six pillars and the room REFUSES an
                               # unknown pillar with a 400, so every
                               # working-state event was being rejected.
    "input-required": "orchestration",  # waiting for human input
    "completed": "orchestration",  # a2a.d (done — success)
    "failed": "orchestration",     # a2a.d (done — failure)
    "canceled": "orchestration",   # a2a.d (done — canceled)
}

_STATE_TO_FLUX = {
    "submitted": "a2a.s",      # Flux code for A2A submit
    "working": "a2a.u",        # Flux code for A2A update
    "input-required": "a2a.u",  # Still working, waiting for input
    "completed": "a2a.d",      # Flux code for A2A done
    "failed": "a2a.d",         # Done, but failed
    "canceled": "a2a.d",       # Done, canceled
}


@dataclass
class A2ATaskEvent:
    """A single event in an A2A task lifecycle."""
    task_id: str
    state: str  # submitted | working | input-required | completed | failed | canceled
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    error: str | None = None
    metadata: dict[str, Any] | None = None


class A2AAdapter:
    """Map A2A task lifecycle to AitherEvents in a room.

    When a remote A2A agent submits a task, returns a set of events that emit
    the task lifecycle as orchestration + cognition pillar events, making the
    remote agent a visible room participant.
    """

    def __init__(
        self,
        room_id: str = "main",
        remote_agent_id: str = "",
        agent_name: str = "a2a_peer",
    ):
        """Initialize the adapter.

        Args:
            room_id: Room to emit events into (default "main")
            remote_agent_id: Identity of the remote A2A agent
            agent_name: Display name for the remote agent
        """
        self.room_id = room_id
        self.remote_agent_id = remote_agent_id or f"a2a_{uuid.uuid4().hex[:8]}"
        self.agent_name = agent_name

    def on_task_submitted(
        self,
        task_id: str,
        prompt: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Emit events for a newly submitted A2A task.

        Args:
            task_id: Unique task identifier
            prompt: The user message/prompt
            metadata: Optional task metadata (user, deadline, etc.)

        Returns:
            True if emission succeeded (queued), False if dropped
        """
        return emit(
            event_type="a2a_task_submitted",
            actor_kind="a2a",
            actor_id=self.remote_agent_id,
            actor_name=self.agent_name,
            room=self.room_id,
            tier="fleet",
            pillar=_STATE_TO_PILLAR.get("submitted", "orchestration"),
            payload={
                "task_id": task_id,
                "prompt": prompt,
                "flux": _STATE_TO_FLUX.get("submitted"),
                **(metadata or {}),
            },
        )

    def on_task_working(
        self,
        task_id: str,
        intermediate_output: str = "",
    ) -> bool:
        """Emit events for task state change to 'working'.

        Args:
            task_id: Task identifier
            intermediate_output: Any intermediate results/progress

        Returns:
            True if emission succeeded
        """
        return emit(
            event_type="a2a_task_working",
            actor_kind="a2a",
            actor_id=self.remote_agent_id,
            actor_name=self.agent_name,
            room=self.room_id,
            tier="fleet",
            pillar=_STATE_TO_PILLAR.get("working", "orchestration"),
            payload={
                "task_id": task_id,
                "output": intermediate_output,
                "flux": _STATE_TO_FLUX.get("working"),
            },
        )

    def on_task_tool_call(
        self,
        task_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str = "",
    ) -> bool:
        """Emit events for a tool call within an A2A task.

        Args:
            task_id: Task identifier
            tool_name: Name of the tool being called
            tool_args: Tool arguments
            tool_call_id: Optional unique call identifier

        Returns:
            True if emission succeeded
        """
        return emit(
            event_type="a2a_tool_call",
            actor_kind="a2a",
            actor_id=self.remote_agent_id,
            actor_name=self.agent_name,
            room=self.room_id,
            tier="fleet",
            pillar="orchestration",
            payload={
                "task_id": task_id,
                "tool_name": tool_name,
                "tool_args": tool_args,
                "tool_call_id": tool_call_id or str(uuid.uuid4()),
                "flux": "a2a.u",  # Tool calls are cognition updates
            },
        )

    def on_task_completed(
        self,
        task_id: str,
        final_output: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Emit events for successful task completion.

        Args:
            task_id: Task identifier
            final_output: The completed output/response
            metadata: Optional completion metadata (duration, token count, etc.)

        Returns:
            True if emission succeeded
        """
        return emit(
            event_type="a2a_task_completed",
            actor_kind="a2a",
            actor_id=self.remote_agent_id,
            actor_name=self.agent_name,
            room=self.room_id,
            tier="fleet",
            pillar=_STATE_TO_PILLAR.get("completed", "orchestration"),
            payload={
                "task_id": task_id,
                "output": final_output,
                "flux": _STATE_TO_FLUX.get("completed"),
                **(metadata or {}),
            },
        )

    def on_task_failed(
        self,
        task_id: str,
        error_message: str,
        error_code: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Emit events for task failure.

        Args:
            task_id: Task identifier
            error_message: Description of the failure
            error_code: Optional error classification
            metadata: Optional failure metadata

        Returns:
            True if emission succeeded
        """
        return emit(
            event_type="a2a_task_failed",
            actor_kind="a2a",
            actor_id=self.remote_agent_id,
            actor_name=self.agent_name,
            room=self.room_id,
            tier="fleet",
            pillar=_STATE_TO_PILLAR.get("failed", "orchestration"),
            payload={
                "task_id": task_id,
                "error": error_message,
                "error_code": error_code,
                "flux": _STATE_TO_FLUX.get("failed"),
                **(metadata or {}),
            },
        )

    def on_task_state_changed(
        self,
        task_id: str,
        new_state: str,
        content: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Generic task state change handler.

        Routes to the appropriate handler based on the new state.
        Useful for external state machines feeding task updates.

        Args:
            task_id: Task identifier
            new_state: One of: submitted, working, input-required,
                       completed, failed, canceled
            content: Any associated output/message
            metadata: Optional metadata

        Returns:
            True if emission succeeded
        """
        if new_state == "submitted":
            return self.on_task_submitted(task_id, content, metadata)
        elif new_state == "working":
            return self.on_task_working(task_id, content)
        elif new_state == "input-required":
            # Emit a cognition pillar event — still working, awaiting input
            return emit(
                event_type="a2a_task_input_required",
                actor_kind="a2a",
                actor_id=self.remote_agent_id,
                actor_name=self.agent_name,
                room=self.room_id,
                tier="fleet",
                pillar="orchestration",
                payload={
                    "task_id": task_id,
                    "message": content,
                    "flux": _STATE_TO_FLUX.get("input-required"),
                    **(metadata or {}),
                },
            )
        elif new_state == "completed":
            return self.on_task_completed(task_id, content, metadata)
        elif new_state in ("failed", "canceled"):
            return self.on_task_failed(
                task_id,
                content or f"Task {new_state}",
                error_code=new_state,
                metadata=metadata,
            )
        else:
            logger.warning(f"Unknown A2A task state: {new_state}")
            return False

    def emit_direct(self, event_type: str, payload: dict[str, Any]) -> bool:
        """Emit a raw A2A event with full control.

        For advanced use cases where the standard state handlers don't fit.

        Args:
            event_type: Custom AitherEvent type
            payload: Event payload dict

        Returns:
            True if emission succeeded
        """
        return emit(
            event_type=event_type,
            actor_kind="a2a",
            actor_id=self.remote_agent_id,
            actor_name=self.agent_name,
            room=self.room_id,
            tier="fleet",
            payload=payload,
        )
