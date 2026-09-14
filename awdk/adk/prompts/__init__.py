"""adk.prompts — bridge AitherPrompts (and file-based prompt refs) into adk agent
construction.

The confirmed gap: ``lib/core/AitherPrompts.py`` (dot-key registry) and adk agent
construction (``AitherAgent(system_prompt=...)``) are disjoint — nothing resolves a
spec's ``prompts:`` map into concrete strings the agent can be built with. PromptBridge
closes that gap with no @-syntax and no dispatch table: a ref is either a file path or an
AitherPrompts dot-key.
"""

from adk.prompts.bridge import PromptBridge

__all__ = ["PromptBridge"]
