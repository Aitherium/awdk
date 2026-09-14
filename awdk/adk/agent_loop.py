"""Agent loop — a supervised host process that emits AitherEvents to the spine.

This module implements the core agent loop lifecycle:
- Registers as a room participant with session_start AitherEvent
- Draws context from the ContextWell each cycle (GET /well)
- Emits pillar-stamped AitherEvents for its work
- Writes liveness to ~/.aither/adk-up.json

The loop is stoppable via a signal handler and does not orphan processes.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

# Import from AitherOS library if available; otherwise use fallback
try:
    aitheros_path = (Path(__file__).parents[2] / "AitherOS").resolve()
    if str(aitheros_path) not in sys.path:
        sys.path.insert(0, str(aitheros_path))
    from lib.core.AitherEventSpine import (
        Actor,
        ActorKind,
        AitherEvent,
        Pillar,
        Tier,
    )
except ImportError:
    # Fallback: define minimal stubs
    class ActorKind(str, Enum):
        ADK_AGENT = "adk_agent"

    class Pillar(str, Enum):
        INTENT = "intent"
        CONTEXT = "context"
        REASONING = "reasoning"
        ORCHESTRATION = "orchestration"
        LEARNING = "learning"
        AUTOMATION = "automation"

    class Tier(str, Enum):
        HOST = "host"
        FLEET = "fleet"

    @dataclass
    class Actor:
        kind: ActorKind
        id: str
        name: str = ""

        def to_dict(self) -> Dict[str, Any]:
            return {"kind": self.kind.value, "id": self.id, "name": self.name or self.id}

    @dataclass
    class AitherEvent:
        type: str
        actor: Actor
        pillar: Optional[Pillar] = None
        tier: Tier = Tier.HOST
        room: str = "main"
        session: str = ""
        payload: Dict[str, Any] = field(default_factory=dict)

        def to_dict(self) -> Dict[str, Any]:
            return {
                "type": self.type,
                "actor": self.actor.to_dict(),
                "pillar": self.pillar.value if self.pillar else None,
                "tier": self.tier.value,
                "room": self.room,
                "session": self.session,
                "payload": self.payload,
            }


AITHER_HOME = Path.home() / ".aither"
STATUS_PATH = AITHER_HOME / "adk-up.json"


def _ensure_dirs() -> None:
    AITHER_HOME.mkdir(parents=True, exist_ok=True)


def _read_agent_status(agent_name: str) -> Optional[dict]:
    """Read the status dict for a specific agent."""
    if not STATUS_PATH.exists():
        return None
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        agents = data.get("agents", {})
        return agents.get(agent_name)
    except (json.JSONDecodeError, OSError):
        return None


def _write_agent_status(agent_name: str, status: dict) -> None:
    """Write or update an agent's entry in the status file."""
    _ensure_dirs()
    try:
        if STATUS_PATH.exists():
            data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        else:
            data = {"agents": {}}

        if "agents" not in data:
            data["agents"] = {}

        data["agents"][agent_name] = status
        STATUS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"[{agent_name}] failed to write status: {e}", file=sys.stderr)


def _clear_agent_status(agent_name: str) -> None:
    """Remove an agent's entry from the status file."""
    _ensure_dirs()
    try:
        if STATUS_PATH.exists():
            data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
            if "agents" in data and agent_name in data["agents"]:
                del data["agents"][agent_name]
            STATUS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"[{agent_name}] failed to clear status: {e}", file=sys.stderr)


