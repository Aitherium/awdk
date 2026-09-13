"""
ADK Shell Plugin: MCP Workstation
Launch local MCP server and optionally register it with the portal.
"""

import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from adk.shell.plugins import SlashCommand


class MCPWorkstationPlugin(SlashCommand):
    """
    /mcp-workstation — Launch a local MCP server and register with portal.

    Starts awnode mcp as a background subprocess and optionally registers
    the endpoint with the portal so agents can discover and use your local tools.

    Subcommands:
      /mcp-workstation                         Start MCP on port 8090, register with portal
      /mcp-workstation --port 9000             Use custom port
      /mcp-workstation --public-url URL        Register with custom public URL (else localhost)
      /mcp-workstation --no-register           Start locally only (no portal registration)
    """

    name = "mcp-workstation"
    aliases = ["mcp"]
    category = "infrastructure"

    _running_process: Optional[subprocess.Popen] = None

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='mcp-workstation',
            description='',
            aliases=['mcp'],
        )

    def execute(self, args: List[str], **kwargs) -> str:
        """Main entry point for /mcp-workstation command."""
        try:
            return self._start_mcp(args)
        except Exception as e:
            return f"ERROR: MCP workstation failed: {e}"

    def _start_mcp(self, args: List[str]) -> str:
        """Start MCP server and optionally register it."""
        # Parse arguments
        port = 8090
        public_url = None
        no_register = False

        i = 0
        while i < len(args):
            if args[i] == "--port" and i + 1 < len(args):
                try:
                    port = int(args[i + 1])
                except ValueError:
                    return f"ERROR: --port must be a number, got '{args[i + 1]}'"
                i += 2
            elif args[i] == "--public-url" and i + 1 < len(args):
                public_url = args[i + 1]
                i += 2
            elif args[i] == "--no-register":
                no_register = True
                i += 1
            else:
                i += 1

        # Check for awnode executable
        awnode = shutil.which("awnode")
        if not awnode:
            return (
                "ERROR: 'aithernode' not found. Install with:\n"
                "  pip install awnode\n"
                "or\n"
                "  adk install awnode"
            )

        # Start MCP server as subprocess
        try:
            cmd = [awnode, "mcp", "--transport", "sse", "--port", str(port)]
            self._running_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as e:
            return f"ERROR: Failed to start awnode: {e}"

        # Determine public URL
        if not public_url:
            public_url = f"http://localhost:{port}"

        # Registration (optional)
        registration_status = ""
        if not no_register:
            reg_result = self._register_endpoint(public_url, port)
            registration_status = f"\n{reg_result}"

        # Format response
        lines = []
        lines.append(f"✓ MCP Workstation started on port {port}")
        lines.append(f"  URL: {public_url}")
        lines.append(f"  PID: {self._running_process.pid}")
        lines.append("")
        lines.append("Local agents can now discover tools from your workstation.")
        lines.append("To see available tools, run: /mcp-workstation --show-tools")
        lines.append("")
        lines.append("Stopping MCP: /mcp-workstation --stop")
        lines.append(registration_status)

        return "\n".join(lines)

    def _register_endpoint(self, public_url: str, port: int) -> str:
        """Register MCP endpoint with portal (best-effort)."""
        try:
            from adk.fleet_enroll import _load_auth_config
            import httpx

            # Get auth token
            auth = _load_auth_config()
            token = auth.get("access_token")
            if not token:
                return "Note: Not registered (no auth token). Run 'adk login' to enable portal registration."

            # Determine endpoint name
            hostname = socket.gethostname()
            endpoint_name = f"{hostname}-mcp"

            # Register endpoint
            portal_url = os.environ.get("AITHER_PORTAL_URL", "https://api.aitherium.com")
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            try:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(
                        f"{portal_url.rstrip('/')}/v1/agent/mcp-endpoints",
                        json={"name": endpoint_name, "url": public_url},
                        headers=headers,
                    )
                    if resp.status_code in (200, 201):
                        return f"✓ Registered with portal as '{endpoint_name}'"
                    else:
                        detail = resp.text[:100]
                        return f"Note: Portal registration failed (HTTP {resp.status_code})"
            except Exception as e:
                return f"Note: Portal registration unavailable ({e})"
        except Exception as e:
            return f"Note: Could not register endpoint ({e})"
