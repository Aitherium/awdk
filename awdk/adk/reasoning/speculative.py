"""Speculative decoding wrapper — draft fast, verify smart.

True token-level speculative decoding requires kernel-level integration with
a transformer's KV cache. We can't (and don't want to) do that from
Python — but we can deliver the same *user-visible* property at the
*response* level:

1. The **draft** backend (FAST tier) produces ``k`` candidate responses
   cheaply.
2. The **verifier** backend (REASONING tier) is asked to pick the best
   candidate, with a single, very short prompt.
3. We return the verifier's pick.

For most everyday turns this delivers REASONING-tier quality at roughly
FAST-tier latency, because the verifier round-trip is tiny compared to a
fresh REASONING-tier completion. When the draft is good enough, the
verifier almost never has to regenerate from scratch.

Opt-in via env::

    AITHER_SPECULATIVE=1
    AITHER_SPECULATIVE_K=3        # default 3 candidate drafts

Or programmatically::

    from adk.reasoning import SpeculativeBackend, ReasoningRouter
    router = ReasoningRouter(...)
    backend = SpeculativeBackend.from_router(router, k=3)
    agent = Agent(name="...", model=backend, ...)
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from adk.core.logging import get_logger
from adk.core.model import Message, ModelBackend, ModelResponse

from .tiers import ModelTier, ReasoningRouter

_log = get_logger("adk.reasoning.speculative")


@dataclass(slots=True)
class SpeculativeStats:
    """Per-backend counters for observability."""

    calls: int = 0
    draft_only: int = 0  # verifier agreed with draft #0
    verifier_picked: int = 0
    verifier_regenerated: int = 0
    failures: int = 0


class SpeculativeBackend:
    """ModelBackend façade that drafts with FAST and verifies with REASONING."""

    name = "speculative"

    def __init__(
        self,
        draft: ModelBackend,
        verifier: ModelBackend,
        *,
        k: int = 3,
        verifier_max_tokens: int = 64,
    ) -> None:
        if k < 1:
            raise ValueError("k must be >= 1")
        self.draft = draft
        self.verifier = verifier
        self.k = k
        self.verifier_max_tokens = verifier_max_tokens
        # Direct access, not ``getattr(draft, "model", "?")``: ``model`` is a
        # total member of the ModelBackend contract (enforced by
        # tests/test_backend_conformance.py), so probing it could only ever
        # mask a genuinely broken backend behind a cosmetic "speculative(?|?)".
        self.model = f"speculative({draft.model}|{verifier.model})"
        self.stats = SpeculativeStats()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_router(
        cls,
        router: ReasoningRouter,
        *,
        k: int | None = None,
    ) -> "SpeculativeBackend":
        """Wire a speculative backend from a :class:`ReasoningRouter`."""

        draft = router.resolve(tier=ModelTier.FAST).backend
        verifier = router.resolve(tier=ModelTier.REASONING).backend
        if k is None:
            try:
                k = int(os.environ.get("AITHER_SPECULATIVE_K", "3"))
            except (TypeError, ValueError):
                k = 3
        return cls(draft, verifier, k=k)

    @classmethod
    def maybe_from_router(
        cls, router: ReasoningRouter
    ) -> "SpeculativeBackend | None":
        """Return a speculative backend iff ``AITHER_SPECULATIVE`` is truthy."""
        flag = (os.environ.get("AITHER_SPECULATIVE") or "").strip().lower()
        if flag not in ("1", "true", "yes", "on"):
            return None
        return cls.from_router(router)

    # ------------------------------------------------------------------
    # ModelBackend Protocol
    # ------------------------------------------------------------------

    async def generate(self, messages: list[Message], **opts: Any) -> ModelResponse:
        """Generate ``k`` drafts in parallel, then pick the best via verifier."""

        self.stats.calls += 1
        try:
            drafts = await self._draft_many(messages, **opts)
        except Exception as e:  # pragma: no cover — propagate verifier failure
            self.stats.failures += 1
            _log.warning("speculative.draft_failed", extra={"error": str(e)})
            raise

        if not drafts:
            self.stats.failures += 1
            return ModelResponse(
                text="", model=self.model, finish_reason="error"
            )

        if len(drafts) == 1:
            self.stats.draft_only += 1
            return drafts[0]

        try:
            pick = await self._verify(messages, drafts)
        except Exception as e:
            # Fall back to the first draft — degraded but not broken.
            self.stats.failures += 1
            _log.warning(
                "speculative.verify_failed_fallback_draft0",
                extra={"error": str(e)},
            )
            return drafts[0]

        if pick == 0:
            self.stats.draft_only += 1
        elif 0 < pick < len(drafts):
            self.stats.verifier_picked += 1
        else:
            # Verifier returned an out-of-range index → regenerate via
            # verifier directly.
            self.stats.verifier_regenerated += 1
            return await self.verifier.generate(messages, **opts)

        chosen = drafts[pick]
        return ModelResponse(
            text=chosen.text,
            model=self.model,
            finish_reason=chosen.finish_reason,
        )

    async def stream(
        self, messages: list[Message], **opts: Any
    ) -> AsyncIterator[str]:  # pragma: no cover — most agent loops use generate
        # Streaming + speculative is not coherent at the response level —
        # just stream from the verifier (the safer choice).
        async for chunk in self.verifier.stream(messages, **opts):
            yield chunk

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _draft_many(
        self, messages: list[Message], **opts: Any
    ) -> list[ModelResponse]:
        # Vary temperature slightly across drafts to encourage diversity.
        base_temp = float(opts.get("temperature", 0.7))
        tasks = []
        for i in range(self.k):
            local = dict(opts)
            local["temperature"] = min(1.5, base_temp + 0.1 * i)
            tasks.append(self.draft.generate(messages, **local))
        out = await asyncio.gather(*tasks, return_exceptions=True)
        drafts: list[ModelResponse] = []
        for r in out:
            if isinstance(r, BaseException):
                continue
            drafts.append(r)
        return drafts

    async def _verify(
        self, messages: list[Message], drafts: list[ModelResponse]
    ) -> int:
        """Ask the verifier to pick the best draft, return its index."""

        last_user = next(
            (m.content for m in reversed(messages) if m.role == "user"),
            "(no prompt)",
        )
        numbered = "\n".join(
            f"[{i}] {d.text.strip()[:600]}" for i, d in enumerate(drafts)
        )
        prompt = (
            "You are a verifier. Several candidate replies were drafted by a "
            "fast model. Pick the index of the BEST reply for the user's "
            "request. Respond with ONLY a single integer index.\n\n"
            f"User request: {last_user}\n\n"
            f"Candidates:\n{numbered}\n\n"
            "Best index:"
        )
        resp = await self.verifier.generate(
            [Message(role="user", content=prompt)],
            max_tokens=self.verifier_max_tokens,
            temperature=0.0,
        )
        return _parse_index(resp.text, max_exclusive=len(drafts))


_INDEX_RE = re.compile(r"-?\d+")


def _parse_index(text: str, *, max_exclusive: int) -> int:
    """Parse the first integer from ``text``; clamp to ``[0, max_exclusive)``.

    Returns ``-1`` if no integer is found (caller treats this as
    "regenerate via verifier directly").
    """
    if not text:
        return -1
    match = _INDEX_RE.search(text)
    if not match:
        return -1
    try:
        idx = int(match.group(0))
    except ValueError:
        return -1
    if idx < 0:
        return -1
    if idx >= max_exclusive:
        return -1
    return idx


__all__ = [
    "SpeculativeBackend",
    "SpeculativeStats",
]
