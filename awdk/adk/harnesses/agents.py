"""Sovereign AitherOS agents as harnesses — and group chat across them.

A coding harness (Claude Code) and a sovereign agent (Atlas, Lyra, Aeon, the
orchestrator) are different things wearing the same shape: you send a turn, you
get back text, thinking, tool calls and a completion. Modelling agents as
harnesses means one session list, one event stream and one UI drives both — you
can put Claude Code in tab 1 and Atlas in tab 2 and never notice the seam.

Transport is Genesis's unified SSE endpoint ``POST /chat/stream``, which its own
docstring calls "the ONE endpoint all clients should use". Its event vocabulary
(session_start / thinking / tool_call / tool_result / answer / complete) maps
almost one-to-one onto ours.

Group chat
----------
:class:`GroupSession` fans ONE user turn out to N agents concurrently and tags
every resulting event with the participant that produced it. That is the
"AitherAeon group chat" case: several sovereign agents in one room, each
answering in its own voice, interleaved in a single transcript rather than
serialized through a moderator.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Iterable, Optional

from adk.harnesses.events import (
    EventKind,
    HarnessEvent,
    error,
    notice,
    raw,
    text_delta,
    thinking_delta,
    tool_call,
    tool_result,
)
from adk.harnesses.session import HarnessSession, SessionState

GENESIS_URL = os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001")
STREAM_PATH = "/chat/stream"

#: The sovereign roster. Ids are what Genesis accepts as ``agent``; the labels
#: are what a human sees. Kept here so every front-end shows the same roster.
AGENT_ROSTER: list[dict[str, str]] = [
    {"id": "aither", "label": "Aither", "role": "The platform itself"},
    {"id": "atlas", "label": "Atlas", "role": "Programs, planning, PM"},
    {"id": "lyra", "label": "Lyra", "role": "Research and synthesis"},
    {"id": "aeon", "label": "Aeon", "role": "Long-horizon reasoning"},
    {"id": "demiurge", "label": "Demiurge", "role": "Code architecture"},
    {"id": "hydra", "label": "Hydra", "role": "Code review"},
    {"id": "athena", "label": "Athena", "role": "Security"},
    {"id": "prometheus", "label": "Prometheus", "role": "Infrastructure"},
    {"id": "viviane", "label": "Viviane", "role": "Memory"},
    {"id": "themis", "label": "Themis", "role": "Legal and contracts"},
    {"id": "hera", "label": "Hera", "role": "Operations"},
    {"id": "plutus", "label": "Plutus", "role": "Commerce and billing"},
]

#: Genesis pipeline events that are progress telemetry rather than content.
#: Surfaced as NOTICE so the UI can show a pipeline strip without them being
#: mistaken for assistant text.
_PIPELINE_EVENTS = frozenset(
    {
        "received", "progress", "classify", "classifier_timing", "intent_resolved",
        "answer_segment", "segment_end", "pipeline", "route", "model_selected",
        "promotion", "capacity", "grounding",
    }
)


def translate_genesis(event_name: str, payload: dict[str, Any]) -> list[HarnessEvent]:
    """Map one Genesis SSE event onto normalized events."""
    name = event_name or str(payload.get("type") or "")

    if name == "session_start":
        return [
            HarnessEvent(
                kind=EventKind.SESSION_READY,
                text=f"agent={payload.get('agent', '?')} model={payload.get('model', '?')}",
                data={
                    "model": payload.get("model", ""),
                    "agent": payload.get("agent", ""),
                    "harness_session_id": payload.get("session_id", ""),
                },
            )
        ]
    if name in ("answer", "answer_delta", "text"):
        text = payload.get("text") or payload.get("answer") or payload.get("content") or ""
        return [text_delta(str(text))] if text else []
    if name == "thinking":
        text = payload.get("text") or payload.get("content") or ""
        return [thinking_delta(str(text))] if text else []
    if name == "tool_call":
        return [
            tool_call(
                tool=str(payload.get("tool") or payload.get("name") or ""),
                tool_use_id=str(payload.get("id") or ""),
                tool_input=payload.get("args") if isinstance(payload.get("args"), dict) else {},
            )
        ]
    if name == "tool_result":
        return [
            tool_result(
                tool_use_id=str(payload.get("id") or ""),
                output=payload.get("result"),
                is_error=bool(payload.get("error")),
                tool=str(payload.get("tool") or ""),
            )
        ]
    if name == "complete":
        return [
            HarnessEvent(kind=EventKind.USAGE, data={"usage": payload}),
            HarnessEvent(
                kind=EventKind.TURN_COMPLETED,
                text=str(payload.get("answer") or ""),
                data={"is_error": False},
            ),
        ]
    if name == "error":
        return [
            error(str(payload.get("message") or payload.get("error") or "agent error")),
            HarnessEvent(kind=EventKind.TURN_COMPLETED, data={"is_error": True}),
        ]
    if name == "heartbeat":
        return []
    if name in _PIPELINE_EVENTS:
        return [notice(name, stage=payload.get("stage"), payload=payload)]
    return [raw(json.dumps(payload)[:1500], event_type=name)]


def fetch_workforce(base_url: str = "") -> tuple[list[dict[str, str]], str]:
    """Aitherium Workforce roster from Genesis. Returns ``(agents, reason)``.

    ``reason`` is non-empty when the roster could NOT be fetched. Genesis's own
    ``workforce.py`` notes the runtime is "frequently not" running, so an empty
    list must be distinguishable from a service that did not answer — otherwise
    a down Workforce renders as "you have hired nobody".
    """
    try:
        import httpx
    except ImportError:
        return ([], "httpx is not installed")
    url = f"{(base_url or GENESIS_URL).rstrip('/')}/workforce/agents"
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.get(url)
    except Exception as exc:  # noqa: BLE001 — any transport failure is a reason
        return ([], f"workforce unreachable: {type(exc).__name__}: {exc}")
    if response.status_code >= 400:
        return ([], f"workforce returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        return ([], "workforce returned a non-JSON body")
    agents = payload.get("agents") if isinstance(payload, dict) else None
    if not isinstance(agents, list):
        return ([], "workforce response carried no 'agents' list")
    roster: list[dict[str, str]] = []
    for entry in agents:
        if isinstance(entry, dict):
            agent_id = str(entry.get("id") or entry.get("name") or "")
            if agent_id:
                roster.append({
                    "id": agent_id,
                    "label": str(entry.get("label") or entry.get("title") or agent_id.title()),
                    "role": str(entry.get("role") or entry.get("description") or ""),
                })
        elif isinstance(entry, str):
            roster.append({"id": entry, "label": entry.title(), "role": ""})
    return (roster, "")


def _iter_sse(response: Any) -> Iterable[tuple[str, dict[str, Any]]]:
    """Yield ``(event_name, payload)`` from an httpx streaming response."""
    event_name = ""
    for line in response.iter_lines():
        if line is None:
            continue
        line = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
        line = line.rstrip("\r")
        if not line:
            event_name = ""
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if line.startswith("data:"):
            body = line[5:].strip()
            if not body:
                continue
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {"raw": body}
            if not isinstance(payload, dict):
                payload = {"value": payload}
            yield (event_name, payload)


class AgentRelaySession(HarnessSession):
    """One sovereign agent, driven over Genesis's unified SSE endpoint."""

    def __init__(self, *args: Any, agent: str = "aither", base_url: str = "", **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.agent = agent or "aither"
        self.base_url = (base_url or GENESIS_URL).rstrip("/")

    def start(self) -> None:
        self._emit(
            HarnessEvent(
                kind=EventKind.SESSION_STARTING,
                text=self.agent,
                data={"agent": self.agent, "base_url": self.base_url, "relay": True},
            )
        )
        self.state = SessionState.IDLE
        self._emit(
            HarnessEvent(
                kind=EventKind.SESSION_READY,
                text=f"agent={self.agent}",
                data={"agent": self.agent, "relay": True},
            )
        )

    def send(self, text: str) -> bool:
        with self._lock:
            self.turn += 1
            turn = self.turn
        self._emit(HarnessEvent(kind=EventKind.TURN_STARTED, text=text, turn=turn))
        self.state = SessionState.BUSY
        thread = threading.Thread(
            target=self._run_turn, args=(text, turn), name=f"relay-{self.id}", daemon=True
        )
        thread.start()
        self._threads.append(thread)
        return True

    def _run_turn(self, text: str, turn: int) -> None:
        try:
            import httpx
        except ImportError:
            self._emit(error("httpx is required for agent sessions: pip install httpx"))
            self.state = SessionState.IDLE
            return

        body: dict[str, Any] = {
            "message": text,
            "session_id": self.harness_session_id or self.id,
            "agent": self.agent,
        }
        if self.config.model:
            body["model"] = self.config.model
        url = f"{self.base_url}{STREAM_PATH}"
        try:
            with httpx.Client(timeout=httpx.Timeout(600.0, connect=15.0)) as client:
                with client.stream("POST", url, json=body) as response:
                    if response.status_code >= 400:
                        response.read()
                        self._emit(
                            error(
                                f"agent {self.agent} returned HTTP {response.status_code}",
                                status=response.status_code,
                                body=response.text[:500],
                            )
                        )
                        self._emit(
                            HarnessEvent(
                                kind=EventKind.TURN_COMPLETED,
                                data={"is_error": True},
                                turn=turn,
                            )
                        )
                        return
                    for name, payload in _iter_sse(response):
                        for event in translate_genesis(name, payload):
                            event.turn = turn
                            self._observe(event)
                            self._emit(event)
        except Exception as exc:  # noqa: BLE001 — network/transport of any shape
            # An agent turn that dies must reach the UI. A swallowed exception
            # here is a stream that silently stops, which reads as a hung model.
            self._emit(error(f"agent {self.agent} turn failed: {type(exc).__name__}: {exc}"))
            self._emit(
                HarnessEvent(kind=EventKind.TURN_COMPLETED, data={"is_error": True}, turn=turn)
            )
        finally:
            self.state = SessionState.IDLE

    def stop(self, timeout: float = 8.0) -> None:
        self.state = SessionState.EXITED
        self._emit(HarnessEvent(kind=EventKind.SESSION_EXITED, data={"exit_code": 0}))
        self._closed.set()

    def info(self) -> dict[str, Any]:
        data = super().info()
        data.update({"agent": self.agent, "base_url": self.base_url, "relay": True})
        return data


class GroupSession(HarnessSession):
    """Several sovereign agents in ONE room, answering the same turn.

    Every event carries ``data["participant"]`` so a transcript can be grouped,
    coloured or filtered per agent. Participants run CONCURRENTLY — a group
    chat that serialized agents would take the sum of their latencies and feel
    like a queue rather than a room.
    """

    def __init__(self, *args: Any, participants: Optional[list[str]] = None,
                 base_url: str = "", **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.participants = participants or ["aither"]
        self.base_url = (base_url or GENESIS_URL).rstrip("/")
        self._members: dict[str, AgentRelaySession] = {}

    def start(self) -> None:
        self._emit(
            HarnessEvent(
                kind=EventKind.SESSION_STARTING,
                text=", ".join(self.participants),
                data={"participants": self.participants, "group": True},
            )
        )
        self.state = SessionState.IDLE
        self._emit(
            HarnessEvent(
                kind=EventKind.SESSION_READY,
                text=f"{len(self.participants)} agents",
                data={"participants": self.participants, "group": True},
            )
        )

    def send(self, text: str) -> bool:
        with self._lock:
            self.turn += 1
            turn = self.turn
        self._emit(HarnessEvent(kind=EventKind.TURN_STARTED, text=text, turn=turn))
        self.state = SessionState.BUSY

        pending = {"count": len(self.participants)}
        lock = threading.Lock()

        def run_one(agent: str) -> None:
            relay = AgentRelaySession(
                self.spec, self.config, None, root=self.dir, session_id=f"{self.id}-{agent}",
                agent=agent, base_url=self.base_url,
            )
            # Re-publish the member's events into the GROUP stream, tagged.
            relay._emit = self._make_forwarder(agent, turn)  # type: ignore[method-assign]
            relay._run_turn(text, turn)
            with lock:
                pending["count"] -= 1
                done = pending["count"] == 0
            if done:
                self.state = SessionState.IDLE
                self._emit(
                    HarnessEvent(
                        kind=EventKind.TURN_COMPLETED,
                        data={"is_error": False, "group": True},
                        turn=turn,
                    )
                )

        for agent in self.participants:
            thread = threading.Thread(
                target=run_one, args=(agent,), name=f"group-{self.id}-{agent}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        return True

    def _make_forwarder(self, agent: str, turn: int) -> Any:
        def forward(event: HarnessEvent) -> HarnessEvent:
            # A member's own turn/session lifecycle events would otherwise close
            # the GROUP session; only content and errors are republished.
            if event.kind in (
                EventKind.SESSION_STARTING,
                EventKind.SESSION_READY,
                EventKind.SESSION_EXITED,
                EventKind.TURN_STARTED,
                EventKind.TURN_COMPLETED,
            ):
                return event
            event.data = dict(event.data)
            event.data["participant"] = agent
            event.turn = turn
            return self._emit(event)

        return forward

    def stop(self, timeout: float = 8.0) -> None:
        self.state = SessionState.EXITED
        self._emit(HarnessEvent(kind=EventKind.SESSION_EXITED, data={"exit_code": 0}))
        self._closed.set()

    def info(self) -> dict[str, Any]:
        data = super().info()
        data.update({"participants": self.participants, "group": True})
        return data
