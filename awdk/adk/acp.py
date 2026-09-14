"""ACP (Agent Client Protocol) client for awdk.

Implements a JSON-RPC 2.0 stdio-based client to connect to ACP-compliant agents
(e.g., hermes, other ACP servers). Supports session management, prompting, and
tool-call streaming.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def _pick(d: dict, *names: str, default: Any = None) -> Any:
    """First present key among *names* (ACP wire is camelCase; snake = legacy)."""
    for n in names:
        if isinstance(d, dict) and d.get(n) is not None:
            return d[n]
    return default


# Real ACP session/update discriminator values (agent-client-protocol).
_START_KINDS = ("tool_call", "tool_call_start")
_UPDATE_KINDS = ("tool_call_update", "tool_call_complete")
_DONE_STATUSES = ("completed", "failed")


def _render_tool_result(update: dict) -> Optional[str]:
    """Extract a tool result string from a real-ACP or legacy update.

    Real ACP carries results in a ``content`` LIST of content blocks; legacy
    mocks use a flat ``result`` string. Returns None when the update carries no
    result yet (e.g. a pending/in_progress tool_call).
    """
    legacy = update.get("result")
    if isinstance(legacy, str):
        return legacy
    content = update.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            inner = block.get("content")
            if isinstance(inner, dict) and inner.get("type") == "text":
                parts.append(inner.get("text", ""))
            elif block.get("type") == "text":
                parts.append(block.get("text", ""))
        if parts:
            return "".join(parts)
    return None

__all__ = [
    "ACPClient",
    "ACPCapabilities",
    "ACPUsage",
    "ACPToolCall",
    "ACPPromptResult",
    "ACPApprovalRequest",
]


class ACPCapabilities(BaseModel):
    """Agent capabilities returned from initialize."""

    protocol_version: int
    agent_name: str
    agent_version: str
    load_session: bool = False
    prompt_capabilities: dict[str, Any] = Field(default_factory=dict)
    session_capabilities: dict[str, Any] = Field(default_factory=dict)


class ACPUsage(BaseModel):
    """Token usage information."""

    input_tokens: int = 0
    output_tokens: int = 0


class ACPToolCall(BaseModel):
    """A tool call within a prompt response."""

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Optional[str] = None


class ACPPromptResult(BaseModel):
    """Result from a prompt request."""

    text: str
    tool_calls: list[ACPToolCall] = Field(default_factory=list)
    usage: ACPUsage = Field(default_factory=ACPUsage)
    stop_reason: str = "end_turn"


class ACPApprovalRequest(BaseModel):
    """Request for user approval (e.g., for file edits, tool execution)."""

    request_id: str
    kind: str  # e.g., "edit", "execute", "tool"
    description: str
    # v2 context surfaced to the callback (session/request_permission carries
    # the operation here; correlation is the JSON-RPC id, so request_id is it).
    session_id: str = ""
    title: str = ""
    tool_call: dict[str, Any] = Field(default_factory=dict)
    options: list[dict[str, Any]] = Field(default_factory=list)


class ACPClient:
    """Async JSON-RPC 2.0 ACP client over stdio.

    Spawns/attaches to an ACP-compliant server and manages JSON-RPC framing.
    Supports initialize, create_session, prompt (with streaming), and other
    session operations.
    """

    def __init__(
        self,
        command: str | None = None,
        args: list[str] | None = None,
        approval_callback: Optional[
            Callable[[ACPApprovalRequest], bool]
        ] = None,
        subprocess_instance: Optional[Any] = None,
        reader: Optional[asyncio.StreamReader] = None,
        writer: Optional[asyncio.StreamWriter] = None,
    ):
        """Initialize an ACP client.

        Args:
            command: Path to ACP server executable. If None, use subprocess_instance.
            args: Command-line arguments for the server.
            approval_callback: Async or sync callback for approval requests.
                               Returns True to approve, False to deny. Default: deny all.
            subprocess_instance: Existing subprocess instance to use instead of spawning.
            reader: In-process StreamReader to read the agent's responses from
                (alternative to a subprocess; used by conformance tests that run
                the peer in the same event loop over a socket pair).
            writer: In-process StreamWriter to send requests to.
        """
        self.command = command
        self.args = args or []
        self.approval_callback = approval_callback or (lambda _: False)
        self.subprocess = subprocess_instance
        self._reader: Optional[asyncio.StreamReader] = reader
        self._writer: Optional[asyncio.StreamWriter] = writer
        self._request_id_counter = 0
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._read_task: Optional[asyncio.Task] = None
        self._capabilities: Optional[ACPCapabilities] = None
        # Handshake config (ACP requires protocolVersion on initialize).
        # v2 is the protocol we speak. The reference SDK (v1-wire names) accepts
        # any int (schema ge=0, le=65535) and never rejects a version, so a v2
        # default does not strand v1-era peers.
        self.protocol_version = 2
        # What this client actually answers: session/request_permission. fs/*,
        # elicitation and terminal are NOT handled, so do not advertise them —
        # never lie about a surface (same rule the server follows). Both wires
        # ignore unknown keys, so this stays honest whether the peer reads
        # `capabilities` (v2) or `clientCapabilities` (v1-wire).
        self.client_capabilities: dict[str, Any] = {"session": {}}

    async def connect(self) -> None:
        """Spawn/attach to the ACP server and start the read loop."""
        # In-process reader/writer (conformance tests) take precedence over a
        # subprocess — with both provided there is nothing to spawn or attach.
        if self._reader is None or self._writer is None:
            if self.subprocess is None:
                if not self.command:
                    raise ValueError("Either command or subprocess_instance must be provided")
                self.subprocess = await asyncio.create_subprocess_exec(
                    self.command,
                    *self.args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            self._reader = self.subprocess.stdout
            self._writer = self.subprocess.stdin
            if not self._reader or not self._writer:
                raise RuntimeError("Failed to open stdio pipes")
        self._read_task = asyncio.create_task(self._read_loop())

    async def disconnect(self) -> None:
        """Close the connection and cleanup."""
        # Fail in-flight requests FIRST so a caller awaiting a prompt during
        # teardown gets an error, not a hang.
        self._fail_pending("client disconnected")
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception as exc:  # teardown is best-effort
                logger.debug("acp disconnect: writer close failed: %s", exc)
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                logger.debug("acp disconnect: read task cancelled (expected)")
        if self.subprocess:
            try:
                await asyncio.wait_for(self.subprocess.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.subprocess.kill()

    def _get_next_request_id(self) -> int:
        """Generate the next unique JSON-RPC request ID."""
        self._request_id_counter += 1
        return self._request_id_counter

    async def _send_request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = 60.0,
    ) -> Any:
        """Send a JSON-RPC 2.0 request and wait for the response.

        Args:
            method: RPC method name.
            params: Method parameters (keyword args).
            timeout: Seconds to wait for the response before raising; None =
                wait indefinitely. ``session/prompt`` must pass a long value —
                a real agent turn (tool loops, sub-agents) routinely exceeds
                60s, and cutting it off mid-turn is a broken drive.

        Returns:
            The result field from the JSON-RPC response.

        Raises:
            RuntimeError: If the response is an error or missing result.
        """
        if not self._writer or not self._reader:
            raise RuntimeError("Not connected")

        request_id = self._get_next_request_id()
        request_obj = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params:
            request_obj["params"] = params

        # Serialize and send
        line = json.dumps(request_obj)
        self._writer.write(line.encode("utf-8") + b"\n")
        await self._writer.drain()

        # Wait for response
        future: asyncio.Future = asyncio.Future()
        self._pending_requests[request_id] = future
        try:
            if timeout is None:
                return await future
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            del self._pending_requests[request_id]
            raise RuntimeError(f"RPC request {method} timed out")

    async def _read_loop(self) -> None:
        """Read and dispatch JSON-RPC messages from the server."""
        if not self._reader:
            return
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError as e:
                    logger.warning("Failed to decode JSON-RPC message: %s", e)
                    continue
                # JSON-RPC batch: an array of requests/notifications/responses.
                # A real agent may batch its output; each element is processed
                # exactly like a standalone message.
                if isinstance(obj, list):
                    for entry in obj:
                        await self._process_message(entry)
                else:
                    await self._process_message(obj)
        except asyncio.CancelledError:
            logger.debug("acp read loop cancelled (expected on disconnect)")
        except Exception as e:
            logger.error("Read loop error: %s", e, exc_info=True)
        else:
            # EOF: the peer closed the connection (the agent exited). Fail every
            # in-flight request so awaiting callers get an error instead of a
            # hang on a dead agent.
            self._fail_pending("peer closed the connection")

    def _fail_pending(self, reason: str) -> None:
        """Resolve every in-flight request with a connection-closed error."""
        for rid, fut in list(self._pending_requests.items()):
            if not fut.done():
                fut.set_exception(RuntimeError(f"ACP connection closed: {reason}"))
            self._pending_requests.pop(rid, None)

    async def _process_message(self, obj: dict[str, Any]) -> None:
        """Process an inbound JSON-RPC message."""
        # An inbound REQUEST (method + id): the agent is asking the CLIENT
        # something (v2 session/request_permission). Answer the id; never let
        # it fall through to the response/notification branches (it has an id,
        # so the response branch would ignore it as an unknown id, and the
        # notification branch excludes it by the id).
        if obj.get("method") is not None and obj.get("id") is not None:
            await self._handle_request(obj)
            return

        # Check if it's a response to a pending request
        if "id" in obj:
            request_id = obj["id"]
            if request_id in self._pending_requests:
                future = self._pending_requests.pop(request_id)
                if "error" in obj:
                    error_obj = obj.get("error", {})
                    error_msg = error_obj.get("message", "Unknown error")
                    future.set_exception(RuntimeError(f"RPC error: {error_msg}"))
                elif "result" in obj:
                    future.set_result(obj["result"])
                else:
                    future.set_exception(
                        RuntimeError("Invalid JSON-RPC response: no result or error")
                    )
                return

        # Check if it's a notification (method without id)
        if "method" in obj and "id" not in obj:
            await self._handle_notification(obj)

    async def _handle_notification(self, obj: dict[str, Any]) -> None:
        """Handle server-side notifications."""
        method = obj.get("method")
        if method in ("session/request_permission", "approval_request"):
            params = obj.get("params", {})
            request_id = params.get("request_id")
            kind = params.get("kind", "unknown")
            description = params.get("description", "")

            # Call approval callback
            approval_req = ACPApprovalRequest(
                request_id=request_id, kind=kind, description=description
            )
            try:
                approved = await self._call_approval_callback(approval_req)
            except Exception as e:
                logger.error("Approval callback raised: %s", e, exc_info=True)
                approved = False

            # Send approval response (notification, no id)
            if self._writer:
                response = {
                    "jsonrpc": "2.0",
                    "method": "approval_response",
                    "params": {"request_id": request_id, "approved": approved},
                }
                line = json.dumps(response)
                self._writer.write(line.encode("utf-8") + b"\n")
                await self._writer.drain()

    async def _handle_request(self, obj: dict[str, Any]) -> None:
        """Answer an inbound server REQUEST (v2 session/request_permission).

        v2 answers the JSON-RPC id with an outcome result; the legacy
        notification + ``approval_response`` path (a v1 server sending
        session/request_permission WITHOUT an id) still lands in
        :meth:`_handle_notification`. Unknown inbound requests fail loud with
        method-not-found rather than being silently dropped.
        """
        method = obj.get("method")
        request_id = obj.get("id")
        params = obj.get("params") or {}

        if method == "session/request_permission":
            # Dual-form context: v2 docs nest it under subject.toolCall; the
            # reference SDK sends a flat toolCall.
            tool_call = params.get("toolCall") or params.get("tool_call") or {}
            subject = params.get("subject")
            if isinstance(subject, dict) and isinstance(subject.get("toolCall"), dict):
                tool_call = subject["toolCall"]
            approval_req = ACPApprovalRequest(
                request_id=str(request_id),
                kind="tool_call",
                title=params.get("title") or params.get("description") or "Approve?",
                description=params.get("description") or params.get("title") or "",
                session_id=params.get("sessionId") or params.get("session_id") or "",
                tool_call=tool_call if isinstance(tool_call, dict) else {},
                options=params.get("options") if isinstance(params.get("options"), list) else [],
            )
            try:
                approved = await self._call_approval_callback(approval_req)
            except Exception as e:  # noqa: BLE001 — a broken callback denies
                logger.error("Approval callback raised: %s", e, exc_info=True)
                approved = False
            # "selected" with an allow/reject option id is the semantic match
            # for a boolean approve/deny decision.
            option_id = "allow-once" if approved else "reject-once"
            await self._send_response(request_id, {
                "outcome": {"outcome": "selected", "optionId": option_id},
            })
            return

        await self._send_response(
            request_id,
            None,
            error={"code": -32601, "message": f"Method not found: {method}"},
        )

    async def _send_response(
        self, request_id: Any, result: Any = None, *, error: Optional[dict] = None
    ) -> None:
        """Send a JSON-RPC response to an inbound request."""
        if not self._writer:
            return
        if error is not None:
            response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "error": error}
        else:
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        self._writer.write((json.dumps(response) + "\n").encode("utf-8"))
        await self._writer.drain()

    async def _send_notification(self, method: str, params: Optional[dict] = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if not self._writer:
            return
        notification: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            notification["params"] = params
        self._writer.write((json.dumps(notification) + "\n").encode("utf-8"))
        await self._writer.drain()

    async def cancel(self, session_id: str) -> None:
        """Notify the agent to cancel the active work (spec: a notification)."""
        await self._send_notification("session/cancel", {"sessionId": session_id})

    async def auth_login(self, method_id: str) -> None:
        """Authenticate with an agent that advertised authMethods (v2)."""
        await self._send_request("auth/login", {"methodId": method_id})

    async def auth_logout(self) -> None:
        """End the authenticated state (v2)."""
        await self._send_request("auth/logout", {})

    async def stream_prompt(
        self,
        session_id: str,
        text: str,
        *,
        settle: float = 0.1,
        poll: float = 0.02,
        drain_timeout: float = 2.0,
        blocks: list[dict[str, Any]] | None = None,
    ):
        """Stream the ``session/update`` notifications for one prompt, LIVE.

        Yields each update payload (the dict nested under ``params.update``)
        as it arrives, in receive order — so a caller can render agent message
        chunks, tool-call lifecycles and the terminal idle state as they
        happen instead of after the turn. Stops when the turn is complete (no
        outstanding tool calls and no trailing updates for *settle* seconds) or
        at *drain_timeout*. Use :meth:`prompt` when you only need the
        aggregated result.

        Args:
            session_id: Session ID from create_session.
            text: Prompt text (used when ``blocks`` is not given).
            blocks: Optional raw ACP content blocks, sent verbatim in place of
                the single text block.

        Yields:
            dict: each raw session/update payload.
        """
        updates: list[dict[str, Any]] = []
        queue: asyncio.Queue = asyncio.Queue()

        async def collector(obj: dict[str, Any]) -> None:
            if obj.get("method") == "session/update":
                params = obj.get("params", {}) or {}
                queue.put_nowait(params.get("update", params))
            else:
                await self._handle_notification(obj)

        original_handle = self._handle_notification
        self._handle_notification = collector
        loop = asyncio.get_running_loop()
        last_yield = loop.time()
        deadline = loop.time() + max(drain_timeout, settle)
        try:
            await self._send_request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": (
                        blocks if blocks is not None else [{"type": "text", "text": text}]
                    ),
                },
                timeout=600.0,
            )
            while True:
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=poll)
                except asyncio.TimeoutError:
                    update = None
                if update is not None:
                    updates.append(update)
                    last_yield = loop.time()
                    yield update
                    continue
                # Nothing arrived for one poll interval.
                if not self._outstanding_tool_calls(updates) and (
                    loop.time() - last_yield
                ) >= settle:
                    return
                if loop.time() >= deadline:
                    if self._outstanding_tool_calls(updates):
                        logger.warning(
                            "acp.stream_prompt.drain_timeout: %d tool call(s) did not "
                            "complete within %.2fs",
                            len(self._outstanding_tool_calls(updates)), drain_timeout,
                        )
                    return
        finally:
            self._handle_notification = original_handle

    async def _call_approval_callback(self, req: ACPApprovalRequest) -> bool:
        """Call the approval callback, handling both sync and async functions."""
        if asyncio.iscoroutinefunction(self.approval_callback):
            return await self.approval_callback(req)
        else:
            return self.approval_callback(req)

    async def initialize(self) -> ACPCapabilities:
        """Initialize the ACP connection.

        Returns:
            Agent capabilities.
        """
        result = await self._send_request(
            "initialize",
            {
                # protocolVersion is REQUIRED by the ACP spec; omitting it makes a
                # real agent reject the handshake with "Invalid params".
                "protocolVersion": self.protocol_version,
                # Dual-form: the v2 wire reads `capabilities` + `info`; the
                # reference SDK (v1-wire names) reads `clientCapabilities` +
                # `clientInfo`. Both ignore unknown keys, so emitting both means
                # one initialize serves a v2 agent and a v1-era one alike.
                "capabilities": self.client_capabilities,
                "info": {"name": "awdk", "version": "1"},
                "clientCapabilities": self.client_capabilities,
                "clientInfo": {"name": "awdk", "version": "1"},
            },
        )
        # ACP wire format is camelCase (verified against agent-client-protocol,
        # the Zed reference lib). snake_case is accepted as a lenient fallback.
        # The v2 agent answers under `info`/`capabilities`; v1-era peers under
        # `agentInfo`/`agentCapabilities`. Read all three.
        info = _pick(result, "info", "agentInfo", "agent_info") or {}
        agent_caps = (
            _pick(result, "capabilities", "agentCapabilities", "agent_capabilities") or {}
        )
        caps = ACPCapabilities(
            protocol_version=_pick(result, "protocolVersion", "protocol_version") or 1,
            agent_name=info.get("name", "unknown"),
            agent_version=info.get("version", "0.0.0"),
            load_session=bool(_pick(agent_caps, "loadSession", "load_session") or False),
            prompt_capabilities=_pick(
                agent_caps, "promptCapabilities", "prompt_capabilities"
            ) or {},
            session_capabilities=_pick(
                # v2 nests session methods under capabilities.session; our server
                # and the reference SDK emit sessionCapabilities / session_capabilities.
                agent_caps, "session", "sessionCapabilities", "session_capabilities"
            ) or {},
        )
        self._capabilities = caps
        return caps

    async def create_session(
        self,
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        mcp_servers: Optional[list] = None,
        additional_directories: Optional[list] = None,
    ) -> str:
        """Create a new ACP session.

        Args:
            cwd: Working directory for the session.
            model: Model to use for the session.
            mcp_servers: Editor-provided MCP servers the agent may reach.
            additional_directories: Extra directories the agent may access
                beyond ``cwd`` (v2 ``additionalDirectories``).

        Returns:
            Session ID.
        """
        # cwd and mcpServers are REQUIRED by the ACP spec on session/new.
        params: dict[str, Any] = {
            "cwd": cwd or os.getcwd(),
            "mcpServers": mcp_servers if mcp_servers is not None else [],
        }
        if model:
            params["model"] = model
        if additional_directories:
            params["additionalDirectories"] = additional_directories
        result = await self._send_request("session/new", params)
        return _pick(result, "sessionId", "session_id") or ""

    async def prompt(
        self,
        session_id: str,
        text: str,
        *,
        drain_timeout: float = 2.0,
        blocks: list[dict[str, Any]] | None = None,
    ) -> ACPPromptResult:
        """Send a prompt to the agent and consume the streamed response.

        The server may stream ToolCallStart, ToolCallProgress, ToolCallComplete,
        AgentMessageChunk, and UsageUpdate messages via session/update notifications.

        Args:
            session_id: Session ID from create_session.
            text: Prompt text (used when ``blocks`` is not given).
            drain_timeout: Max seconds to wait for trailing notifications after the
                prompt response, so tool calls that complete late are not dropped.
                Returns as soon as every started tool call has completed.
            blocks: Optional raw ACP content blocks. When given, sent verbatim in
                place of ``[{"type": "text", "text": text}]`` — lets callers like
                the ACP LLM provider replay a multi-turn message list.

        Returns:
            Aggregated result with text, tool calls, and usage.
        """
        # Temporarily collect session updates during this prompt
        pending_updates: list[dict[str, Any]] = []

        original_handle = self._handle_notification
        self._handle_notification = self._make_update_collector(pending_updates)

        try:
            params = {
                "sessionId": session_id,
                "prompt": (
                    blocks if blocks is not None else [{"type": "text", "text": text}]
                ),
            }
            # A real agent turn (tool loops, sub-agents) can take minutes; the
            # generic 60s request timeout would cut it off mid-turn.
            result = await self._send_request("session/prompt", params, timeout=600.0)

            # Drain trailing session/update notifications (bounded, never silent).
            await self._drain_updates(pending_updates, timeout=drain_timeout)

            # Parse the aggregated response
            return self._parse_prompt_result(result, pending_updates)
        finally:
            self._handle_notification = original_handle

    def _make_update_collector(
        self, pending_updates: list[dict[str, Any]]
    ) -> Callable:
        """Return a handler that collects session/update notifications."""

        async def collector(obj: dict[str, Any]) -> None:
            method = obj.get("method")
            if method == "session/update":
                params = obj.get("params", {}) or {}
                # Real ACP nests the payload under "update"; legacy mocks send it flat.
                pending_updates.append(params.get("update", params))
            else:
                await self._handle_notification(obj)

        return collector

    @staticmethod
    def _outstanding_tool_calls(updates: list[dict[str, Any]]) -> set[str]:
        """Tool call ids that have started but not yet reported completion."""
        started: set[str] = set()
        done: set[str] = set()
        for update in updates:
            kind = _pick(update, "sessionUpdate", "session_update")
            tid = _pick(update, "toolCallId", "tool_call_id", default="")
            if kind in _START_KINDS:
                started.add(tid)
                # a start may already carry a terminal status
                if _pick(update, "status") in _DONE_STATUSES:
                    done.add(tid)
            elif kind in _UPDATE_KINDS:
                status = _pick(update, "status")
                # legacy tool_call_complete has no status but IS terminal
                if status in _DONE_STATUSES or kind == "tool_call_complete":
                    done.add(tid)
        return started - done

    @staticmethod
    def _terminal_idle(updates: list[dict[str, Any]]) -> bool:
        """True once a terminal ``state_update: idle`` has arrived.

        A v2 turn is only complete at idle — the agent text is streamed as
        ``agent_message_chunk`` updates BEFORE idle, all of them arriving after
        the immediate ``session/prompt`` response. Waiting on "no outstanding
        tool calls" alone returns before any text has arrived on a text-only
        turn (measured live: PONG was dropped), so the drain must key on idle.
        """
        for update in updates:
            if (
                _pick(update, "sessionUpdate", "session_update") == "state_update"
                and _pick(update, "state", default="") == "idle"
            ):
                return True
        return False

    async def _drain_updates(
        self,
        updates: list[dict[str, Any]],
        *,
        timeout: float = 2.0,
        settle: float = 0.1,
        poll: float = 0.02,
    ) -> None:
        """Wait (bounded) for trailing ``session/update`` notifications.

        Replaces a fixed sleep, which silently dropped tool-call completions that
        arrived later than the sleep. Returns once the terminal
        ``state_update: idle`` has arrived (after a short settle window for
        trailing usage/plan updates), and at the deadline logs a WARNING naming
        anything still outstanding OR the missing idle — so a dead turn is never
        silently empty and late data is never lost silently.
        """
        loop = asyncio.get_running_loop()
        start = loop.time()
        deadline = start + max(timeout, settle)
        while True:
            if self._terminal_idle(updates) and (loop.time() - start) >= settle:
                return
            outstanding = self._outstanding_tool_calls(updates)
            if loop.time() >= deadline:
                if outstanding:
                    logger.warning(
                        "acp.prompt.drain_timeout: %d tool call(s) did not complete "
                        "within %.2fs: %s",
                        len(outstanding),
                        timeout,
                        ", ".join(sorted(outstanding)),
                    )
                if not self._terminal_idle(updates):
                    logger.warning(
                        "acp.prompt.drain_timeout: no terminal idle within %.2fs "
                        "(%d update(s) collected)",
                        timeout,
                        len(updates),
                    )
                return
            await asyncio.sleep(poll)

    def _parse_prompt_result(
        self,
        response: dict[str, Any],
        updates: list[dict[str, Any]],
    ) -> ACPPromptResult:
        """Aggregate streamed updates into a single prompt result.

        Args:
            response: The prompt request response.
            updates: List of session/update notifications received.

        Returns:
            Parsed ACPPromptResult.
        """
        result_text = ""
        usage = ACPUsage()
        # Key by tool_call_id (NOT a single "active" slot) so interleaved tool
        # calls cannot overwrite each other, and a call that never completes is
        # still REPORTED (result=None) rather than silently dropped.
        started: dict[str, ACPToolCall] = {}

        # Process streamed updates
        for update in updates:
            update_type = _pick(update, "sessionUpdate", "session_update")
            tool_call_id = _pick(update, "toolCallId", "tool_call_id", default="")

            if update_type in ("agent_message_chunk", "agent_thought_chunk"):
                content = update.get("content") or {}
                if isinstance(content, dict) and content.get("type") == "text":
                    if update_type == "agent_message_chunk":
                        result_text += content.get("text", "")

            elif update_type in _START_KINDS:
                started[tool_call_id] = ACPToolCall(
                    tool_call_id=tool_call_id,
                    # real ACP uses `title`; legacy mocks use `tool_name`
                    tool_name=_pick(update, "title", "tool_name", default=""),
                    arguments=_pick(update, "rawInput", "function_args", default={}) or {},
                    result=_render_tool_result(update),
                )

            elif update_type in _UPDATE_KINDS:
                res = _render_tool_result(update)
                call = started.get(tool_call_id)
                if call is not None:
                    if res is not None:
                        call.result = res
                    if not call.tool_name:
                        call.tool_name = _pick(update, "title", "tool_name", default="")
                else:
                    # Update without a start — record it rather than drop it.
                    started[tool_call_id] = ACPToolCall(
                        tool_call_id=tool_call_id,
                        tool_name=_pick(update, "title", "tool_name", default=""),
                        arguments={},
                        result=res,
                    )

            elif update_type == "usage_update":
                usage = ACPUsage(
                    input_tokens=_pick(update, "inputTokens", "input_tokens", default=0),
                    output_tokens=_pick(update, "outputTokens", "output_tokens", default=0),
                )

        # Real ACP returns usage on the PromptResponse, not as a notification.
        resp_usage = _pick(response, "usage") or {}
        if resp_usage:
            usage = ACPUsage(
                input_tokens=_pick(resp_usage, "inputTokens", "input_tokens", default=0),
                output_tokens=_pick(resp_usage, "outputTokens", "output_tokens", default=0),
            )

        incomplete = [c.tool_call_id for c in started.values() if c.result is None]
        if incomplete:
            logger.warning(
                "acp.prompt.tool_calls_incomplete: %d tool call(s) reported no "
                "result: %s (returned with result=None)",
                len(incomplete),
                ", ".join(incomplete),
            )

        return ACPPromptResult(
            text=result_text,
            tool_calls=list(started.values()),
            usage=usage,
            stop_reason=_pick(response, "stopReason", "stop_reason") or "end_turn",
        )

    async def set_session_model(
        self,
        session_id: str,
        model: str,
    ) -> None:
        """Change the model for an active session.

        Args:
            session_id: Session ID.
            model: Model name/identifier.
        """
        params = {"session_id": session_id, "model": model}
        await self._send_request("session/set_model", params)

    async def load_session(
        self,
        session_id: str,
        cwd: Optional[str] = None,
    ) -> None:
        """Load/restore a persisted session.

        Args:
            session_id: Session ID to load.
            cwd: Working directory.
        """
        params = {"session_id": session_id}
        if cwd:
            params["cwd"] = cwd
        await self._send_request("session/load", params)

    async def resume_session(
        self,
        session_id: str,
        cwd: Optional[str] = None,
    ) -> None:
        """Resume a paused or stopped session.

        Args:
            session_id: Session ID to resume.
            cwd: Working directory.
        """
        params = {"session_id": session_id}
        if cwd:
            params["cwd"] = cwd
        await self._send_request("session/resume", params)

    async def fork_session(
        self,
        session_id: str,
        cwd: Optional[str] = None,
    ) -> str:
        """Fork an existing session into a new one.

        Args:
            session_id: Session to fork.
            cwd: Working directory for the new session.

        Returns:
            New session ID.
        """
        params = {"session_id": session_id}
        if cwd:
            params["cwd"] = cwd
        result = await self._send_request("session/fork", params)
        return result.get("session_id", "")

    async def list_sessions(
        self,
        cwd: Optional[str] = None,
        cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        """List available sessions.

        Args:
            cwd: Filter by working directory.
            cursor: Pagination cursor.

        Returns:
            Session list response with pagination info.
        """
        params = {}
        if cwd:
            params["cwd"] = cwd
        if cursor:
            params["cursor"] = cursor
        return await self._send_request("session/list", params)

    async def close_session(self, session_id: str) -> dict[str, Any]:
        """Close an active session; the agent cancels its ongoing work."""
        return await self._send_request("session/close", {"sessionId": session_id})

    async def delete_session(self, session_id: str) -> dict[str, Any]:
        """Delete a session (removed from future session/list responses)."""
        return await self._send_request("session/delete", {"sessionId": session_id})

    async def __aenter__(self):
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.disconnect()
