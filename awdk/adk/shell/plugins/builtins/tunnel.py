"""
Tunnel Plugin for AitherShell
===============================

Set up a secure Cloudflare tunnel so api.aitherium.com can reach your
local AitherOS node for remote management, inference routing, and sync.

Usage:
    /tunnel setup                  — Create and start a tunnel
    /tunnel status                 — Show tunnel status
    /tunnel stop                   — Stop the tunnel
    /tunnel url                    — Show your tunnel URL
    /tunnel register               — Register tunnel URL with your account

Aliases: /tun
"""

import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore


def _identity_url() -> str:
    return os.environ.get(
        "AITHER_IDENTITY_URL",
        os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001"),
    )


def _headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def _require_auth() -> Optional[str]:
    if not AuthStore:
        return "Auth module not available. Run `aither setup` first."
    if not AuthStore.get_active_token():
        return "Not logged in. Run `aither setup` to authenticate."
    return None


def _cloudflared_path() -> Optional[str]:
    """Find cloudflared binary."""
    return shutil.which("cloudflared")


class TunnelPlugin(SlashCommand):
    name: str = "tunnel"
    aliases: List[str] = ["tun"]
    description: str = "Manage Cloudflare tunnel for remote portal access"
    category: str = "infrastructure"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='tunnel',
            description='Manage Cloudflare tunnel for remote portal access',
            aliases=['tun'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        err = _require_auth()
        if err:
            return err

        if not args:
            return await self._status(args, ctx)

        sub = args[0].lower()
        dispatch = {
            "setup": self._setup,
            "start": self._setup,
            "status": self._status,
            "stop": self._stop,
            "url": self._url,
            "register": self._register,
            "help": self._help,
        }
        handler = dispatch.get(sub)
        if handler:
            return await handler(args[1:], ctx)
        return await self._help(args, ctx)

    async def _setup(self, args: List[str], ctx: Dict[str, Any]) -> str:
        cf = _cloudflared_path()
        if not cf:
            return self._install_instructions()

        # Get user info for tunnel naming
        user = AuthStore.get_active_user() if AuthStore else None
        username = user.get("username", "aither") if user else "aither"

        # Default port mapping: expose Genesis (8001) through tunnel
        local_port = 8001
        for i, a in enumerate(args):
            if a in ("--port", "-p") and i + 1 < len(args):
                local_port = int(args[i + 1])

        # Use cloudflared quick tunnel (no Cloudflare account needed)
        # This creates a temporary *.trycloudflare.com URL
        lines = [f"**Setting up tunnel for local node (port {local_port})...**\n"]
        lines.append("Starting cloudflared quick tunnel...")
        lines.append(f"  Command: `cloudflared tunnel --url http://localhost:{local_port}`\n")

        try:
            # Start cloudflared in background
            proc = subprocess.Popen(
                [cf, "tunnel", "--url", f"http://localhost:{local_port}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            # Read initial output to get the tunnel URL
            import time
            tunnel_url = None
            deadline = time.time() + 15
            while time.time() < deadline:
                line = proc.stderr.readline() if proc.stderr else ""
                if "trycloudflare.com" in line or "cfargotunnel.com" in line:
                    # Extract URL
                    for word in line.split():
                        if "trycloudflare.com" in word or "cfargotunnel.com" in word:
                            tunnel_url = word.strip()
                            break
                    if tunnel_url:
                        break
                if proc.poll() is not None:
                    break
                time.sleep(0.5)

            if tunnel_url:
                # Clean up URL
                if not tunnel_url.startswith("http"):
                    tunnel_url = f"https://{tunnel_url}"

                # Save tunnel info
                tunnel_info = {
                    "url": tunnel_url,
                    "local_port": local_port,
                    "pid": proc.pid,
                    "username": username,
                }
                _save_tunnel_info(tunnel_info)

                lines.append(f"**Tunnel active!**")
                lines.append(f"  URL: `{tunnel_url}`")
                lines.append(f"  Local: `http://localhost:{local_port}`")
                lines.append(f"  PID: {proc.pid}")
                lines.append(f"\nRegister with portal: `/tunnel register`")
                lines.append(f"This URL is temporary. For persistent tunnels, use `cloudflared tunnel create`.")
            else:
                stderr = proc.stderr.read() if proc.stderr else ""
                lines.append(f"Tunnel may still be starting... check `/tunnel status`")
                if stderr:
                    lines.append(f"Output: {stderr[:300]}")

        except FileNotFoundError:
            return self._install_instructions()
        except Exception as e:
            lines.append(f"Error: {e}")

        return "\n".join(lines)

    async def _status(self, args: List[str], ctx: Dict[str, Any]) -> str:
        info = _load_tunnel_info()
        if not info:
            return (
                "No tunnel running.\n"
                "Start one: `/tunnel setup`"
            )

        pid = info.get("pid")
        url = info.get("url", "?")
        port = info.get("local_port", "?")
        alive = _is_process_alive(pid) if pid else False

        if alive:
            lines = ["**Tunnel Active**"]
            lines.append(f"  URL: `{url}`")
            lines.append(f"  Local: `http://localhost:{port}`")
            lines.append(f"  PID: {pid}")
            return "\n".join(lines)
        else:
            _clear_tunnel_info()
            return "Tunnel process not running. Start one: `/tunnel setup`"

    async def _stop(self, args: List[str], ctx: Dict[str, Any]) -> str:
        info = _load_tunnel_info()
        if not info:
            return "No tunnel running."

        pid = info.get("pid")
        if pid and _is_process_alive(pid):
            try:
                import signal
                os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass

        _clear_tunnel_info()
        return "Tunnel stopped."

    async def _url(self, args: List[str], ctx: Dict[str, Any]) -> str:
        info = _load_tunnel_info()
        if not info or not info.get("url"):
            return "No tunnel running. Start one: `/tunnel setup`"
        return f"`{info['url']}`"

    async def _register(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        info = _load_tunnel_info()
        if not info or not info.get("url"):
            return "No tunnel running. Start one: `/tunnel setup`"

        tunnel_url = info["url"]

        # Register tunnel URL as user's node endpoint
        # This updates the user's inference config to include the tunnel URL
        # so portal.aitherium.com can route requests through it
        async with httpx.AsyncClient(
            base_url=_identity_url(), headers=_headers(), timeout=15
        ) as c:
            # Update user metadata with tunnel URL
            resp = await c.patch(
                "/auth/me/profile",
                json={
                    "metadata": {
                        "node_tunnel_url": tunnel_url,
                        "node_registered_at": __import__("datetime").datetime.utcnow().isoformat(),
                    }
                },
            )

        if resp.status_code != 200:
            return f"Failed to register tunnel: {resp.status_code}"

        lines = ["**Tunnel registered with portal**"]
        lines.append(f"  Tunnel URL: `{tunnel_url}`")
        lines.append(f"\nPortal can now reach your local node remotely.")
        lines.append(f"Configure inference routing: `/inference set --provider ollama --url {tunnel_url}/ollama`")
        return "\n".join(lines)

    async def _help(self, args: List[str], ctx: Dict[str, Any]) -> str:
        return (
            "**Tunnel Management**\n\n"
            "Expose your local AitherOS node to api.aitherium.com:\n"
            "  `/tunnel setup` — Start a Cloudflare quick tunnel\n"
            "  `/tunnel setup --port 8001` — Tunnel a specific port\n"
            "  `/tunnel status` — Show tunnel status and URL\n"
            "  `/tunnel url` — Get just the URL\n"
            "  `/tunnel register` — Register URL with your portal account\n"
            "  `/tunnel stop` — Stop the tunnel\n\n"
            "The tunnel lets you:\n"
            "  - Manage agents from api.aitherium.com remotely\n"
            "  - Route portal inference to your local GPU\n"
            "  - Sync local Strata data with the cloud"
        )

    def _install_instructions(self) -> str:
        import platform
        system = platform.system().lower()
        if "linux" in system:
            cmd = "curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared"
        elif "darwin" in system:
            cmd = "brew install cloudflared"
        else:
            cmd = "winget install Cloudflare.cloudflared"
        return (
            f"**cloudflared not found**\n\n"
            f"Install it:\n"
            f"  `{cmd}`\n\n"
            f"Then run `/tunnel setup` again."
        )


# ── Tunnel state persistence ──

from pathlib import Path

_TUNNEL_FILE = Path.home() / ".aither" / "tunnel.json"


def _save_tunnel_info(info: Dict[str, Any]) -> None:
    _TUNNEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TUNNEL_FILE.write_text(json.dumps(info, indent=2, default=str), encoding="utf-8")


def _load_tunnel_info() -> Optional[Dict[str, Any]]:
    if not _TUNNEL_FILE.exists():
        return None
    try:
        return json.loads(_TUNNEL_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _clear_tunnel_info() -> None:
    try:
        _TUNNEL_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
