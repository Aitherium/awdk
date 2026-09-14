"""A2A (Agent-to-Agent) protocol server — Google A2A v0.3.0 compatible.

Exposes any AitherAgent as a compliant A2A service with:
- Agent Card discovery at ``/.well-known/agent.json``
- Task lifecycle (submitted → working → completed/failed)
- Message send/receive via JSON-RPC 2.0
- SSE streaming for long-running tasks
- Skill auto-detection from @tool decorated functions

Every ``aither-serve`` node becomes an A2A-compatible agent that can
interoperate with any other A2A agent (Google, LangGraph, CrewAI, etc.).

Usage::

    from adk.a2a import A2AServer
    a2a = A2AServer(agent=my_agent, base_url="http://localhost:8080")
    a2a.mount(app)  # adds /.well-known/agent.json + /a2a (JSON-RPC)
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import AsyncIterator

# `from __future__ import annotations` stringizes every annotation, and FastAPI
# resolves an endpoint's annotations via get_type_hints() against THIS module's
# globals. The /a2a endpoint is annotated `request: Request`, so `Request` must
# be resolvable here — otherwise FastAPI mis-reads it as a query param and every
# POST /a2a 422s. Import it at module scope (guarded: fastapi stays optional for
# adk core; mount() cannot run without it anyway).
try:
    from fastapi import Request
except ImportError:  # pragma: no cover - fastapi is optional for non-server use
    Request = None  # type: ignore[assignment,misc]

logger = logging.getLogger("adk.a2a")

_PROTOCOL_VERSION = "0.3.0"


# ─────────────────────────────────────────────────────────────────────────────
# Data models (Google A2A spec)
# ─────────────────────────────────────────────────────────────────────────────

class TaskState(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass
class TextPart:
    text: str
    type: str = "text"

    def to_dict(self) -> dict:
        return {"type": self.type, "text": self.text}


@dataclass
class DataPart:
    data: dict
    type: str = "data"

    def to_dict(self) -> dict:
        return {"type": self.type, "data": self.data}


@dataclass
class A2AMessage:
    role: str  # "user" or "agent"
    parts: list[dict] = field(default_factory=list)
    messageId: str = ""
    taskId: str = ""

    def to_dict(self) -> dict:
        d: dict = {"role": self.role, "parts": self.parts}
        if self.messageId:
            d["messageId"] = self.messageId
        if self.taskId:
            d["taskId"] = self.taskId
        return d


@dataclass
class Artifact:
    artifactId: str = ""
    parts: list[dict] = field(default_factory=list)
    name: str = ""

    def to_dict(self) -> dict:
        d: dict = {"parts": self.parts}
        if self.artifactId:
            d["artifactId"] = self.artifactId
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class TaskStatus:
    state: TaskState = TaskState.SUBMITTED
    message: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "message": self.message,
            "timestamp": self.timestamp or _now(),
        }


@dataclass
class Task:
    id: str = ""
    contextId: str = ""
    status: TaskStatus = field(default_factory=TaskStatus)
    history: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "contextId": self.contextId,
            "status": self.status.to_dict(),
            "history": self.history,
            "artifacts": self.artifacts,
            "metadata": self.metadata,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Task Manager — Durable (FileStore-backed JSONL)
# ─────────────────────────────────────────────────────────────────────────────

class TaskManager:
    """Persistent task lifecycle manager backed by JSONL file.

    Tasks are stored in an append-only JSONL file (same format as FileStore)
    so they survive container restarts. In-memory dict is used for fast access.
    """

    def __init__(self, store_path: str | Path = ".adk/tasks.jsonl"):
        """Initialize TaskManager with optional persistent file backing.

        Args:
            store_path: Path to JSONL file. If relative, resolved from current directory.
                       Parent directory is created if missing.
        """
        self._store_path = Path(store_path)
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, Task] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        # Load existing tasks from file
        self._load_from_store()

    def _load_from_store(self):
        """Load tasks from JSONL file on startup."""
        if not self._store_path.exists():
            return
        try:
            with self._store_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        task = self._deserialize_task(obj)
                        self._tasks[task.id] = task
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning(f"Skipping malformed task record: {e}")
        except Exception as e:
            logger.error(f"Failed to load tasks from {self._store_path}: {e}")

    def _save_to_store(self, task: Task):
        """Append a task to the JSONL file."""
        try:
            with self._store_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(self._serialize_task(task)) + "\n")
        except Exception as e:
            logger.error(f"Failed to save task {task.id} to store: {e}")

    @staticmethod
    def _serialize_task(task: Task) -> dict:
        """Convert Task to JSON-serializable dict."""
        return {
            "id": task.id,
            "contextId": task.contextId,
            "status": {
                "state": task.status.state.value,
                "message": task.status.message,
                "timestamp": task.status.timestamp,
            },
            "history": task.history,
            "artifacts": task.artifacts,
            "metadata": task.metadata,
        }

    @staticmethod
    def _deserialize_task(obj: dict) -> Task:
        """Reconstruct Task from JSON dict."""
        status = TaskStatus(
            state=TaskState(obj["status"]["state"]),
            message=obj["status"].get("message", ""),
            timestamp=obj["status"].get("timestamp", _now()),
        )
        return Task(
            id=obj["id"],
            contextId=obj["contextId"],
            status=status,
            history=obj.get("history", []),
            artifacts=obj.get("artifacts", []),
            metadata=obj.get("metadata", {}),
        )

    def create_task(self, context_id: str = "", metadata: dict | None = None) -> Task:
        task = Task(
            id=str(uuid.uuid4()),
            contextId=context_id or str(uuid.uuid4()),
            status=TaskStatus(state=TaskState.SUBMITTED, timestamp=_now()),
            metadata=metadata or {},
        )
        self._tasks[task.id] = task
        self._save_to_store(task)
        return task

    def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def update_status(self, task_id: str, state: TaskState, message: str = ""):
        task = self._tasks.get(task_id)
        if not task:
            return
        task.status = TaskStatus(state=state, message=message, timestamp=_now())
        # Persist updated task
        self._save_to_store(task)
        self._notify(task_id, {"type": "status", "task": task.to_dict()})

    def add_artifact(self, task_id: str, artifact: Artifact):
        task = self._tasks.get(task_id)
        if not task:
            return
        task.artifacts.append(artifact.to_dict())
        # Persist updated task
        self._save_to_store(task)
        self._notify(task_id, {"type": "artifact", "artifact": artifact.to_dict()})

    def add_message(self, task_id: str, message: A2AMessage):
        task = self._tasks.get(task_id)
        if not task:
            return
        task.history.append(message.to_dict())
        # Persist updated task
        self._save_to_store(task)

    def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        if task.status.state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED):
            return False
        self.update_status(task_id, TaskState.CANCELED)
        return True

    def subscribe(self, task_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(task_id, []).append(q)
        return q

    def _notify(self, task_id: str, event: dict):
        for q in self._subscribers.get(task_id, []):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# A2A Server
# ─────────────────────────────────────────────────────────────────────────────

class A2AServer:
    """Google A2A v0.3.0 protocol server wrapping an AitherAgent.

    Handles:
      - /.well-known/agent.json — Agent Card discovery
      - POST /a2a — JSON-RPC 2.0 (message/send, tasks/get, tasks/cancel)
      - GET /a2a/tasks/{id}/subscribe — SSE streaming
    """

    def __init__(
        self,
        agent=None,
        base_url: str = "http://localhost:8080",
        server_name: str = "",
        task_store_path: str | Path = ".adk/tasks.jsonl",
    ):
        self._agent = agent
        self._base_url = base_url.rstrip("/")
        self._server_name = server_name
        self._task_store_path = Path(task_store_path)
        self._tasks = TaskManager(store_path=self._task_store_path)
        self._agent_card: dict | None = None

    @property
    def agent(self):
        return self._agent

    @agent.setter
    def agent(self, value):
        self._agent = value
        self._agent_card = None  # Rebuild on next request

    def build_agent_card(self) -> dict:
        """Build the A2A agent card from the agent's identity and tools."""
        if self._agent_card:
            return self._agent_card

        # Try identity-based card first
        if self._agent and hasattr(self._agent, "_identity"):
            card = self._agent._identity.to_a2a_card(base_url=self._base_url)
        else:
            name = self._server_name or (self._agent.name if self._agent else "adk-agent")
            card = {
                "name": name,
                "description": f"AitherADK agent: {name}",
                "url": self._base_url,
                "version": _get_version(),
                "provider": {"organization": "Aitherium", "url": "https://aitherium.com"},
                "capabilities": {"streaming": True, "pushNotifications": False,
                                 "stateTransitionHistory": True},
                "authentication": {"schemes": ["bearer"]},
                "defaultInputModes": ["text/plain"],
                "defaultOutputModes": ["text/plain"],
                "skills": [],
            }

        # Enrich with tool-derived skills
        if self._agent and hasattr(self._agent, "_tools"):
            tool_skills = []
            for td in self._agent._tools.list_tools():
                tool_skills.append({
                    "id": td.name,
                    "name": td.name,
                    "description": td.description,
                    "tags": ["tool"],
                    "inputModes": ["application/json"],
                    "outputModes": ["text/plain"],
                })
            # Merge: keep identity skills, add tool skills
            existing_ids = {s.get("id") for s in card.get("skills", [])}
            for ts in tool_skills:
                if ts["id"] not in existing_ids:
                    card.setdefault("skills", []).append(ts)

        # Add public_key for signed A2A trust verification
        if self._agent and hasattr(self._agent, "_a2a_public_key"):
            card["public_key"] = self._agent._a2a_public_key

        # Ensure streaming capability
        card.setdefault("capabilities", {})["streaming"] = True
        card.setdefault("capabilities", {})["stateTransitionHistory"] = True

        # Protocol version
        card["protocolVersion"] = _PROTOCOL_VERSION

        # Interfaces
        card["interfaces"] = [
            {"url": f"{self._base_url}/a2a", "transport": "JSONRPC"},
            {"url": f"{self._base_url}/mcp", "transport": "JSONRPC"},
        ]
        # Preferred transport (A2A AgentCard field) — the /a2a JSON-RPC endpoint is
        # the primary interface, so advertise it. Completes AgentCard field coverage
        # for strict spec consumers that read preferredTransport.
        card.setdefault("preferredTransport", "JSONRPC")

        self._agent_card = card
        return card

    # ── JSON-RPC handler ──────────────────────────────────────────────────

    async def handle_jsonrpc(
        self,
        body: dict,
        caller_public_key: str | None = None,
        caller_trusted: bool = False,
    ) -> dict:
        """Handle a JSON-RPC 2.0 A2A request.

        caller_public_key / caller_trusted carry the verified A2A identity from the
        mount() gate. They are only consulted by security-bearing methods
        (skills/invoke); benign methods ignore them.
        """
        method = body.get("method", "")
        params = body.get("params", {})
        req_id = body.get("id")

        if method == "message/send":
            return await self._handle_message_send(req_id, params)
        elif method == "tasks/get":
            return self._handle_tasks_get(req_id, params)
        elif method == "tasks/cancel":
            return self._handle_tasks_cancel(req_id, params)
        elif method == "skills/invoke":
            return await self._handle_skills_invoke(
                req_id, params, caller_public_key, caller_trusted)
        else:
            return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")

    async def _handle_message_send(self, req_id, params: dict) -> dict:
        """Handle message/send — create or continue a task."""
        message = params.get("message", {})
        task_id = message.get("taskId", "")
        context_id = message.get("contextId", params.get("contextId", ""))

        # Extract text from message parts
        text = ""
        for part in message.get("parts", []):
            if isinstance(part, str):
                text += part
            elif isinstance(part, dict) and part.get("type") == "text":
                text += part.get("text", "")

        if not text:
            return _jsonrpc_error(req_id, -32602, "No text content in message")

        # Create or get task
        if task_id:
            task = self._tasks.get_task(task_id)
            if not task:
                return _jsonrpc_error(req_id, -32602, f"Task not found: {task_id}")
        else:
            task = self._tasks.create_task(context_id=context_id)

        # Record user message
        user_msg = A2AMessage(
            role="user",
            parts=message.get("parts", [{"type": "text", "text": text}]),
            messageId=str(uuid.uuid4()),
            taskId=task.id,
        )
        self._tasks.add_message(task.id, user_msg)
        self._tasks.update_status(task.id, TaskState.WORKING)

        # Execute agent chat
        try:
            if not self._agent:
                raise RuntimeError("No agent configured")

            # Build history from task context
            history = []
            for hist_msg in task.history[:-1]:  # Exclude the message we just added
                role = hist_msg.get("role", "user")
                parts = hist_msg.get("parts", [])
                content = " ".join(p.get("text", "") for p in parts if p.get("type") == "text")
                if content:
                    history.append({
                        "role": "user" if role == "user" else "assistant",
                        "content": content,
                    })

            resp = await self._agent.chat(text, history=history or None)

            # Record agent response
            agent_msg = A2AMessage(
                role="agent",
                parts=[{"type": "text", "text": resp.content}],
                messageId=str(uuid.uuid4()),
                taskId=task.id,
            )
            self._tasks.add_message(task.id, agent_msg)

            # Add artifacts if any
            if resp.artifacts:
                for art_data in resp.artifacts:
                    art = Artifact(
                        artifactId=art_data.get("id", str(uuid.uuid4())),
                        parts=[{"type": "data", "data": art_data}],
                        name=art_data.get("type", "artifact"),
                    )
                    self._tasks.add_artifact(task.id, art)

            self._tasks.update_status(task.id, TaskState.COMPLETED)

            return _jsonrpc_success(req_id, {
                "task": task.to_dict(),
                "message": agent_msg.to_dict(),
            })

        except Exception as exc:
            logger.error("A2A message/send failed: %s", exc)
            self._tasks.update_status(task.id, TaskState.FAILED, message=str(exc))
            return _jsonrpc_success(req_id, {"task": task.to_dict()})

    def _handle_tasks_get(self, req_id, params: dict) -> dict:
        task_id = params.get("id", params.get("taskId", ""))
        task = self._tasks.get_task(task_id)
        if not task:
            return _jsonrpc_error(req_id, -32602, f"Task not found: {task_id}")
        return _jsonrpc_success(req_id, {"task": task.to_dict()})

    def _handle_tasks_cancel(self, req_id, params: dict) -> dict:
        task_id = params.get("id", params.get("taskId", ""))
        ok = self._tasks.cancel_task(task_id)
        if not ok:
            return _jsonrpc_error(req_id, -32602, f"Cannot cancel task: {task_id}")
        task = self._tasks.get_task(task_id)
        return _jsonrpc_success(req_id, {"task": task.to_dict() if task else {}})

    async def _handle_skills_invoke(
        self,
        req_id,
        params: dict,
        caller_public_key: str | None = None,
        caller_trusted: bool = False,
    ) -> dict:
        """Handle skills/invoke — call a tool on this agent and return the result.

        This is the ONE method that lets a remote peer run local code, so it is
        fail-closed on three independent controls (defense in depth):

          1. TRUST — the caller must present a cryptographically-verified,
             *trusted* Ed25519 key. mount() enforces this for skills/invoke
             regardless of the global AITHER_A2A_REQUIRE_TRUST mode; we re-check
             `caller_trusted` here so the handler can never run for an untrusted
             caller even if wired differently in future.
          2. ALLOWLIST — the tool must be explicitly opted in via
             ToolDef.expose_to_a2a (default False). A tool being registered
             locally does NOT make it remotely reachable.
          3. AUTHORIZATION — the tool executes under a constrained AuthContext
             (verified peer, clearance 0, no action classes), so any tool marked
             with required_clearance>0 or an action_class (write/delete/admin) is
             denied by ToolRegistry.execute() even when exposed.
        """
        # Control 1: trust (defense-in-depth; mount() already gated this).
        if not caller_trusted:
            logger.warning("A2A skills/invoke: rejecting untrusted caller")
            return _jsonrpc_error(req_id, -32600, "Untrusted caller — skills/invoke denied")

        skill_name = params.get("skill", "").strip()
        args = params.get("args", {})

        if not skill_name:
            return _jsonrpc_error(req_id, -32602, "skill name is required")

        if not isinstance(args, dict):
            return _jsonrpc_error(req_id, -32602, "args must be a dict")

        # Check if agent exists and has tools
        if not self._agent:
            logger.error("A2A skills/invoke: no agent configured")
            return _jsonrpc_error(req_id, -32603, "Agent not configured")

        if not hasattr(self._agent, "_tools"):
            return _jsonrpc_error(req_id, -32602, "Agent does not support tools")

        # Look up the tool in the agent's registry (strict dict lookup, never getattr)
        tool_def = self._agent._tools.get(skill_name)
        if tool_def is None:
            logger.warning(f"A2A skills/invoke: tool not found: {skill_name}")
            return _jsonrpc_error(req_id, -32602, f"Tool not found: {skill_name}")

        # Control 2: allowlist — only tools explicitly exposed to A2A are reachable.
        if not getattr(tool_def, "expose_to_a2a", False):
            logger.warning(f"A2A skills/invoke: tool not exposed for remote invoke: {skill_name}")
            return _jsonrpc_error(
                req_id, -32601, f"Tool not exposed for remote invocation: {skill_name}")

        # Control 3: authorization — run under a constrained principal derived from the
        # verified caller key. clearance=0 + no action classes => clearance/action-gated
        # tools are denied by execute() even if exposed.
        try:
            from adk.auth import AuthContext, Principal
            key_fp = (caller_public_key or "unknown")[:16]
            auth = AuthContext(Principal(
                subject_id=f"a2a:{key_fp}",
                principal_class="agent",
                role="mesh_peer",
                clearance=0,
                allowed_action_types=frozenset(),
                channel="a2a",
                verified=True,
            ))
        except Exception as e:  # pragma: no cover - auth import/construction is stable
            logger.error(f"A2A skills/invoke: failed to build AuthContext: {e}")
            return _jsonrpc_error(req_id, -32603, "Authorization context error")

        # Execute the tool under the constrained AuthContext
        try:
            result_json = await self._agent._tools.execute(skill_name, args, auth=auth)
            # result_json is a JSON string; parse it
            result_obj = json.loads(result_json)

            return _jsonrpc_success(req_id, {
                "skill": skill_name,
                "output": result_obj,
            })
        except json.JSONDecodeError as e:
            # Log full detail server-side; return a generic message to the caller
            # so tool internals / paths / secrets never leak over the wire.
            logger.error(f"A2A skills/invoke {skill_name}: JSON parse error: {e}")
            return _jsonrpc_error(req_id, -32603, "Tool returned invalid JSON")
        except Exception as e:
            logger.error(f"A2A skills/invoke {skill_name} failed: {e}")
            return _jsonrpc_error(req_id, -32603, f"Tool execution error in '{skill_name}'")

    # ── SSE streaming ─────────────────────────────────────────────────────

    async def stream_task(self, task_id: str) -> AsyncIterator[str]:
        """Yield SSE events for a task."""
        task = self._tasks.get_task(task_id)
        if not task:
            yield f"data: {json.dumps({'error': 'Task not found'})}\n\n"
            return

        q = self._tasks.subscribe(task_id)

        # Send current state first
        yield f"data: {json.dumps({'type': 'status', 'task': task.to_dict()})}\n\n"

        # Stream updates
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=30.0)
                yield f"data: {json.dumps(event)}\n\n"
                # Check if terminal
                task = self._tasks.get_task(task_id)
                if task and task.status.state in (
                    TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED
                ):
                    break
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"

    # ── FastAPI mount ─────────────────────────────────────────────────────

    def mount(self, app):
        """Mount A2A endpoints on a FastAPI app.

        Adds:
          - GET  /.well-known/agent-card.json — Agent Card (canonical)
          - GET  /.well-known/agent.json — Agent Card (legacy, 308 redirect)
          - POST /a2a — JSON-RPC 2.0 (with optional A2A trust verification)
          - GET  /a2a/tasks/{task_id}/subscribe — SSE stream
        """
        from fastapi import Request
        from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse

        @app.get("/.well-known/agent-card.json")
        async def agent_card_canonical():
            """Canonical A2A agent card endpoint."""
            return self.build_agent_card()

        @app.get("/.well-known/agent.json")
        async def agent_card_legacy():
            """Legacy agent.json path — redirect to canonical location."""
            return RedirectResponse(
                url="/.well-known/agent-card.json",
                status_code=308,  # 308 Permanent Redirect (preserves method)
            )

        @app.post("/a2a")
        async def a2a_endpoint(request: Request):
            try:
                body_bytes = await request.body()
                body = await request.json()
            except Exception:
                return JSONResponse(
                    _jsonrpc_error(None, -32700, "Parse error"),
                    status_code=200,
                )

            # Optional A2A trust verification (OPT-IN via AITHER_A2A_REQUIRE_TRUST)
            from adk.a2a_trust import (
                verify_a2a_request,
                should_require_a2a_trust,
                should_audit_a2a_trust,
            )

            x_signature = request.headers.get("X-Signature")
            x_public_key = request.headers.get("X-Public-Key")
            x_node_id = request.headers.get("X-Node-ID")
            method = body.get("method", "") if isinstance(body, dict) else ""

            # Verify the signature ONCE up front whenever headers are present, so
            # both the global gate and the method-specific gate below share one
            # verification result (and one caller identity).
            trust_result = None
            if x_signature and x_public_key:
                trust_result = await verify_a2a_request(
                    request_body=body_bytes,
                    x_signature=x_signature,
                    x_public_key=x_public_key,
                    x_node_id=x_node_id,
                )

            # FAIL-CLOSED when trust is globally REQUIRED: a request with NO
            # signature headers must be REJECTED, not passed through. The old code
            # only ran `if x_signature or x_public_key`, so an attacker bypassed
            # enforcement simply by omitting the headers.
            if should_require_a2a_trust():
                if not (x_signature and x_public_key):
                    logger.warning("Rejecting A2A request: trust required but no signature")
                    return JSONResponse(
                        {"error": "Untrusted request",
                         "reason": "signature required (AITHER_A2A_REQUIRE_TRUST)"},
                        status_code=403,
                    )
                if not (trust_result and trust_result.trusted):
                    reason = trust_result.reason if trust_result else "no signature"
                    logger.warning(f"Rejecting untrusted A2A request: {reason}")
                    return JSONResponse(
                        {"error": "Untrusted request", "reason": reason},
                        status_code=403,
                    )
                logger.debug(f"A2A trust OK: {trust_result.reason}")
            elif should_audit_a2a_trust() and trust_result and not trust_result.trusted:
                # Audit mode: verify if present, log only, do not block.
                logger.warning(f"A2A trust audit: untrusted key would be rejected: "
                             f"{trust_result.reason}")

            # METHOD-SPECIFIC HARD GATE: skills/invoke lets a peer run local code,
            # so it ALWAYS requires a verified + trusted signature — independent of
            # the global AITHER_A2A_REQUIRE_TRUST mode (which stays 'false' by
            # default so benign message/send chat keeps working unsigned). Flipping
            # the global default would break existing chat and, with the mesh-trust
            # lookup still a stub, reject everything; gating the dangerous method
            # specifically is the safer, narrower control.
            if method == "skills/invoke":
                if not (x_signature and x_public_key):
                    logger.warning("Rejecting skills/invoke: signature required")
                    return JSONResponse(
                        {"error": "Untrusted request",
                         "reason": "skills/invoke requires a signed request"},
                        status_code=403,
                    )
                if not (trust_result and trust_result.trusted):
                    reason = trust_result.reason if trust_result else "no signature"
                    logger.warning(f"Rejecting untrusted skills/invoke: {reason}")
                    return JSONResponse(
                        {"error": "Untrusted request",
                         "reason": f"skills/invoke requires a trusted key: {reason}"},
                        status_code=403,
                    )

                # REPLAY PROTECTION: check ts/nonce (only after trust is verified)
                from adk.a2a_trust import check_replay
                ok, why = check_replay(body)
                if not ok:
                    logger.warning(f"Rejecting skills/invoke: replay check failed: {why}")
                    return JSONResponse(
                        {"error": "Untrusted request",
                         "reason": f"replay check failed: {why}"},
                        status_code=403,
                    )

            caller_public_key = x_public_key if (trust_result and trust_result.verified) else None
            caller_trusted = bool(trust_result and trust_result.trusted)
            result = await self.handle_jsonrpc(
                body, caller_public_key=caller_public_key, caller_trusted=caller_trusted)
            return JSONResponse(result)

        @app.get("/a2a/tasks/{task_id}/subscribe")
        async def a2a_subscribe(task_id: str):
            return StreamingResponse(
                self.stream_task(task_id),
                media_type="text/event-stream",
            )

    # ── Status ────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "protocol": "a2a",
            "protocolVersion": _PROTOCOL_VERSION,
            "agent": self._agent.name if self._agent else None,
            "tasks_total": len(self._tasks._tasks),
            "tasks_active": sum(
                1 for t in self._tasks._tasks.values()
                if t.status.state in (TaskState.SUBMITTED, TaskState.WORKING)
            ),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _jsonrpc_success(req_id, result) -> dict:
    return {"jsonrpc": "2.0", "result": result, "id": req_id}


def _jsonrpc_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": req_id}


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _get_version() -> str:
    try:
        from adk import __version__
        return __version__
    except Exception:
        return "0.0.0"
