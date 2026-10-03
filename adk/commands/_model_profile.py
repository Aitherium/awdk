"""Model backend profiles shared by ``adk solve`` and ``adk eval arc``.

A *profile* names where the model comes from:

* ``microscheduler`` (default) -- :class:`adk.core.backends.microscheduler.MicroSchedulerBackend`,
  the fleet's one LLM front door (``--scheduler-url``, ``--model``; ``auto`` picks
  the strongest locally served model). An unreachable scheduler is a dead
  backend, never a scored zero.
* ``fast`` / ``orchestrator`` / ``reasoning`` -- that tier of
  ``~/.aither/reasoning.json`` through :class:`adk.reasoning.tiers.ReasoningRouter`.

Everything is imported lazily so ``adk --help`` stays cheap.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

PROFILES = ("microscheduler", "fast", "orchestrator", "reasoning")


class BackendDeadError(RuntimeError):
    """The model never answered. The ARC suite and ``adk solve`` report exit 2."""

    backend_dead = True


def add_model_args(p: Any) -> None:
    """The model-selection flags, identical on both verbs."""
    p.add_argument(
        "--backend",
        default="microscheduler",
        choices=PROFILES,
        help="Model backend profile: microscheduler (default, :8150) or a "
        "reasoning.json tier (fast|orchestrator|reasoning)",
    )
    p.add_argument("--model", default=None, help="Model id (microscheduler: default auto)")
    p.add_argument(
        "--scheduler-url",
        default=None,
        help="MicroScheduler base URL (default $AITHER_MICROSCHEDULER_URL or https://127.0.0.1:8150)",
    )


def build_backend(args: Any) -> Any:
    """A ModelBackend for ``args.backend``. Tests monkeypatch this."""
    profile = getattr(args, "backend", None) or "microscheduler"
    if profile == "microscheduler":
        from adk.core.backends.microscheduler import MicroSchedulerBackend

        # Strict (solve / eval-arc): a stand-in answer is refused, not scored.
        return MicroSchedulerBackend(
            base_url=getattr(args, "scheduler_url", None),
            model=getattr(args, "model", None),
            allow_cross_model=False,
        )
    if profile in PROFILES:
        from adk.reasoning.tiers import ReasoningRouter

        return ReasoningRouter().resolve(tier=profile).backend
    raise ValueError("unknown backend profile %r; known: %s" % (profile, list(PROFILES)))


def resolve_label(backend: Any) -> str:
    """Pin and describe the model. Raises when a backend that can check is dead."""
    resolve = getattr(backend, "resolve_model", None)
    if callable(resolve):
        model = resolve()
        where = getattr(backend, "base_url", "")
        src = getattr(backend, "model_source", "")
        return "%s (%s) via %s" % (model, src, where) if where else str(model)
    name = getattr(backend, "model", None) or getattr(backend, "name", None)
    return str(name or type(backend).__name__)


class SyncChat:
    """A blocking ``chat()`` over any async ``ModelBackend.generate``, with counters.

    Used by the per-step ``llm`` ARC policy when the backend has no ``chat`` of its
    own. A failure before any successful call is re-raised as
    :class:`BackendDeadError`, so a dead backend is exit 2, not a scored zero.
    """

    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self.model = getattr(backend, "model", None) or getattr(backend, "name", "model")
        self.calls = 0
        self.llm_s = 0.0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def chat(
        self, messages: List[Dict[str, Any]], max_tokens: int = 1000, temperature: float = 0.4
    ) -> Any:
        import time

        from adk.core.model import Message

        msgs = [Message(role=m["role"], content=m["content"]) for m in messages]
        t0 = time.perf_counter()
        try:
            resp = asyncio.run(
                self.backend.generate(msgs, temperature=temperature, max_tokens=max_tokens)
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            if self.calls == 0 and not getattr(exc, "backend_dead", False):
                raise BackendDeadError("%s: %s" % (type(exc).__name__, exc)) from exc
            raise
        self.calls += 1
        self.llm_s += time.perf_counter() - t0
        usage = getattr(resp, "usage", None) or {}
        self.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
        self.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
        return _Reply(getattr(resp, "text", "") or "")

    def stats(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "llm_calls": self.calls,
            "llm_s": round(self.llm_s, 3),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


class _Reply:
    def __init__(self, content: str) -> None:
        self.content = content


def backend_stats(backend: Any) -> Optional[Dict[str, Any]]:
    fn = getattr(backend, "stats", None)
    try:
        return dict(fn()) if callable(fn) else None
    except Exception:  # noqa: BLE001 - stats are a report, never a failure
        return None
