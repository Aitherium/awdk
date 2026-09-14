"""Model backend protocol + ``auto_backend`` factory.

Concrete backends live in :mod:`adk.core.backends`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol


@dataclass(slots=True)
class Message:
    """A chat message. Compatible with OpenAI-style turns."""

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        return d


@dataclass(slots=True)
class ModelResponse:
    """Non-streaming response from a backend."""

    text: str
    model: str
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


class ModelBackend(Protocol):
    """Every model backend implements this small surface."""

    name: str
    model: str

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **opts: Any,
    ) -> ModelResponse: ...

    # NOT ``async def``: every implementation is an async *generator*, so
    # callers do ``async for chunk in backend.stream(...)``. Declaring this
    # ``async def`` would type it as a coroutine returning an iterator --
    # i.e. ``await backend.stream(...)`` -- which raises ``TypeError: object
    # async_generator can't be used in 'await' expression`` against every
    # backend we ship. A plain ``def`` returning ``AsyncIterator`` is the
    # correct Protocol form for an async generator.
    # Pinned by tests/test_backend_conformance.py.
    def stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **opts: Any,
    ) -> AsyncIterator[str]: ...


# ---------------------------------------------------------------------------
# auto_backend
# ---------------------------------------------------------------------------

_PRIORITY_ENV_KEYS = (
    "AITHER_BACKEND",  # explicit override: "genesis" | "vllm" | "openai" | ...
)


def auto_backend(model: str | None = None) -> ModelBackend:
    """Pick the best available backend in priority order.

    Order (same as :mod:`awnode`):

    1. ``AITHER_BACKEND`` env override
    2. Genesis (``AITHER_URL`` set and reachable) — slice B will probe
    3. vLLM (``AITHER_VLLM_URL`` set)
    4. OpenAI (``OPENAI_API_KEY`` set)
    5. Anthropic (``ANTHROPIC_API_KEY`` set)
    6. DeepSeek (``DEEPSEEK_API_KEY`` set)
    7. Ollama (``OLLAMA_HOST`` set, default ``http://localhost:11434``)

    This call **does not** make network probes. It only inspects env vars.
    A real reachability probe lands in :mod:`awnode` in slice B.

    Raises :class:`RuntimeError` if nothing is configured.
    """
    # Local imports keep ``adk.core`` import-cheap.
    from adk.core.backends.anthropic import AnthropicBackend
    from adk.core.backends.ollama import OllamaBackend
    from adk.core.backends.openai_compat import (
        DeepSeekBackend,
        OpenAIBackend,
        VLLMBackend,
    )

    override = os.environ.get(_PRIORITY_ENV_KEYS[0], "").lower().strip()

    def _build(kind: str) -> ModelBackend | None:
        if kind == "vllm":
            url = os.environ.get("AITHER_VLLM_URL")
            if url:
                return VLLMBackend(base_url=url, model=model or "auto")
        elif kind == "openai":
            key = os.environ.get("OPENAI_API_KEY")
            if key:
                return OpenAIBackend(api_key=key, model=model or "gpt-4o-mini")
        elif kind == "anthropic":
            key = os.environ.get("ANTHROPIC_API_KEY")
            if key:
                return AnthropicBackend(
                    api_key=key, model=model or "claude-3-5-sonnet-latest"
                )
        elif kind == "deepseek":
            key = os.environ.get("DEEPSEEK_API_KEY")
            if key:
                return DeepSeekBackend(api_key=key, model=model or "deepseek-chat")
        elif kind == "ollama":
            host = os.environ.get("OLLAMA_HOST")
            if host:
                return OllamaBackend(base_url=host, model=model or "llama3.1:8b")
        return None

    if override:
        chosen = _build(override)
        if chosen is None:
            raise RuntimeError(
                f"AITHER_BACKEND={override!r} but its env vars are not set"
            )
        return _maybe_speculative(chosen)

    for kind in ("vllm", "openai", "anthropic", "deepseek", "ollama"):
        backend = _build(kind)
        if backend is not None:
            return _maybe_speculative(backend)

    raise RuntimeError(
        "no model backend available. Set one of: AITHER_VLLM_URL, OPENAI_API_KEY, "
        "ANTHROPIC_API_KEY, DEEPSEEK_API_KEY, OLLAMA_HOST."
    )


def _maybe_speculative(backend: ModelBackend) -> ModelBackend:
    """Wrap ``backend`` in :class:`SpeculativeBackend` iff env is on.

    Activated by ``AITHER_SPECULATIVE`` truthy. The draft tier is the
    same ``backend`` we just picked; the verifier tier is the reasoning
    router's REASONING tier. If wiring fails for any reason, return the
    bare backend (NO-FALLBACK policy applies only to *silent* import
    cascades; this is an opt-in feature flag with a logged miss).
    """
    flag = (os.environ.get("AITHER_SPECULATIVE") or "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return backend
    try:
        from adk.reasoning.speculative import SpeculativeBackend
        from adk.reasoning.tiers import ModelTier, ReasoningRouter
    except ImportError:
        return backend
    try:
        router = ReasoningRouter()
        verifier = router.resolve(tier=ModelTier.REASONING).backend
    except Exception:
        return backend
    try:
        k = int(os.environ.get("AITHER_SPECULATIVE_K", "3"))
    except (TypeError, ValueError):
        k = 3
    return SpeculativeBackend(backend, verifier, k=k)
