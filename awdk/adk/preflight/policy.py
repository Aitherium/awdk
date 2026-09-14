"""apply_policy — bind the operator's answers into process state.

Phase 0 binds EXACTLY ONE thing: ``allowed_roots`` (the agent's writable file
roots). It is bound two ways so the binding is airtight:

  1. ``builtin_tools.set_allowed_roots(...)`` — resets the memoized
     ``_ALLOWED_ROOTS`` cache so it takes effect IMMEDIATELY. Setting only the
     env var is a no-op after the first file-tool call, because the first call
     memoizes ``_ALLOWED_ROOTS`` and never re-reads the environment.
  2. ``os.environ["AITHER_ALLOWED_ROOTS"]`` — so child processes (shell tools,
     subprocess REPLs, spawned agents) inherit the same fence.

The OTHER binders named in the full design are DEFERRED in Phase 0, on purpose:
  * ``allowed_domains``  - deferred: purely process-local network policy; there
    is no adk chokepoint that enforces an egress allowlist yet, so binding it
    now would be a promise the runtime does not keep.
  * ``safety_level``     - deferred: local OpenAI-compatible endpoints (vLLM /
    Ollama / DGX) ignore any server-side safety tier, so this is prompt-only and
    belongs to a later prompt-composition phase, not to preflight.
  * ``rules``            - deferred here (handled as a prompt append by the
    orchestrator, not a hard binder): rules are advisory prompt text, not an
    enforced capability gate.
"""

from __future__ import annotations

import os
from typing import Any

from adk import builtin_tools


def apply_policy(answers: Any) -> None:
    """Bind Phase-0 policy from ``answers`` (a dict or small dataclass).

    Only ``allowed_roots`` is bound. Returns nothing.
    """
    roots = _get(answers, "allowed_roots", None)
    if not roots:
        return
    roots = [str(r) for r in roots if r]
    if not roots:
        return
    # In-process: reset the memoized cache so the fence is live immediately.
    builtin_tools.set_allowed_roots(roots)
    # Child processes: pass the same fence down.
    os.environ["AITHER_ALLOWED_ROOTS"] = ";".join(roots)


def _get(answers: Any, key: str, default: Any) -> Any:
    if answers is None:
        return default
    if isinstance(answers, dict):
        return answers.get(key, default)
    return getattr(answers, key, default)
