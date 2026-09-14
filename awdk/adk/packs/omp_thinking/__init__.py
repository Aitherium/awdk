"""External thinking — give the model a scratchpad when the provider took one away.

Ported from oh-my-pi (`packages/coding-agent/src/tools/think.ts`), MIT:
(c) 2025 Mario Zechner, (c) 2025-2026 Can Boluk. See NOTICE in this directory.

The technique: turn the model's NATIVE reasoning channel off, then hand it a
tool whose only parameter is a string described as a private scratchpad. The
model keeps reasoning — it writes into the tool call instead, and tool calls
come back in plaintext. What you get is not a cleaned-up summary; it is the
model's own shorthand.

Two things this pack is careful about
-------------------------------------
**It refuses more than it allows.** :func:`supports_external_thinking` is a
refusal list first. Enabling the scratchpad on a model that cannot suppress its
native channel gets you both channels or a rejected request, so an unknown model
is refused, not attempted.

**Everything the model thinks becomes a tool parameter.** That is the whole
point, and it is also the risk: tool parameters flow through logs, traces and
whatever observability stack is attached. If the context held a credential, the
reasoning about it lands in all of those. Do not enable this on a surface whose
tool calls you would not be willing to read out loud.

Tools registered
----------------
``deep_think``              the scratchpad itself
``deep_think_supported``    capability query for a model description
``deep_think_directive``    build the system-prompt effort directive
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("omp_thinking_pack")

PACK_ID = "omp-thinking"

TOOL_NAME = "deep_think"

#: Transports whose native reasoning channel can be switched off.
SUPPRESSIBLE_APIS = frozenset({
    "openai-responses",
    "azure-openai-responses",
    "openai-codex-responses",
    "anthropic-messages",
})

#: Google transports, which decide on the thinking mode rather than the API.
GOOGLE_APIS = frozenset({
    "google-generative-ai",
    "google-gemini-cli",
    "google-vertex",
})

_TOOL_NAMES = [
    "deep_think",
    "deep_think_supported",
    "deep_think_directive",
]


def supports_external_thinking(model: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """Whether ``model`` can have its native reasoning suppressed.

    ``model`` is a plain dict so this stays free of any host model class::

        {"api": "anthropic-messages", "reasoning": True,
         "requires_thinking_enabled": False,
         "thinking_requires_effort": True,
         "thinking_suppress_when_off": True,
         "thinking_mode": "effort"}

    Returns ``(supported, reason)``. The reason is a stable key, so a refusal
    can be counted and attributed instead of vanishing into a bare ``False``.

    The final clause — admitting any model with no native reasoning channel at
    all — is an addition to the upstream rule, which enumerates transports that
    can SUPPRESS a channel. Where there is nothing to suppress the scratchpad is
    an ordinary tool call, and without this clause every non-reasoning model is
    needlessly refused.
    """
    if not isinstance(model, dict):
        return False, "unknown_model"

    api = str(model.get("api") or "")
    reasoning = bool(model.get("reasoning", False))
    requires_thinking = bool(model.get("requires_thinking_enabled", False))
    requires_effort = bool(model.get("thinking_requires_effort", False))
    suppress_when_off = bool(model.get("thinking_suppress_when_off", False))
    thinking_mode = model.get("thinking_mode")

    if reasoning and requires_thinking:
        return False, "requires_thinking_enabled"
    if reasoning and requires_effort and not suppress_when_off:
        return False, "effort_required_not_suppressible"

    if api in GOOGLE_APIS:
        if not reasoning or thinking_mode == "budget" or suppress_when_off:
            return True, "google_suppressible"
        return False, "google_not_suppressible"

    if api in SUPPRESSIBLE_APIS:
        return True, "suppressible_api"

    if not reasoning:
        return True, "no_native_reasoning"

    return False, "unsupported_api"


def deep_think(thoughts: str) -> str:
    """private scratchpad; not shown to user

    The description above is the tool's contract with the model and is
    deliberately terse — it is what makes the model treat this as somewhere to
    think rather than somewhere to report. Do not "improve" it into an
    explanation; a descriptive prompt produces a written-for-an-audience
    summary, which is the thing this pack exists to avoid.

    The return value is intentionally content-free. The model needs an
    acknowledgement to continue its turn, and anything meaningful here would
    invite it to converse with the scratchpad instead of thinking in it.
    """
    return "------"


def deep_think_supported(
    api: str = "",
    reasoning: bool = False,
    requires_thinking_enabled: bool = False,
    thinking_requires_effort: bool = False,
    thinking_suppress_when_off: bool = False,
    thinking_mode: str = "",
) -> Dict[str, Any]:
    """Report whether a model described by these fields can use the scratchpad."""
    supported, reason = supports_external_thinking({
        "api": api,
        "reasoning": reasoning,
        "requires_thinking_enabled": requires_thinking_enabled,
        "thinking_requires_effort": thinking_requires_effort,
        "thinking_suppress_when_off": thinking_suppress_when_off,
        "thinking_mode": thinking_mode or None,
    })
    return {"ok": True, "supported": supported, "reason": reason}


def deep_think_directive(effort: int = 8) -> Dict[str, Any]:
    """Build the system-prompt directive that arms the scratchpad.

    With the vendor's thinking channel off, its own effort dial is inert — so
    the effort that still steers the model is the number it can read. Writing it
    into the system prompt aims it at the scratchpad instead.
    """
    try:
        level = max(0, min(10, int(effort)))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "effort must be an integer 0-10"}

    directive = (
        f"reasoning effort: {level}/10\n"
        f"Before answering, call the `{TOOL_NAME}` tool and reason in it as long "
        "as you need. Use your own shorthand; it is a private scratchpad and is "
        "not shown to the user. Then answer normally."
    )
    return {"ok": True, "effort": level, "directive": directive}


def tool_schema() -> Dict[str, Any]:
    """The scratchpad in OpenAI tool shape, for callers building requests."""
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "private scratchpad; not shown to user",
            "parameters": {
                "type": "object",
                "properties": {
                    "thoughts": {
                        "type": "string",
                        "description": "private scratchpad; not shown to user",
                    },
                },
                "required": ["thoughts"],
                "additionalProperties": False,
            },
        },
    }


def reconcile(registry, model: Optional[Dict[str, Any]], enabled: bool = True) -> Dict[str, Any]:
    """Arm or disarm the scratchpad for the CURRENT model. Call on every swap.

    Port of upstream's ``reconcileThinkTool``. Whether the scratchpad is legal
    is a property of the model, not of the session, so it has to be re-decided
    whenever the model changes — registering it once at startup is wrong the
    moment someone swaps models.

    The failure this prevents is silent in the usual way. A model that cannot
    suppress its native reasoning, handed the scratchpad anyway, either emits on
    both channels or has the request rejected by the provider; neither reads as
    "the scratchpad should not have been offered". Nothing logs a malformed tool
    contract.

    ``enabled=False`` disarms unconditionally — the session-level off switch,
    separate from the model-level capability answer, so a user turning it off
    and a model that cannot support it stay distinguishable.

    Returns ``{"armed": bool, "reason": str, "changed": bool}``.
    """
    supported, reason = supports_external_thinking(model)
    want = bool(enabled) and supported
    if not enabled:
        reason = "disabled_by_setting"

    present = registry.get(TOOL_NAME) is not None if hasattr(registry, "get") else False

    if want and not present:
        registry.register(deep_think)
        return {"armed": True, "reason": reason, "changed": True}
    if not want and present:
        # Prefer a real removal; fall back to reporting rather than pretending.
        remover = getattr(registry, "unregister", None)
        if callable(remover):
            remover(TOOL_NAME)
            return {"armed": False, "reason": reason, "changed": True}
        logger.warning(
            "omp_thinking: registry cannot unregister %s — the scratchpad stays "
            "armed on a model that reports %s", TOOL_NAME, reason,
        )
        return {"armed": True, "reason": f"stuck:{reason}", "changed": False}

    return {"armed": want, "reason": reason, "changed": False}


def register(registry) -> int:
    """Register the pack's control tools.

    Note what is NOT registered here: ``deep_think`` itself. The scratchpad is
    model-conditional, so it is armed by :func:`reconcile` once the current
    model is known — registering it unconditionally at pack load is exactly the
    bug that function exists to prevent.
    """
    registered = 0
    for name in _TOOL_NAMES:
        if name == TOOL_NAME:
            continue
        fn = globals().get(name)
        if not callable(fn):
            logger.debug("omp_thinking: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            registered += 1
        except Exception as exc:  # noqa: BLE001 - a pack must not sink an agent
            logger.debug("omp_thinking: skip tool %s: %s", name, exc)

    logger.info(
        "omp-thinking pack registered %d control tool(s); %s is armed by "
        "reconcile() once the model is known", registered, TOOL_NAME,
    )
    return registered
