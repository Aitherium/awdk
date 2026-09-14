"""Anthropic advisor-tool support (beta ``advisor-tool-2026-03-01``).

The advisor tool lets a fast/cheap **executor** model (Haiku/Sonnet) consult a
stronger **advisor** model (Opus 4.8) *mid-generation*, all inside one
``/v1/messages`` request. The advisor reads the full transcript, returns a plan
or course-correction, and the executor continues — near-advisor quality at
executor token rates.

This module holds the provider-agnostic config + prompt fragments. The wiring
lives in :mod:`adk.llm.anthropic` (request/response) and :mod:`adk.agent`
(threading, steering, round-trip preservation). Off by default: when
``AdvisorConfig.enabled`` is false the request/response are byte-identical to a
plain call.

Designed for the "Haiku executor + Opus advisor" pairing (cheap edge loop,
cloud-grade strategic guidance) and a config shape that supports downstream
per-agent / per-severity gating.
"""

from __future__ import annotations

from dataclasses import dataclass

# The beta header that unlocks the advisor tool. Sent only when advisor.enabled.
ADVISOR_BETA = "advisor-tool-2026-03-01"

# The server tool type + name (fixed by the API).
ADVISOR_TOOL_TYPE = "advisor_20260301"
ADVISOR_TOOL_NAME = "advisor"

# Advisor models must be Opus 4.7/4.8 (per the compatibility table). Executors
# must be Haiku 4.5 / Sonnet 4.6 / Opus 4.6/4.7/4.8. We match on a prefix so a
# dated alias (e.g. ``claude-haiku-4-5-20251001``) still validates.
_VALID_ADVISORS = ("claude-opus-4-8", "claude-opus-4-7")
_VALID_EXECUTORS = (
    "claude-haiku-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
)


@dataclass
class AdvisorConfig:
    """Per-request advisor-tool configuration.

    A plain value object threaded through ``agent.chat(advisor=...)`` → router →
    provider. ``enabled=False`` (the default) makes every downstream branch a
    no-op, so passing ``AdvisorConfig()`` is the same as passing nothing.
    """

    enabled: bool = False
    advisor_model: str = "claude-opus-4-8"   # must be Opus 4.7/4.8
    max_uses: int | None = None              # server-enforced per-request cap
    max_tokens: int = 2048                   # caps advisor output; min 1024 (~7x reduction)
    caching_ttl: str | None = None           # "5m" | "1h"; enable for 3+ advisor calls/convo
    system_steering: bool = True             # inject timing + treatment guidance
    brevity_words: int = 150                 # soft cap requested of the advisor
    conversation_cap: int | None = None      # client-side cap across one chat()'s ReAct loop

    @classmethod
    def coerce(cls, value: "AdvisorConfig | dict | None") -> "AdvisorConfig | None":
        """Normalize a kwarg into an ``AdvisorConfig`` (or ``None``).

        Accepts an instance, a dict of fields, or ``None``. Unknown/garbage
        shapes degrade to ``None`` (advisor off) rather than raising — a bad
        config must never break a chat call.
        """
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            try:
                known = {k: v for k, v in value.items() if k in cls.__dataclass_fields__}
                return cls(**known)
            except (TypeError, ValueError):
                return None
        return None

    def tool_dict(self) -> dict:
        """Build the advisor tool entry for the Anthropic ``tools`` array.

        Carries an internal ``__no_cache_control`` marker so the prompt-cache
        breakpoint logic skips it (the advisor tool has its own ``caching``
        field). The marker is stripped before the request is sent.
        """
        tool: dict = {
            "type": ADVISOR_TOOL_TYPE,
            "name": ADVISOR_TOOL_NAME,
            "model": self.advisor_model,
            "__no_cache_control": True,
        }
        if self.max_uses is not None:
            tool["max_uses"] = self.max_uses
        if self.max_tokens is not None:
            tool["max_tokens"] = self.max_tokens
        if self.caching_ttl:
            tool["caching"] = {"type": "ephemeral", "ttl": self.caching_ttl}
        return tool


