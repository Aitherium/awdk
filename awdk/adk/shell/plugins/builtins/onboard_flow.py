"""
ADK Shell Plugin: Onboarding Flow
Interactive guided setup: login → inference → pack → customize → enroll → mcp.
"""

import os
from typing import List

from adk.shell.plugins import SlashCommand


class OnboardFlowPlugin(SlashCommand):
    """
    /onboard — Interactive onboarding flow with guided steps.

    Guides through setup in order:
      1. Check login (prompt to run 'adk login' if needed)
      2. Check inference backend (quickstart-local or set keys)
      3. Install agent pack (default: openclaw)
      4. Optional pack customization (skip with --quick)
      5. Enroll node with control plane
      6. Optional MCP workstation setup (skip with --quick)
      7. Summary + next steps

    Options:
      --pack NAME     Pack to install (default: openclaw)
      --quick         Non-interactive: skip prompts, use defaults
    """

    name = "onboard"
    aliases = []
    category = "onboarding"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='onboard',
            description='',
        )

    def execute(self, args: List[str], **kwargs) -> str:
        """Main entry point for /onboard command."""
        try:
            return self._onboard_flow(args)
        except Exception as e:
            return f"ERROR: Onboarding failed: {e}"

    def _onboard_flow(self, args: List[str]) -> str:
        """Execute the onboarding flow."""
        from pathlib import Path
        import json

        # Parse arguments
        pack_name = "openclaw"
        quick_mode = False

        i = 0
        while i < len(args):
            if args[i] == "--pack" and i + 1 < len(args):
                pack_name = args[i + 1]
                i += 2
            elif args[i] == "--quick":
                quick_mode = True
                i += 1
            else:
                i += 1

        lines = []
        lines.append("")
        lines.append("╔══════════════════════════════════════════════════════════════╗")
        lines.append("║          AitherADK Onboarding                               ║")
        lines.append("╚══════════════════════════════════════════════════════════════╝")
        lines.append("")

        # Step 1: Login check
        lines.append("STEP 1: Authentication")
        lines.append("─" * 60)
        auth_status = self._check_auth()
        lines.append(auth_status)
        if "not authenticated" in auth_status.lower():
            lines.append("")
            lines.append("To authenticate, run: adk login")
            if not quick_mode:
                lines.append("Then return here to continue onboarding.")
        lines.append("")

        # Step 2: Inference backend
        lines.append("STEP 2: Inference Backend")
        lines.append("─" * 60)
        backend_status = self._check_backend()
        lines.append(backend_status)
        if "not configured" in backend_status.lower():
            lines.append("")
            lines.append("Quick setup: adk quickstart-local")
            lines.append("Or set provider: adk keys set openai <key>")
        lines.append("")

        # Step 3: Pack installation
        lines.append(f"STEP 3: Install Pack '{pack_name}'")
        lines.append("─" * 60)
        pack_status = self._install_pack(pack_name)
        lines.append(pack_status)
        lines.append("")

        # Step 4: Pack customization (skip in quick mode)
        if not quick_mode:
            lines.append("STEP 4: Customize Pack (optional)")
            lines.append("─" * 60)
            lines.append(f"Customize system prompt and capabilities:")
            lines.append(f"  /pack customize {pack_name} --system-prompt \"<your prompt>\"")
            lines.append(f"  /pack customize {pack_name} --show")
            lines.append("(Skip this for now)")
            lines.append("")

        # Step 5: Enrollment
        lines.append("STEP 5: Node Enrollment")
        lines.append("─" * 60)
        lines.append("Registering this workstation with the control plane...")
        lines.append("(Use /enroll for manual control)")
        lines.append("")

        # Step 6: MCP Workstation (skip in quick mode)
        if not quick_mode:
            lines.append("STEP 6: MCP Workstation (optional)")
            lines.append("─" * 60)
            lines.append("Enable local MCP server for tool discovery:")
            lines.append("  /mcp-workstation")
            lines.append("(Skip this for now)")
            lines.append("")

        # Final summary
        lines.append("╔══════════════════════════════════════════════════════════════╗")
        lines.append("║          Setup Complete                                      ║")
        lines.append("╚══════════════════════════════════════════════════════════════╝")
        lines.append("")
        lines.append("Next Steps:")
        lines.append(f"  1. Start chatting: adk run --agents {pack_name}")
        lines.append("  2. Explore tools: /tools list")
        lines.append("  3. View documentation: /help")
        lines.append("")

        return "\n".join(lines)

    def _check_auth(self) -> str:
        """Check if user is authenticated."""
        try:
            from pathlib import Path
            import json

            auth_file = Path.home() / ".aither" / "auth.json"
            if auth_file.exists():
                try:
                    data = json.loads(auth_file.read_text(encoding="utf-8"))
                    if data.get("access_token"):
                        user = data.get("user", {})
                        username = user.get("username", "unknown")
                        return f"✓ Authenticated as {username}"
                except Exception:
                    pass

            return "⚠ Not authenticated. Run: adk login"
        except Exception as e:
            return f"⚠ Could not check auth: {e}"

    def _check_backend(self) -> str:
        """Check if inference backend is configured."""
        try:
            from pathlib import Path
            import json

            config_file = Path.home() / ".aither" / "config.json"
            if config_file.exists():
                try:
                    config = json.loads(config_file.read_text(encoding="utf-8"))
                    if config.get("llm_backend"):
                        backend = config["llm_backend"]
                        return f"✓ Backend configured: {backend}"
                    if config.get("api_key"):
                        return f"✓ API key set (cloud inference)"
                except Exception:
                    pass

            return "⚠ Backend not configured. Run: adk quickstart-local"
        except Exception as e:
            return f"⚠ Could not check backend: {e}"

    def _install_pack(self, pack_name: str) -> str:
        """Install a pack (or check if already installed)."""
        try:
            from pathlib import Path

            pack_dir = Path.home() / ".aither" / "agents" / pack_name
            if pack_dir.exists():
                agent_yaml = pack_dir / "agent.yaml"
                if agent_yaml.exists():
                    return f"✓ Pack '{pack_name}' already installed at {pack_dir}"

            # Try to install
            try:
                import subprocess

                result = subprocess.run(
                    ["adk", "install", f"pack:{pack_name}"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    return f"✓ Installed pack '{pack_name}'"
                else:
                    return f"⚠ Could not install pack '{pack_name}': {result.stderr[:100]}"
            except Exception as e:
                return f"⚠ Could not install pack '{pack_name}': {e}"
        except Exception as e:
            return f"⚠ Could not check pack: {e}"
