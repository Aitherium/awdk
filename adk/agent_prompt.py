"""Print the canonical agent setup prompt to stdout.

Usage:
    adk agent-prompt              # Print prompt (pipe to clipboard, file, or agent)
    adk agent-prompt --raw        # No header/footer, just the prompt
    adk agent-prompt --start      # The one-prompt on-ramp (START.md): first agent, one gateway tool call
    python -m adk.agent_prompt    # Same thing, works without CLI installed
"""

from __future__ import annotations

from pathlib import Path

_PROMPT_PATH = Path(__file__).parent / "AGENT_PROMPT.md"
#: The one-prompt on-ramp. AGENT_PROMPT.md is the reference manual (eleven steps,
#: every feature); START.md is one job with a pass/fail end: a running agent that
#: has called one gateway tool. The shape is borrowed from Flue's start.md
#: (https://flueframework.com/start.md, Apache-2.0); the text is ours.
_START_PATH = Path(__file__).parent / "START.md"


def get_prompt() -> str:
    """Return the canonical agent setup prompt as a string."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def get_start_prompt() -> str:
    """Return the one-prompt on-ramp (START.md) as a string."""
    return _START_PATH.read_text(encoding="utf-8")


def cmd_agent_prompt(args=None) -> int:
    """Print the agent prompt to stdout."""
    raw = getattr(args, "raw", False) if args else False
    prompt = get_start_prompt() if getattr(args, "start", False) else get_prompt()

    if raw:
        print(prompt)
    else:
        print(prompt)
        print()
        print("# Copy the above into any AI coding agent (Claude Code, Cursor, Copilot, etc.)")
        print("# Or pipe it: adk agent-prompt | pbcopy")
    return 0


def main():
    cmd_agent_prompt()


if __name__ == "__main__":
    main()
