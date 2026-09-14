"""ACP (Agent Client Protocol) v2 SERVER — expose an adk agent to any ACP client.

The mirror of :mod:`adk.acp` (which DRIVES other agents). This module makes an
``adk`` agent drivable BY editors and hosts that speak ACP — Zed, VS Code,
JetBrains, neovim, Obsidian — over stdio JSON-RPC 2.0, with no editor-specific
code.

Wire format is ACP v2 (https://agentclientprotocol.com/protocol/v2/): the full
prompt lifecycle (``user_message`` confirm, ``state_update`` running /
requires_action / idle with a stop reason, ``agent_message``, ``tool_call``
updates, ``usage_update``), session list/resume/close/delete, and
``session/request_permission`` for human-in-the-loop gating. Emission is
v2-native camelCase; parsing is lenient, accepting the reference
``agent-client-protocol`` SDK's older snake_case wire and v1 field aliases.

Human-in-the-loop maps onto the adk agent's own approval gate: ``AitherAgent``
returns ``requires_action=True`` with a ``pending`` tool-call list and resumes
via ``resume(session_id, decisions)``. The server bridges that to ACP
``session/request_permission``: on ``requires_action`` it asks the editor for
each pending tool, maps the outcome to an allow/deny decision, and resumes the
turn (which may pause again on a different gated tool).

Agent contract: anything exposing ``async run(prompt, **kwargs)`` returning an
object with ``content`` (or ``output`` / ``str``) and optionally
``requires_action`` / ``pending`` / ``resume(...)`` / ``finish_reason`` /
``tokens_used``. ``AitherAgent`` satisfies it via ``chat()``/``resume()``.
Per-token streaming of a tool-capable agent is a documented follow-up: the
ReAct loop does not yet expose per-tool events to callers, and a single
``agent_message`` update is spec-valid.

Usage::

    from adk import AitherAgent
    from adk.acp_server import serve_stdio

    asyncio.run(serve_stdio(AitherAgent("atlas")))
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from adk.acp_common import (
    AUTH_METHOD_TYPES,
    AUTH_REQUIRED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    STOP_REASONS,
    RpcError,
    agent_message_chunk,
    available_commands_update,
    extract_prompt_text,
    make_error,
    make_notification,
    make_request,
    make_response,
    pick,
    state_update,
    text_message,
    tool_call_update,
    usage_update,
    user_message_update,
)
from adk.stdio_compat import ThreadStdinReader, ThreadStdoutWriter

logger = logging.getLogger(__name__)

__all__ = ["ACPServer", "ACPSession", "serve_stdio", "PROTOCOL_VERSION"]


class ACPSession:
    """One ACP conversation bound to a working directory."""

    def __init__(
        self,
        session_id: str,
        cwd: str,
        *,
        model: Optional[str] = None,
        mcp_servers: Optional[list] = None,
        additional_directories: Optional[list] = None,
    ) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.model = model
        self.mcp_servers = list(mcp_servers or [])
        self.additional_directories = list(additional_directories or [])
        self.history: list[dict[str, Any]] = []
        self.title: Optional[str] = None
        self.created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.updated_at = self.created_at
        self.cancel_event = asyncio.Event()
        self._message_n = 0
        self.active_turn: Optional[asyncio.Task] = None

    def next_message_id(self) -> str:
        self._message_n += 1
        return f"m{self._message_n}"

    def touch(self) -> None:
        self.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ACPServer:
    """Serve an adk agent over the Agent Client Protocol (v2).

    Args:
        agent: Anything exposing ``async run(prompt, **kwargs)`` returning an
            object with ``content`` (or ``output`` / ``str``); see the module
            docstring for the optional ``requires_action`` / ``pending`` /
            ``resume()`` contract that powers human-in-the-loop approval.
        name: Agent name reported in ``initialize`` (``info.name``).
        version: Agent version reported in ``initialize``.
    """

    def __init__(
        self,
        agent: Any,
        *,
        name: Optional[str] = None,
        version: str = "1.0.0",
    ) -> None:
        self.agent = agent
        self.name = name or getattr(agent, "name", None) or "aither-adk-agent"
        self.version = version
        self.sessions: dict[str, ACPSession] = {}
        self._writer: Any = None
        self._write_lock = asyncio.Lock()
        self._rpc_id = 0
        # Outbound requests (session/request_permission) awaiting the client's
        # response, keyed by JSON-RPC id -> (session_id, future). The session_id
        # lets a cancel/close resolve ONLY this session's pending permission —
        # never a concurrent session's (a global resolve would wrongly end a
        # different session's in-flight turn when this one is cancelled).
        self._pending_requests: dict[int, tuple[str, asyncio.Future]] = {}
        # Whether `authenticate` succeeded THIS connection. Deliberately NOT
        # seeded from ~/.aither/auth.json: the stdio-local path has always been
        # usable without a login (a self-hosted fleet is the owner's own box),
        # so this only records what happened on this wire.
        self._authenticated = False

    # ---- wire I/O ---------------------------------------------------------

    async def _write(self, obj: dict[str, Any]) -> None:
        """Write one JSON-RPC frame, serialized under the connection write lock."""
        if self._writer is None:
            return
        from adk.acp_common import write_frame

        await write_frame(self._writer, obj, lock=self._write_lock)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        await self._write(make_notification(method, params))

    async def session_update(self, session_id: str, update: dict[str, Any]) -> None:
        """Emit a ``session/update`` notification in ACP v2's nested shape."""
        await self.notify(
            "session/update", {"sessionId": session_id, "update": update}
        )

    # ---- session/update helpers -------------------------------------------

    async def _advertise_commands(self, session: ACPSession) -> None:
        """Announce the slash commands available in this session (spec: MAY)."""
        await self.session_update(
            session.session_id,
            available_commands_update([
                {"name": "help", "description": "Show available commands",
                 "input": {"type": "text", "hint": "command topic"}},
                {"name": "status", "description": "Show agent + backend status"},
            ]),
        )

    # ---- protocol methods --------------------------------------------------

    async def handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        # Lenient read: v2 docs send protocolVersion; the reference SDK sends
        # protocol_version. Echo a supported version (we speak v2).
        client_version = pick(
            params, "protocolVersion", "protocol_version", default=PROTOCOL_VERSION
        )
        version = min(int(client_version or PROTOCOL_VERSION), PROTOCOL_VERSION)
        # Dual-form capabilities: the v2 docs nest session methods under
        # capabilities.session; the reference SDK reads session_capabilities /
        # prompt_capabilities top-level. Unknown keys are ignored, so advertise
        # both and never lie about a surface we do not implement.
        session_caps = {"close": {}, "delete": {}, "list": {}, "resume": {}}
        return {
            "protocolVersion": version,
            "capabilities": {
                "session": session_caps,
                "sessionCapabilities": session_caps,
                "promptCapabilities": {"text": {}},
            },
            # Dual-form info: the v2 docs read `info`; the reference SDK and
            # v1-era clients read `agentInfo`. Extra keys are ignored, so emit
            # both and never starve one wire of the agent's identity.
            "info": {"name": self.name, "title": self.name, "version": self.version},
            "agentInfo": {"name": self.name, "title": self.name, "version": self.version},
            # Advertised UNCONDITIONALLY, including when credentials are already
            # on disk. The ACP registry's CI verifier reads this field on a cold
            # `initialize` and rejects an agent that returns none, and a client
            # needs to know how a user could RE-authenticate after a logout or an
            # expiry. Answering "[] because this machine happens to be logged in"
            # would make the entry pass locally and fail in CI.
            "authMethods": self.auth_methods(),
            # Both spellings: v2 nests under `capabilities`, the v1-era clients
            # (and the registry validator) read `agentCapabilities`.
            "agentCapabilities": {"auth": {"logout": {}}},
        }

    # ---- authentication ----------------------------------------------------
    #
    # AitherIdentity device flow (RFC 8628) — the platform's own sign-in, not an
    # API key. Two methods are advertised because ACP clients differ in what they
    # can host:
    #   * `aither-device` (type "agent")    — WE run the flow: open the browser,
    #     poll the token endpoint, persist to ~/.aither/auth.json. Needs no
    #     terminal, which is what a GUI editor can offer.
    #   * `aither-terminal` (type "terminal") — the client RE-LAUNCHES us as
    #     `adk acp login` in a real terminal. The args below replace our normal
    #     ones for that one invocation (per AUTHENTICATION.md), so they must name
    #     a command that exists; `adk acp login` is asserted by the CLI tests.
    #
    # The registry accepts only these two types. An agent that advertises, say,
    # an env-var method is silently ignored by its verifier — it reads as "no
    # auth" while looking configured, so keep both entries typed explicitly.

    #: Method id → the ACP auth method type. Single source for the advertisement
    #: and the `authenticate` dispatch, so the two can never disagree.
    AUTH_METHODS: tuple[tuple[str, str, str, str], ...] = (
        (
            "aither-device",
            "agent",
            "Sign in with Aitherium",
            "Opens your browser to approve this device (AitherIdentity device flow).",
        ),
        (
            "aither-terminal",
            "terminal",
            "Sign in from a terminal",
            "Runs `adk acp login` interactively and prints a code to enter.",
        ),
    )

    def auth_methods(self) -> list[dict[str, Any]]:
        """The `authMethods` advertisement, in registry-validated shape."""
        methods: list[dict[str, Any]] = []
        for mid, mtype, name, description in self.AUTH_METHODS:
            if mtype not in AUTH_METHOD_TYPES:
                # Fail LOUD here rather than shipping an advertisement the
                # registry silently ignores: a bad type reads as "no auth" in
                # CI while every local probe shows a populated authMethods.
                raise RuntimeError(
                    f"auth method {mid!r} has type {mtype!r}; the ACP registry "
                    f"accepts only {sorted(AUTH_METHOD_TYPES)}"
                )
            entry: dict[str, Any] = {
                "id": mid,
                "name": name,
                "description": description,
                "type": mtype,
            }
            if mtype == "terminal":
                # Replaces the default args for the setup launch only.
                entry["args"] = ["acp", "login"]
            methods.append(entry)
        return methods

    async def handle_auth_login(self, params: dict[str, Any]) -> dict[str, Any]:
        """`authenticate` — run the AitherIdentity device flow for real.

        Returns `{}` on success per the spec. Every failure path raises, because
        an authenticate that returns `{}` without a token tells the client it may
        proceed and then fails at the first prompt with an unrelated error.
        """
        method_id = pick(params, "methodId", "method_id", default="")
        known = {mid for mid, _t, _n, _d in self.AUTH_METHODS}
        if method_id and method_id not in known:
            raise RpcError(
                INVALID_PARAMS,
                f"Unknown authentication method: {method_id!r} "
                f"(advertised: {', '.join(sorted(known))})",
            )

        from adk.auth import AuthError, begin_device_login, finish_device_login

        try:
            challenge = await begin_device_login()
        except AuthError as e:
            raise RpcError(AUTH_REQUIRED, f"Could not start device login: {e}") from e
        except Exception as e:  # noqa: BLE001 — network/DNS/proxy all land here
            raise RpcError(AUTH_REQUIRED, f"Could not reach AitherIdentity: {e}") from e

        # Tell the human what to do on BOTH channels: stderr is what a terminal
        # client shows, and the notification is what a GUI client can render.
        # A device flow that prints nowhere is a silent 10-minute hang.
        prompt = (
            f"To sign in, open {challenge.verification_uri} "
            f"and enter the code {challenge.user_code}"
        )
        logger.warning("acp_server: %s", prompt)
        try:
            print(prompt, file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001 — stderr may be closed by the host
            logger.debug("acp_server: stderr unavailable for the auth prompt")
        if self.auth_method_is(method_id, "agent"):
            # Agent Auth: WE open the browser. Best-effort — a headless box has
            # no browser, and the code printed above is still actionable.
            try:
                import webbrowser

                webbrowser.open(challenge.verification_uri_complete)
            except Exception as e:  # noqa: BLE001
                logger.debug("acp_server: could not open a browser: %s", e)

        try:
            await finish_device_login(challenge)
        except AuthError as e:
            raise RpcError(AUTH_REQUIRED, f"Device login failed: {e}") from e
        self._authenticated = True
        return {}

    def auth_method_is(self, method_id: str, mtype: str) -> bool:
        """True when *method_id* is advertised with type *mtype*.

        An empty/absent id means "the default", which per AUTHENTICATION.md is
        the agent type — so a client that omits `methodId` still gets a browser.
        """
        if not method_id:
            return mtype == "agent"
        for mid, t, _n, _d in self.AUTH_METHODS:
            if mid == method_id:
                return t == mtype
        return False

    async def handle_auth_logout(self, params: dict[str, Any]) -> dict[str, Any]:
        """`logout` — drop the stored credentials. Advertised via auth.logout."""
        self._authenticated = False
        try:
            from adk.auth import AuthStore

            # "portal" is the profile finish_device_login writes; clearing any
            # other name would return {} having revoked nothing, which reads to
            # the client as a successful logout.
            AuthStore().clear_profile("portal")
        except Exception as e:  # noqa: BLE001
            raise RpcError(INTERNAL_ERROR, f"Could not clear credentials: {e}") from e
        return {}

    async def handle_new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = params.get("cwd") or os.getcwd()
        session_id = f"adk-{uuid.uuid4().hex[:12]}"
        session = ACPSession(
            session_id,
            cwd,
            model=params.get("model"),
            mcp_servers=params.get("mcpServers") or params.get("mcp_servers"),
            additional_directories=(
                params.get("additionalDirectories")
                or params.get("additional_directories")
            ),
        )
        self.sessions[session_id] = session
        await self._advertise_commands(session)
        return {"sessionId": session_id}

    async def handle_list_sessions(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = params.get("cwd")
        sessions = []
        for sid, s in self.sessions.items():
            if cwd and s.cwd != cwd:
                continue
            info: dict[str, Any] = {"sessionId": sid, "cwd": s.cwd}
            if s.title:
                info["title"] = s.title
            info["updatedAt"] = s.updated_at
            sessions.append(info)
        return {"sessions": sessions}

    async def _apply_session_params(
        self, session: ACPSession, params: dict[str, Any]
    ) -> None:
        """Merge optional cwd / mcpServers / additionalDirectories on resume."""
        if params.get("cwd"):
            session.cwd = params["cwd"]
        if params.get("mcpServers") or params.get("mcp_servers"):
            session.mcp_servers = list(
                params.get("mcpServers") or params.get("mcp_servers")
            )
        if params.get("additionalDirectories") or params.get("additional_directories"):
            session.additional_directories = list(
                params.get("additionalDirectories")
                or params.get("additional_directories")
            )
        session.touch()

    async def handle_resume_session(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId") or params.get("session_id") or ""
        session = self.sessions.get(session_id)
        if session is None:
            raise RpcError(INVALID_PARAMS, f"unknown sessionId {session_id!r}")
        await self._apply_session_params(session, params)
        # replayFrom.start -> replay history so the client rebuilds its view.
        if params.get("replayFrom"):
            for msg in session.history:
                await self.session_update(
                    session_id,
                    user_message_update(session.next_message_id(),
                                       text_message(msg.get("content", ""))),
                )
        return {}

    async def handle_load_session(self, params: dict[str, Any]) -> dict[str, Any]:
        """Legacy session/load (v1 alias for resume; recreates a missing session)."""
        session_id = params.get("sessionId") or params.get("session_id") or ""
        if session_id not in self.sessions:
            self.sessions[session_id] = ACPSession(
                session_id, params.get("cwd") or os.getcwd()
            )
        return {}

    async def handle_close_session(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId") or params.get("session_id") or ""
        session = self.sessions.pop(session_id, None)
        if session is None:
            raise RpcError(INVALID_PARAMS, f"unknown sessionId {session_id!r}")
        session.cancel_event.set()
        self._answer_pending_permissions(session_id)
        return {}

    async def handle_delete_session(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId") or params.get("session_id") or ""
        session = self.sessions.pop(session_id, None)
        if session is not None:
            session.cancel_event.set()
            self._answer_pending_permissions(session_id)
        # Deleting a nonexistent session succeeds silently (spec).
        return {}

    async def handle_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId") or params.get("session_id") or ""
        session = self.sessions.get(session_id)
        if session is not None:
            session.cancel_event.set()
            # Any permission request blocked on the editor is cancelled: the
            # bridge turns a None outcome into idle(cancelled).
            self._answer_pending_permissions(session_id)
        return {}

    def _answer_pending_permissions(self, session_id: str) -> None:
        """Resolve the SESSION's in-flight session/request_permission as cancelled.

        Scoped by session: a cancel/close of one session must never resolve
        another (concurrent) session's pending permission, which would wrongly
        end its turn with idle(cancelled).
        """
        for rid, (owner, fut) in list(self._pending_requests.items()):
            if owner != session_id:
                continue
            if not fut.done():
                fut.set_result(None)
            self._pending_requests.pop(rid, None)

    async def handle_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId") or params.get("session_id") or ""
        session = self.sessions.get(session_id)
        if session is None:
            raise RpcError(INVALID_PARAMS, f"unknown sessionId {session_id!r}")
        if session.active_turn is not None and not session.active_turn.done():
            raise RpcError(INVALID_PARAMS, f"session {session_id!r} already running a prompt")

        text = extract_prompt_text(params.get("prompt"))
        session.cancel_event.clear()
        session.history.append({"role": "user", "content": text})
        session.touch()
        message_id = session.next_message_id()
        # The spec: acknowledge the prompt IMMEDIATELY, then run the turn as a
        # background task that streams session/update notifications and ends
        # with an idle state_update carrying the authoritative stop reason.
        # The reference SDK validates the prompt RESPONSE as a PromptResponse
        # that REQUIRES stopReason (a v1 relic); the v2-docs shape is `{}`.
        # Return a placeholder stop reason to satisfy both — the idle update is
        # what any client must treat as authoritative.
        session.active_turn = asyncio.create_task(
            self._drive_turn(session, text, message_id)
        )
        return {"stopReason": "end_turn"}

    # ---- the turn (agent driving + permission bridge) ---------------------

    async def _call_agent(
        self, session: ACPSession, text: str, decisions: Optional[list[dict]]
    ) -> Any:
        """Run the agent for one step of the turn.

        *decisions* is None on the first pass (fresh ``run``) and a list of
        allow/deny decisions on every resume (paused turn continuing). A turn
        may pause repeatedly on different gated tools.
        """
        if decisions is not None:
            resume = getattr(self.agent, "resume", None)
            if resume is None:
                raise RpcError(INTERNAL_ERROR, "agent paused for approval but has no resume()")
            return await resume(session.session_id, decisions)
        run = getattr(self.agent, "run", None)
        if run is None:
            raise RpcError(INTERNAL_ERROR, "agent has no run()")
        try:
            return await run(text, session_id=session.session_id)
        except TypeError:
            # An arbitrary agent's run(prompt) may not accept session_id.
            return await run(text)

    async def _request_permission(
        self, session: ACPSession, pending_item: dict[str, Any]
    ) -> tuple[str, Optional[str]]:
        """Ask the editor to allow/deny one pending tool call.

        Returns ``(decision, kind)`` where decision is ``allow`` / ``deny`` /
        ``cancelled``. Dual-form params (v2 ``subject`` + flat ``toolCall``)
        so both the current v2 docs and the older reference-SDK wire parse.
        """
        tool = str(pending_item.get("tool") or "unknown")
        tool_id = str(pending_item.get("tool_use_id") or tool)
        args = pending_item.get("args") or {}

        self._rpc_id += 1
        rid = self._rpc_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_requests[rid] = (session.session_id, future)

        description = f"The agent wants to call {tool}."
        if args:
            description += f"\n{json.dumps(args, default=str)[:500]}"
        await self._write(make_request(rid, "session/request_permission", {
            "sessionId": session.session_id,
            "session_id": session.session_id,
            "title": f"Allow {tool}?",
            "description": description,
            "subject": {"type": "tool_call", "toolCall": {"toolCallId": tool_id}},
            "toolCall": {"toolCallId": tool_id},
            "options": [
                {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "allow-always", "name": "Allow always", "kind": "allow_always"},
                {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                {"optionId": "reject-always", "name": "Reject always", "kind": "reject_always"},
            ],
        }))

        try:
            outcome = await asyncio.wait_for(future, timeout=300.0)
        except asyncio.TimeoutError:
            self._pending_requests.pop(rid, None)
            return "deny", "reject_once"

        if outcome is None:  # cancelled (session/cancel or close)
            return "cancelled", None

        # v2: result.outcome = {outcome: selected|cancelled, optionId?}
        inner = outcome.get("outcome") if isinstance(outcome, dict) else None
        if isinstance(inner, dict):
            if inner.get("outcome") == "cancelled":
                return "cancelled", None
            if inner.get("outcome") == "selected":
                option_id = pick(inner, "optionId", "option_id", default="")
                kind = "allow_always" if str(option_id).endswith("always") else "allow_once"
                return ("allow" if str(option_id).startswith("allow") else "deny", kind)
        # Legacy v1: approval_response -> {approved: bool}
        if isinstance(outcome, dict) and "approved" in outcome:
            return ("allow" if outcome["approved"] else "deny", "allow_once")
        return "deny", "reject_once"

    async def _bridge_permissions(
        self, session: ACPSession, pending: list[dict]
    ) -> Optional[list[dict]]:
        """Ask for every pending tool; return resume decisions, or None if cancelled."""
        decisions: list[dict] = []
        for item in pending:
            decision, _kind = await self._request_permission(session, item)
            tool_id = str(item.get("tool_use_id") or item.get("tool") or "tool")
            await self.session_update(
                session.session_id,
                tool_call_update(
                    tool_id,
                    title=str(item.get("tool") or tool_id),
                    kind="other",
                    status="cancelled" if decision == "deny" else "in_progress",
                ),
            )
            if decision == "cancelled":
                return None
            decisions.append({
                "tool_use_id": str(item.get("tool_use_id") or item.get("tool")),
                "tool": str(item.get("tool")),
                "result": decision,
            })
        return decisions

    async def _drive_turn(
        self, session: ACPSession, text: str, message_id: str
    ) -> None:
        """Run one prompt through the full v2 lifecycle to a terminal idle update.

        The agent's own approval gate drives the loop: on ``requires_action``
        the turn pauses, we ask the editor via session/request_permission, and
        resume() re-enters the agent with the allow/deny decisions.
        """
        try:
            await self.session_update(
                session.session_id,
                user_message_update(message_id, text_message(text)),
            )
            await self.session_update(
                session.session_id, state_update("running")
            )

            decisions: Optional[list[dict]] = None
            result: Any = None
            while True:
                if session.cancel_event.is_set():
                    await self._finish_idle(session, "cancelled")
                    return
                result = await self._call_agent(session, text, decisions)
                if getattr(result, "requires_action", False) and getattr(result, "pending", None):
                    await self.session_update(
                        session.session_id, state_update("requires_action")
                    )
                    decisions = await self._bridge_permissions(session, result.pending)
                    if decisions is None:
                        await self._finish_idle(session, "cancelled")
                        return
                    continue
                break

            if session.cancel_event.is_set():
                await self._finish_idle(session, "cancelled")
                return

            content = _result_content(result)
            await self.session_update(
                session.session_id,
                agent_message_chunk(content, message_id=message_id),
            )
            session.history.append({"role": "assistant", "content": content})

            tokens = getattr(result, "tokens_used", 0) or 0
            prompt_tokens = getattr(result, "prompt_tokens", 0) or 0
            completion_tokens = getattr(result, "completion_tokens", 0) or 0
            if tokens or prompt_tokens or completion_tokens:
                await self.session_update(
                    session.session_id,
                    usage_update(prompt_tokens or tokens, completion_tokens or tokens),
                )

            stop = _map_stop_reason(getattr(result, "finish_reason", None))
            await self._finish_idle(session, stop)
        except asyncio.CancelledError:
            await self._finish_idle(session, "cancelled")
        except RpcError:
            await self._finish_idle(session, "refusal")
        except Exception as e:  # noqa: BLE001 — report to the editor, don't die
            logger.exception("acp_server: turn failed for session %s", session.session_id)
            try:
                await self.session_update(
                    session.session_id,
                    agent_message_chunk(f"[agent error] {e}"),
                )
            except Exception as exc:  # noqa: BLE001 — best-effort after a failure
                logger.debug("acp_server: could not report turn failure: %s", exc)
            await self._finish_idle(session, "refusal")
        finally:
            session.active_turn = None

    async def _finish_idle(self, session: ACPSession, stop_reason: str) -> None:
        """Terminal transition: a stop reason MUST accompany idle."""
        session.touch()
        try:
            await self.session_update(
                session.session_id,
                state_update("idle", stop_reason=stop_reason),
            )
        except Exception:  # noqa: BLE001 — the connection may already be closed
            logger.exception(
                "acp_server: failed to emit idle(%s) for session %s",
                stop_reason, session.session_id,
            )

    # ---- dispatch ---------------------------------------------------------

    @property
    def _routes(self) -> dict[str, Callable[[dict], Awaitable[dict]]]:
        return {
            "initialize": self.handle_initialize,
            "auth/login": self.handle_auth_login,
            "auth/logout": self.handle_auth_logout,
            # The SPEC names these `authenticate` / `logout`; `auth/*` is our v2
            # namespaced spelling. Both are routed because the registry's
            # verifier and the reference clients use the bare names.
            "authenticate": self.handle_auth_login,
            "logout": self.handle_auth_logout,
            "session/new": self.handle_new_session,
            "session/list": self.handle_list_sessions,
            "session/resume": self.handle_resume_session,
            "session/load": self.handle_load_session,  # v1 legacy alias
            "session/close": self.handle_close_session,
            "session/delete": self.handle_delete_session,
            "session/prompt": self.handle_prompt,
            "session/cancel": self.handle_cancel,
        }

    async def _dispatch(self, msg: dict[str, Any]) -> Optional[dict[str, Any]]:
        msg_id = msg.get("id")
        method = msg.get("method")
        if method is None:
            # A response to one of OUR outbound requests (session/request_permission).
            if msg_id is not None and msg_id in self._pending_requests:
                _, fut = self._pending_requests.pop(msg_id)
                if "error" in msg:
                    fut.set_result({"outcome": {"outcome": "cancelled"}})
                else:
                    fut.set_result(msg.get("result") or {})
            return None
        handler = self._routes.get(method)
        if handler is None:
            if msg_id is None:
                return None  # unknown notification: ignore
            return make_error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        try:
            result = await handler(msg.get("params") or {})
        except RpcError as e:
            return make_error(msg_id, e.code, e.message)
        except Exception as e:  # noqa: BLE001
            logger.exception("acp_server: handler %s failed", method)
            return make_error(msg_id, INTERNAL_ERROR, f"Internal error: {e}")
        if msg_id is None:
            return None  # it was a notification
        return make_response(msg_id, result)

    async def _serve_batch(self, batch: list[Any]) -> None:
        """JSON-RPC 2.0 batch: process every entry, reply with an array of
        responses (no response at all when every entry is a notification)."""
        responses: list[dict[str, Any]] = []
        for entry in batch:
            if not isinstance(entry, dict):
                responses.append(make_error(None, INVALID_REQUEST, "Invalid Request"))
                continue
            response = await self._dispatch(entry)
            if response is not None:
                responses.append(response)
        if responses:
            await self._write(responses)

    async def serve(self, reader: Any, writer: Any) -> None:
        """Read JSON-RPC frames from *reader*, answer on *writer*, until EOF."""
        self._writer = writer
        while True:
            line = await reader.readline()
            if not line:
                break
            raw = line.decode("utf-8", errors="replace").strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as e:
                await self._write(make_error(None, PARSE_ERROR, f"Parse error: {e}"))
                continue
            if isinstance(msg, list):
                await self._serve_batch(msg)
            elif isinstance(msg, dict):
                response = await self._dispatch(msg)
                if response is not None:
                    await self._write(response)
            else:
                await self._write(make_error(None, INVALID_REQUEST, "Invalid Request"))


# ---- helpers ---------------------------------------------------------------


def _result_content(result: Any) -> str:
    """The agent's reply text from any result shape (content / output / str)."""
    for attr in ("content", "output"):
        value = getattr(result, attr, None)
        if isinstance(value, str):
            return value
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str)
    except (TypeError, ValueError):
        return str(result)


def _map_stop_reason(finish_reason: Any) -> str:
    """Map an agent finish reason onto the ACP stopReason vocabulary."""
    if finish_reason in STOP_REASONS:
        return str(finish_reason)
    if finish_reason in ("stop", "end_turn", "tool_use", "tool_calls", "requires_action"):
        return "end_turn"
    if finish_reason in ("length", "max_tokens"):
        return "max_tokens"
    if finish_reason == "cancelled":
        return "cancelled"
    if finish_reason == "refusal":
        return "refusal"
    return "end_turn"


# The Windows-safe stdio adapters live in adk.stdio_compat so the ACP and MCP
# stdio servers share ONE proven implementation (see that module for why
# loop.connect_read_pipe cannot be used on stdin under Windows' Proactor loop).
_ThreadStdinReader = ThreadStdinReader
_ThreadStdoutWriter = ThreadStdoutWriter


async def serve_stdio(agent: Any, *, name: Optional[str] = None, version: str = "1.0.0") -> None:
    """Serve *agent* over ACP on stdin/stdout (the editor-facing entrypoint)."""
    server = ACPServer(agent, name=name, version=version)
    await server.serve(_ThreadStdinReader(), _ThreadStdoutWriter())
