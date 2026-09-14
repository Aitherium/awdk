"""Print the canonical agent setup prompt to stdout.

Usage:
    adk agent-prompt              # Print prompt (pipe to clipboard, file, or agent)
    adk agent-prompt --raw        # No header/footer, just the prompt
    python -m adk.agent_prompt    # Same thing, works without CLI installed
"""

from __future__ import annotations

from pathlib import Path

_PROMPT_PATH = Path(__file__).parent / "AGENT_PROMPT.md"


def get_prompt() -> str:
    """Return the canonical agent setup prompt as a string."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def cmd_agent_prompt(args=None) -> int:
    """Print the agent prompt to stdout."""
    raw = getattr(args, "raw", False) if args else False
    prompt = get_prompt()

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
