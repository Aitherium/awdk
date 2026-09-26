"""The one bridge from the synchronous core to :class:`adk.core.model.ModelBackend`.

The vendored loop calls ``llm.chat(messages, max_tokens=, temperature=, extra=)``
and reads ``.content`` / ``.usage`` / ``.latency_s`` (the h30 ``LLM`` protocol,
re-exported here as :data:`LLM` for code written against h30). :class:`SyncModel`
implements that protocol over ``await backend.generate(...)``:

* with ``loop=`` (what :class:`adk.reasoning.solve.SolveRun` passes) the coroutine
  is scheduled onto that running event loop with ``run_coroutine_threadsafe``, so
  backends whose HTTP clients are bound to the caller's loop keep working, and
  steering is drained ON that loop (``asyncio.Queue`` is not thread-safe);
* without a loop it runs ``asyncio.run`` per call in the worker thread.

Every call is preceded by ``Governor.before_llm()`` and bounded by
``Budget.llm_timeout_s``; while waiting, a cancel is noticed within 0.2 s.
Any backend failure becomes :class:`LLMFailure` (the core falls back to its
explorer and declares the endpoint dead after 3 in a row); a stop is re-raised.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from typing import Any, Callable, Dict, List, Optional

from ._control import Governor, Stopped
from ._vendor.interfaces import LLM, ChatReply, LLMFailure
from ._vendor.memory import est_tokens

__all__ = ["SyncModel", "as_llm", "LLM", "ChatReply", "LLMFailure"]


class SyncModel:
    """h30 ``LLM`` over a ``ModelBackend``. Thread-safe per instance for one worker."""

    def __init__(
        self,
        backend: Any,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        governor: Optional[Governor] = None,
        timeout_s: float = 120.0,
        steering: Optional[Callable[[], List[str]]] = None,
        on_steer: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.backend = backend
        self.loop = loop
        self.governor = governor
        self.timeout_s = float(timeout_s)
        self.steering = steering
        self.on_steer = on_steer
        self.calls: int = 0
        self.errors: int = 0

    # -- the h30 LLM protocol ------------------------------------------------
    def chat(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int = 1000,
        temperature: float = 0.4,
        extra: Optional[Dict[str, Any]] = None,
    ) -> ChatReply:
        if self.governor is not None:
            self.governor.before_llm()
        t0 = time.perf_counter()
        coro = self._generate(list(messages), max_tokens, temperature, dict(extra or {}))
        try:
            resp = self._run(coro)
        except Stopped:
            raise
        except LLMFailure:
            self.errors += 1
            raise
        except Exception as exc:  # noqa: BLE001 - every backend failure is an LLMFailure
            self.errors += 1
            raise LLMFailure(
                "%s: %s" % (type(exc).__name__, str(exc)[:300]),
                int(getattr(exc, "status", 0) or getattr(exc, "status_code", 0) or 0),
            ) from exc
        text, sent = resp
        content = getattr(text, "text", None)
        if content is None:
            content = getattr(text, "content", "") or ""
        usage = dict(getattr(text, "usage", None) or {})
        prompt = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
        completion = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
        estimated = prompt == 0 and completion == 0
        if estimated:
            prompt = sum(est_tokens(str(m.get("content", ""))) for m in sent)
            completion = est_tokens(content)
        self.calls += 1
        if self.governor is not None:
            self.governor.charge_llm(prompt, completion, estimated)
        return ChatReply(
            content,
            {"prompt_tokens": prompt, "completion_tokens": completion},
            time.perf_counter() - t0,
        )

    # -- internals -------------------------------------------------------------
    async def _generate(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int,
        temperature: float,
        extra: Dict[str, Any],
    ) -> Any:
        if self.steering is not None:
            notes = [n for n in self.steering() if n]
            if notes:
                for n in notes:
                    if self.on_steer is not None:
                        self.on_steer(n)
                last = dict(messages[-1])
                last["content"] = "%s\n\nSTEERING from the operator (follow it):\n%s" % (
                    last.get("content", ""),
                    "\n".join("- " + n for n in notes),
                )
                messages[-1] = last
        from adk.core.model import Message

        msgs = [
            Message(role=str(m.get("role", "user")), content=str(m.get("content", "")))
            for m in messages
        ]
        resp = await self.backend.generate(
            msgs, temperature=temperature, max_tokens=max_tokens, **extra
        )
        return resp, messages

    def _run(self, coro: Any) -> Any:
        if self.loop is None:
            return asyncio.run(asyncio.wait_for(coro, timeout=self.timeout_s))
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                return fut.result(timeout=0.2)
            except concurrent.futures.TimeoutError:
                if self.governor is not None and self.governor.token.cancelled:
                    fut.cancel()
                    raise Stopped("cancelled") from None
                if time.monotonic() >= deadline:
                    fut.cancel()
                    raise LLMFailure("model call exceeded %.0fs" % self.timeout_s) from None


def as_llm(model: Any, **kw: Any) -> Any:
    """A ``ModelBackend`` (has ``generate``) becomes a :class:`SyncModel`; an
    object that already speaks the h30 ``LLM`` protocol (``chat``) and has no
    ``generate`` is returned unchanged."""
    if model is None:
        return None
    if hasattr(model, "generate"):
        return SyncModel(model, **kw)
    if hasattr(model, "chat"):
        return model
    raise TypeError("model must be an adk ModelBackend (generate) or an h30 LLM (chat)")
