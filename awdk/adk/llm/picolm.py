"""PicoLM LLM provider — pure C edge inference via subprocess.

PicoLM is a zero-dependency C binary (~80KB, 45MB RAM) for running GGUF
models on resource-constrained devices. Communication is stdin/stdout
with ChatML prompt format.

Usage::

    from adk.llm.picolm import PicoLMProvider

    provider = PicoLMProvider(
        binary="/usr/local/bin/picolm",
        model="/models/phi-2.gguf",
    )
    resp = await provider.chat([Message(role="user", content="Hello")])

Environment variables:
    PICOLM_BINARY  — Path to the picolm executable
    PICOLM_MODEL   — Path to the .gguf model file
    PICOLM_THREADS — Number of CPU threads (default: 4)
    PICOLM_CACHE   — Directory for KV cache files (optional)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import AsyncIterator

from .base import (
    LLMProvider,
    LLMResponse,
    Message,
    StreamChunk,
    ToolCall,
    _timer,
)

logger = logging.getLogger("adk.llm.picolm")


def _build_chatml(messages: list[Message]) -> str:
    """Convert Message objects to ChatML format for PicoLM stdin.

    PicoLM expects ChatML::

        <|im_start|>system
        You are a helpful assistant.<|im_end|>
        <|im_start|>user
        Hello<|im_end|>
        <|im_start|>assistant

    The trailing assistant tag prompts the model to generate a response.
    """
    parts = []
    for msg in messages:
        parts.append(f"<|im_start|>{msg.role}\n{msg.content}<|im_end|>")
    parts.append("<|im_start|>assistant\n")
    return "\n".join(parts)


class PicoLMProvider(LLMProvider):
    """Edge LLM inference via PicoLM subprocess.

    PicoLM runs as a subprocess: prompt on stdin, response on stdout.
    Supports KV caching via MD5-hashed system prompts and JSON mode
    for tool calling.
    """

    def __init__(
        self,
        binary: str = "",
        model: str = "",
        threads: int | None = None,
        cache_dir: str = "",
        timeout: float = 120.0,
    ):
        self.binary = binary or os.getenv("PICOLM_BINARY", "")
        self.model = model or os.getenv("PICOLM_MODEL", "")
        self.threads = threads if threads is not None else int(os.getenv("PICOLM_THREADS", "4"))
        self.cache_dir = cache_dir or os.getenv("PICOLM_CACHE", "")
        self.timeout = timeout
        self.default_model = os.path.basename(self.model) if self.model else "picolm"

    async def chat(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        if not self.binary:
            raise RuntimeError(
                "PicoLM binary not configured. Set PICOLM_BINARY env var."
            )
        if not self.model:
            raise RuntimeError(
                "PicoLM model not configured. Set PICOLM_MODEL env var."
            )

        chatml = _build_chatml(messages)

        # Build command
        cmd = [
            self.binary, self.model,
            "-n", str(max_tokens),
            "-t", str(temperature),
            "-j", str(self.threads),
        ]

        # JSON mode for tool calling
        if tools:
            cmd.append("--json")

        # KV cache keyed on system prompt hash
        if self.cache_dir:
            system_text = ""
            for msg in messages:
                if msg.role == "system":
                    system_text = msg.content
                    break
            if system_text:
                cache_hash = hashlib.md5(system_text.encode()).hexdigest()[:12]
                cache_path = os.path.join(self.cache_dir, f"{cache_hash}.kvc")
                cmd.extend(["--cache", cache_path])

        start = _timer()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=chatml.encode("utf-8")),
                timeout=self.timeout,
            )

            stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                raise RuntimeError(
                    f"PicoLM exited with code {proc.returncode}: {stderr_text}"
                )

            if stderr_text:
                logger.debug("PicoLM stderr: %s", stderr_text)

        except asyncio.TimeoutError:
            raise RuntimeError(f"PicoLM timed out after {self.timeout}s")
        except FileNotFoundError:
            raise RuntimeError(
                f"PicoLM binary not found at '{self.binary}'. "
                "Ensure PICOLM_BINARY points to the picolm executable."
            )

        latency = _timer() - start

        # Parse response
        content = stdout_text
        tool_calls: list[ToolCall] = []
        finish_reason = "stop"

        # JSON mode: try to parse tool calls from output
        if tools and content.strip().startswith("{"):
            try:
                parsed = json.loads(content)
                if "tool_calls" in parsed:
                    for tc in parsed["tool_calls"]:
                        fn = tc.get("function", tc)
                        tool_calls.append(ToolCall(
                            id=fn.get("name", ""),
                            name=fn.get("name", ""),
                            arguments=fn.get("arguments", {}),
                        ))
                    content = parsed.get("content", "")
                    finish_reason = "tool_calls"
                elif "name" in parsed:
                    # Single function call format
                    tool_calls.append(ToolCall(
                        id=parsed["name"],
                        name=parsed["name"],
                        arguments=parsed.get("arguments", {}),
                    ))
                    content = ""
                    finish_reason = "tool_calls"
            except json.JSONDecodeError:
                pass  # Not valid JSON, treat as plain text

        # Token estimation — PicoLM doesn't report counts
        est_prompt = len(chatml) // 4
        est_completion = len(content) // 4

        return LLMResponse(
            content=content,
            model=self.default_model,
            tokens_used=est_prompt + est_completion,
            prompt_tokens=est_prompt,
            completion_tokens=est_completion,
            latency_ms=latency,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

    async def chat_stream(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """PicoLM doesn't support true streaming — wrap chat() as single chunk."""
        resp = await self.chat(messages, model, temperature, max_tokens, tools, **kwargs)
        yield StreamChunk(
            content=resp.content,
            done=True,
            model=resp.model,
            tool_calls=resp.tool_calls,
        )

    async def list_models(self) -> list[str]:
        """Return the configured model (PicoLM runs one model at a time)."""
        if self.model:
            return [self.default_model]
        return []

    async def health_check(self) -> bool:
        """Check if PicoLM binary and model are available."""
        if not self.binary or not self.model:
            return False
        return os.path.isfile(self.binary) and os.path.isfile(self.model)
