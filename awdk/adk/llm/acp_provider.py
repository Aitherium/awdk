"""LLMProvider that drives an external ACP agent as the model for AitherAgent.

``adk backend add acp --command claude`` registers an external Agent Client
Protocol server (claude-agent-acp, codex-acp, gemini-cli, ...) as an LLM
backend. This provider is the glue: it speaks the ACP **client** role against
that subprocess, so AitherAgent's own memory, faculties and approval surface
ride on top of the external agent's loop.

How the mapping works
---------------------
* One long-lived ACP session per provider instance. Each ``chat()`` replays
  only the message turns the caller has not yet delivered, so the external
  agent's session history stays in sync with the caller's ``messages`` list;
  a conversation reset (message count going backwards) starts a fresh session.
* Assistant turns are the external agent's OWN output — they already live in
  its session — so they are skipped rather than re-sent. System, user and tool
  turns become ACP content blocks.
* The returned :class:`LLMResponse` carries **no tool calls**: the external
  agent executes tools inside its own autonomous loop. AitherAgent's ReAct
  loop sees a plain completed turn, which is exactly the "memory + faculties on
  top of the external agent's loop" contract. (If the external agent needs
  AitherAgent-side tools, that is the ``session/request_permission`` /
  ``fs/*`` surface — future work, not advertised yet.)

Fail loud: a missing command raises at construction, so a misconfigured
backend can never silently degrade into an inert provider.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncIterator, Callable

from adk.llm.base import LLMProvider, LLMResponse, Message, StreamChunk


def _render_block(m: Message) -> dict[str, Any]:
    """Serialize one caller message into an ACP prompt content block."""
    if m.role == "tool":
        label = f"[Tool result{': ' + m.name if m.name else ''}]: "
        return {"type": "text", "text": label + (m.content or "")}
    if m.role == "system":
        return {"type": "text", "text": m.content or "", "metadata": {"role": "system"}}
    return {"type": "text", "text": m.content or ""}


class ACPProvider(LLMProvider):
    """Drive an external ACP agent (``<command> [args...]``) as an LLM provider."""

    name = "acp"

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        model: str = "acp",
        cwd: str | None = None,
        approval_callback: Callable[[Any], bool] | None = None,
        connect_timeout: float = 30.0,
        drain_timeout: float = 120.0,
    ) -> None:
        if not command or not str(command).strip():
            raise ValueError(
                "ACPProvider requires a command for the external ACP agent "
                "(e.g. 'claude' for claude-agent-acp). Configure one with "
                "`adk backend add acp --command <cmd>`."
            )
        self.command = str(command).strip()
        self.args = list(args or [])
        self.model = model
        self.cwd = cwd or os.getcwd()
        self._approval_callback = approval_callback
        self._connect_timeout = connect_timeout
        # An external agent turn (tool loops, sub-agents) takes seconds to
        # minutes; the client must wait for the terminal idle, not a 2s settle
        # (measured: a text-only turn came back empty at drain_timeout=2.0).
        self._drain_timeout = drain_timeout
        self._client: Any = None
        self._session_id: str | None = None
        self._delivered = 0
        self._lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------

    async def _ensure_ready(self, message_count: int) -> None:
        """Connect the client and open a session, resyncing if the caller reset
        the conversation (``message_count`` went backwards)."""
        if self._client is None:
            from adk.acp import ACPClient  # local import: keep acp off the hot path

            client = ACPClient(
                command=self.command,
                args=self.args,
                approval_callback=self._approval_callback,
            )
            await asyncio.wait_for(client.connect(), timeout=self._connect_timeout)
            await asyncio.wait_for(client.initialize(), timeout=self._connect_timeout)
            self._client = client
        if self._session_id is None or message_count < self._delivered:
            if self._session_id is not None:
                await self._client.delete_session(self._session_id)
            self._session_id = await self._client.create_session(cwd=self.cwd)
            self._delivered = 0

    def _build_blocks(
        self, messages: list[Message]
    ) -> tuple[int, list[dict[str, Any]]]:
        """Compute the undelivered slice and its prompt blocks.

        Returns ``(new_delivered, blocks)``. Assistant turns are skipped (the
        external agent already has them in its session). Never returns an empty
        block list: a retry of an identical message list still gets the last
        message sent so the caller always receives a response.
        """
        slice_ = messages[self._delivered :]
        blocks = [_render_block(m) for m in slice_ if m.role != "assistant"]
        if not blocks:
            if slice_:
                blocks = [_render_block(slice_[-1])]
            elif messages:
                blocks = [_render_block(messages[-1])]
            else:
                blocks = [{"type": "text", "text": ""}]
        return self._delivered + len(slice_), blocks

    async def disconnect(self) -> None:
        """Close the external agent subprocess and drop the session."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            finally:
                self._client = None
                self._session_id = None
                self._delivered = 0

    # -- LLMProvider --------------------------------------------------------

    async def chat(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        top_p: float | None = None,
        repetition_penalty: float | None = None,
        cache: bool = False,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send the undelivered turns to the external agent, aggregate its reply.

        The external agent owns tool execution; the returned response carries no
        ``tool_calls`` and ``finish_reason`` reflects the agent's stop reason.
        """
        async with self._lock:
            await self._ensure_ready(len(messages))
            delivered, blocks = self._build_blocks(messages)
            result = await self._client.prompt(
                self._session_id, "", blocks=blocks, drain_timeout=self._drain_timeout
            )
            self._delivered = delivered
        return LLMResponse(
            content=result.text,
            model=self.model,
            tokens_used=result.usage.input_tokens + result.usage.output_tokens,
            prompt_tokens=result.usage.input_tokens,
            completion_tokens=result.usage.output_tokens,
            finish_reason=result.stop_reason,
        )

    async def chat_stream(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        top_p: float | None = None,
        repetition_penalty: float | None = None,
        cache: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Stream the external agent's reply live from ``session/update`` chunks."""
        async with self._lock:
            await self._ensure_ready(len(messages))
            delivered, blocks = self._build_blocks(messages)
            async for update in self._client.stream_prompt(
                self._session_id, "", blocks=blocks, drain_timeout=self._drain_timeout
            ):
                if update.get("sessionUpdate") == "agent_message_chunk":
                    content = update.get("content") or {}
                    text = content.get("text") if isinstance(content, dict) else None
                    if text:
                        yield StreamChunk(content=text, model=self.model)
            self._delivered = delivered
        yield StreamChunk(content="", done=True, model=self.model)

    async def list_models(self) -> list[str]:
        """The single model name this provider routes to."""
        return [self.model]

    async def health_check(self) -> bool:
        """Reachability probe: actually spawn the agent and initialize."""
        try:
            async with self._lock:
                await self._ensure_ready(0)
            return True
        except Exception:
            return False
