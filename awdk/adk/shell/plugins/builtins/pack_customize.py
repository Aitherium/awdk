"""
ADK Shell Plugin: Pack Customization
Override pack system prompts, capabilities, and domains via local agent.yaml.local overlay.
"""

import os
import shlex
from pathlib import Path
from typing import List, Optional

import yaml

from adk.shell.plugins import SlashCommand


class PackCustomizePlugin(SlashCommand):
    """
    /pack customize — Override agent pack system_prompt, capabilities, or enabled_domains.

    Persists overrides to ~/.aither/agents/<name>/agent.yaml.local (never mutates shipped agent.yaml).
    Overrides apply when the agent is next run via `adk run --agents <name>`.

    Subcommands:
      /pack customize <name> [--system-prompt "TEXT"]       Set system prompt
      /pack customize <name> --system-prompt-file PATH      Load prompt from file
      /pack customize <name> --capabilities a,b,c           Override capabilities list
      /pack customize <name> --show                         Show current effective spec
      /pack customize <name> --show                         Show all overrides (default)
    """

    name = "pack"
    aliases = []
    category = "agents"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='pack',
            description='',
        )

    def execute(self, args: List[str], **kwargs) -> str:
        """Main entry point for /pack command."""
        if not args:
            return self._show_help()

        subcommand = args[0].lower()

        if subcommand == "customize":
            return self._customize(args[1:])
        else:
            return f"Unknown /pack subcommand '{subcommand}'. Try '/pack customize <name>'"

    def _customize(self, args: List[str]) -> str:
        """Handle /pack customize <name> [options]."""
        if not args:
            return "Usage: /pack customize <name> [--system-prompt TEXT | --system-prompt-file PATH] [--capabilities a,b,c] [--show]"

        pack_name = args[0]
        args = args[1:]

        # Resolve pack directory
        pack_dir = Path.home() / ".aither" / "agents" / pack_name
        if not pack_dir.exists():
            return (
                f"ERROR: Pack '{pack_name}' not installed.\n"
                f"Install it with: adk install pack:{pack_name}"
            )

        # Parse options
        system_prompt = None
        system_prompt_file = None
        capabilities = None
        show_spec = False

        i = 0
        while i < len(args):
            if args[i] == "--system-prompt" and i + 1 < len(args):
                system_prompt = args[i + 1]
                i += 2
            elif args[i] == "--system-prompt-file" and i + 1 < len(args):
                system_prompt_file = args[i + 1]
                i += 2
            elif args[i] == "--capabilities" and i + 1 < len(args):
                capabilities = [c.strip() for c in args[i + 1].split(",")]
                i += 2
            elif args[i] == "--show":
                show_spec = True
                i += 1
            else:
                i += 1

        # Load prompt from file if specified
        if system_prompt_file:
            try:
                sp = Path(system_prompt_file).expanduser()
                system_prompt = sp.read_text(encoding="utf-8")
            except Exception as e:
                return f"ERROR: Failed to read system prompt file: {e}"

        # If --show and no overrides, just display current spec
        if show_spec and not system_prompt and not capabilities:
            return self._show_spec(pack_dir)

        # Build overlay
        if not system_prompt and not capabilities:
            return "ERROR: No options provided. Use --system-prompt, --system-prompt-file, --capabilities, or --show"

        overlay = {}
        if system_prompt:
            if len(system_prompt) > 8000:
                return "ERROR: system_prompt exceeds 8000 character limit"
            overlay["system_prompt"] = system_prompt

        if capabilities:
            overlay["capabilities"] = capabilities

        # Write agent.yaml.local
        local_yaml = pack_dir / "agent.yaml.local"
        try:
            local_yaml.write_text(yaml.dump(overlay, default_flow_style=False), encoding="utf-8")
        except Exception as e:
            return f"ERROR: Failed to write agent.yaml.local: {e}"

        # Show confirmation
        lines = []
        lines.append(f"✓ Customized pack '{pack_name}'")
        lines.append("")
        lines.append("Overrides saved to: " + str(local_yaml))
        lines.append("")
        if system_prompt:
            prompt_preview = system_prompt[:100].replace("\n", " ")
            lines.append(f"  system_prompt: {prompt_preview}...")
        if capabilities:
            lines.append(f"  capabilities: {', '.join(capabilities)}")
        lines.append("")
        lines.append("Apply with: adk run --agents " + pack_name)
        lines.append("")

        if show_spec:
            lines.append("Current effective spec:")
            lines.append("─" * 50)
            spec_text = self._show_spec(pack_dir)
            lines.append(spec_text)

        return "\n".join(lines)

    def _show_spec(self, pack_dir: Path) -> str:
        """Show the current effective agent spec (base + overrides)."""
        try:
            from adk.pack_discovery import load_agent_spec

            # Load the effective spec
            base_yaml = pack_dir / "agent.yaml"
            spec = load_agent_spec(base_yaml)

            if not spec:
                return "No agent.yaml found in pack"

            output = []
            for key, value in sorted(spec.items()):
                if isinstance(value, (list, tuple)):
                    val_str = "[" + ", ".join(str(v) for v in value) + "]"
                elif isinstance(value, dict):
                    val_str = "{...}"
                elif isinstance(value, str) and len(value) > 80:
                    val_str = value[:80] + "..."
                else:
                    val_str = str(value)
                output.append(f"  {key}: {val_str}")

            return "\n".join(output)
        except Exception as e:
            return f"ERROR: Failed to load spec: {e}"

    def _show_help(self) -> str:
        """Show detailed help text."""
        return """
=== PACK CUSTOMIZE HELP ===

Override agent pack configuration without modifying the installed pack.

SYNTAX:
  /pack customize <name> [options]

OPTIONS:
  --system-prompt TEXT         Override system prompt (max 8000 chars)
  --system-prompt-file PATH    Load system prompt from file
  --capabilities a,b,c         Override capabilities (comma-separated list)
  --show                       Display current effective spec

EXAMPLES:
  /pack customize openclaw --system-prompt "You are a coding expert"
  /pack customize hermes --system-prompt-file ~/.aither/my_prompt.txt
  /pack customize claude-code --capabilities code,web,file
  /pack customize openclaw --show

The customizations are saved to ~/.aither/agents/<name>/agent.yaml.local
and take effect the next time you run: adk run --agents <name>
"""
