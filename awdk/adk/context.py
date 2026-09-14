"""Context management — token-aware message history with truncation.

Prevents context window overflow by:
  1. Counting tokens (tiktoken if available, else word-based estimate)
  2. Truncating oldest messages when approaching the limit
  3. Preserving system prompt + last N turns
  4. Optional summarization of dropped messages

Usage:
    from adk.context import ContextManager

    ctx = ContextManager(max_tokens=8000)
    ctx.add_system("You are an AI agent.")
    ctx.add_user("Hello")
    ctx.add_assistant("Hi there!")
    # ... many turns later ...
    messages = ctx.build()  # Returns truncated message list that fits in max_tokens
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger("adk.context")

# Try tiktoken for accurate counting, fall back to word estimate
_tiktoken_encoder = None
try:
    import tiktoken
    _tiktoken_encoder = tiktoken.get_encoding("cl100k_base")
except ImportError:
    pass


def count_tokens(text: str) -> int:
    """Count tokens in text. Uses tiktoken if available, else ~4 chars/token."""
    if _tiktoken_encoder:
        return len(_tiktoken_encoder.encode(text))
    # Rough estimate: 1 token ≈ 4 characters
    return max(1, len(text) // 4)


@dataclass
class ContextMessage:
    """A message with cached token count."""
    role: str
    content: str
    tokens: int = 0
    tool_calls: list | None = None
    tool_call_id: str = ""

    def __post_init__(self):
        if self.tokens == 0:
            self.tokens = count_tokens(self.content) + 4  # +4 for role/format overhead


class ContextManager:
    """Token-aware context window manager.

    Keeps messages within max_tokens by dropping oldest non-system messages.
    Always preserves: system prompt + last `preserve_turns` turns.

    Context layers (ported from monorepo UCB context pipeline):
      [SYSTEM FACTS] — Authoritative framing of system state
      [IDENTITY]     — Agent identity / persona
      [RULES]        — Behavioral rules
      [CONTEXT]      — Conversation context
      [MEMORIES]     — Retrieved memories
    """

    def __init__(
        self,
        max_tokens: int = 8000,
        preserve_turns: int = 4,
        reserve_for_response: int = 1000,
    ):
        self.max_tokens = max_tokens
        self.preserve_turns = preserve_turns
        self.reserve = reserve_for_response
        self._messages: list[ContextMessage] = []
        self._system_facts: str | None = None
        self._intent_type: str | None = None  # Intent discrimination for context scaling

    def add(self, role: str, content: str, **kwargs) -> ContextMessage:
        """Add a message to the context."""
        msg = ContextMessage(role=role, content=content, **kwargs)
        self._messages.append(msg)
        return msg

    def set_system_facts(self, facts: dict) -> None:
        """Set authoritative system facts that frame the agent's state.

        These are injected as a [SYSTEM FACTS] layer before the identity prompt.
        """
        lines = ["[SYSTEM FACTS]"]
        for key, value in facts.items():
            lines.append(f"- {key}: {value}")
        self._system_facts = "\n".join(lines)

    def set_intent(self, intent_type: str | None) -> None:
        """Store the current intent type (CODE/CONVERSATION/DEFAULT).

        Used to gate context scaling and system fact prioritization.
        Optional; fail-open if not set.
        """
        self._intent_type = intent_type

    def add_system(self, content: str) -> ContextMessage:
        # If system facts are set, prepend them to the first system message —
        # BUT skip the (technical/infra) facts for conversational intents, where
        # they are pure noise. Fail-open: None/unknown intent → inject as before.
        if self._system_facts:
            _intent = (self._intent_type or "").strip().upper()
            if _intent not in ("CONVERSATION", "GREETING"):
                content = f"{self._system_facts}\n\n{content}"
            self._system_facts = None  # Only consult once per turn
        return self.add("system", content)

    def add_user(self, content: str) -> ContextMessage:
        return self.add("user", content)

    def add_assistant(self, content: str, **kwargs) -> ContextMessage:
        return self.add("assistant", content, **kwargs)

    def add_tool(self, content: str, tool_call_id: str = "") -> ContextMessage:
        return self.add("tool", content, tool_call_id=tool_call_id)

    @property
    def total_tokens(self) -> int:
        return sum(m.tokens for m in self._messages)

    @property
    def message_count(self) -> int:
        return len(self._messages)

    def build(self) -> list[dict]:
        """Build the final message list, truncating if over budget.

        Strategy:
          1. Always keep system messages
          2. Always keep last `preserve_turns` user+assistant pairs
          3. Drop oldest middle messages until under budget
          4. If still over, truncate content of middle messages

        Returns list of dicts in OpenAI message format.
        """
        budget = self.max_tokens - self.reserve

        if self.total_tokens <= budget:
            return self._to_dicts(self._messages)

        # Separate system, recent, and middle
        system_msgs = [m for m in self._messages if m.role == "system"]
        non_system = [m for m in self._messages if m.role != "system"]

        # Keep last N turns (user+assistant pairs)
        keep_count = min(self.preserve_turns * 2, len(non_system))
        recent = non_system[-keep_count:] if keep_count > 0 else []
        middle = non_system[:-keep_count] if keep_count > 0 and len(non_system) > keep_count else []

        system_tokens = sum(m.tokens for m in system_msgs)
        recent_tokens = sum(m.tokens for m in recent)
        remaining_budget = budget - system_tokens - recent_tokens

        # Drop middle messages from oldest until under budget
        kept_middle = []
        middle_tokens = 0
        for msg in reversed(middle):
            if middle_tokens + msg.tokens <= remaining_budget:
                kept_middle.insert(0, msg)
                middle_tokens += msg.tokens
            else:
                logger.debug("Dropped message (role=%s, tokens=%d) for context budget",
                             msg.role, msg.tokens)

        # If still over, we've done our best
        final = system_msgs + kept_middle + recent

        total = sum(m.tokens for m in final)
        if total > budget:
            logger.warning("Context still over budget (%d/%d) after truncation", total, budget)

        return self._to_dicts(final)

    def clear(self):
        """Clear all messages."""
        self._messages.clear()

    def _to_dicts(self, messages: list[ContextMessage]) -> list[dict]:
        result = []
        for m in messages:
            d: dict = {"role": m.role, "content": m.content}
            if m.tool_calls:
                d["tool_calls"] = m.tool_calls
            if m.tool_call_id:
                d["tool_call_id"] = m.tool_call_id
            result.append(d)
        return result
