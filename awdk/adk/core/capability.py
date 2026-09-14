"""Capability tokens. Default-deny.

In-process: simple enum + context manager.
Cross-process: HMAC-signed token (see ``adk.core.capability.sign`` —
not implemented in slice A; stub for the contract).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from typing import Iterator


class Capability(str, Enum):
    """Canonical capability tokens.

    Mirrors the AitherOS capability-token taxonomy so agents drop in
    without remapping. Add to this enum, not ad-hoc strings.
    """

    LLM_INFERENCE = "llm_inference"
    LLM_EMBED = "llm_embed"
    # Non-LLM structured-data foundation models (TabFM tabular, TimesFM forecasting).
    STRUCTURED_INFERENCE = "structured_inference"

    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"

    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    SHELL_EXEC = "shell_exec"

    NET_HTTP = "net_http"
    NET_WEB_SEARCH = "net_web_search"

    AGENT_CALL = "agent_call"
    AGENT_SPAWN = "agent_spawn"

    SECRET_READ = "secret_read"


class CapabilityDenied(PermissionError):
    """Raised when a tool or agent action lacks a required capability."""


class CapabilityContext:
    """A bundle of granted capabilities for a single agent run.

    Default-deny: an empty context grants nothing. Use :meth:`grant` to add.
    """

    __slots__ = ("_granted",)

    def __init__(self, granted: set[Capability] | None = None) -> None:
        self._granted: set[Capability] = set(granted) if granted else set()

    def grant(self, *caps: Capability) -> "CapabilityContext":
        self._granted.update(caps)
        return self

    def revoke(self, *caps: Capability) -> "CapabilityContext":
        self._granted.difference_update(caps)
        return self

    def has(self, cap: Capability) -> bool:
        return cap in self._granted

    def check(self, cap: Capability) -> None:
        if cap not in self._granted:
            raise CapabilityDenied(f"capability not granted: {cap.value}")

    def __contains__(self, cap: object) -> bool:
        return isinstance(cap, Capability) and cap in self._granted

    def __iter__(self) -> Iterator[Capability]:
        return iter(self._granted)

    def __repr__(self) -> str:
        granted = ",".join(sorted(c.value for c in self._granted)) or "none"
        return f"CapabilityContext({granted})"


_current: ContextVar[CapabilityContext | None] = ContextVar(
    "aither_adk_capability_ctx", default=None
)


def current_context() -> CapabilityContext:
    """Return the active capability context, or an empty (deny-all) one."""
    ctx = _current.get()
    return ctx if ctx is not None else CapabilityContext()


@contextmanager
def use_context(ctx: CapabilityContext) -> Iterator[CapabilityContext]:
    """Activate ``ctx`` as the current capability context for this scope."""
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def requires(*caps: Capability):
    """Decorator: enforce that the current context grants all ``caps``.

    Works on sync and async callables.
    """
    import asyncio
    import functools

    def deco(fn):
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                ctx = current_context()
                for cap in caps:
                    ctx.check(cap)
                return await fn(*args, **kwargs)

            awrapper.__aither_required_caps__ = tuple(caps)  # type: ignore[attr-defined]
            return awrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            ctx = current_context()
            for cap in caps:
                ctx.check(cap)
            return fn(*args, **kwargs)

        wrapper.__aither_required_caps__ = tuple(caps)  # type: ignore[attr-defined]
        return wrapper

    return deco
