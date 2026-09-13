"""
ADK Shell Plugin: Node Enrollment
Register this workstation with the control plane (portal/Genesis).
"""

import asyncio
import os
from typing import List

from adk.shell.plugins import SlashCommand


class EnrollPlugin(SlashCommand):
    """
    /enroll — Register this workstation with the control plane.

    Shows hardware info, available models, and enrollment status.
    Persists node_id to ~/.aither/node_auth.json for future heartbeats.

    Subcommands:
      /enroll                          Enroll with defaults (api.aitherium.com)
      /enroll --portal https://...     Custom portal URL
      /enroll --genesis http://...     Custom Genesis URL
      /enroll --no-heartbeat           Skip background heartbeat
      /enroll --force                  Re-enroll even if already registered
    """

    name = "enroll"
    aliases = []
    category = "onboarding"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='enroll',
            description='',
        )

    def execute(self, args: List[str], **kwargs) -> str:
        """Main entry point for /enroll command."""
        try:
            return asyncio.run(self._enroll_async(args))
        except RuntimeError:
            # Event loop already running (shouldn't happen in shell, but handle gracefully)
            return (
                "ERROR: Cannot enroll from within an async context. "
                "Try running 'adk enroll' from the command line instead."
            )
        except Exception as e:
            return f"ERROR: Enrollment failed: {e}"

    async def _enroll_async(self, args: List[str]) -> str:
        """Async enrollment logic."""
        from adk.fleet_enroll import enroll_on_boot, _load_node_auth, _generate_node_id
        from adk.enrollment import build_registration

        # Parse arguments
        portal_url = None
        genesis_url = None
        no_heartbeat = False
        force = False

        i = 0
        while i < len(args):
            if args[i] == "--portal" and i + 1 < len(args):
                portal_url = args[i + 1]
                i += 2
            elif args[i] == "--genesis" and i + 1 < len(args):
                genesis_url = args[i + 1]
                i += 2
            elif args[i] == "--no-heartbeat":
                no_heartbeat = True
                i += 1
            elif args[i] == "--force":
                force = True
                i += 1
            else:
                i += 1

        # Check if already enrolled
        existing = _load_node_auth()
        if existing.get("node_id") and not force:
            node_id = existing["node_id"]
            lines = []
            lines.append(f"✓ Already enrolled: {node_id}")
            lines.append("")
            lines.append("Node details:")
            lines.append(f"  Tenant: {existing.get('tenant_slug', 'unknown')}")
            lines.append(f"  Hub: {existing.get('hub_url', 'unknown')}")
            lines.append(f"  Mode: {existing.get('mode', 'legacy')}")
            lines.append("")
            lines.append("To re-enroll, use: /enroll --force")
            return "\n".join(lines)

        # Defaults
        portal_url = portal_url or os.environ.get("AITHER_PORTAL_URL", "https://api.aitherium.com")
        genesis_url = genesis_url or os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001")

        # Generate node ID if needed
        node_id = _generate_node_id()

        # Build registration payload
        reg = build_registration(node_id)

        # Perform enrollment
        result = await enroll_on_boot(
            genesis_url=genesis_url,
            portal_url=portal_url,
            enable_heartbeat=not no_heartbeat,
        )

        if not result.get("enrolled"):
            error_detail = result.get("error", "unknown error")
            return f"ERROR: Enrollment failed: {error_detail}"

        # Format success response
        lines = []
        lines.append("✓ Enrollment successful")
        lines.append("")
        lines.append("Node Information:")
        lines.append(f"  ID: {result.get('node_id', 'unknown')}")
        lines.append(f"  Hardware:")
        lines.append(f"    CPU: {reg.get('cpu_count', 0)} cores")
        lines.append(f"    RAM: {reg.get('ram_mb', 0)} MB")
        if reg.get("gpu_name"):
            lines.append(f"    GPU: {reg['gpu_name']} ({reg.get('gpu_vram_mb', 0)} MB)")
        else:
            lines.append(f"    GPU: none")
        lines.append(f"  Available Models: {', '.join(reg.get('available_models', []))[:80]}")
        lines.append("")
        lines.append("Fleet Status:")
        lines.append(f"  Workspace ID: {result.get('workspace_id', 'N/A')}")
        lines.append(f"  Agents Upserted: {result.get('agents_upserted', 0)}")
        if not no_heartbeat:
            lines.append(f"  Heartbeat: enabled (60s interval)")
        lines.append("")
        lines.append(f"View in portal: {portal_url.rstrip('/')}/portal/workstation")
        lines.append("")

        return "\n".join(lines)
