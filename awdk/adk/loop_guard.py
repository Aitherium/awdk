"""LoopGuard — SHA256-based tool-call loop detection with circuit breaker.

SHA256-based tool-call loop detection. Detects when an agent is stuck
in a repetitive tool-call loop by hashing tool_name + canonical(args),
counting per hash, and issuing graduated verdicts:

    Allow  → first occurrence, proceed normally
    Warn   → 2nd duplicate, inject nudge message
    Block  → 4th+ duplicate, skip the call entirely
    CircuitBreak → N+ unique-but-similar calls, force synthesis
                   (soft nudge when effort_level >= 4)

SHA256 gives collision resistance. Canonical JSON sort ensures
`{"a":1,"b":2}` == `{"b":2,"a":1}`.

Usage (standalone):
    guard = LoopGuard()
    verdict = guard.check("web_search", {"query": "weather"})
    if verdict.action == LoopAction.BLOCK:
        # skip this tool call
    elif verdict.action == LoopAction.CIRCUIT_BREAK:
        # force agent to synthesize

Usage (with OODAReflection — AitherOS):
    # The OODAReflection class uses LoopGuard internally

Usage (with AitherAgent — ADK):
    # AitherAgent.chat() uses LoopGuard internally in the ReAct loop
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("adk.loop_guard")


# ─────────────────────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────────────────────

class LoopAction(str, Enum):
    """Graduated verdict for a tool call."""
    ALLOW = "allow"                # First occurrence — proceed
    WARN = "warn"                  # 2nd duplicate — inject nudge
    BLOCK = "block"                # 3rd+ duplicate — skip call
    CIRCUIT_BREAK = "circuit_break"  # Too many similar calls — force stop


@dataclass
class LoopVerdict:
    """Result of checking a tool call against the loop guard."""
    action: LoopAction
    tool_name: str
    call_hash: str
    hit_count: int
    reason: str = ""
    nudge_message: str = ""


@dataclass
class LoopGuardStats:
    """Snapshot of loop guard state for telemetry."""
    total_checks: int = 0
    unique_hashes: int = 0
    warns_issued: int = 0
    blocks_issued: int = 0
    circuit_breaks: int = 0
    call_counts: Dict[str, int] = field(default_factory=dict)
    window_seconds: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Main class
# ─────────────────────────────────────────────────────────────────────────────

class LoopGuard:
    """SHA256-based tool-call loop detection with graduated verdicts.

    Args:
        warn_threshold: Number of identical calls before WARN (default: 2).
        block_threshold: Number of identical calls before BLOCK (default: 4).
        circuit_break_total: Total calls in window before CIRCUIT_BREAK (default: 15).
        window_seconds: Rolling window for circuit breaker (0 = no window).
        similarity_threshold: Number of calls to same tool (any args) before
            counting toward circuit break (default: 5).
        effort_level: Agent effort level (1-10). When >= 4, circuit break
            becomes a soft nudge (no tool stripping, trust the model).
    """

    def __init__(
        self,
        warn_threshold: int = 2,
        block_threshold: int = 4,
        circuit_break_total: int = 15,
        window_seconds: float = 0.0,
        similarity_threshold: int = 5,
        effort_level: int = 5,
    ):
        self.warn_threshold = warn_threshold
        self.block_threshold = block_threshold
        self.circuit_break_total = circuit_break_total
        self.window_seconds = window_seconds
        self.similarity_threshold = similarity_threshold
        self.effort_level = effort_level

        # State
        self._hash_counts: Dict[str, int] = defaultdict(int)
        self._tool_counts: Dict[str, int] = defaultdict(int)
        self._timestamps: List[float] = []
        self._total_checks: int = 0
        self._warns: int = 0
        self._blocks: int = 0
        self._circuit_breaks: int = 0
        self._tripped: bool = False  # Once circuit-broken, stays tripped

    # ─────────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────────────────

    def check(self, tool_name: str, arguments: Dict[str, Any] | None = None) -> LoopVerdict:
        """Check a tool call against the loop guard.

        Returns a LoopVerdict with the recommended action.
        """
        if self._tripped:
            # Soft nudge for effort >= 4: don't force stop, just advise
            if self.effort_level >= 4:
                return LoopVerdict(
                    action=LoopAction.WARN,
                    tool_name=tool_name,
                    call_hash="",
                    hit_count=0,
                    reason="Circuit breaker tripped — soft nudge (effort >= 4)",
                    nudge_message=(
                        "[SYNTHESIS NUDGE] You have been calling tools for a while. "
                        "Consider synthesizing a response from the data you have, "
                        "or continue if you are making progress."
                    ),
                )
            return LoopVerdict(
                action=LoopAction.CIRCUIT_BREAK,
                tool_name=tool_name,
                call_hash="",
                hit_count=0,
                reason="Circuit breaker already tripped — force synthesis",
                nudge_message=(
                    "[CIRCUIT BREAKER] The tool-call loop guard has tripped. "
                    "You must stop calling tools and synthesize your answer now."
                ),
            )

        now = time.monotonic()
        self._timestamps.append(now)
        self._total_checks += 1

        # Prune old timestamps if windowed
        if self.window_seconds > 0:
            cutoff = now - self.window_seconds
            self._timestamps = [t for t in self._timestamps if t >= cutoff]

        # Compute SHA256 hash of tool_name + canonical args
        call_hash = self._hash_call(tool_name, arguments)

        self._hash_counts[call_hash] += 1
        self._tool_counts[tool_name] += 1
        hit_count = self._hash_counts[call_hash]

        # ── Check circuit breaker (total volume) ──
        active_calls = len(self._timestamps) if self.window_seconds > 0 else self._total_checks
        if active_calls >= self.circuit_break_total:
            self._tripped = True
            self._circuit_breaks += 1
            # Soft nudge for effort >= 4: trust the model, don't force stop
            if self.effort_level >= 4:
                return LoopVerdict(
                    action=LoopAction.WARN,
                    tool_name=tool_name,
                    call_hash=call_hash,
                    hit_count=hit_count,
                    reason=(
                        f"Circuit break (soft): {active_calls} total calls "
                        f"(limit: {self.circuit_break_total}, effort: {self.effort_level})"
                    ),
                    nudge_message=(
                        "[SYNTHESIS NUDGE] You have made many tool calls. "
                        "Consider synthesizing a response from the data gathered, "
                        "or continue if you are making meaningful progress."
                    ),
                )
            return LoopVerdict(
                action=LoopAction.CIRCUIT_BREAK,
                tool_name=tool_name,
                call_hash=call_hash,
                hit_count=hit_count,
                reason=(
                    f"Circuit break: {active_calls} total calls "
                    f"(limit: {self.circuit_break_total})"
                ),
                nudge_message=(
                    "[CIRCUIT BREAKER] You have made too many tool calls. "
                    "STOP calling tools immediately and synthesize a response "
                    "from the data you have gathered so far."
                ),
            )

        # ── Check same-tool similarity flood ──
        if self._tool_counts[tool_name] >= self.similarity_threshold:
            # Not a hard block, but counts toward circuit break pressure
            # Only trip if it's also a repeated exact call
            pass

        # ── Check exact duplicate graduated response ──
        if hit_count >= self.block_threshold:
            self._blocks += 1
            return LoopVerdict(
                action=LoopAction.BLOCK,
                tool_name=tool_name,
                call_hash=call_hash,
                hit_count=hit_count,
                reason=(
                    f"Blocked: {tool_name} called {hit_count} times "
                    f"with identical arguments (threshold: {self.block_threshold})"
                ),
                nudge_message=(
                    f"[BLOCKED] Call to {tool_name} was blocked — you've called it "
                    f"{hit_count} times with the same arguments. The result won't change. "
                    f"Use a different approach or respond with what you have."
                ),
            )

        if hit_count >= self.warn_threshold:
            self._warns += 1
            return LoopVerdict(
                action=LoopAction.WARN,
                tool_name=tool_name,
                call_hash=call_hash,
                hit_count=hit_count,
                reason=(
                    f"Warning: {tool_name} called {hit_count} times "
                    f"with identical arguments"
                ),
                nudge_message=(
                    f"[LOOP DETECTED] You already called {tool_name} with these "
                    f"exact arguments ({hit_count} times). The result is the same. "
                    f"Try a different approach, modify your arguments, or respond "
                    f"with what you have."
                ),
            )

        # ── Allow ──
        return LoopVerdict(
            action=LoopAction.ALLOW,
            tool_name=tool_name,
            call_hash=call_hash,
            hit_count=hit_count,
        )

    def reset(self) -> None:
        """Reset all state for a new task/session."""
        self._hash_counts.clear()
        self._tool_counts.clear()
        self._timestamps.clear()
        self._total_checks = 0
        self._warns = 0
        self._blocks = 0
        self._circuit_breaks = 0
        self._tripped = False

    @property
    def tripped(self) -> bool:
        """Whether the circuit breaker has been tripped."""
        return self._tripped

    @property
    def stats(self) -> LoopGuardStats:
        """Snapshot of current state for telemetry/debugging."""
        return LoopGuardStats(
            total_checks=self._total_checks,
            unique_hashes=len(self._hash_counts),
            warns_issued=self._warns,
            blocks_issued=self._blocks,
            circuit_breaks=self._circuit_breaks,
            call_counts=dict(self._tool_counts),
            window_seconds=self.window_seconds,
        )

    # ─────────────────────────────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _hash_call(tool_name: str, arguments: Dict[str, Any] | None) -> str:
        """SHA256 hash of tool_name + canonical JSON args.

        Canonical = sorted keys, deterministic serialization.
        This ensures {"a":1,"b":2} and {"b":2,"a":1} produce the same hash.
        """
        try:
            canonical_args = json.dumps(arguments or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            canonical_args = str(arguments or {})

        payload = f"{tool_name}:{canonical_args}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
