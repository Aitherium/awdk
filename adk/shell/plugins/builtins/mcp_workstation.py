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

        # The bearer the awnode SSE server enforces (AITHER_MCP_KEY). Reuse the
        # configured one, else mint one, so the SAME key reaches the server and
        # the portal registration (an endpoint registered without it is a 401).
        mcp_key, key_note = self._resolve_mcp_key()

        # Start MCP server as subprocess
        try:
            cmd = [awnode, "mcp", "--transport", "sse", "--port", str(port)]
            env = os.environ.copy()
            env["AITHER_MCP_KEY"] = mcp_key
            self._running_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        except Exception as e:
            return f"ERROR: Failed to start awnode: {e}"

        # Determine public URL
        if not public_url:
            public_url = f"http://localhost:{port}"

        # Registration (optional)
        registration_status = ""
        if not no_register:
            reg_result = self._register_endpoint(public_url, port, mcp_key)
            registration_status = f"\n{reg_result}"

        # Format response
        lines = []
        lines.append(f"✓ MCP Workstation started on port {port}")
        lines.append(f"  URL: {public_url}")
        lines.append(f"  PID: {self._running_process.pid}")
        lines.append(f"  Bearer: {key_note}")
        lines.append("")
        lines.append("Local agents can now discover tools from your workstation.")
        lines.append("To see available tools, run: /mcp-workstation --show-tools")
        lines.append("")
        lines.append("Stopping MCP: /mcp-workstation --stop")
        lines.append(registration_status)

        return "\n".join(lines)

    @staticmethod
    def _resolve_mcp_key() -> Tuple[str, str]:
        """(key, note). The note names a fingerprint and where the key lives,
        never the key value itself."""
        from adk.mcp_server import key_fingerprint, persist_session_key

        for var in ("AITHER_MCP_KEY", "AITHER_SERVER_API_KEY"):
            val = os.environ.get(var, "").strip()
            if val:
                return val, f"{key_fingerprint(val)} (from ${var})"
        import secrets

        key = f"adk_mcp_{secrets.token_hex(16)}"
        path = persist_session_key(key, "mcp-workstation")
        where = f"stored at {path}" if path else "not persisted; set AITHER_MCP_KEY"
        return key, f"{key_fingerprint(key)} (generated, {where})"

    def _register_endpoint(self, public_url: str, port: int, mcp_key: str = "") -> str:
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
            body = {"name": endpoint_name, "url": public_url}
            if mcp_key:
                # Genesis stores it in the vault so the gateway can call back;
                # without it the registered endpoint lists 0 tools (401).
                body["token"] = mcp_key
            if public_url.startswith(("http://localhost", "http://127.0.0.1")):
                body["local"] = True

            try:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(
                        f"{portal_url.rstrip('/')}/v1/agent/mcp-endpoints",
                        json=body,
                        headers=headers,
                    )
                    hint = ""
                    try:
                        payload = resp.json()
                        if isinstance(payload, dict):
                            hint = str(payload.get("hint") or "")
                            detail = payload.get("detail")
                            if not hint and isinstance(detail, str):
                                hint = detail
                            elif not hint and isinstance(detail, dict):
                                # Genesis 400s carry {error, detail}: show both.
                                keys = ("hint", "error", "detail")
                                parts = [str(detail.get(k) or "") for k in keys]
                                hint = ": ".join(p for p in parts if p)
                    except Exception:  # noqa: BLE001 - non-JSON body
                        hint = ""
                    if resp.status_code in (200, 201):
                        msg = f"✓ Registered with portal as '{endpoint_name}'"
                    else:
                        msg = f"Note: Portal registration failed (HTTP {resp.status_code})"
                    if hint:
                        msg += f"\n  Hint: {hint[:300]}"
                    return msg
            except Exception as e:
                return f"Note: Portal registration unavailable ({e})"
        except Exception as e:
            return f"Note: Could not register endpoint ({e})"