class AgentLoop:
    """A supervised host-tier agent loop that emits onto the spine."""

    def __init__(
        self,
        name: str,
        daemon_url: str = "http://127.0.0.1:8362",
        daemon_token: str = "",
        room: str = "main",
    ):
        """Initialize the agent loop.

        Args:
            name: Agent name (used as actor_id)
            daemon_url: URL of the harness daemon
            daemon_token: Bearer token for the daemon (reads from env if empty)
            room: Room name to emit into (default: "main")
        """
        self.name = name
        self.daemon_url = daemon_url.rstrip("/")
        self.daemon_token = daemon_token or os.environ.get("AITHER_HARNESS_TOKEN", "")
        self.room = room
        self.actor_id = f"agent-{name}"
        self.session_id = str(uuid.uuid4())
        self.should_stop = False
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the async HTTP client."""
        if self._client is None:
            headers = {}
            if self.daemon_token:
                headers["Authorization"] = f"Bearer {self.daemon_token}"
            self._client = httpx.AsyncClient(
                base_url=self.daemon_url,
                headers=headers,
                timeout=30.0,
            )
        return self._client

    async def _emit_event(
        self,
        event_type: str,
        pillar: Optional[Pillar] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Emit an AitherEvent to the daemon.

        Returns True on success, False on failure.
        """
        try:
            client = await self._get_client()
            actor = Actor(kind=ActorKind.ADK_AGENT, id=self.actor_id, name=self.name)

            body = {
                "type": event_type,
                "actor": actor.to_dict(),
                "room": self.room,
                "tier": Tier.HOST.value,
                "session": self.session_id,
                "payload": payload or {},
            }
            if pillar:
                body["pillar"] = pillar.value

            response = await client.post("/events", json=body)
            return response.status_code in (200, 201)
        except Exception as exc:
            print(f"[{self.name}] emit_event failed: {exc}", file=sys.stderr)
            return False

    async def _draw_well(self) -> Optional[dict[str, Any]]:
        """Draw context from the well.

        Returns the well state dict, or None on failure.
        """
        try:
            client = await self._get_client()
            response = await client.get(
                "/well",
                params={"cwd": str(Path.cwd()), "actor": self.actor_id},
            )
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as exc:
            print(f"[{self.name}] draw_well failed: {exc}", file=sys.stderr)
            return None

    async def _fetch_room_events(self, since: int = 0, limit: int = 10) -> Optional[list]:
        """Fetch recent events from the room."""
        try:
            client = await self._get_client()
            response = await client.get(
                f"/rooms/{self.room}/events",
                params={"since": since, "limit": limit},
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("events", [])
            return None
        except Exception:
            return None

    async def _loop_iteration(self) -> None:
        """One iteration of the agent loop."""
        # Draw context
        well = await self._draw_well()

        # Emit a tick event
        await self._emit_event(
            "agent_tick",
            pillar=Pillar.ORCHESTRATION,
            payload={
                "agent_name": self.name,
                "session_id": self.session_id,
                "well_ready": well is not None,
                "tier": "host",
            }
        )

        # Check if there are any new events in the room
        events = await self._fetch_room_events(limit=5)
        if events:
            await self._emit_event(
                "agent_observe",
                pillar=Pillar.CONTEXT,
                payload={
                    "event_count": len(events),
                    "latest_types": [e.get("type") for e in events[:3]],
                }
            )

    async def run(self, interval: float = 5.0) -> None:
        """Run the agent loop.

        Args:
            interval: Seconds between loop iterations
        """
        print(f"[{self.name}] Starting agent loop (actor_id={self.actor_id})")

        _ensure_dirs()
        pid = os.getpid()
        _write_agent_status(self.name, {
            "pid": pid,
            "name": self.name,
            "actor_id": self.actor_id,
            "session_id": self.session_id,
            "started_at": time.time(),
            "room": self.room,
        })

        # Emit session_start to register with the room
        await self._emit_event(
            "session_start",
            pillar=Pillar.ORCHESTRATION,
            payload={
                "agent_name": self.name,
                "session_id": self.session_id,
                "actor_id": self.actor_id,
                "tier": "host",
            }
        )

        print(f"[{self.name}] Registered with room '{self.room}'")
        print(f"[{self.name}] Status file: {STATUS_PATH}")

        iteration = 0
        try:
            while not self.should_stop:
                try:
                    await self._loop_iteration()
                    iteration += 1
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    print(f"[{self.name}] Loop iteration {iteration} failed: {exc}",
                          file=sys.stderr)
                    await asyncio.sleep(interval)
        finally:
            # Emit session_end to deregister
            await self._emit_event(
                "session_end",
                pillar=Pillar.ORCHESTRATION,
                payload={
                    "agent_name": self.name,
                    "session_id": self.session_id,
                    "iterations": iteration,
                    "tier": "host",
                }
            )

            # Cleanup
            if self._client:
                await self._client.aclose()

            _clear_agent_status(self.name)
            print(f"[{self.name}] Stopped after {iteration} iterations")


async def async_main(
    agent_name: str,
    daemon_url: str = "http://127.0.0.1:8362",
    daemon_token: str = "",
    room: str = "main",
    interval: float = 5.0,
) -> int:
    """Run an agent loop (async entry point).

    Returns 0 on clean exit, non-zero on error.
    """
    loop = AgentLoop(
        name=agent_name,
        daemon_url=daemon_url,
        daemon_token=daemon_token,
        room=room,
    )

    # Install signal handler for graceful shutdown
    def signal_handler(sig: int, frame: Any) -> None:
        print(f"\n[{agent_name}] Received signal {sig}, shutting down gracefully...")
        loop.should_stop = True

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        await loop.run(interval=interval)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"[{agent_name}] Fatal error: {exc}", file=sys.stderr)
        return 1


def main(
    agent_name: str,
    daemon_url: str = "http://127.0.0.1:8362",
    daemon_token: str = "",
    room: str = "main",
    interval: float = 5.0,
) -> int:
    """Run an agent loop (sync entry point).

    Returns 0 on clean exit, non-zero on error.
    """
    return asyncio.run(async_main(
        agent_name=agent_name,
        daemon_url=daemon_url,
        daemon_token=daemon_token,
        room=room,
        interval=interval,
    ))
