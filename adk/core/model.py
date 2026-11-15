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


#: Env vars that name the AitherOS spine (MicroScheduler, ``:8150``), first wins.
SPINE_URL_ENVS = ("AITHER_MICROSCHEDULER_URL", "AITHER_LOCAL_LLM_URL", "AITHER_CORE_LLM_URL")
#: The spine on this host. HTTPS only: a plain ``http://`` request into the TLS
#: spine hangs or closes the socket and reads as "down".
SPINE_HOST_URL = "https://127.0.0.1:8150"
#: What an interactive ``auto`` turn asks the spine for, first one it serves wins:
#: the orchestrator alias (Bonsai 2 27B on the 5090), Bonsai itself, the DeepSeek-V4
#: pool served LOCALLY on the DGX Spark, then Gemma 4 12B (vision). All local.
INTERACTIVE_MODELS = (
    "aither-orchestrator",
    "bonsai2-27b",
    "deepseek-v4-flash-pool",
    "gemma4-12b",
)
#: Paid cloud APIs are never auto-picked. ``ADK_ALLOW_PAID_CLOUD=1`` lets OpenAI /
#: Anthropic keys join auto-detect (after every local backend); DeepSeek cloud is
#: reachable only by naming it (``AITHER_BACKEND=deepseek``) -- 2026-11-12 its
#: account answers 402, and a key in the env must never route a turn there.
PAID_CLOUD_OPT_IN = "ADK_ALLOW_PAID_CLOUD"


def _truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _is_spine_url(url: str) -> bool:
    u = (url or "").lower()
    return ":8150" in u or "microscheduler" in u


def spine_url_from_env() -> str:
    """The configured spine URL (``https`` forced, ``/v1`` stripped), or ``""``."""
    for name in SPINE_URL_ENVS:
        url = (os.environ.get(name) or "").strip()
        if url and _is_spine_url(url):
            return _normalise_spine_url(url)
    return ""


def _normalise_spine_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    return url


def default_scheduler_metadata(source: str = "adk") -> dict[str, Any]:
    """Scheduler hints for an interactive call: user priority unless the env says otherwise.

    ``ADK_SCHEDULER_METADATA`` (a JSON object) overrides key by key. Without a priority
    the spine files the owner's turn as AGENT background work behind every agent
    (2026-10-30: a "hey" waited 174-241 s).
    """
    import json

    md: dict[str, Any] = {"priority": "user", "source": source}
    raw = (os.environ.get("ADK_SCHEDULER_METADATA") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            md.update(parsed)
    return md


def spine_backend(
    model: str | None = None, *, base_url: str | None = None, source: str = "adk"
) -> ModelBackend:
    """A local-only MicroScheduler backend at user priority (interactive default)."""
    from adk.core.backends.microscheduler import MicroSchedulerBackend

    return MicroSchedulerBackend(
        base_url=_normalise_spine_url(base_url or spine_url_from_env() or SPINE_HOST_URL),
        model=model or "auto",
        prefer=INTERACTIVE_MODELS,
        source=source,
        metadata=default_scheduler_metadata(source),
        local_only=True,
    )


def auto_backend(model: str | None = None) -> ModelBackend:
    """Pick the best available backend, LOCAL FIRST.

    Order:

    1. ``AITHER_BACKEND`` env override (the only way to reach DeepSeek cloud)
    2. The AitherOS spine when configured (``AITHER_MICROSCHEDULER_URL`` /
       ``AITHER_LOCAL_LLM_URL`` / ``AITHER_CORE_LLM_URL`` naming ``:8150``)
    3. vLLM (``AITHER_VLLM_URL`` set)
    4. llama.cpp (``AITHER_LLAMACPP_URL`` set -- a local ``llama-server``)
    5. OpenAI / Anthropic keys -- ONLY with ``ADK_ALLOW_PAID_CLOUD=1``
    6. The spine on this host, ``https://127.0.0.1:8150`` (the default)

    A ``DEEPSEEK_API_KEY`` in the env is never auto-picked, and Ollama is not a
    backend here. The spine backend asks for :data:`INTERACTIVE_MODELS` at user
    priority with ``local_only`` set, so the scheduler never hops to a paid route.

    This call **does not** make network probes. It only inspects env vars.
    """
    # Local imports keep ``adk.core`` import-cheap.
    from adk.core.backends.anthropic import AnthropicBackend
    from adk.core.backends.openai_compat import (
        DeepSeekBackend,
        LlamaCppBackend,
        OpenAIBackend,
        VLLMBackend,
    )

    from adk.legacy_backend import legacy_env_base_url, migrate_backend_name

    override = migrate_backend_name(
        os.environ.get(_PRIORITY_ENV_KEYS[0], "").lower().strip(), "AITHER_BACKEND"
    )

    def _build(kind: str) -> ModelBackend | None:
        if kind in ("spine", "microscheduler", "aither"):
            url = spine_url_from_env()
            if url or kind != "spine":
                return spine_backend(model, base_url=url or None)
        elif kind == "spine-default":
            return spine_backend(model)
        elif kind == "vllm":
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
        elif kind == "llamacpp":
            # Only an explicit AITHER_LLAMACPP_URL counts in auto-detect. A leftover
            # OLLAMA_HOST (legacy_env_base_url) must not beat the spine default
            # (2026-11-12 review: the owner's host sets it and lost the spine).
            host = os.environ.get("AITHER_LLAMACPP_URL") or (
                legacy_env_base_url() if override == "llamacpp" else ""
            )
            if host:
                return LlamaCppBackend(base_url=host, model=model or "default")
        return None

    if override:
        chosen = _build(override)
        if chosen is None:
            raise RuntimeError(
                f"AITHER_BACKEND={override!r} but its env vars are not set"
            )
        return _maybe_speculative(chosen)

    order: list[str] = ["spine", "vllm", "llamacpp"]
    if _truthy(PAID_CLOUD_OPT_IN):
        order += ["openai", "anthropic"]
    order.append("spine-default")
    for kind in order:
        backend = _build(kind)
        if backend is not None:
            return _maybe_speculative(backend)

    raise RuntimeError("no model backend available")  # unreachable: spine-default


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