def validate_pair(executor_model: str, advisor_model: str) -> str | None:
    """Return a human-readable warning if executor/advisor are an invalid pair.

    Returns ``None`` when the pairing is valid. Callers should *warn*, not
    hard-fail — the API is the source of truth and would 400 a truly bad pair.
    """
    exe = (executor_model or "").strip()
    adv = (advisor_model or "").strip()
    if not any(adv.startswith(p) for p in _VALID_ADVISORS):
        return (
            f"advisor model {adv!r} is not a supported advisor "
            f"(expected one of {_VALID_ADVISORS})"
        )
    if exe and not any(exe.startswith(p) for p in _VALID_EXECUTORS):
        return (
            f"executor model {exe!r} is not a supported advisor-tool executor "
            f"(expected one of {_VALID_EXECUTORS})"
        )
    return None


# ── Executor system-prompt steering (from the Anthropic advisor-tool guide) ──
# Prepended to the executor system prompt when ``system_steering`` is on. The
# advisor sees the system prompt as quoted context, so this also shapes when the
# executor consults it and how it treats the advice.

ADVISOR_TIMING_BLOCK = (
    "You have access to an `advisor` tool backed by a stronger reviewer model. "
    "It takes NO parameters — when you call advisor(), your entire conversation "
    "history is forwarded automatically (the task, every tool call, every result).\n\n"
    "Call advisor BEFORE substantive work — before writing, before committing to "
    "an interpretation, before building on an assumption. If the task needs "
    "orientation first (finding files, fetching a source), do that, then call "
    "advisor. Orientation is not substantive work; writing, editing, and declaring "
    "an answer are.\n\n"
    "Also call advisor: when you believe the task is complete (make the deliverable "
    "durable FIRST); when stuck (errors recurring, not converging); when considering "
    "a change of approach. On tasks longer than a few steps, consult at least once "
    "before committing to an approach and once before declaring done. On short "
    "reactive steps dictated by tool output you just read, you don't need to keep "
    "calling — the advisor adds most of its value on the first call."
)

ADVISOR_TREATMENT_BLOCK = (
    "Give the advice serious weight. If you follow a step and it fails empirically, "
    "or you have primary-source evidence that contradicts a specific claim, adapt. A "
    "passing self-test is not evidence the advice is wrong. If you've already "
    "retrieved data pointing one way and the advisor points another, don't silently "
    "switch — surface the conflict in one more advisor call to break the tie."
)


def advisor_brevity_line(words: int = 150) -> str:
    """The user-message line that softly caps advisor output length."""
    return (
        f"(Advisor: please keep your guidance under {words} words — I need a "
        "focused starting point, not a comprehensive plan.)"
    )


def steering_system_block(cfg: AdvisorConfig) -> str:
    """The combined timing + treatment system block to inject when steering is on."""
    return ADVISOR_TIMING_BLOCK + "\n\n" + ADVISOR_TREATMENT_BLOCK


# Advisor block types that must be filtered out when the client-side
# conversation cap trips (drop the tool *and* its result blocks together, per
# the API's 400 rule), while keeping text/tool_use blocks intact.
_ADVISOR_BLOCK_TYPES = ("advisor_tool_result",)


def strip_advisor_blocks(blocks: list[dict] | None) -> list[dict] | None:
    """Remove advisor server-tool blocks from a native content array.

    Keeps text and ordinary ``tool_use`` blocks (so tool round-trip stays valid)
    and drops the advisor's ``server_tool_use`` + ``advisor_tool_result`` pair.
    Returns ``None`` if nothing meaningful remains.
    """
    if not blocks:
        return None
    kept = [
        b
        for b in blocks
        if not (
            b.get("type") in _ADVISOR_BLOCK_TYPES
            or (b.get("type") == "server_tool_use" and b.get("name") == ADVISOR_TOOL_NAME)
        )
    ]
    return kept or None
