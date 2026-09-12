"""CLI scaffolding — `aither init` and `aither run` commands.

Usage:
    aither init myproject          # Scaffold a new agent project
    aither run                     # Start the server (reads config.yaml)
    aither run --identity lyra -p 9000
"""

from __future__ import annotations
from adk._tls import tls_verify

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from adk.config import load_saved_config, save_saved_config
from adk.images import cmd_image


def _control_plane() -> str:
    """Base URL of the control plane (the SERVER-RENDERED portal).

    The default was `https://veil.aitherium.com` at 13 sites, and that host
    is a GitHub Pages STATIC export by deliberate design — it serves the
    marketing site and NO API, so every control-plane call against it 404s.
    Measured 2026-09-04:

        veil.aitherium.com   /api/genesis/v1/agent/agents -> 404  (Server: GitHub.com)
        portal.aitherium.com /api/genesis/v1/agent/agents -> 401  (route exists, wants auth)

    The visible symptom was NOT a 404. `adk agents ls` folded it into
    "Cloud registry unreachable" and then listed only the local agents, so a
    fleet of healthy running agents read as "no agent named X in the mesh" —
    which sends you to look at the agent, not at the URL. `awrun` dispatch of
    `kind=agent` failed the same way for the same reason.

    Order matches the one site in this file that was already correct, so
    there is now exactly one answer to "where is the control plane".
    """
    import os as _os
    return (
        _os.environ.get("AITHER_PORTAL_URL")
        or _os.environ.get("AITHER_ELYSIUM_URL")
        or "https://portal.aitherium.com"
    ).rstrip("/")

def _fix_ollama_host(raw: str) -> str:
    """Rewrite Ollama's bind address (0.0.0.0) to connectable localhost."""
    if not raw:
        return "http://localhost:11434"
    if "0.0.0.0" in raw:
        raw = raw.replace("0.0.0.0", "localhost")
    if not raw.startswith("http"):
        raw = "http://" + raw
    return raw

_AGENT_TEMPLATE = '''\
"""My AitherADK agent."""

from adk import AitherAgent, tool

agent = AitherAgent("{name}")


@agent.tool
def hello(name: str) -> str:
    """Greet someone by name."""
    return f"Hello, {{name}}!"


async def main():
    response = await agent.chat("Say hello to the world")
    print(response.content)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
'''

_CONFIG_TEMPLATE = """\
# AitherADK agent configuration
# See https://github.com/Aitherium/awdk for docs

identity: {name}
port: 8080

# LLM backend: auto, ollama, openai, anthropic, gateway
llm_backend: auto

# Uncomment to set a specific model
# model: nemotron-orchestrator-8b

# Built-in tools (enabled by default)
builtin_tools: true

# Safety checks (enabled by default)
safety: true

# Required tool packs (auto-loaded at startup)
# required_packs:
#   - git-github
#   - codegraph
"""

_TOOLS_TEMPLATE = '''\
"""Custom tools for your agent."""

from adk import tool


@tool
def search_docs(query: str) -> str:
    """Search project documentation."""
    # Replace with your actual implementation
    return f"Found docs matching: {{query}}"


@tool
def get_status() -> str:
    """Get current project status."""
    return "All systems operational."
'''


def cmd_init(args):
    """Scaffold a new agent project directory."""
    name = args.name or "my-agent"
    target = Path(args.directory or name)

    if target.exists() and any(target.iterdir()):
        print(f"Error: {target} already exists and is not empty.")
        return 1

    target.mkdir(parents=True, exist_ok=True)

    (target / "agent.py").write_text(
        _AGENT_TEMPLATE.format(name=name), encoding="utf-8"
    )
    (target / "config.yaml").write_text(
        _CONFIG_TEMPLATE.format(name=name), encoding="utf-8"
    )
    (target / "tools.py").write_text(
        _TOOLS_TEMPLATE, encoding="utf-8"
    )

    print(f"Created AitherADK project at {target}/")
    print("  agent.py   — Your agent definition")
    print("  config.yaml — Configuration")
    print("  tools.py   — Custom tools")
    print()
    print("Next steps:")
    print(f"  cd {target}")
    print("  adk run              # Start the server")
    print("  python agent.py      # Run directly")
    print()
    print("Using an AI agent? Run `adk agent-prompt` for a copy-paste setup guide.")

    # OpenClaw detection — prompt integration if detected
    openclaw_dir = Path.home() / ".openclaw"
    if openclaw_dir.exists():
        oc_config = {}
        oc_config_path = openclaw_dir / "openclaw.json"
        if oc_config_path.exists():
            try:
                import json
                oc_config = json.loads(oc_config_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

        aither_integrated = any(
            "aither" in k.lower()
            for k in oc_config.get("mcpServers", {})
        )
        if not aither_integrated:
            print()
            print("  OpenClaw detected! Connect it to AitherOS agents:")
            print("  adk integrate openclaw")

    return 0


def cmd_new(args):
    """Scaffold a complete project from a bundled template — e.g. `adk new deep-research`.

    Templates live in adk/templates/<name>/ and are copied verbatim. Unlike `adk init`
    (a minimal stub), these are full, runnable apps (a pack + a server + a web UI).
    """
    import shutil

    templates_dir = Path(__file__).resolve().parent / "templates"
    src = templates_dir / args.template
    if not src.is_dir() or not any(src.iterdir()):
        available = sorted(
            p.name for p in templates_dir.iterdir()
            if p.is_dir() and (p / "serve.py").exists()
        ) if templates_dir.is_dir() else []
        print(f"Unknown template '{args.template}'.")
        if available:
            print(f"Available templates: {', '.join(available)}")
        return 1

    target = Path(args.directory or args.template)
    if target.exists() and any(target.iterdir()):
        print(f"Error: {target} already exists and is not empty.")
        return 1

    shutil.copytree(src, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"Created '{args.template}' at {target}/")
    print()
    print("Next steps:")
    print(f"  cd {target}")
    print("  pip install -r requirements.txt")
    print("  python serve.py        # opens a browser; paste your LLM key in the UI")
    return 0


def cmd_create_app(args):
    """Scaffold a full awkit workspace app using WorkspaceRuntime template."""
    import subprocess as _sp

    slug = args.subdomain or re.sub(r"[^a-z0-9-]", "-", args.name.lower().strip()).strip("-")[:30]
    output = args.output or f"./{slug}"

    # Locate scaffold.py — check several known paths
    scaffold_candidates = [
        # When installed inside a dev workspace with aitheros monorepo
        Path("/workspace/aitheros/AitherOS/apps/WorkspaceRuntime/scaffold.py"),
        # Relative to repo root on local dev
        Path(__file__).resolve().parents[2] / "AitherOS" / "apps" / "WorkspaceRuntime" / "scaffold.py",
        # Sibling directory (standalone workspace)
        Path.cwd() / "WorkspaceRuntime" / "scaffold.py",
    ]

    scaffold_path = None
    for candidate in scaffold_candidates:
        if candidate.exists():
            scaffold_path = candidate
            break

    if not scaffold_path:
        # Fallback: download scaffold.py from GitHub
        print("WorkspaceRuntime scaffold not found locally — downloading...")
        import urllib.request
        import tempfile
        dl_url = "https://raw.githubusercontent.com/Aitherium/AitherOS/develop/AitherOS/apps/WorkspaceRuntime/scaffold.py"
        try:
            tmp = Path(tempfile.mkdtemp()) / "scaffold.py"
            urllib.request.urlretrieve(dl_url, str(tmp))
            scaffold_path = tmp
            print(f"  Downloaded to {tmp}")
        except Exception as e:
            print(f"Error: Could not download scaffold: {e}")
            print()
            print("Expected locations:")
            for c in scaffold_candidates:
                print(f"  {c}")
            print()
            print("If you're in a dev workspace, make sure 'aitheros' is in your repos.")
            return 1

    # Build scaffold.py arguments
    cmd = [
        sys.executable, str(scaffold_path),
        "--name", args.name,
        "--output", output,
        "--subdomain", slug,
        "--llm-provider", args.llm_provider,
    ]
    if args.company:
        cmd += ["--company", args.company]
    if args.industry:
        cmd += ["--industry", args.industry]
    if args.description:
        cmd += ["--description", args.description]
    if args.color:
        cmd += ["--color", args.color]
    if args.force:
        cmd.append("--force")

    print(f"Scaffolding '{args.name}' -> {output}")
    print()
    result = _sp.run(cmd)
    if result.returncode == 0:
        print()
        print("Next: connect to AitherOS backend for inference:")
        print(f"  cd {output}")
        print("  .DEPLOYMENT/scripts/compose.sh aitheros -f docker-compose.yml up -d")
        print()
        print("Or standalone (local LLM):")
        print(f"  cd {output}")
        print("  docker compose up -d")
    return result.returncode


def cmd_ssh_cert(args):
    """Fetch a short-lived SSH certificate from the AitherCert SSH CA.

    Signs your SSH public key with the tenant SSH CA (the one whose public key
    is registered in GitHub org settings -> SSH certificate authorities) and
    installs the certificate next to the private key, where OpenSSH picks it
    up automatically. Renewal = re-run this command.

    Passphrase resolution order (never echoed, never stored by adk):
      1. $AITHER_SSH_CA_PASSPHRASE
      2. interactive prompt (getpass)
    """
    import getpass

    import requests

    key_path = Path(getattr(args, "key", None)
                    or os.path.expanduser("~/.ssh/id_ed25519.pub"))
    if not key_path.name.endswith(".pub"):
        print(f"--key must point at a PUBLIC key (.pub): {key_path}")
        return 1
    if not key_path.exists():
        print(f"Public key not found: {key_path} (generate one: ssh-keygen -t ed25519)")
        return 1
    public_key = key_path.read_text().strip()

    github_user = getattr(args, "github_user", None)
    if not github_user:
        cfg = load_saved_config()
        github_user = cfg.get("github_username") or ""
    if not github_user:
        print("No GitHub username. Pass --github-user <login>.")
        return 1

    base = (getattr(args, "cert_url", None)
            or os.environ.get("AITHER_CERT_URL", "https://localhost:8113")).rstrip("/")
    passphrase = os.environ.get("AITHER_SSH_CA_PASSPHRASE") or getpass.getpass(
        "SSH CA passphrase: ")
    if not passphrase:
        print("A passphrase is required (the CA signs nothing without it).")
        return 1

    body = {
        "passphrase": passphrase,
        "public_key": public_key,
        "github_username": github_user,
        "ttl_hours": int(getattr(args, "ttl_hours", 24) or 24),
    }
    try:
        resp = requests.post(f"{base}/ssh-ca/sign", json=body,
                             timeout=60, verify=tls_verify())
    except requests.RequestException as e:
        print(f"SSH CA unreachable at {base}: {e}")
        return 1
    if resp.status_code == 403:
        print("Wrong SSH CA passphrase (fail-closed; nothing was issued).")
        return 1
    if resp.status_code == 404:
        print("No SSH CA exists yet — create it first: POST /ssh-ca/create on AitherCert.")
        return 1
    if resp.status_code != 200:
        print(f"Signing failed: HTTP {resp.status_code}: {resp.text[:300]}")
        return 1

    data = resp.json()
    cert_path = key_path.with_name(key_path.name[:-len(".pub")] + "-cert.pub")
    cert_path.write_text(data["certificate"] + "\n")
    print(f"Certificate installed: {cert_path}")
    print(f"  github user: {data.get('github_username', github_user)}")
    print(f"  serial:      {data.get('serial', '?')}")
    print(f"  valid until: {data.get('valid_until', '?')}")
    print("OpenSSH picks the cert up automatically. Verify: ssh -T git@github.com")
    return 0


def cmd_ssh(args):
    """Open a raw interactive terminal into a prod/dev environment via the tunnel.

    Self-contained websocket-PTY client (POSIX): reuses the SAME server gateway as
    `aither connect` — wss://<tunnel>/tunnel/ssh?token=<jwt>[&container=<ws>] — and the
    same JSON frame protocol. The PTY is server-side (tmux-backed), so no local pty
    lib is needed; we just put the local TTY in raw mode and shuttle frames.
    Ctrl-Q detaches (leaves the session running for reconnect).
    """
    import asyncio
    import json as _json
    from urllib.parse import urlencode

    container = getattr(args, "container", None) or getattr(args, "container_opt", None)
    tunnel = getattr(args, "tunnel_url", None) or "tunnel.aitherium.com"
    host = tunnel.replace("https://", "").replace("wss://", "").replace("http://", "").rstrip("/")

    cfg = load_saved_config()
    # Prefer the device-flow access_token (a real JWT the tunnel validates at
    # /identity/auth/me); fall back to an API key / env. The auto-provisioned
    # local root key (`aither_root_local`) is NOT a login the tunnel accepts —
    # it closes 4001 — so when config.json still carries it, look past it to
    # the active auth.json profile (where `adk login` / `aither login` write).
    from adk.config import _active_profile_creds
    token = (cfg.get("access_token") or cfg.get("api_key")
             or os.environ.get("AITHER_API_KEY", ""))
    if not token or token.startswith("aither_root_"):
        token = _active_profile_creds().get("access_token") or token
    if not token or token.startswith("aither_root_"):
        print("Not authenticated. Run: adk login")
        return 1

    # ── Headless one-shot: `adk ssh -x "<cmd>"` / `adk ssh [container] -- <cmd…>` ──
    # No TTY, no termios, works on Windows: CI, cron, PowerShell, Claude Code/Codex.
    commands = list(getattr(args, "exec_cmd", None) or [])
    trailing = [t for t in (getattr(args, "trailing", None) or []) if t != "--"]
    if "--" in sys.argv:
        # argparse hands the first word after `--` to the optional `container`
        # positional (nargs="?") and only the rest to REMAINDER. Rebuild the
        # command from argv, and drop `container` if it came from after `--`.
        dash = sys.argv.index("--")
        trailing = sys.argv[dash + 1:]
        if container and container not in sys.argv[:dash]:
            container = None
    if trailing:
        commands.append(" ".join(trailing))
    if commands:
        from adk.tunnel_exec import exec_remote_sync
        as_json = bool(getattr(args, "json", False))
        sink = None if as_json else (lambda chunk: (sys.stdout.write(chunk), sys.stdout.flush()))
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # the banner has a ⚠
        except Exception:
            pass
        result = exec_remote_sync(
            commands, token=token, container=container, host=host,
            timeout_s=float(getattr(args, "timeout", 120) or 120), sink=sink,
        )
        if as_json:
            print(json.dumps({"code": result.code, "output": result.output, "reason": result.reason}))
        elif result.reason in ("error", "timeout"):
            last = result.output.strip().splitlines()[-1:] or [""]
            print(f"{result.reason}: {last[0]}", file=sys.stderr)
        return result.code

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("adk ssh needs an interactive terminal (TTY) — or pass -x \"<cmd>\" to run headless.")
        return 1
    if os.name != "posix":
        print("adk ssh raw mode is POSIX-only. On Windows use `aither connect`, or `adk ssh -x \"<cmd>\"`.")
        return 1
    try:
        import signal
        import termios
        import tty

        import websockets
    except ImportError:
        print("adk shell needs the 'websockets' package: pip install websockets")
        return 1

    qs = {"token": token}
    if container:
        qs["container"] = container
    url = f"wss://{host}/tunnel/ssh?{urlencode(qs)}"
    print(f"  connecting to {container or host} … (Ctrl-Q to detach)")

    async def _run():
        async with websockets.connect(url, max_size=None) as ws:
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setraw(fd)
            loop = asyncio.get_event_loop()

            async def _send_size():
                cols, rows = os.get_terminal_size()
                await ws.send(_json.dumps({"type": "resize", "cols": cols, "rows": rows}))

            try:
                loop.add_signal_handler(
                    signal.SIGWINCH, lambda: asyncio.ensure_future(_send_size()))
            except (NotImplementedError, RuntimeError):
                pass
            await _send_size()

            async def _pump_stdin():
                reader = asyncio.StreamReader()
                await loop.connect_read_pipe(
                    lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
                while True:
                    data = await reader.read(1024)
                    if not data:
                        break
                    if data == b"\x11":  # Ctrl-Q → detach
                        await ws.close()
                        break
                    await ws.send(_json.dumps(
                        {"type": "input", "data": data.decode("utf-8", "replace")}))

            async def _pump_ws():
                async for raw in ws:
                    msg = _json.loads(raw)
                    mtype = msg.get("type")
                    if mtype == "output":
                        sys.stdout.write(msg.get("data", ""))
                        sys.stdout.flush()
                    elif mtype == "ping":
                        await ws.send(_json.dumps({"type": "pong"}))

            try:
                done, pending = await asyncio.wait(
                    [asyncio.create_task(_pump_stdin()), asyncio.create_task(_pump_ws())],
                    return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    try:
        asyncio.run(_run())
    except websockets.exceptions.ConnectionClosed as exc:
        code = getattr(exc, "code", None)
        if code == 4003:
            print("\nTerminal access denied (your role lacks the 'terminal' capability).")
            return 13
        if code == 4001:
            print("\nAuthentication failed — run: adk login")
            return 1
        if code == 4004:
            print("\nContainer not running.")
            return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\nconnect failed: {exc}")
        return 1
    return 0


def cmd_workspace(args):
    """Manage dev workspaces on AitherOS tunnel."""
    import json as _json
    import urllib.request
    import urllib.error

    ws_cmd = getattr(args, "ws_command", None)
    if not ws_cmd:
        print("Usage: adk workspace [create|bundle|list|submit|scopes]")
        return 1

    # Load auth token
    cfg = load_saved_config()
    token = cfg.get("api_key") or cfg.get("access_token") or os.environ.get("AITHER_API_KEY", "")
    if not token and ws_cmd != "scopes":
        print("Error: Not authenticated. Run: adk login")
        return 1

    tunnel_url = getattr(args, "tunnel_url", "https://tunnel.aitherium.com")

    def _api(method, path, body=None):
        """Make authenticated request to tunnel API."""
        url = f"{tunnel_url}{path}"
        data = _json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                return _json.loads(resp.read()), resp.status
        except urllib.error.HTTPError as e:
            try:
                err_body = _json.loads(e.read())
            except (ValueError, OSError):
                err_body = {"detail": str(e)}
            return err_body, e.code

    if ws_cmd == "scopes":
        scopes = {
            "fullstack": "Full monorepo + AitherZero (admin/developer)",
            "frontend": "AitherVeil + packages",
            "backend": "lib + services + config + tests",
            "app-crm": "Custom app + awkit",
            "app-bot": "Custom bot app + awkit",
            "veil": "AitherVeil + all packages",
            "portal": "AitherVeil + awkit + desktop-core",
            "node": "awnode (standalone + monorepo)",
            "connect": "Awconnect",
            "shell": "AitherShell (standalone + monorepo)",
            "adk": "awdk (this package)",
            "desktop": "AitherDesktop + Veil + packages",
            "creative": "Canvas-Studio + creative services",
            "gpu": "VRAM-Sentinel + GPU services",
            "custom-app-dev": "custom apps + awkit (indie devs)",
        }
        print("Available workspace scopes:")
        print()
        for name, desc in scopes.items():
            print(f"  {name:20s} {desc}")
        print()
        print("Usage: adk workspace create --scope app-crm")
        return 0

    if ws_cmd == "create":
        scope = getattr(args, "scope", "fullstack")
        print(f"Creating cloud workspace (scope: {scope})...")
        data, status = _api("POST", "/tunnel/developer/workspace", {
            "scope_template": scope,
        })
        if status >= 400:
            print(f"Error ({status}): {data.get('detail', data.get('error', 'Unknown'))}")
            return 1
        print(f"  Container: {data.get('container_name', 'unknown')}")
        print(f"  Terminal:  {data.get('terminal_url', 'N/A')}")
        print(f"  VS Code:   {data.get('code_server_url', 'N/A')}")
        print(f"  Branch:    {data.get('branch', 'develop')}")
        print(f"  Scope:     {data.get('scope_template', scope)}")
        print()
        print("Connect via SSH:")
        print(f"  ssh dev@tunnel.aitherium.com -p {data.get('ssh_port', '22')}")
        print()
        print("Or open the terminal in browser:")
        print(f"  {data.get('terminal_url', tunnel_url)}")
        return 0

    if ws_cmd == "bundle":
        scope = getattr(args, "scope", "fullstack")
        output = getattr(args, "output", "aitheros-devws.zip")
        print(f"Downloading workspace bundle (scope: {scope})...")
        url = f"{tunnel_url}/tunnel/developer/workspace/bundle?scope={scope}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
                with open(output, "wb") as f:
                    f.write(resp.read())
            print(f"  Saved: {output}")
            print()
            print("Next steps:")
            print(f"  unzip {output}")
            print("  cd aitheros-devws-*/")
            print("  docker compose up -d")
            return 0
        except urllib.error.HTTPError as e:
            print(f"Error ({e.code}): {e.reason}")
            return 1

    if ws_cmd == "list":
        data, status = _api("GET", "/tunnel/developer/workspaces")
        if status >= 400:
            print(f"Error ({status}): {data.get('detail', 'Unknown')}")
            return 1
        workspaces = data.get("workspaces", [])
        if not workspaces:
            print("No active workspaces.")
            return 0
        print(f"Active workspaces ({len(workspaces)}):")
        for ws in workspaces:
            name = ws.get("container_name", ws.get("name", "?"))
            scope = ws.get("scope_template", "?")
            status_str = ws.get("status", "?")
            print(f"  {name:40s} scope={scope:15s} status={status_str}")
        return 0

    if ws_cmd == "submit":
        message = getattr(args, "message", "")
        workspace = getattr(args, "workspace", "")
        if not workspace:
            # Auto-detect: check if we're inside a dev workspace container
            workspace = os.environ.get("DEV_SESSION_ID", "")
            if not workspace:
                # Try container hostname
                import socket
                hostname = socket.gethostname()
                if hostname.startswith("aitheros-devws-"):
                    workspace = hostname
        if not workspace:
            print("Error: --workspace required (or run from inside a dev workspace)")
            return 1
        print(f"Submitting changes from {workspace}...")
        data, status = _api("POST", "/tunnel/developer/workspace/submit-changes", {
            "workspace": workspace,
            "message": message,
        })
        if status >= 400:
            print(f"Error ({status}): {data.get('detail', data.get('error', 'Unknown'))}")
            return 1
        print(f"  Status: {data.get('status', '?')}")
        if data.get("pr_url"):
            print(f"  PR: {data['pr_url']}")
        for step in data.get("steps", []):
            icon = "+" if step["status"] == "success" else "x" if step["status"] == "failed" else "."
            print(f"  [{icon}] {step['name']}: {step.get('detail', '')}")
        return 0

    print(f"Unknown workspace command: {ws_cmd}")
    return 1


def cmd_run(args):
    """Start the agent server."""
    from adk.server import main as server_main
    # Re-inject args into sys.argv for server's argparse
    sys_args = ["aither-serve"]
    if args.identity:
        sys_args += ["--identity", args.identity]
    if args.port:
        sys_args += ["--port", str(args.port)]
    if args.host:
        sys_args += ["--host", args.host]
    if args.backend:
        sys_args += ["--backend", args.backend]
    if args.model:
        sys_args += ["--model", args.model]
    if args.fleet:
        sys_args += ["--fleet", args.fleet]
    if args.agents:
        sys_args += ["--agents", args.agents]

    sys.argv = sys_args
    server_main()


def _sync_entitled_packs_quiet() -> None:
    """Run a license-driven pack sync (best-effort) and print a one-line result.

    Shared by `adk up` startup. Resolves the portal + saved token like
    `adk pack sync`; a portal/auth absence is a no-op, never a hard error.
    """
    from adk.shell.plugins.builtins.packs import PacksPlugin

    plugin = PacksPlugin()
    portal = (
        os.environ.get("AITHER_PORTAL_URL")
        or os.environ.get("AITHER_ELYSIUM_URL")
        or "https://portal.aitherium.com"
    ).rstrip("/")
    plugin._base_url = portal
    cfg = load_saved_config()
    token = cfg.get("api_key") or cfg.get("access_token") or os.environ.get("AITHER_API_KEY", "")
    if not token:
        return  # not authenticated → nothing to converge
    plugin.auth.set_auth(token, cfg.get("tenant_id"))
    result = plugin._sync([])
    # Print only the summary line to keep startup output tight.
    first_line = (result or "").strip().splitlines()
    if first_line:
        print(f"  packs: {first_line[1] if len(first_line) > 1 else first_line[0]}")


def cmd_stack(args):
    """Start the consumer stack (Room + Ollama) as native processes.

    Supervises AitherRoom (:8350) and Ollama (:11434) as foreground processes
    with health checks, crash restarts, and graceful shutdown on Ctrl+C.

    (Formerly ``adk up``; that name now brings up a connected agent — see cmd_up.
    This behaviour lives under ``adk stack``.)

    Services:
    - default: Room + Ollama (default)
    - qdrant: Local Qdrant for graph memory (requires Docker)
    """
    # Handle "adk stack qdrant" for Qdrant provisioning
    service = getattr(args, "service", "default") or "default"
    if service == "qdrant":
        print()
        print("Provisioning local Qdrant for graph memory...")
        success, url, api_key = _provision_qdrant()
        if success:
            print(f"  [+] Qdrant URL: {url}")
            print("  [+] API key saved to ~/.aither/config.yaml")
            print()
            print("  Configuration:")
            print(f"    AITHER_FLEET_QDRANT_URL={url}")
            print(f"    AITHER_FLEET_QDRANT_API_KEY={api_key}")
            print()
            print("  Ready to use in graph_memory. Run 'adk status' to verify.")
            return 0
        # Provisioning failed — a non-zero exit so the caller/CI can detect it
        # (gate finding: never report a failed setup as success).
        print("  [!] Qdrant provisioning failed. Manual setup required.")
        return 1

    from adk.process_supervisor import ProcessSpec, ProcessSupervisor, resolve_room_launch_target

    # Converge entitled packs first so the agent comes up with everything the
    # customer has bought (a portal purchase → `adk up` → packs present). Opt-out
    # with --no-sync; never block startup if the portal is unreachable.
    if not getattr(args, "no_sync", False):
        try:
            _sync_entitled_packs_quiet()
        except Exception as e:  # noqa: BLE001
            print(f"  (pack sync skipped: {e})")

    supervisor = ProcessSupervisor()

    # Gather specs
    specs = []

    # Room service (if launch target resolvable)
    # Resolution order: env var → installed package → repo checkout → binary
    room_target = resolve_room_launch_target()
    if room_target:
        # Determine if target is a binary or a Python module
        target_path = Path(room_target)
        if target_path.exists() and target_path.is_file():
            # Binary: run directly
            room_spec = ProcessSpec(
                name="AitherRoom",
                argv=[str(target_path)],
                port=8350,
                health_url="http://127.0.0.1:8350/health",
                description="Company agent room service (binary)",
            )
        else:
            # Python module: run via uvicorn
            room_spec = ProcessSpec(
                name="AitherRoom",
                argv=[
                    sys.executable,
                    "-m",
                    "uvicorn",
                    room_target,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8350",
                ],
                port=8350,
                health_url="http://127.0.0.1:8350/health",
                description="Company agent room service (Python)",
            )
        specs.append(room_spec)
    else:
        print("Warning: AitherRoom not found — Room service will not start.")
        print("Tried: 1) cached in ~/.aither/bin/, 2) download from GitHub,")
        print("       3) bundled in adk/room_binaries/<platform>/.")
        print(
            "Ensure CI populates bundled binaries or set "
            "AITHER_ROOM_LAUNCH_TARGET to a dev module path."
        )

    # Ollama (if installed)
    if os.path.exists(os.path.expanduser("~/.ollama")):
        ollama_spec = ProcessSpec(
            name="Ollama",
            argv=["ollama", "serve"],
            port=11434,
            health_url="http://127.0.0.1:11434/api/tags",
            description="Local model serving",
        )
        specs.append(ollama_spec)

    if not specs:
        print("Error: No services available to start.")
        print("Ensure Ollama is installed and Room is available.")
        return 1

    # Start supervision
    print()
    print("Starting AitherOS consumer stack...")
    print()
    supervisor.supervise(specs, interval=10)


def _render_phone_access(public_url: str, token: str) -> str:
    """Build the phone-ready SECURE chat URL (+ QR) for a tunnelled agent.

    The bearer rides in the URL fragment (``#k=…``) so the browser never sends it
    to the server or writes it to logs, yet the chat page can still call the
    bearer-gated ``/chat/stream``. Anyone who opens the tunnel URL WITHOUT the
    fragment hits 401 — the publicly-exposed surface stays fail-closed.

    The QR uses Unicode block characters; on a terminal whose encoding can't
    represent them (e.g. a legacy cp1252 Windows console) it is silently dropped
    and only the (ASCII) URL is shown, so this never crashes the caller.
    """
    phone_url = f"{public_url.rstrip('/')}/#k={token}"
    lines = [
        "",
        "     Phone access (open on any device - the token is in the link,",
        "     never sent in a header or logged; without it the URL is 401):",
        f"       {phone_url}",
    ]
    try:
        import io

        import qrcode  # optional dep; pure-python ASCII/Unicode QR

        qr = qrcode.QRCode(border=1)
        qr.add_data(phone_url)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        block = buf.getvalue()
        enc = (sys.stdout.encoding or "utf-8")
        block.encode(enc)  # raises on a terminal that can't render the QR glyphs
        lines.append("")
        lines.extend("       " + ln for ln in block.splitlines())
    except Exception:  # noqa: BLE001 — QR is a convenience; the URL is the essential
        # Missing qrcode dep, or a terminal that can't render the glyphs — skip it.
        pass
    return "\n".join(lines)


def _email_tunnel_link(to: str, name: str, phone_url: str, public_url: str,
                       quiet: bool = False) -> bool:
    """Best-effort: email the phone-ready access link to a configured address.

    Uses the ADK MailRelay (configured once via a provider preset / SMTP creds).
    Never raises and never blocks ``adk up``; prints a short status unless quiet.
    """
    def _note(msg: str) -> None:
        if not quiet:
            print(msg)

    try:
        from adk.smtp import get_mail_relay
        relay = get_mail_relay()
        if not relay.is_configured:
            _note("  [!] Email not sent — SMTP not configured "
                  "(configure a provider first, e.g. gmail/resend/sendgrid).")
            return False
        cfg = relay.get_config(redact=False)
        sender = cfg.get("from_addr") or cfg.get("username", "")
        subject = f"Your Aither agent '{name}' is live"
        body = (f"Your agent '{name}' is running and reachable from anywhere.\n\n"
                f"Open on your phone (the access token is in the link):\n{phone_url}\n\n"
                f"Public URL: {public_url}\n")
        html = (f"<p>Your agent <b>{name}</b> is live. Tap to chat from your phone:</p>"
                f'<p><a href="{phone_url}" style="display:inline-block;padding:10px 18px;'
                f'background:#3b82f6;color:#fff;border-radius:8px;text-decoration:none">'
                f"Open {name}</a></p>"
                f'<p style="color:#888;font-size:12px">The access token is embedded in this '
                f"link - treat it like a password. Public URL: {public_url}</p>")
        row = {"from_addr": sender, "to_addr": to, "subject": subject,
               "body": body, "html": html, "attachments": "[]", "agent": name}
        ok, err = relay._send_direct(row)
        _note(f"  [+] Access link emailed to {to}" if ok else f"  [!] Email not sent: {err}")
        return ok
    except Exception as e:  # noqa: BLE001 — email is best-effort, never blocks up
        _note(f"  [!] Email not sent: {e}")
        return False


def cmd_up(args):
    """One command: run a persistent agent connected to your AitherOS fleet.

    Brings up ``aither-serve`` (single agent, identity ``aither`` by default),
    opens a secure Cloudflare quick-tunnel so the fleet can reach it, registers
    with the portal, and installs an autostart entry so it survives reboot.

    Two modes:
    - Interactive (a human at a terminal): minimal prompts, device-flow browser login.
    - Non-interactive (``--yes`` / not a TTY): ZERO prompts — everything from flags,
      env, and saved config; emits a machine-readable JSON status line. This is the
      path an autonomous agent uses to stand itself up unattended.

    By default the agent is DETACHED (the terminal is freed). ``--foreground`` blocks.
    Companions: ``adk status`` (state), ``adk down`` (stop + remove autostart).
    """
    import base64
    import getpass
    import re
    import secrets as _secrets
    import socket

    import httpx

    from adk import agent_daemon as daemon

    non_interactive = bool(getattr(args, "yes", False)) or not sys.stdin.isatty()
    offline = (os.environ.get("AITHER_OFFLINE", "").lower() in ("1", "true", "yes")
               or bool(getattr(args, "offline", False)))
    identity = getattr(args, "identity", None) or "aither"
    name = (getattr(args, "name", "") or "").strip() or re.sub(
        r"[^a-z0-9_-]", "-", f"{socket.gethostname()}-adk".lower())
    port = getattr(args, "port", None) or 8080
    foreground = bool(getattr(args, "foreground", False))
    persist = not getattr(args, "no_persist", False)
    force = bool(getattr(args, "force", False))
    dry_run = bool(getattr(args, "dry_run", False))
    require_register = bool(getattr(args, "require_register", False))
    provider = (getattr(args, "provider", "") or "").strip().lower()
    approval = getattr(args, "approve", None) or "file_write,shell_exec,shell"
    reach = (getattr(args, "reach", "") or "tunnel").lower()
    mesh_overlay_ip = None  # Set below if reach == "mesh"
    will_register = not getattr(args, "no_register", False) and not offline

    def _fail(code: int, error: str, hint: str = "", next_action: str = "") -> int:
        obj = {"ok": False, "error": error, "error_code": code}
        if hint:
            obj["hint"] = hint
        if next_action:
            obj["next_action"] = next_action
        if non_interactive:
            print(json.dumps(obj))
        else:
            print(f"  [x] {error}")
            if hint:
                print(f"      {hint}")
            if next_action:
                print(f"      -> {next_action}")
        return code

    # ── Idempotency: already running? ──
    existing = daemon.read_status()
    if existing and daemon.pid_alive(existing.get("server_pid")):
        if force:
            daemon.kill_pid(existing.get("tunnel_pid"))
            daemon.kill_pid(existing.get("server_pid"))
            daemon.clear_status()
        else:
            existing["ok"] = True
            existing["already_running"] = True
            if non_interactive:
                print(json.dumps(existing))
            else:
                print(f"  [+] Agent already running on :{existing.get('port')} "
                      f"(pid {existing.get('server_pid')}). "
                      "Use --force to restart, or 'adk down' to stop.")
            return 0

    # ── Portal token (used for BOTH the hosted brain and registration) ──
    portal = (getattr(args, "portal", "") or _control_plane()).rstrip("/")
    saved = load_saved_config()
    portal_token = (getattr(args, "token", "") or os.environ.get("AITHER_PORTAL_TOKEN", "")
                    or saved.get("api_key", "") or saved.get("access_token", ""))

    def _device_login(client: str) -> str:
        login_url = _resolve_identity_url(
            getattr(args, "login_url", "") or os.environ.get("AITHER_PORTAL_URL", "")
            or portal or _DEFAULT_IDENTITY_URL)
        # `--github` federates GitHub's device flow (Identity mints the Aither
        # token from the GitHub identity); otherwise the standard Aither flow.
        use_github = bool(getattr(args, "github", False))
        print(f"  Signing in at {login_url} … ({'GitHub' if use_github else 'email'})")
        if use_github:
            dev = _github_device_flow_login(login_url, client_name=client)
        else:
            dev = _device_flow_login(login_url, client_name=client)
        tok = dev.get("access_token", "") or dev.get("token", "")
        if tok:
            try:
                save_saved_config({"api_key": tok})
            except (OSError, ValueError):
                pass
        return tok

    # ── Backend: local model  >  a provider key  >  the hosted Aither brain ──
    # The hosted-brain default means a normal human never needs an API key: if
    # there's no local model and no provider key, we sign in (device-flow) and
    # route inference through the gateway using the portal token.
    try:
        from adk.shell_launcher import _preflight_check
        have_backend, _backend_desc = _preflight_check()
    except (ImportError, RuntimeError, OSError):
        have_backend, _backend_desc = (False, "")

    use_gateway = False
    if not have_backend:
        cand = provider
        if not cand:
            _pk = _load_provider_keys()
            for p in ("deepseek", "openai", "anthropic"):
                if _pk.get(p) or os.environ.get(_KNOWN_PROVIDERS[p]["env"]):
                    cand = p
                    break
        if cand and cand not in _KNOWN_PROVIDERS:
            return _fail(2, f"unknown provider '{cand}'",
                         f"known: {', '.join(sorted(_KNOWN_PROVIDERS))}",
                         next_action="re-run with a supported --provider, or omit it")
        cand_key = ""
        if cand:
            cand_key = (_load_provider_keys().get(cand, "")
                        or os.environ.get(_KNOWN_PROVIDERS[cand]["env"], ""))

        if cand_key:
            provider = cand
            os.environ[_KNOWN_PROVIDERS[provider]["env"]] = cand_key
        elif provider:
            # Explicit --provider but no key for it.
            env_name = _KNOWN_PROVIDERS[provider]["env"]
            if non_interactive:
                return _fail(3, "no_backend",
                             f"provider '{provider}' selected but {env_name} is not set",
                             next_action=f"set {env_name}, or omit --provider to use the hosted brain")
            key = getpass.getpass(f"  {_KNOWN_PROVIDERS[provider]['label']} API key: ").strip()
            if not key:
                return _fail(3, "no_backend", "no key entered",
                             next_action="paste your API key, or omit --provider for the hosted brain")
            ok, msg = _test_provider_key(provider, key)
            print(f"  [{'+' if ok else 'x'}] {msg}")
            if not ok:
                return _fail(3, "invalid_key", "key failed validation (not saved)",
                             next_action="check the key and try again")
            _pk = _load_provider_keys()
            _pk[provider] = key
            _save_provider_keys(_pk)
            os.environ[env_name] = key
        else:
            # No local model + no provider key → default to the hosted Aither brain.
            if not portal_token and not offline:
                if non_interactive:
                    return _fail(3, "no_backend",
                                 "no local model, no API key, and not signed in",
                                 next_action="run 'adk login' (or set AITHER_PORTAL_TOKEN), "
                                             "or 'adk setup' for a local model")
                print("  No local model or API key found — using the hosted Aither brain "
                      "(no key needed).")
                try:
                    portal_token = _device_login(f"adk-up:{name}")
                except Exception as e:  # noqa: BLE001 — surface a clear next step
                    return _fail(3, f"sign-in failed: {e}", "",
                                 next_action="run 'adk login', pass --provider + key, "
                                             "or 'adk setup' for a local model")
            if portal_token:
                use_gateway = True
                provider = "gateway"
            else:
                return _fail(3, "no_backend",
                             "no local model and no hosted brain available",
                             next_action="run 'adk setup' for a local model, "
                                         "or drop --offline to use the hosted brain")

    # ── Portal token for REGISTRATION (reuse the one above, or acquire) ──
    if will_register and not portal_token:
        if non_interactive:
            if require_register:
                return _fail(5, "no_portal_token",
                             "unattended registration needs a token",
                             next_action="set AITHER_PORTAL_TOKEN or pass --token")
            will_register = False
        else:
            try:
                portal_token = _device_login(f"adk-up:{name}")
            except Exception as e:  # noqa: BLE001 — surface a clear next step
                return _fail(5, f"device-flow login failed: {e}", "",
                             next_action="pass --token, run 'adk login', or use --no-register")
            if not portal_token:
                will_register = False

    # ── cloudflared availability (auto-download if missing) ──
    # Skip cloudflared for mesh mode (no tunnel needed)
    cf = None
    if will_register and reach != "mesh":
        cf = _find_cloudflared()
        if not cf:
            if not non_interactive:
                print("  cloudflared not found — downloading it (one-time, ~50MB) …")
            cf = _ensure_cloudflared()
        if not cf:
            return _fail(4, "cloudflared_missing", _cloudflared_install_hint().strip(),
                         next_action="install cloudflared manually, or re-run with --no-register")

    # ── Callback bearer (the one secret the control plane presents back to us) ──
    auth_token = (getattr(args, "passphrase", "") or getattr(args, "auth_token", "")
                  or os.environ.get("AITHER_SERVER_API_KEY", ""))
    if not auth_token:
        auth_token = base64.urlsafe_b64encode(_secrets.token_bytes(24)).decode().rstrip("=")

    if dry_run:
        plan = {"ok": True, "dry_run": True, "identity": identity, "name": name,
                "port": port, "will_register": will_register, "offline": offline,
                "detached": not foreground, "persist": persist,
                "backend": "local" if have_backend else provider}
        print(json.dumps(plan) if non_interactive else f"  [dry-run] {json.dumps(plan)}")
        return 0

    # ── Launch aither-serve (detached, logs to file) ──
    daemon._ensure_dirs()
    agent_log = daemon.LOG_DIR / "agent.log"
    tunnel_log = daemon.LOG_DIR / "tunnel.log"
    child_env = dict(os.environ)
    child_env["AITHER_SERVER_API_KEY"] = auth_token
    child_env["AITHER_TOOL_APPROVAL"] = approval or ""
    if offline:
        child_env["AITHER_OFFLINE"] = "1"
    if reach == "mesh":
        # Mesh mode: bind to 0.0.0.0 and set overlay IP for registration
        child_env["AITHER_SERVE_HOST"] = "0.0.0.0"
        if mesh_overlay_ip:
            child_env["AITHER_MESH_OVERLAY_IP"] = mesh_overlay_ip
    if use_gateway and portal_token:
        # Route inference through the hosted Aither brain with the portal token —
        # the router honours AITHER_LLM_BACKEND=gateway + AITHER_API_KEY (bearer).
        child_env["AITHER_API_KEY"] = portal_token
        child_env["AITHER_LLM_BACKEND"] = "gateway"
        _eps = _derive_cloud_endpoints(_resolve_identity_url(
            getattr(args, "login_url", "") or os.environ.get("AITHER_PORTAL_URL", "")
            or portal or _DEFAULT_IDENTITY_URL))
        if _eps and _eps.get("inference_url"):
            child_env["AITHER_INFERENCE_URL"] = _eps["inference_url"]
    serve_argv = [sys.executable, "-m", "adk.server",
                  "--port", str(port), "--identity", identity]
    if reach == "mesh":
        # Pass --host 0.0.0.0 for mesh mode (allow external access on overlay)
        serve_argv += ["--host", "0.0.0.0"]
    if provider and provider != "gateway":
        serve_argv += ["--backend", provider]
    if not non_interactive:
        _brain = "hosted Aither brain" if use_gateway else (provider or "local")
        print(f"  Starting agent ({_brain}, :{port}) …")
    server_pid = daemon.spawn_detached(serve_argv, agent_log, env=child_env)

    if not daemon.wait_for_health(port, timeout=60):
        daemon.kill_pid(server_pid)
        return _fail(1, "agent did not become healthy in 60s",
                     f"see {agent_log}")
    if not non_interactive:
        print(f"  [+] Agent healthy on :{port}")

    # ── Tunnel + register ──
    public_url = None
    tunnel_pid = None
    registered = False
    if reach == "mesh":
        # Mesh mode: use overlay IP, no tunnel
        mesh_overlay_ip = mesh_overlay_ip or os.environ.get("AITHER_MESH_OVERLAY_IP", "").strip()
        if will_register and not mesh_overlay_ip:
            return _fail(2, "mesh_mode_no_overlay_ip",
                        "reach='mesh' requires AITHER_MESH_OVERLAY_IP to be set")
        if mesh_overlay_ip and will_register:
            if not non_interactive:
                print(f"  [+] Mesh mode: overlay IP {mesh_overlay_ip}")
            reg = (getattr(args, "register_url", "")
                   or f"{portal}/api/genesis/v1/agent/fleet/register")
            public_url = f"http://{mesh_overlay_ip}:{port}"
            from adk.a2a_identity import get_a2a_public_key
            public_key = get_a2a_public_key()
            body = {"name": name, "invoke_url": public_url, "reach": "mesh",
                    "agent_type": "adk-agent", "token": auth_token,
                    "model": getattr(args, "model", None) or "", "provider_hint": provider,
                    "public_key": public_key}
            hdrs = {"Content-Type": "application/json"}
            if portal_token:
                hdrs["Authorization"] = f"Bearer {portal_token}"
            try:
                rr = httpx.post(reg, json=body, headers=hdrs, timeout=20)
                registered = rr.status_code < 300
                if not registered:
                    if require_register:
                        daemon.kill_pid(server_pid)
                        return _fail(5, f"registration rejected ({rr.status_code})",
                                     rr.text[:200])
                    if not non_interactive:
                        print(f"  [!] Registration returned {rr.status_code} — "
                              "agent is up locally; heartbeat will retry.")
            except (httpx.HTTPError, OSError) as e:
                if require_register:
                    daemon.kill_pid(server_pid)
                    return _fail(5, f"registration failed: {e}")
                if not non_interactive:
                    print(f"  [!] Registration failed ({e}) — heartbeat will retry.")
    elif will_register and cf:
        tunnel_pid = daemon.spawn_detached(
            [cf, "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate"],
            tunnel_log)
        public_url = daemon.tail_for_pattern(
            tunnel_log, r"https://[a-z0-9-]+\.trycloudflare\.com", timeout=45)
        if not public_url:
            daemon.kill_pid(tunnel_pid)
            tunnel_pid = None
            if require_register:
                daemon.kill_pid(server_pid)
                return _fail(4, "tunnel_failed", f"no trycloudflare URL; see {tunnel_log}")
            if not non_interactive:
                print("  [!] Tunnel did not come up — running local-only (will retry via heartbeat).")
        else:
            if not non_interactive:
                print(f"  [+] Tunnel live: {public_url}")
            reg = (getattr(args, "register_url", "")
                   or f"{portal}/api/genesis/v1/agent/fleet/register")
            from adk.a2a_identity import get_a2a_public_key
            public_key = get_a2a_public_key()
            body = {"name": name, "invoke_url": public_url, "reach": "tunnel",
                    "agent_type": "adk-agent", "token": auth_token,
                    "model": getattr(args, "model", None) or "", "provider_hint": provider,
                    "public_key": public_key}
            hdrs = {"Content-Type": "application/json"}
            if portal_token:
                hdrs["Authorization"] = f"Bearer {portal_token}"
            try:
                rr = httpx.post(reg, json=body, headers=hdrs, timeout=20)
                registered = rr.status_code < 300
                if not registered:
                    if require_register:
                        daemon.kill_pid(tunnel_pid)
                        daemon.kill_pid(server_pid)
                        return _fail(5, f"registration rejected ({rr.status_code})",
                                     rr.text[:200])
                    if not non_interactive:
                        print(f"  [!] Registration returned {rr.status_code} — "
                              "agent is up locally; heartbeat will retry.")
            except (httpx.HTTPError, OSError) as e:
                if require_register:
                    daemon.kill_pid(tunnel_pid)
                    daemon.kill_pid(server_pid)
                    return _fail(5, f"registration failed: {e}")
                if not non_interactive:
                    print(f"  [!] Registration failed ({e}) — heartbeat will retry.")

    # ── Persist (autostart) ──
    autostart = None
    if persist:
        up_argv = _autostart_up_argv(identity, port, provider, offline)
        autostart = daemon.install_autostart(up_argv)

    # ── Status file (single source of truth for status/down) ──
    # The agent serves its own streaming chat page at "/" — that's where a human
    # actually talks to it (live tokens, no 30s blank wait), reachable locally.
    chat_url = f"http://127.0.0.1:{port}/"
    status = {
        "ok": True, "identity": identity, "name": name, "port": port,
        "server_pid": server_pid, "tunnel_pid": tunnel_pid,
        "invoke_url": public_url, "registered": registered,
        "offline": offline, "detached": not foreground,
        "backend": "local" if have_backend else provider,
        "autostart": autostart, "portal": portal, "chat_url": chat_url,
        "log_path": str(agent_log),
    }
    daemon.write_status(status)

    # ── Email the access link (optional) ──
    email_to = getattr(args, "email", "") or saved.get("notify_email", "")
    if email_to and public_url:
        _email_tunnel_link(email_to, name, f"{public_url.rstrip('/')}/#k={auth_token}",
                           public_url, quiet=non_interactive)

    # ── Report ──
    if non_interactive:
        print(json.dumps({**status, "health": "healthy"}))
    else:
        print()
        print("  [OK] Your agent is running.")
        if public_url:
            print(f"     Fleet: {portal}/portal/fleet")
            print(f"     URL:   {public_url}")
            print(_render_phone_access(public_url, auth_token))
        print(f"     Chat:  {chat_url}")
        print(f"     Logs:  {agent_log}")
        print("     Stop:  adk down     Status: adk status")
        # Open the chat page so a non-technical user has somewhere to talk to it.
        # Interactive + detached only (never in --yes or blocking --foreground).
        if not foreground:
            try:
                import webbrowser
                # Pass the callback bearer in the URL fragment (#k=…) — the browser
                # never sends it to the server or logs, so the chat page can call the
                # gated /chat/stream without the user pasting a token.
                webbrowser.open(f"{chat_url}#k={auth_token}")
            except Exception:  # noqa: BLE001 — opening a browser is best-effort
                pass

    if foreground:
        if not non_interactive:
            print("\n  Foreground mode — Ctrl+C to stop.")
        try:
            while daemon.pid_alive(server_pid):
                time.sleep(2)
        except KeyboardInterrupt:
            print("\n  Stopping …")
            daemon.kill_pid(tunnel_pid)
            daemon.kill_pid(server_pid)
            daemon.clear_status()
    return 0


def _autostart_up_argv(identity: str, port: int, provider: str, offline: bool) -> list[str]:
    """Build the command the autostart entry re-runs at logon (unattended, detached)."""
    argv = [sys.executable, "-m", "adk.cli", "up",
            "--identity", identity, "--port", str(port), "--yes"]
    # 'gateway' is the hosted-brain sentinel, not a real --provider — at reboot the
    # saved portal token is re-resolved and the hosted brain is chosen again.
    if provider and provider != "gateway":
        argv += ["--provider", provider]
    if offline:
        argv += ["--offline"]
    return argv


def cmd_sandbox(args) -> int:
    """Self-host AitherSandbox and link it to your portal (optional safe-testing).

    ``adk sandbox up`` deploys the aitheros-sandbox service container on THIS
    machine, opens a Cloudflare quick-tunnel, and registers the sandbox with the
    portal so aitherium.com can route "try it in your sandbox" to you. ``adk
    sandbox down`` stops it; ``adk sandbox status`` shows the linked URL.

    The sandbox is OPTIONAL and self-hosted by design — this machine keeps all
    test/eval execution local; only the linked tunnel URL reaches the portal.
    """
    action = getattr(args, "sandbox_action", "status")
    if action == "up":
        return _cmd_sandbox_up(args)
    if action == "down":
        return _cmd_sandbox_down(args)
    return _cmd_sandbox_status(args)


def _sandbox_status_path() -> str:
    return os.path.join(os.path.expanduser("~/.aither"), "adk-sandbox.json")


def _cmd_sandbox_status(args) -> int:
    import json as _json
    import subprocess as _sp
    st = {}
    try:
        with open(_sandbox_status_path(), encoding="utf-8") as f:
            st = _json.load(f)
    except Exception:
        pass
    if not st:
        print("  [·] AitherSandbox is not deployed on this machine — 'adk sandbox up' to deploy")
        return 0
    try:
        r = _sp.run(["docker", "inspect", "-f", "{{.State.Running}}", "aitheros-sandbox"],
                    capture_output=True, text=True, timeout=15)
        running = r.stdout.strip() == "true"
    except Exception:
        running = False
    print(f"  [+] AitherSandbox: {'running' if running else 'stopped'}")
    print(f"      local:  http://localhost:{st.get('port', 8131)}")
    print(f"      public: {st.get('public_url') or '(none — tunnel down)'}")
    print(f"      portal: {'registered' if st.get('registered') else 'not registered'}")
    return 0


def _cmd_sandbox_up(args) -> int:
    import json as _json
    import socket
    import subprocess as _sp
    import time as _time

    import httpx

    from adk import agent_daemon as daemon

    non_interactive = bool(getattr(args, "yes", False)) or not sys.stdin.isatty()
    offline = os.environ.get("AITHER_OFFLINE", "").lower() in ("1", "true", "yes")
    port = getattr(args, "port", None) or 8131
    image = os.environ.get("AITHER_SANDBOX_IMAGE", "aitheros-sandbox:latest")
    portal = getattr(args, "portal", _control_plane())
    portal_token = getattr(args, "token", "") or os.environ.get("AITHER_PORTAL_TOKEN", "")
    name = (getattr(args, "name", "") or "").strip() or re.sub(
        r"[^a-z0-9_-]", "-", f"{socket.gethostname()}-sandbox".lower())

    def _out(code: int, obj: dict) -> int:
        if non_interactive:
            print(json.dumps(obj))
        return code

    # 1) Deploy the sandbox container (idempotent: remove + recreate).
    try:
        _sp.run(["docker", "rm", "-f", "aitheros-sandbox"], capture_output=True, text=True, timeout=20)
    except Exception:
        pass
    try:
        r = _sp.run(
            ["docker", "run", "-d", "--name", "aitheros-sandbox",
             "--restart", "unless-stopped", "-p", f"{port}:8131", image],
            capture_output=True, text=True, timeout=300,
        )
    except FileNotFoundError:
        return _out(4, {"ok": False, "error": "docker_missing",
                        "hint": "AitherSandbox self-hosting needs Docker on this machine"})
    if r.returncode != 0:
        return _out(4, {"ok": False, "error": "docker run failed",
                        "detail": r.stderr.strip()[:400],
                        "hint": "set AITHER_SANDBOX_IMAGE to a published aitheros-sandbox image"})

    # 2) Tunnel (skip for offline / --no-register).
    public_url = None
    tunnel_pid = None
    if not offline and not getattr(args, "no_register", False):
        cf = _find_cloudflared() or _ensure_cloudflared()
        if cf:
            tunnel_log = os.path.join(os.path.expanduser("~/.aither"), "adk-sandbox-tunnel.log")
            tunnel_pid = daemon.spawn_detached(
                [cf, "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate"], tunnel_log)
            public_url = daemon.tail_for_pattern(
                tunnel_log, r"https://[a-z0-9-]+\.trycloudflare\.com", timeout=45)

    # 3) Register the sandbox with the portal.
    registered = False
    if public_url and not offline:
        try:
            body = {"name": name, "invoke_url": public_url, "reach": "tunnel",
                    "agent_type": "sandbox", "token": portal_token, "port": port}
            hdrs = {"Content-Type": "application/json"}
            if portal_token:
                hdrs["Authorization"] = f"Bearer {portal_token}"
            rr = httpx.post(f"{portal}/api/genesis/v1/agent/fleet/register",
                            json=body, headers=hdrs, timeout=20)
            registered = rr.status_code < 300
        except (httpx.HTTPError, OSError):
            registered = False

    try:
        with open(_sandbox_status_path(), "w", encoding="utf-8") as f:
            _json.dump({"port": port, "public_url": public_url, "registered": registered,
                        "tunnel_pid": tunnel_pid, "updated": _time.time()}, f)
    except Exception:
        pass

    obj = {"ok": True, "port": port, "local_url": f"http://localhost:{port}",
           "public_url": public_url, "registered": registered}
    if non_interactive:
        print(json.dumps(obj))
    else:
        print(f"  [+] AitherSandbox up on :{port}")
        print(f"      local:  http://localhost:{port}")
        if public_url:
            print(f"      public: {public_url}")
        if registered:
            print("      portal: registered — aitherium.com can route try-it-in-sandbox to you")
        else:
            print("      portal: NOT registered (tunnel/portal unreachable) — sandbox is local-only")
    return 0


def _cmd_sandbox_down(args) -> int:
    import json as _json
    import subprocess as _sp
    try:
        _sp.run(["docker", "rm", "-f", "aitheros-sandbox"], capture_output=True, text=True, timeout=60)
    except Exception:
        pass
    try:
        with open(_sandbox_status_path(), encoding="utf-8") as f:
            st = _json.load(f)
        if st.get("tunnel_pid"):
            from adk import agent_daemon as daemon
            daemon.kill_pid(st["tunnel_pid"])
    except Exception:
        pass
    try:
        os.remove(_sandbox_status_path())
    except Exception:
        pass
    print("  [x] AitherSandbox stopped (container removed, tunnel closed)")
    return 0


def cmd_bonsai_local(args) -> int:
    """One command: run Bonsai-27B locally on :8090, reachable as `--backend bonsai-local`.

    NOT the `local` preset: that one targets AitherVLLMSwap on :8201, a fleet service.
    This docstring used to claim otherwise, which is why the pairing looked wired.

    Runs the baked PrismML llama.cpp fork image (aither-llamacpp-bonsai:latest) —
    llama-server on the 1-bit Q1_0 GGUF — mapped to host :8090, so the Living OS at
    aitherium.com detects it (localhost:8090/health) and chats on YOUR hardware.
    GPU is used automatically when present (the image requests -ngl 99); on a CPU-only
    box llama.cpp falls back to CPU (AVX). No GPU required to run — just slower.

    Serves OpenAI-compat: GET /health, POST /v1/chat/completions, POST /completion.
    """
    import shutil
    import subprocess

    image = os.environ.get("AITHER_BONSAI_IMAGE", "aither-llamacpp-bonsai:latest")
    name = os.environ.get("AITHER_BONSAI_CONTAINER", "aither-bonsai-local")
    port = int(
        getattr(args, "port", 0)
        or os.environ.get("AITHER_BONSAI_PORT", str(BONSAI_LOCAL_PORT))
    )
    gpus = os.environ.get("AITHER_BONSAI_GPUS", "all")

    if not shutil.which("docker"):
        print("  [!] Docker not found. Install Docker Desktop, or use `adk up` with a cloud provider.")
        return 1

    if getattr(args, "stop", False):
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        print(f"  [+] Stopped and removed {name}.")
        return 0

    # Use the GPU when the nvidia runtime is available; else CPU (llama.cpp handles both).
    gpu_args = []
    probe = subprocess.run(["docker", "info", "--format", "{{json .Runtimes}}"], capture_output=True, text=True)
    if "nvidia" in (probe.stdout or ""):
        gpu_args = ["--gpus", gpus]

    run_cmd = [
        "docker", "run", "-d", "--name", name, "--restart", "unless-stopped",
        *gpu_args, "-p", f"127.0.0.1:{port}:8090", image,
    ]

    if getattr(args, "dry_run", False):
        print("  [dry-run] would run Bonsai-27B locally:")
        print("    image      :", image)
        print("    serve on   : http://localhost:%d  (/health, /v1/chat/completions)" % port)
        print("    gpu        :", "yes (nvidia runtime)" if gpu_args else "no — CPU (AVX) fallback")
        print("    command    :", " ".join(run_cmd))
        print("  Then the Living OS at aitherium.com auto-detects it and chats on your box.")
        print("    agents via : adk --backend bonsai-local"
              if port == BONSAI_LOCAL_PORT else
              f"    agents via : adk --backend openai --base-url http://localhost:{port}/v1")
        return 0

    # Reuse an existing container if already up.
    existing = subprocess.run(["docker", "ps", "-q", "-f", f"name=^{name}$"], capture_output=True, text=True)
    if (existing.stdout or "").strip():
        print(f"  [=] {name} already running on :{port}.")
        return 0
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)  # clear any stopped remnant

    print(f"  [*] Starting Bonsai-27B ({image}) on :{port} ...")
    res = subprocess.run(run_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        err = (res.stderr or "").strip()
        if "No such image" in err or "not found" in err:
            print(f"  [!] Image {image} not present. Build/pull it first (PrismML llama.cpp fork "
                  f"+ Bonsai Q1_0 GGUF baked), then re-run `adk bonsai-local`.")
        else:
            print(f"  [!] docker run failed: {err}")
        return 1

    # Poll health.
    import urllib.request
    import time
    healthy = False
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=2) as r:
                if r.status == 200:
                    healthy = True
                    break
        except Exception:
            time.sleep(2)
    if healthy:
        print(f"  [+] Bonsai-27B live on http://localhost:{port} — open aitherium.com; it'll chat on your hardware.")
        if port == BONSAI_LOCAL_PORT:
            print("  [+] Point agents at it with:  adk --backend bonsai-local")
        else:
            print(f"  [!] Non-default port {port}: no preset targets it. Use "
                  f"`adk --backend openai --base-url http://localhost:{port}/v1`.")
        return 0
    print(f"  [!] Container started but :{port}/health didn't come up in ~60s. Check `docker logs {name}`.")
    return 1


def cmd_down(args):
    """Stop the running agent + tunnel and remove its autostart entry."""
    from adk import agent_daemon as daemon

    import httpx

    st = daemon.read_status()
    if not st:
        print("  No agent is running (no status file).")
        return 0

    if daemon.kill_pid(st.get("tunnel_pid")):
        print("  [+] Tunnel stopped.")
    if daemon.kill_pid(st.get("server_pid")):
        print("  [+] Agent stopped.")

    # Best-effort deregister from the fleet.
    if st.get("registered") and st.get("name"):
        saved = load_saved_config()
        token = saved.get("api_key", "") or os.environ.get("AITHER_PORTAL_TOKEN", "")
        portal = (st.get("portal") or _control_plane()).rstrip("/")
        try:
            hdrs = {"Authorization": f"Bearer {token}"} if token else {}
            httpx.request("DELETE",
                          f"{portal}/api/genesis/v1/agent/agent-endpoints/{st['name']}",
                          headers=hdrs, timeout=10)
            print("  [+] Deregistered from the fleet.")
        except (httpx.HTTPError, OSError):
            pass

    if not getattr(args, "keep_autostart", False):
        if daemon.remove_autostart():
            print("  [+] Autostart removed.")

    daemon.clear_status()
    return 0


def cmd_reregister(args):
    """Re-register one or more endpoints with their new A2A public keys.

    Useful for backfilling existing endpoints that were registered before
    public_key support was added. Fetches the endpoint(s) from the registry
    and re-sends the registration with the current agent's public key.

    Usage:
        adk reregister --name <endpoint>  # Re-register one endpoint
        adk reregister --all              # Re-register all endpoints for this agent
    """
    import httpx

    name = getattr(args, "name", None)
    all_endpoints = bool(getattr(args, "all", False))

    if not name and not all_endpoints:
        print("  Error: provide --name <endpoint> or --all")
        return 1

    # Load config and token
    saved = load_saved_config()
    token = (getattr(args, "token", "") or os.environ.get("AITHER_PORTAL_TOKEN", "")
             or saved.get("api_key", "") or saved.get("access_token", ""))
    if not token:
        print("  Error: Not authenticated. Run: adk login")
        return 1

    portal = (getattr(args, "portal", "") or _control_plane()).rstrip("/")

    # Get current public key
    from adk.a2a_identity import get_a2a_public_key
    public_key = get_a2a_public_key()

    hdrs = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    if all_endpoints:
        # Fetch all endpoints, then re-register each with updated public_key
        try:
            list_url = f"{portal}/api/genesis/v1/agent/agent-endpoints"
            list_resp = httpx.get(list_url, headers=hdrs, timeout=20)
            if list_resp.status_code != 200:
                print(f"  Error: Could not fetch endpoint list ({list_resp.status_code})")
                return 1
            endpoints = list_resp.json()
            if not isinstance(endpoints, list):
                endpoints = endpoints.get("data", []) if isinstance(endpoints, dict) else []
        except (httpx.HTTPError, OSError, ValueError) as e:
            print(f"  Error: Failed to fetch endpoints: {e}")
            return 1

        if not endpoints:
            print("  No endpoints found to re-register.")
            return 0

        print(f"  Re-registering {len(endpoints)} endpoint(s) with public_key...")
        success_count = 0
        for ep in endpoints:
            ep_name = ep.get("name", "")
            if not ep_name:
                continue
            # Re-register with updated public_key
            reg_url = f"{portal}/api/genesis/v1/agent/agent-endpoints/{ep_name}"
            ep_copy = dict(ep)
            ep_copy["public_key"] = public_key
            try:
                rr = httpx.put(reg_url, json=ep_copy, headers=hdrs, timeout=20)
                if rr.status_code < 300:
                    print(f"    [+] {ep_name}")
                    success_count += 1
                else:
                    print(f"    [!] {ep_name} ({rr.status_code})")
            except (httpx.HTTPError, OSError) as e:
                print(f"    [!] {ep_name}: {e}")
        print(f"  Re-registered {success_count}/{len(endpoints)} endpoint(s).")
        return 0 if success_count == len(endpoints) else 1
    else:
        # Re-register single endpoint
        print(f"  Re-registering {name} with public_key...")
        # Fetch current endpoint details
        get_url = f"{portal}/api/genesis/v1/agent/agent-endpoints/{name}"
        try:
            get_resp = httpx.get(get_url, headers=hdrs, timeout=20)
            if get_resp.status_code != 200:
                print(f"  Error: Endpoint not found or access denied ({get_resp.status_code})")
                return 1
            ep = get_resp.json()
        except (httpx.HTTPError, OSError, ValueError) as e:
            print(f"  Error: Failed to fetch endpoint: {e}")
            return 1

        # Update with new public_key
        ep["public_key"] = public_key

        # Re-register
        reg_url = f"{portal}/api/genesis/v1/agent/agent-endpoints/{name}"
        try:
            rr = httpx.put(reg_url, json=ep, headers=hdrs, timeout=20)
            if rr.status_code < 300:
                print(f"  [+] {name} re-registered with public_key: {public_key[:16]}...")
                return 0
            else:
                print(f"  Error: Re-registration failed ({rr.status_code})")
                print(f"  Response: {rr.text[:300]}")
                return 1
        except (httpx.HTTPError, OSError) as e:
            print(f"  Error: {e}")
            return 1


def _provision_qdrant() -> tuple[bool, str, str]:
    """Provision local Qdrant for adk stack.

    Generates/loads API key from config, starts container if needed.
    Returns (success, url, api_key).
    """
    import secrets
    import subprocess

    saved = load_saved_config()
    api_key = saved.get("qdrant_api_key", "").strip()

    # Generate if missing
    if not api_key:
        api_key = secrets.token_urlsafe(32)
        save_saved_config({"qdrant_api_key": api_key})
        print("  [+] Generated Qdrant API key")

    # Start the container, VERIFYING the start actually succeeded — a failed
    # `docker start` must not be reported as success (gate finding).
    try:
        subprocess.run(
            ["docker", "inspect", "aitheros-workspace-qdrant"],
            capture_output=True, timeout=5, check=False
        )
        start = subprocess.run(
            ["docker", "start", "aitheros-workspace-qdrant"],
            capture_output=True, timeout=5, check=False
        )
        if start.returncode != 0:
            print(f"  [!] Failed to start Qdrant: {start.stderr.decode(errors='replace').strip()}")
            print("      AITHER_FLEET_QDRANT_URL=http://localhost:6333")
            print(f"      AITHER_FLEET_QDRANT_API_KEY={api_key}")
            return False, "", api_key
        print("  [+] Qdrant container is running")
    except (OSError, subprocess.TimeoutExpired):
        # Docker not available; just return config for manual setup
        print("  [!] Docker not available; set env manually:")
        print("      AITHER_FLEET_QDRANT_URL=http://localhost:6333")
        print(f"      AITHER_FLEET_QDRANT_API_KEY={api_key}")
        return False, "", api_key

    # Set env for this process + child processes — but a customer's explicit
    # AITHER_FLEET_QDRANT_URL/API_KEY takes precedence (gate finding: don't
    # clobber a pre-existing setup).
    url = "http://localhost:6333"
    if not os.environ.get("AITHER_FLEET_QDRANT_URL"):
        os.environ["AITHER_FLEET_QDRANT_URL"] = url
    if not os.environ.get("AITHER_FLEET_QDRANT_API_KEY"):
        os.environ["AITHER_FLEET_QDRANT_API_KEY"] = api_key
    save_saved_config({
        "qdrant_url": url,
        "qdrant_api_key": api_key,
    })
    return True, url, api_key


def cmd_register(args):
    """Register a new Aitherium account."""
    import asyncio
    import getpass

    async def _register():
        from adk.elysium import Elysium

        email = args.email
        password = args.password

        # Interactive prompts when flags are omitted
        if not email:
            email = input("  Email: ").strip()
        if not password:
            password = getpass.getpass("  Password: ")

        if not email or not password:
            print("  Error: email and password are required.")
            return 1

        print()
        print(f"  Registering {email}...")

        ely = Elysium()
        try:
            result = await ely.register(email, password)
        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
            print(f"  Error: {exc}")
            return 1

        user_id = result.get("user_id", "")
        api_key = result.get("api_key", "")

        if api_key:
            save_saved_config({"api_key": api_key, "email": email})
            print("  API key saved to ~/.aither/config.json")

        print()
        print(f"  Account created (user_id: {user_id}).")
        print("  Check your email to verify, then run: aither connect")
        return 0

    return asyncio.run(_register())


# ---------------------------------------------------------------------------
# adk login / whoami / logout — device flow auth (RFC 8628)
# ---------------------------------------------------------------------------

_DEFAULT_IDENTITY_URL = "https://portal.aitherium.com"


def _derive_cloud_endpoints(identity_url: str) -> dict | None:
    """Map the identity/portal URL a user logged into → their workspace's
    data-plane endpoints (inference + MCP + identity).

    The Aitherium cloud serves both OpenAI-compatible inference (``/v1/*``)
    and the MCP protocol (``/mcp``) from ``mcp.aitherium.com`` — see
    AitherOS/config/tunnel-routes.yaml. We only auto-derive for the known
    aitherium.com topology; localhost and unknown hosts return None so we
    never clobber a local-dev or bespoke sovereign setup with a guess.
    """
    from urllib.parse import urlparse

    host = (urlparse(identity_url).hostname or "").lower()
    if not host or host in ("localhost", "127.0.0.1", "::1"):
        return None
    if host == "aitherium.com" or host.endswith(".aitherium.com"):
        return {
            "api_url": "https://mcp.aitherium.com",
            "mcp_url": "https://mcp.aitherium.com/mcp",
            "inference_url": "https://mcp.aitherium.com/v1",
            "identity_url": "https://idp.aitherium.com",
        }
    return None


def _persist_workspace_endpoints(identity_url: str) -> dict | None:
    """After login, write the workspace endpoints to ~/.aither/config.json AND
    ~/.aither/shell.yaml so the `aither` REPL connects to the cloud with no
    manual env vars. Returns the endpoints, or None if nothing was derived.
    """
    eps = _derive_cloud_endpoints(identity_url)
    if not eps:
        return None

    # 1) config.json — Python SDK + `adk whoami`.
    save_saved_config(eps)

    # 2) shell.yaml — the TS `aither` shell reads api_url/mcp_url/identity_url.
    try:
        cfg_dir = Path.home() / ".aither"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        shell_yaml = cfg_dir / "shell.yaml"
        existing: dict[str, str] = {}
        if shell_yaml.exists():
            for line in shell_yaml.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                existing[key.strip()] = value.strip()
        existing.update({
            "api_url": eps["api_url"],
            "mcp_url": eps["mcp_url"],
            "identity_url": eps["identity_url"],
        })
        shell_yaml.write_text(
            "\n".join(f"{k}: {v}" for k, v in existing.items()) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass  # config.json still carries the endpoints

    return eps


def _persist_shell_auth(identity_url: str, eps: dict | None, result: dict) -> bool:
    """Mirror the login into ~/.aither/auth.json so the `aither` (npm) shell
    picks up the token + cloud endpoint without a second login.

    `adk login` historically wrote only ~/.aither/config.json, but the
    TypeScript shell reads its token from auth.json — so one login left the
    REPL unauthenticated. Best-effort: never fails the login.
    """
    try:
        from adk.shell.auth import AuthStore, build_profile_from_response
    except Exception:
        return False
    genesis_url = (eps or {}).get("api_url") or identity_url
    try:
        profile = build_profile_from_response(identity_url, genesis_url, result)
        AuthStore.set_profile("cloud" if eps else "default", profile)
        return True
    except Exception:
        return False


def _resolve_identity_url(url: str) -> str:
    """Resolve the AitherIdentity API host from a portal/identity URL.

    The device-flow API (`/auth/device/code|token`) lives on AitherIdentity
    (prod: idp.aitherium.com), NOT the Veil/portal frontend (portal/veil
    .aitherium.com) — those 307-redirect API auth calls to `/login`. For the
    known aitherium.com topology, map to the idp host; otherwise (localhost /
    bespoke sovereign) use the URL as-is.
    """
    eps = _derive_cloud_endpoints(url)
    return (eps["identity_url"] if eps else url).rstrip("/")


def _post_json_resilient(
    url: str,
    payload: dict,
    headers: dict,
    *,
    attempt_timeout: int = 15,
    total_budget: float = 90.0,
) -> dict:
    """POST JSON with retry/backoff over TRANSIENT failures, returning parsed JSON.

    Why this exists: the first request to a just-restarted security-core (or
    through a cold Cloudflare tunnel) can be slow or briefly return 5xx
    (502/503/504/520-530) while it warms its vault/DB connections. A single-shot
    request dies in that window -- the #1 cause of "Cannot reach idp.aitherium.com"
    on `adk login`. We retry transient failures with exponential backoff up to
    `total_budget` seconds so login sails through the cold-start window. Genuine
    client errors (4xx other than 408/429) fail fast and are NOT masked.
    """
    import json as _json
    import time as _time
    import urllib.request as _ur
    import urllib.error as _ue

    _TRANSIENT = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530}  # noqa: N806 — deliberate constant-style set of transient HTTP codes
    body = _json.dumps(payload).encode()
    deadline = _time.time() + total_budget
    delay = 2.0
    attempt = 0
    printed = False
    last_exc = None
    while True:
        attempt += 1
        req = _ur.Request(url, data=body, headers=headers, method="POST")
        try:
            with _ur.urlopen(req, timeout=attempt_timeout) as resp:
                result = _json.loads(resp.read())
            if printed:
                print(" ok")
            return result
        except _ue.HTTPError as exc:
            if exc.code not in _TRANSIENT:
                if printed:
                    print()
                raise  # genuine error -- surface it, don't retry or mask
            last_exc = exc
        except (_ue.URLError, OSError) as exc:
            last_exc = exc
        remaining = deadline - _time.time()
        if remaining <= 0:
            if printed:
                print()
            raise RuntimeError(
                f"Cannot reach {url} after {attempt} attempts over "
                f"{int(total_budget)}s (last error: {last_exc}). The service may "
                f"be starting up -- wait a few seconds and run `adk login` again."
            )
        if not printed:
            print("  Service waking up (cold start) -- retrying", end="", flush=True)
            printed = True
        print(".", end="", flush=True)
        _time.sleep(min(delay, remaining))
        delay = min(delay * 1.5, 10.0)


def _device_flow_login(identity_url: str, client_name: str = "adk") -> dict:
    """Run RFC 8628 device code flow. Returns token response dict or raises."""
    import json as _json
    import time
    import urllib.request
    import urllib.error
    import webbrowser

    # A real User-Agent: idp.* sits behind Cloudflare, which 403s urllib's default
    # "Python-urllib/3.x" UA as a bot. Without this, device flow fails before it starts.
    try:
        from adk import __version__ as _v
    except Exception:  # noqa: BLE001
        _v = "0"
    _ua = f"awdk/{_v} ({client_name})"

    # Step 1: Request device code. Resilient: the first call after a fleet
    # restart can be slow or briefly 5xx while security-core warms its vault/DB,
    # so retry transient failures with backoff instead of dying on attempt 1.
    try:
        data = _post_json_resilient(
            f"{identity_url}/auth/device/code",
            {"client_name": client_name, "scopes": "full"},
            {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": _ua},
        )
    except RuntimeError:
        raise  # already a clear, budget-exhausted message
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Cannot reach {identity_url}: {exc}") from exc

    user_code = data["user_code"]
    device_code = data["device_code"]
    verification_uri = data.get("verification_uri_complete") or data.get("verification_uri", "")
    interval = max(2, int(data.get("interval", 5)))
    expires_in = int(data.get("expires_in", 900))

    # Step 2: Show code + open browser
    print()
    print(f"  Your code: {user_code}")
    print()
    print(f"  Opening browser to: {verification_uri}")
    print("  (If it doesn't open, visit the URL manually and enter the code)")
    print()

    try:
        webbrowser.open(verification_uri)
    except OSError:
        pass  # Browser open is best-effort

    print("  Waiting for approval", end="", flush=True)

    # Step 3: Poll for token
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        poll_data = _json.dumps({"device_code": device_code}).encode()
        poll_req = urllib.request.Request(
            f"{identity_url}/auth/device/token",
            data=poll_data,
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": _ua},
            method="POST",
        )
        try:
            with urllib.request.urlopen(poll_req, timeout=10) as resp:
                result = _json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                try:
                    err_body = _json.loads(exc.read())
                    detail = err_body.get("detail", "")
                except (ValueError, OSError):
                    detail = ""
                if detail == "expired_token":
                    print()
                    raise RuntimeError("Device code expired. Run `adk login` again.")
                if detail == "invalid_device_code":
                    print()
                    raise RuntimeError("Invalid device code. Run `adk login` again.")
            print(".", end="", flush=True)
            continue
        except (urllib.error.URLError, OSError):
            print(".", end="", flush=True)
            continue

        status = result.get("status", "")
        if status == "authorization_pending":
            print(".", end="", flush=True)
            continue
        if result.get("access_token"):
            print(" approved!")
            return result
        # Unknown status — keep polling
        print(".", end="", flush=True)

    print()
    raise RuntimeError("Timed out waiting for approval. Run `adk login` again.")


def _github_device_flow_login(identity_url: str, client_name: str = "adk") -> dict:
    """Log in with a GitHub identity via GitHub's device flow.

    Identity drives GitHub's RFC-8628 device flow server-side (the GitHub
    client secret never touches the CLI) and mints an Aither session token from
    the GitHub identity. Returns the same {access_token, ...} shape as
    `_device_flow_login`, so callers are interchangeable.
    """
    import json as _json
    import time
    import urllib.request
    import urllib.error
    import webbrowser

    try:
        from adk import __version__ as _v
    except Exception:  # noqa: BLE001
        _v = "0"
    _ua = f"awdk/{_v} ({client_name})"
    _hdrs = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": _ua}

    try:
        data = _post_json_resilient(f"{identity_url}/auth/github/device/start", {}, _hdrs)
    except RuntimeError:
        raise
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Cannot reach {identity_url}: {exc}") from exc

    handle = data.get("handle")
    user_code = data.get("user_code", "")
    verification_uri = data.get("verification_uri", "https://github.com/login/device")
    interval = max(2, int(data.get("interval", 5)))
    expires_in = int(data.get("expires_in", 900))
    if not handle or not user_code:
        raise RuntimeError("GitHub device start returned no code. Try `adk login` (email) instead.")

    print()
    print(f"  Your GitHub code: {user_code}")
    print()
    print(f"  Opening browser to: {verification_uri}")
    print("  (If it doesn't open, visit the URL manually and enter the code)")
    print()
    try:
        webbrowser.open(verification_uri)
    except OSError:
        pass
    print("  Waiting for GitHub approval", end="", flush=True)

    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        poll_req = urllib.request.Request(
            f"{identity_url}/auth/github/device/poll",
            data=_json.dumps({"handle": handle}).encode(),
            headers=_hdrs,
            method="POST",
        )
        try:
            with urllib.request.urlopen(poll_req, timeout=10) as resp:
                result = _json.loads(resp.read())
        except urllib.error.HTTPError:
            print(".", end="", flush=True)
            continue
        except (urllib.error.URLError, OSError):
            print(".", end="", flush=True)
            continue
        status = result.get("status", "")
        if status == "pending":
            interval = max(interval, int(result.get("interval", interval)))
            print(".", end="", flush=True)
            continue
        if status == "error":
            print()
            raise RuntimeError(f"GitHub login failed: {result.get('error', 'unknown')}. Run `adk login --github` again.")
        if status == "complete" and result.get("access_token"):
            print(" approved!")
            return result
        print(".", end="", flush=True)

    print()
    raise RuntimeError("Timed out waiting for GitHub approval. Run `adk login --github` again.")


def _save_account_license(result: dict) -> str:
    """Persist the account's signed license from a login result to
    ``~/.aither/license.json`` so LicenseManager gates packs on the user's real
    AitherIdentity/ACTA entitlements. Returns the tier (or "" if none/failed).

    The ``license_key`` from AitherIdentity is the base64 outer envelope
    (base64(json({payload, signature}))); LicenseManager's file path expects the
    decoded {payload, signature} envelope. Fail-soft: a bad/absent key just leaves
    the runtime on its prior tier (COMMUNITY) — never blocks login.
    """
    import base64 as _b64
    import json as _json
    from pathlib import Path as _Path

    license_key = (result.get("license_key") or "").strip()
    if not license_key:
        return ""
    try:
        envelope = _json.loads(_b64.b64decode(license_key).decode("utf-8"))
        if not (isinstance(envelope, dict) and "payload" in envelope and "signature" in envelope):
            return ""
        lic_path = _Path.home() / ".aither" / "license.json"
        lic_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = lic_path.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(envelope), encoding="utf-8")
        tmp.replace(lic_path)
        return str(result.get("tier") or "")
    except Exception as exc:  # never break login over license persistence
        import logging as _logging
        _logging.getLogger("adk.cli").debug("license persistence skipped: %s", exc)
        return ""


def cmd_login(args) -> int:
    """Authenticate with Aitherium — device flow, email/password, or API key."""

    identity_url = (args.portal_url or os.getenv("AITHER_PORTAL_URL", _DEFAULT_IDENTITY_URL)).rstrip("/")

    # Path 1: Direct API key
    api_key = getattr(args, "api_key", None)
    if api_key:
        save_saved_config({"api_key": api_key})
        print("  API key saved to ~/.aither/config.json")
        eps = _persist_workspace_endpoints(identity_url)
        _persist_shell_auth(identity_url, eps, {"access_token": api_key, "token_type": "api_key", "user": {}})
        if eps:
            print(f"  Workspace endpoints configured → {eps['api_url']}")
        return 0

    # Path 2: Email/password
    email = getattr(args, "email", None)
    if email:
        import asyncio
        import getpass
        password = getattr(args, "password", None) or getpass.getpass("  Password: ")
        if not password:
            print("  Error: password required.")
            return 1

        async def _login():
            from adk.elysium import Elysium
            ely = Elysium(gateway_url=identity_url)
            try:
                result = await ely.login(email, password)
            except (ConnectionError, OSError, RuntimeError) as exc:
                print(f"  Error: {exc}")
                return 1
            token = result.get("token", "")
            if token:
                save_saved_config({"api_key": token, "email": email})
                print(f"  Logged in as {email}")
                print("  Token saved to ~/.aither/config.json")
                eps = _persist_workspace_endpoints(identity_url)
                _persist_shell_auth(identity_url, eps, result)
                if eps:
                    print(f"  Workspace endpoints configured → {eps['api_url']}")
                return 0
            print(f"  Login failed: {result}")
            return 1

        return asyncio.run(_login())

    # Path 3: Device flow (default — opens browser)
    print()
    print("  AitherOS Login")
    print("  ==============")
    try:
        # Device flow must hit AitherIdentity (idp.*), not the portal frontend.
        result = _device_flow_login(_resolve_identity_url(identity_url))
    except RuntimeError as exc:
        print(f"  Error: {exc}")
        return 1

    token = result.get("access_token", "")
    user = result.get("user", {})
    if not token:
        print("  Error: no token in response.")
        return 1

    # Save token
    config_update = {"api_key": token}
    if isinstance(user, dict):
        if user.get("tenant_id"):
            config_update["tenant_id"] = user["tenant_id"]
        if user.get("username"):
            config_update["username"] = user["username"]
    save_saved_config(config_update)

    # Persist the account's Ed25519 license so pack entitlements (can_use_formbridge,
    # can_use_untether, can_use_computer_use, ...) flow from the USER'S AitherIdentity
    # account into this runtime. AitherIdentity's device-flow already fetches it from
    # ACTA (/v1/billing/license) and returns it here — previously it was dropped, so
    # LicenseManager fell back to COMMUNITY (free) and every premium pack gated closed.
    # LicenseManager reads ~/.aither/license.json as the {payload, signature} envelope.
    license_tier = _save_account_license(result)

    eps = _persist_workspace_endpoints(identity_url)
    _persist_shell_auth(identity_url, eps, result)

    # Auto-sync the account's secrets vault into the local encrypted keyring so a
    # just-logged-in user has their keys (BYO provider keys, tokens) available
    # immediately. The #1 onboarding gap was that `adk secret sync` was manual and
    # nobody ran it after login. Best-effort — a sync hiccup must NEVER fail login.
    # Opt out with `adk login --no-sync`.
    synced_count = None
    if not getattr(args, "no_sync", False):
        try:
            import asyncio as _aio
            from adk.sync.secrets import SecretsSync
            _synced = _aio.run(SecretsSync(api_key=token).sync())
            synced_count = len(_synced) if _synced is not None else 0
        except Exception:  # noqa: BLE001 — sync is a convenience, not a gate
            synced_count = -1

    username = user.get("username", "") if isinstance(user, dict) else ""
    print()
    print(f"  Logged in{' as ' + username if username else ''}!")
    print("  Token saved to ~/.aither/config.json")
    if license_tier:
        print(f"  License saved → tier '{license_tier}' (pack entitlements active)")
    if eps:
        print(f"  Workspace endpoints configured → {eps['api_url']} (inference + MCP)")
    if synced_count is not None and synced_count >= 0:
        print(f"  Vault synced → {synced_count} secret(s) available locally")
    elif synced_count == -1:
        print("  (vault sync skipped — run `adk secret sync` to pull your keys)")
    print()
    return 0


def _local_license_tier() -> str:
    """Entitlement tier from the saved account license, or "" if none.

    Reads the same `~/.aither/license.json` that `adk login` writes via
    `_save_account_license`, so `whoami` reports the tier the pack gates
    actually use rather than a second, differently-derived answer.
    """
    import json as _json
    from pathlib import Path as _Path

    path = _Path.home() / ".aither" / "license.json"
    if not path.exists():
        return ""
    try:
        env = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    payload = env.get("payload") if isinstance(env, dict) else None
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("tier") or payload.get("plan_tier") or "").strip()


def cmd_whoami(args) -> int:
    """Show current auth status and entitlement tier."""
    saved = load_saved_config()
    api_key = saved.get("api_key", "")
    username = saved.get("username", "")
    email = saved.get("email", "")
    tenant_id = saved.get("tenant_id", "")
    backend = saved.get("setup_backend", saved.get("default_backend", ""))
    inference_url = saved.get("inference_url", "")

    # NOTE on the absent --refresh: the portal advertised
    # `aither entitlement --refresh --json`, and a refresh cannot be honoured —
    # the signed license is minted ONLY inside the device-flow token exchange
    # (`/auth/device/token`), and AitherIdentity exposes no license endpoint to
    # re-fetch from. Shipping a `--refresh` that quietly did nothing would be
    # the silent-no-op pattern: a user who just paid would run it, see the old
    # tier, and conclude the purchase failed. Until such an endpoint exists,
    # `adk login` is the way to pick up a new plan, and this says so below.
    tier = _local_license_tier()

    if getattr(args, "json", False):
        import json as _json
        out = {
            "logged_in": bool(api_key or backend),
            "username": username,
            "email": email,
            "tenant_id": tenant_id,
            "tier": tier,
            "entitled": bool(tier) and tier.lower() not in ("", "free", "community"),
            "backend": backend,
            "inference_url": inference_url,
            "api_key_present": bool(api_key),
        }
        print(_json.dumps(out, indent=2))
        # Exit code carries the answer too, so a script can gate on it without
        # parsing: 0 = authenticated, 1 = not.
        return 0 if (api_key or backend) else 1

    if not api_key and not backend:
        print("  Not logged in. Run: adk login")
        return 1

    print()
    print("  AitherOS Identity")
    print("  =================")
    if username:
        print(f"  User:      {username}")
    if email:
        print(f"  Email:     {email}")
    if api_key:
        # Mask the key
        if len(api_key) > 16:
            print(f"  API key:   {api_key[:12]}...{api_key[-4:]}")
        else:
            print("  API key:   (set)")
    if tenant_id:
        print(f"  Tenant:    {tenant_id}")
    if backend:
        print(f"  Backend:   {backend}")
    if inference_url:
        print(f"  Inference: {inference_url}")
    print(f"  Tier:      {tier or 'free (no paid entitlement)'}")
    if not tier:
        print("  (bought a plan? run `adk login` again to pick up the new tier)")
    print()
    return 0


def cmd_logout(args) -> int:
    """Clear saved auth tokens."""
    import json as _json
    config_path = Path.home() / ".aither" / "config.json"
    if config_path.exists():
        try:
            config = _json.loads(config_path.read_text())
        except (OSError, ValueError):
            config = {}
        for key in ("api_key", "username", "email", "tenant_id"):
            config.pop(key, None)
        config_path.write_text(_json.dumps(config, indent=2))
        print("  Logged out. Auth tokens cleared from ~/.aither/config.json")
    else:
        print("  No config found — already logged out.")

    # Also clear auth.json active profile if it exists
    auth_path = Path.home() / ".aither" / "auth.json"
    if auth_path.exists():
        try:
            import json as _json
            auth = _json.loads(auth_path.read_text())
            if isinstance(auth, dict):
                auth["active_profile"] = ""
                auth_path.write_text(_json.dumps(auth, indent=2))
                print("  Cleared active profile in ~/.aither/auth.json")
        except Exception:
            pass
    return 0


def cmd_ambient(args) -> int:
    """Terminal sensor for the ambient expertise loop."""
    from adk import ambient as amb

    action = getattr(args, "ambient_command", None)

    if action == "install":
        print(amb.install_hook(args.shell))
        print("The hook reports finished commands so the agent can research what")
        print("you get stuck on. Disable anytime with AITHER_AMBIENT=0.")
        return 0

    if action == "uninstall":
        print(amb.uninstall_hook(args.shell))
        return 0

    if action == "report":
        try:
            data = json.loads(args.payload)
        except json.JSONDecodeError as e:
            print(f"bad payload: {e}", file=sys.stderr)
            return 1
        result = amb.observe_command(
            command=data.get("command", ""),
            exit_code=int(data.get("exit_code", 0)),
            cwd=data.get("cwd", ""),
            error_text=data.get("error", ""),
        )
        # The hook discards this; printing keeps `aither ambient report` usable
        # by hand, which is how you debug a sensor that looks installed and is
        # silently 401ing.
        print(json.dumps(result))
        return 0

    if action == "brief":
        result = amb.brief(args.locator, args.surface)
        if result.get("available"):
            print(f"Topic: {result.get('topic')}\n")
            print(result.get("brief", ""))
        else:
            print(f"Nothing known yet — {result.get('reason', 'no data')}")
        return 0

    if action == "status":
        import urllib.request

        url = f"{amb.DEFAULT_GENESIS.rstrip('/')}/ambient/stats"
        try:
            req = urllib.request.Request(url, headers=amb._auth_headers())
            with urllib.request.urlopen(req, timeout=8) as resp:
                print(json.dumps(json.loads(resp.read().decode("utf-8")), indent=2))
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"could not reach {url}: {e}", file=sys.stderr)
            return 1

    print("usage: aither ambient {install|uninstall|report|brief|status}")
    return 1


def cmd_balance(args) -> int:
    """Show Aitherium credit account (balance, earnings, spending)."""
    import requests

    saved = load_saved_config()
    api_key = saved.get("api_key", "")
    username = saved.get("username", "")
    email = saved.get("email", "")
    tenant_id = saved.get("tenant_id", "")

    if not api_key:
        print("  Error: Not logged in. Run: adk login")
        return 1

    # Resolve user_id: use tenant_id if set, else username or email
    user_id = tenant_id or username or email
    if not user_id:
        print("  Error: Could not determine user ID from config")
        return 1

    # Call the ACTA endpoint: GET /v1/billing/member/{user_id}
    # Try several possible endpoints (local, cloud, configured)
    acta_urls = [
        "http://localhost:8200",  # Local fleet
        "https://localhost:8200",  # Local fleet with TLS
        "https://portal-gateway.aitherium.com",  # Cloud
    ]

    account_info = None
    last_error = None

    for base_url in acta_urls:
        try:
            url = f"{base_url}/v1/billing/member/{user_id}"
            headers = {"Authorization": f"Bearer {api_key}"}
            # Never disable verification here: this request carries a bearer
            # token, so an unverified TLS session hands the API key to any MITM.
            # tls_verify() verifies by default and returns the AitherNet CA
            # bundle when present, so self-signed internal certs are trusted
            # *with* verification rather than by turning it off.
            from adk._tls import tls_verify

            resp = requests.get(
                url, headers=headers, timeout=5, verify=tls_verify()
            )
            if resp.status_code == 200:
                account_info = resp.json()
                break
            elif resp.status_code == 401:
                print("  Error: Authentication failed (invalid API key)")
                return 1
            elif resp.status_code == 403:
                print("  Error: Access denied (cannot view this account)")
                return 1
            elif resp.status_code == 404:
                print("  Error: Account not found")
                return 1
        except Exception as e:
            last_error = str(e)
            continue

    if not account_info:
        print("  Error: Could not reach ACTA service")
        if last_error:
            print(f"  Last error: {last_error}")
        print("  Make sure you are connected to AitherOS (adk login / adk connect)")
        return 1

    # Format and display the account info
    print()
    print("  Aitherium Credit Account")
    print("  ════════════════════════")
    print()

    # Spendable balance
    spendable = account_info.get("spendable_tokens", 0)
    print(f"  Spendable Credits:  {spendable:,} tokens")

    # Monthly allotment
    monthly = account_info.get("monthly", {})
    allotment = monthly.get("plan_allotment", 0)
    used = monthly.get("used_this_month", 0)
    remaining = monthly.get("remaining", 0)
    if allotment > 0:
        pct = (used / allotment * 100) if allotment else 0
        print(f"  Monthly Plan:       {used:,}/{allotment:,} used ({pct:.1f}%)")
        print(f"  Remaining This Mo:  {remaining:,} tokens")

    print()
    print("  Lifetime Earnings")
    print("  ─────────────────")
    earning = account_info.get("earning", {})
    earned_tokens = earning.get("lifetime_settled_tokens", 0)
    earned_usd = earning.get("lifetime_usd", 0)
    print(f"  Total Earned:       {earned_tokens:,} tokens (${earned_usd:.2f})")

    recent_earnings = earning.get("recent_earnings", [])
    if recent_earnings:
        print(f"  Recent Serves:      {len(recent_earnings)} (last 10)")
        for rc in recent_earnings[:3]:
            ts = rc.get("created_at", "")[:10]
            toks = rc.get("tokens", 0)
            print(f"    • {ts}: {toks} tokens")
        if len(recent_earnings) > 3:
            print(f"    + {len(recent_earnings) - 3} more...")

    print()
    print("  Lifetime Spending")
    print("  ─────────────────")
    spending = account_info.get("spending", {})
    spent_tokens = spending.get("lifetime_consumed_tokens", 0)
    spent_usd = spending.get("lifetime_usd", 0)
    print(f"  Total Spent:        {spent_tokens:,} tokens (${spent_usd:.2f})")

    print()
    print("  Account Summary")
    print("  ───────────────")
    net = account_info.get("net", {})
    net_earned = net.get("earned_minus_spent", 0)
    print(f"  Net Position:       {net_earned:,} tokens")

    print()
    return 0


def cmd_x_session(args) -> int:
    """Bootstrap / inspect the browser-transport X session.

    The autonomous X poster (browser transport) needs a logged-in x.com session
    stored in the fleet's local vault. This never handles a password: it takes an
    ALREADY logged-in session — exported from a browser you are signed into — and
    hands it to the fleet's verify-and-store endpoint, which proves it is live
    before persisting it. The Awconnect extension does the same thing with a
    single click; this is the headless/operator path.

    Export a state file with any of:
      * the Awconnect sidepanel "Sync X session" (no file needed — it POSTs
        directly), or
      * `playwright open --save-storage=x_state.json x.com` after logging in, or
      * any tool that writes a Playwright storage_state JSON.
    """
    import json as _json

    import requests

    from adk._tls import tls_verify

    sub = getattr(args, "x_session_command", None)

    # Fleet endpoints, host-side. Genesis LB terminates plain HTTP on 8001; the
    # worker serves the same router on 8159 (TLS). Try both.
    bases = [
        ("http://localhost:8001", False),
        ("https://localhost:8159", tls_verify()),
    ]
    saved = load_saved_config()
    api_key = saved.get("api_key", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def _call(method, path, body=None):
        # Try each base; a 404 means THIS host does not serve the route (Genesis
        # bakes its code and lacks /social/x/* until rebuilt), so fall through to
        # the next base rather than reporting the 404. Only a non-404 response —
        # or the last base — is authoritative. Without this the worker's real
        # verdict is shadowed by Genesis's 404 (the route lives on the worker).
        last = None
        last_404 = None
        for base, verify in bases:
            try:
                url = f"{base}{path}"
                resp = requests.request(
                    method, url, headers=headers, json=body, timeout=90, verify=verify
                )
                data = resp.json()
                if resp.status_code == 404:
                    last_404 = (resp.status_code, data)
                    continue
                return resp.status_code, data
            except Exception as exc:  # noqa: BLE001 — try the next base
                last = exc
        if last_404 is not None:
            return last_404
        return 0, {"error": f"no fleet endpoint reachable: {last}"}

    if sub == "status":
        code, data = _call("GET", "/social/x/session-status")
        print(_json.dumps(data, indent=2))
        return 0 if data.get("connected") else 1

    if sub == "import":
        state_path = getattr(args, "state", None)
        if not state_path:
            print("  Error: --state <file.json> is required for import")
            return 1
        try:
            with open(state_path, encoding="utf-8") as fh:
                state = _json.load(fh)
        except Exception as exc:  # noqa: BLE001
            print(f"  Error: could not read {state_path}: {exc}")
            return 1
        # Accept either a full storage_state or {"cookies": [...]}.
        body = {"storage_state": state} if "cookies" in state else state
        code, data = _call("POST", "/social/x/import-session", body)
        if data.get("ok"):
            print(f"  Stored a verified X session for @{data.get('handle')} "
                  f"({data.get('cookie_count')} cookies).")
            print("  Flip x_autopost.enabled=true in config/social.yaml to go live.")
            return 0
        print(f"  Import failed ({data.get('reason')}): {data.get('error')}")
        return 1

    print("  Usage: adk x-session {import --state <file.json> | status}")
    return 1


def cmd_connect(args):
    """Connect to AitherOS — detect local LLMs, activate cloud, join mesh."""
    import asyncio

    # ── Elysium desktop connect shortcut ──
    if getattr(args, "elysium", None):
        return _connect_elysium(args)

    async def _connect():
        from adk.elysium import Elysium

        print()
        print("  AitherOS Connect")
        print("  ================")
        print()

        # ── 1. Local inference ─────────────────────────────────────
        print("  LOCAL INFERENCE")
        print("  ───────────────")
        backends_found = []
        import httpx

        # vLLM (preferred — enables true concurrent/parallel agents)
        for port in [8000, 8100, 8101, 8102, 8120, 8209, 8201, 8202, 8203]:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    resp = await client.get(f"http://localhost:{port}/v1/models")
                    if resp.status_code == 200:
                        data = resp.json()
                        models = [m["id"] for m in data.get("data", [])]
                        backends_found.append(("vllm", models))
                        print(f"  [OK] vLLM (:{port}) — {', '.join(models[:3])}")
            except Exception:
                pass

        # Ollama (fallback — serializes requests, no true parallelism)
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get("http://localhost:11434/api/tags")
                if resp.status_code == 200:
                    data = resp.json()
                    models = [m["name"] for m in data.get("models", [])]
                    backends_found.append(("ollama", models))
                    print(f"  [OK] Ollama — {len(models)} model(s): {', '.join(models[:5])}")
        except Exception:
            if not backends_found:
                print("  [--] Ollama — not detected")

        if not backends_found:
            print("  [--] No local LLM backends found")
            print("       Run 'aither setup' to auto-configure vLLM (recommended)")
            print("       Or install Ollama as fallback: https://ollama.com")

        # ── 2. Cloud acceleration ──────────────────────────────────
        print()
        print("  CLOUD ACCELERATION (Elysium)")
        print("  ────────────────────────────")

        # Resolve API key: flag > env > saved config
        api_key = args.api_key or os.environ.get("AITHER_API_KEY", "")
        if not api_key:
            saved = load_saved_config()
            api_key = saved.get("api_key", "")

        gateway_ok = False
        models_available = []
        balance_info = {}

        if api_key:
            print(f"  [OK] API key: {api_key[:16]}...")

            # Test inference endpoint
            try:
                async with httpx.AsyncClient(timeout=5.0, headers={
                    "Authorization": f"Bearer {api_key}",
                }) as client:
                    resp = await client.get("https://mcp.aitherium.com/health")
                    if resp.status_code == 200:
                        print("  [OK] Inference gateway: mcp.aitherium.com")
            except Exception:
                print("  [!!] Inference gateway: unreachable")

            # Fetch models
            try:
                async with httpx.AsyncClient(timeout=5.0, headers={
                    "Authorization": f"Bearer {api_key}",
                }) as client:
                    resp = await client.get("https://mcp.aitherium.com/v1/models")
                    if resp.status_code == 200:
                        data = resp.json()
                        models_available = [m["id"] for m in data.get("data", []) if m.get("accessible", True)]
                        if models_available:
                            print(f"  [OK] Models: {', '.join(models_available[:5])}")
                            if len(models_available) > 5:
                                print(f"       + {len(models_available) - 5} more")
            except Exception:
                pass

            # Test gateway + balance
            try:
                async with httpx.AsyncClient(timeout=5.0, headers={
                    "Authorization": f"Bearer {api_key}",
                }) as client:
                    resp = await client.get("https://gateway.aitherium.com/health")
                    if resp.status_code == 200:
                        gateway_ok = True
                        print("  [OK] Gateway: gateway.aitherium.com")

                    # gateway.aitherium.com answers 404 for /v1/* — the v1 API
                    # plane lives on mcp.aitherium.com (measured 2026-08-31:
                    # 404 on gateway vs 401-with-auth on mcp). On the old host
                    # this check silently never fired and the Plan/Balance line
                    # never printed.
                    resp = await client.get("https://mcp.aitherium.com/v1/billing/balance")
                    if resp.status_code == 200:
                        balance_info = resp.json()
                        plan = balance_info.get("plan", "free")
                        bal = balance_info.get("balance", 0)
                        print(f"  [OK] Plan: {plan} | Balance: {bal} tokens")
            except Exception:
                pass
        else:
            print("  [--] No API key found")
            print()
            print("  No account? Run: aither register")
            print()
            print("  Or set an existing key:")
            print("    aither connect --api-key aither_sk_live_...")
            print()
            print("  What you get with Elysium:")
            print("    - Cloud inference (no local GPU needed)")
            print("    - 100+ MCP tools (code search, memory, training)")
            print("    - AitherMesh — share compute with other nodes")
            print("    - Agent marketplace — discover and use community agents")

        # ── 2b. Tenant info ────────────────────────────────────────
        tenant_info = {}
        if api_key and gateway_ok:
            ely = Elysium(api_key=api_key)
            tenant_info = await ely.fetch_tenant_info()
            if tenant_info:
                tid = tenant_info.get("tenant_id", "unknown")
                tier = tenant_info.get("tier", tenant_info.get("plan", "unknown"))
                role = tenant_info.get("role", "member")
                print(f"  [OK] Tenant: {tid} | Tier: {tier} | Role: {role}")

        # ── 3. MCP tools ──────────────────────────────────────────
        print()
        print("  MCP TOOLS")
        print("  ─────────")

        # Local awnode
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get("http://localhost:8080/health")
                if resp.status_code == 200:
                    data = resp.json()
                    mode = data.get("mode", "unknown")
                    print(f"  [OK] awnode (local): port 8080, mode={mode}")
        except Exception:
            print("  [--] awnode (local): not running")

        # Cloud MCP
        if api_key and gateway_ok:
            print("  [OK] MCP Gateway (cloud): mcp.aitherium.com")
        elif api_key:
            print("  [--] MCP Gateway (cloud): gateway unreachable")
            # A status line that says a thing is down and stops there leaves the
            # user with nowhere to go. Self-hosting is the answer to exactly this
            # situation, so offer it here rather than making them find it.
            from adk.config import self_host_hint
            print(f"       {self_host_hint()}")
        else:
            print("  [--] MCP Gateway (cloud): needs API key")

        # ── 4. Mesh network ────────────────────────────────────────
        print()
        print("  MESH NETWORK (AitherNet)")
        print("  ────────────────────────")
        if api_key and gateway_ok:
            try:
                async with httpx.AsyncClient(timeout=5.0, headers={
                    "Authorization": f"Bearer {api_key}",
                }) as client:
                    # same wrong-host class as the balance probe above:
                    # /v1/mesh/status exists on mcp.aitherium.com, not gateway
                    resp = await client.get("https://mcp.aitherium.com/v1/mesh/status")
                    if resp.status_code == 200:
                        mesh = resp.json()
                        nodes = mesh.get("total_nodes", 0)
                        print(f"  [OK] Mesh active — {nodes} node(s) online")
                    else:
                        print("  [--] Mesh status unknown")
            except Exception:
                print("  [--] Mesh: not connected")
        else:
            print("  [--] Mesh: needs API key + gateway")
            print("       Join the mesh to share compute and accelerate inference")

        # ── 5. Save config ─────────────────────────────────────────
        if args.save:
            save_data = {
                "gateway_url": "https://gateway.aitherium.com",
                "inference_url": "https://mcp.aitherium.com/v1",
            }
            if api_key:
                save_data["api_key"] = api_key
            if backends_found:
                save_data["default_backend"] = backends_found[0][0]
            if tenant_info.get("tenant_id"):
                save_data["tenant_id"] = tenant_info["tenant_id"]

            config_path = save_saved_config(save_data)
            print(f"\n  Config saved to {config_path}")

        # ── Summary ───────────────────────────────────────────────
        print()
        print("  " + "=" * 48)
        local_count = sum(len(m) for _, m in backends_found)
        cloud_count = len(models_available)
        total_models = local_count + cloud_count

        if total_models > 0:
            parts = []
            if local_count:
                parts.append(f"{local_count} local")
            if cloud_count:
                parts.append(f"{cloud_count} cloud")
            print(f"  READY — {total_models} models ({', '.join(parts)})")
            print()
            print("  Next steps:")
            print("    aither init my-agent       # Create an agent")
            print("    cd my-agent && python agent.py")
            if not api_key:
                print()
                print("  Want more? Connect to Elysium for cloud acceleration:")
                print("    aither connect --api-key aither_sk_live_...")
        elif api_key:
            print("  CLOUD MODE — using Elysium for inference")
            print()
            print("  Next steps:")
            print("    aither init my-agent       # Create an agent")
            print("    cd my-agent && python agent.py")
        else:
            print("  NO BACKEND — install Ollama or connect to Elysium")
            print()
            print("  Option A (local):  Install Ollama at https://ollama.com")
            print("  Option B (cloud):  aither connect --api-key aither_sk_live_...")
            print("  No account?        aither register")

        # ── Tier comparison ───────────────────────────────────────
        if not api_key or (api_key and balance_info.get("plan") == "free"):
            print()
            print("  " + "-" * 48)
            print("  TIERS")
            print()
            print("  Free       Your GPU, your models, basic MCP tools")
            print("  Pro        + Cloud inference, 100+ MCP tools, mesh compute")
            print("  Enterprise + Sovereign deployment, full AitherOS, RBAC,")
            print("               tenant isolation, training pipelines")
            print()
            print("  https://aitherium.com/pricing")

        # ── OpenClaw detection ───────────────────────────────────
        from pathlib import Path as _Path
        openclaw_dir = _Path.home() / ".openclaw"
        if openclaw_dir.exists():
            import json as _oc_json
            oc_config = {}
            oc_config_path = openclaw_dir / "openclaw.json"
            if oc_config_path.exists():
                try:
                    oc_config = _oc_json.loads(oc_config_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            already = any("aither" in k.lower() for k in oc_config.get("mcpServers", {}))
            if not already:
                print()
                print("  " + "-" * 48)
                print("  OPENCLAW DETECTED")
                print()
                print("  Connect OpenClaw to AitherOS agent fleet:")
                print("    adk integrate openclaw")
                print()
                print("  This gives OpenClaw access to 29 agents, swarm coding,")
                print("  memory graph, and 100+ MCP tools.")

        print()
        return 0

    return asyncio.run(_connect())


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> reasoning blocks from LLM output."""
    # Closed tags (including <thinking>)
    text = re.sub(r'<think(?:ing)?>[\s\S]*?</think(?:ing)?>', '', text, flags=re.IGNORECASE)
    # Unclosed trailing tag
    text = re.sub(r'<think(?:ing)?>[^<]*$', '', text, flags=re.IGNORECASE)
    return text.strip()


def cmd_ui(args):
    """Manage the agent's swappable web UI pack (ls / set / path)."""
    from adk.server import (
        list_ui_packs, load_ui_pack, resolve_ui_pack_name, _ui_packs_dir,
    )
    action = getattr(args, "ui_command", None) or "ls"

    if action == "ls":
        packs = list_ui_packs()
        current = resolve_ui_pack_name()
        print("\n  Agent UI packs")
        print("  " + "=" * 56)
        for name, src in sorted(packs.items()):
            mark = "*" if name == current else " "
            where = "custom drop-in" if src not in ("builtin", "packaged") else src
            print(f"   {mark} {name:<18} ({where})")
        print("  " + "-" * 56)
        print(f"  selected: {current}   (drop custom packs in {_ui_packs_dir()})")
        print("  swap with:  adk ui set <name>")
        return 0

    if action == "set":
        name = getattr(args, "name", None)
        if not name:
            print("ERROR: usage: adk ui set <name>  (see 'adk ui ls')")
            return 1
        packs = list_ui_packs()
        if name not in packs:
            print(f"ERROR: no UI pack named '{name}'. Available: {', '.join(sorted(packs))}")
            print(f"  (or drop a folder with index.html into {_ui_packs_dir()}/{name}/)")
            return 1
        from adk.config import save_saved_config
        path = save_saved_config({"agent_ui": name})
        print(f"✓ Agent UI pack set to '{name}' (saved to {path}).")
        print("  Reload the agent page (or restart `adk up`) to see it.")
        return 0

    if action == "path":
        current = resolve_ui_pack_name()
        html = load_ui_pack(current)
        print(f"selected pack : {current}")
        print(f"drop-in dir   : {_ui_packs_dir()}")
        print(f"resolves      : {'OK (%d bytes)' % len(html) if html else 'MISSING'}")
        return 0

    print("usage: adk ui [ls | set <name> | path]")
    return 1


async def _stream_to_agent(ref, message: str) -> None:
    """Stream a completion FROM a mesh agent, protocol-aware. The remote agent runs
    on ITS OWN inference backend (that's the whole point)."""
    import sys
    if ref.chat_protocol == "openai":
        # Raw OpenAI-compatible inference server (llama.cpp / ollama / vllm).
        from adk.llm.base import Message
        from adk.llm.openai_compat import OpenAIProvider
        base = ref.invoke_url.rstrip("/")
        if not base.endswith("/v1"):
            base = base + "/v1"
        prov = OpenAIProvider(base_url=base, default_model=ref.model or "")
        got = False
        async for chunk in prov.chat_stream([Message(role="user", content=message)],
                                            max_tokens=1024):
            text = getattr(chunk, "content", "") or ""
            if text:
                got = True
                sys.stdout.write(text)
                sys.stdout.flush()
        if not got:
            # Some reasoning models (e.g. Bonsai) emit only reasoning_content on short
            # replies; fall back to a non-streaming completion so the user sees output.
            resp = await prov.chat([Message(role="user", content=message)], max_tokens=1024)
            sys.stdout.write(getattr(resp, "content", "") or "(no content)")
        print()
    else:
        # A full adk agent server — its own /chat/stream, its own backend.
        from adk.shell.genesis_client import GenesisClient
        client = GenesisClient(base_url=ref.invoke_url)
        async for chunk in client.chat_stream(message):
            sys.stdout.write(chunk)
            sys.stdout.flush()
        print()


def cmd_agents(args) -> int:
    """Discover + list every agent in the mesh (name, where, what inference)."""
    import asyncio
    from adk.mesh_discovery import discover_agents
    fmt = getattr(args, "format", "table")
    agents, warnings = asyncio.run(discover_agents())
    if fmt == "json":
        import json
        print(json.dumps({"agents": [a.to_dict() for a in agents],
                          "warnings": warnings}, indent=2))
        return 0
    print("\n  Mesh Agents")
    print("  " + "=" * 80)
    if not agents:
        print("  (none discovered)")
    else:
        print(f"  {'NAME':<22}{'REACH':<9}{'MODEL':<24}{'BACKEND':<11}{'STATUS'}")
        print("  " + "-" * 80)
        for a in agents:
            print(f"  {a.name[:21]:<22}{a.reach[:8]:<9}"
                  f"{(a.model or '-')[:23]:<24}{(a.provider_hint or '-')[:10]:<11}{a.status}")
    for w in warnings:
        print(f"  ! {w}")
    print("  " + "-" * 80)
    print("  chat:   adk chat <name> \"message\"")
    print("  invoke: adk invoke <name> <skill> [--arg k=v]")
    print("  add an endpoint: ~/.aither/agents.json (name, invoke_url, model, provider_hint)")
    return 0


def cmd_agent(args) -> int:
    """Run and manage host-tier agent loops."""
    import subprocess
    import json
    from pathlib import Path

    agent_cmd = getattr(args, "agent_command", None)

    if agent_cmd == "run":
        agent_name = getattr(args, "name", None)
        if not agent_name:
            print("usage: adk agent run <name>")
            return 1

        daemon_url = getattr(args, "daemon_url", "http://127.0.0.1:8362")
        token = getattr(args, "token", "") or os.environ.get("AITHER_HARNESS_TOKEN", "")
        room = getattr(args, "room", "main")
        interval = getattr(args, "interval", 5.0)
        foreground = getattr(args, "foreground", False)

        # Build the command
        argv = [
            sys.executable, "-m", "adk.agent_loop_cmd",
            agent_name,
            "--daemon-url", daemon_url,
            "--room", room,
            "--interval", str(interval),
        ]
        if token:
            argv.extend(["--token", token])

        if foreground:
            # Run in foreground
            env = dict(os.environ)
            return subprocess.call(argv, env=env)
        else:
            # Detach as a background process
            from adk.agent_daemon import spawn_detached
            log_path = Path.home() / ".aither" / "logs" / f"agent-{agent_name}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            pid = spawn_detached(argv, log_path)
            print(f"[+] Agent '{agent_name}' started (pid={pid})")
            print(f"    Log: {log_path}")
            print(f"    Check status: adk agent status {agent_name}")
            return 0

    elif agent_cmd == "list":
        aither_home = Path.home() / ".aither"
        status_path = aither_home / "adk-up.json"

        if not status_path.exists():
            print("No agents running.")
            return 0

        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
            agents = data.get("agents", {})
            if not agents:
                print("No agents running.")
                return 0

            print("\n  Running Agent Loops")
            print("  " + "=" * 80)
            print(f"  {'NAME':<20}{'PID':<10}{'STATUS':<15}{'ROOM':<10}{'STARTED'}")
            print("  " + "-" * 80)

            import time
            for name, agent_info in agents.items():
                pid = agent_info.get("pid")
                room = agent_info.get("room", "?")
                started_at = agent_info.get("started_at", 0)
                started_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_at))

                # Check if process is alive
                from adk.agent_daemon import pid_alive
                alive = pid_alive(pid)
                status = "alive" if alive else "dead"

                print(f"  {name:<20}{pid:<10}{status:<15}{room:<10}{started_str}")

            print("  " + "-" * 80)
            return 0
        except Exception as exc:
            print(f"Error reading status: {exc}", file=sys.stderr)
            return 1

    elif agent_cmd == "status":
        agent_name = getattr(args, "name", None)
        if not agent_name:
            print("usage: adk agent status <name>")
            return 1

        aither_home = Path.home() / ".aither"
        status_path = aither_home / "adk-up.json"

        if not status_path.exists():
            print(f"Agent '{agent_name}' is not running.")
            return 1

        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
            agents = data.get("agents", {})
            agent_info = agents.get(agent_name)

            if not agent_info:
                print(f"Agent '{agent_name}' is not running.")
                return 1

            pid = agent_info.get("pid")
            from adk.agent_daemon import pid_alive
            alive = pid_alive(pid)

            print(f"\n  Agent: {agent_name}")
            print(f"  Status: {'alive' if alive else 'dead'}")
            print(f"  PID: {pid}")
            print(f"  Room: {agent_info.get('room', '?')}")
            print(f"  Session: {agent_info.get('session_id', '?')}")
            print(f"  Actor ID: {agent_info.get('actor_id', '?')}")

            import time
            started_at = agent_info.get("started_at", 0)
            started_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_at))
            print(f"  Started: {started_str}")

            if alive:
                print(f"\n  → See status file: {status_path}")
                print(f"  → Kill: adk agent stop {agent_name}")
            print()
            return 0
        except Exception as exc:
            print(f"Error reading status: {exc}", file=sys.stderr)
            return 1

    elif agent_cmd == "stop":
        agent_name = getattr(args, "name", None)
        if not agent_name:
            print("usage: adk agent stop <name>")
            return 1

        aither_home = Path.home() / ".aither"
        status_path = aither_home / "adk-up.json"

        if not status_path.exists():
            print(f"Agent '{agent_name}' is not running.")
            return 1

        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
            agents = data.get("agents", {})
            agent_info = agents.get(agent_name)

            if not agent_info:
                print(f"Agent '{agent_name}' is not running.")
                return 1

            pid = agent_info.get("pid")
            from adk.agent_daemon import pid_alive, kill_pid

            if not pid_alive(pid):
                print(f"Agent '{agent_name}' (pid={pid}) is already dead.")
                # Clean up status
                del agents[agent_name]
                status_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                return 0

            print(f"Stopping agent '{agent_name}' (pid={pid})...")
            if kill_pid(pid):
                print("[+] Agent stopped.")
                # Clean up status
                del agents[agent_name]
                status_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                return 0
            else:
                print("[-] Failed to stop agent.", file=sys.stderr)
                return 1
        except Exception as exc:
            print(f"Error stopping agent: {exc}", file=sys.stderr)
            return 1

    else:
        print("usage: adk agent [run|list|status|stop]")
        print()
        print("  run <name>      Start a host-tier agent loop")
        print("  list            List running agent loops")
        print("  status <name>   Show status of an agent")
        print("  stop <name>     Stop a running agent")
        return 1


def cmd_chat(args) -> int:
    """Chat with a mesh agent BY NAME — it responds on its own inference backend."""
    import asyncio
    from adk.mesh_discovery import resolve_agent
    name = getattr(args, "agent", None)
    message = getattr(args, "message", None)
    if not name:
        print("usage: adk chat <agent-name> [message]   (see `adk agents ls`)")
        return 1

    async def _run() -> int:
        ref = await resolve_agent(name)
        if ref is None:
            print(f"ERROR: no agent named '{name}' in the mesh. Try `adk agents ls`.")
            return 1
        if not ref.invoke_url:
            print(f"ERROR: agent '{ref.name}' has no invoke_url — can't reach it.")
            return 1
        tag = ref.model or ref.provider_hint or ref.chat_protocol
        print(f"  → {ref.name}  [{tag} @ {ref.invoke_url}]\n")
        if message:
            await _stream_to_agent(ref, message)
            return 0
        # interactive loop
        while True:
            try:
                msg = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if msg in ("", "exit", "quit", ":q"):
                return 0
            print(f"{ref.name}: ", end="", flush=True)
            await _stream_to_agent(ref, msg)

    return asyncio.run(_run())


def cmd_invoke(args) -> int:
    """Invoke a TOOL (skill) on a mesh agent over signed A2A — command & control.

    The remote agent runs the tool on ITS side and returns the result. The call
    is Ed25519-signed; the remote only runs tools it has opted in via
    `expose_to_a2a=True`, and only if our key is trusted
    (AITHER_A2A_TRUSTED_KEYS / AITHER_A2A_REQUIRE_TRUST on the peer).
    """
    import asyncio
    import json as _json
    from adk.a2a_client import invoke_skill

    name = getattr(args, "agent", None)
    skill = getattr(args, "skill", None)
    if not name or not skill:
        print("usage: adk invoke <agent-name> <skill> [--arg k=v ...]   (see `adk agents ls`)")
        return 1

    # Parse --arg k=v pairs. Values are JSON-parsed when possible (so numbers /
    # bools / lists pass through typed), else kept as strings.
    call_args: dict = {}
    for pair in (getattr(args, "arg", None) or []):
        if "=" not in pair:
            print(f"ERROR: --arg must be k=v, got '{pair}'")
            return 1
        k, v = pair.split("=", 1)
        try:
            call_args[k] = _json.loads(v)
        except Exception:
            call_args[k] = v

    this_agent = getattr(args, "as_agent", None) or os.environ.get(
        "AITHER_AGENT_NAME", "adk-client")

    async def _run() -> int:
        result = await invoke_skill(name, skill, call_args, this_agent_name=this_agent)
        if isinstance(result, dict) and result.get("error"):
            print(f"ERROR ({result.get('error')}): {result.get('message', result)}")
            if result.get("code") is not None:
                print(f"  rpc code: {result['code']}")
            return 1
        output = result.get("output") if isinstance(result, dict) else result
        print(_json.dumps(output, indent=2, default=str))
        return 0

    return asyncio.run(_run())


def cmd_aeon(args):
    """Interactive multi-agent group chat."""
    import asyncio

    async def _aeon():
        from adk.aeon import AeonSession

        preset = args.preset or "balanced"
        custom_agents = args.agents.split(",") if args.agents else None
        rounds = args.rounds or 1
        synthesize = not args.no_synthesize

        participants = custom_agents
        if custom_agents:
            # Ensure orchestrator is present
            if "aither" not in custom_agents:
                custom_agents.append("aither")

        session = AeonSession(
            participants=participants,
            preset=preset,
            rounds=rounds,
            synthesize=synthesize,
        )

        # ANSI colors for agent names
        colors = [
            "\033[96m",   # cyan
            "\033[93m",   # yellow
            "\033[95m",   # magenta
            "\033[92m",   # green
            "\033[94m",   # blue
            "\033[91m",   # red
        ]
        reset = "\033[0m"
        bold = "\033[1m"

        agent_colors = {}
        for i, name in enumerate(session.participants):
            agent_colors[name] = colors[i % len(colors)]

        names = ", ".join(session.participants)
        print(f"\n  Aeon Group Chat — [{preset}] {names}")
        print(f"  Session: {session.session_id}")
        print(f"  Rounds: {rounds} | Synthesize: {synthesize}")
        print("  Type 'quit' to exit, 'reset' to start a new session.\n")

        while True:
            try:
                user_input = input(f"  {bold}you>{reset} ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n  Bye!")
                break

            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit"):
                print("  Bye!")
                break
            if user_input.lower() == "reset":
                session.reset()
                # Re-assign colors
                for i, name in enumerate(session.participants):
                    agent_colors[name] = colors[i % len(colors)]
                print(f"  New session: {session.session_id}\n")
                continue

            response = await session.chat(user_input)

            print()
            for msg in response.messages:
                color = agent_colors.get(msg.agent, "")
                content = _strip_think_tags(msg.content)
                print(f"  {color}[{msg.agent}]{reset} {content}")
                print()

            if response.synthesis:
                color = agent_colors.get(response.synthesis.agent, colors[0])
                content = _strip_think_tags(response.synthesis.content)
                print(f"  {color}{bold}[{response.synthesis.agent} - synthesis]{reset} {content}")
                print()

            print(f"  --- round {response.round_number} | {response.total_tokens} tokens | {response.total_latency_ms:.0f}ms ---\n")

        return 0

    return asyncio.run(_aeon())


def cmd_deploy(args):
    """Package and deploy an agent to AitherOS via the gateway."""
    import asyncio
    import json as _json
    import zipfile
    import tempfile

    async def _deploy():
        project_dir = Path(args.directory or ".").resolve()
        print(f"📦 Deploying agent from {project_dir}\n")

        # Validate project
        agent_file = project_dir / "agent.py"
        config_file = project_dir / "config.yaml"
        if not agent_file.exists():
            print("❌ No agent.py found. Run 'aither init' first.")
            return 1

        # Get API key
        api_key = args.api_key or os.environ.get("AITHER_API_KEY", "")
        if not api_key:
            # Try saved config
            config_path = Path.home() / ".aither" / "config.json"
            if config_path.exists():
                try:
                    saved = _json.loads(config_path.read_text())
                    api_key = saved.get("api_key", "")
                except Exception:
                    pass
        if not api_key:
            print("❌ No API key. Run 'aither connect --api-key <key>' first.")
            return 1

        # Read agent name from config or args
        agent_name = args.name
        if not agent_name and config_file.exists():
            try:
                import yaml
                cfg = yaml.safe_load(config_file.read_text())
                agent_name = cfg.get("identity", "my-agent")
            except Exception:
                agent_name = project_dir.name

        if not agent_name:
            agent_name = project_dir.name

        print(f"  Agent: {agent_name}")

        # Package the project into a zip
        print("  📁 Packaging project...")
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = tmp.name
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in project_dir.rglob("*"):
                if f.is_file() and not any(
                    part.startswith(".") or part == "__pycache__"
                    for part in f.relative_to(project_dir).parts
                ):
                    zf.write(f, f.relative_to(project_dir))

        zip_size = os.path.getsize(tmp_path)
        print(f"  📦 Package size: {zip_size / 1024:.1f} KB")

        # Register agent with gateway
        print("  🚀 Registering with gateway...")
        try:
            import httpx
            gateway = args.gateway or "https://gateway.aitherium.com"
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Register agent metadata
                resp = await client.post(
                    f"{gateway}/v1/agents/register",
                    json={
                        "agent_name": agent_name,
                        "capabilities": args.capabilities.split(",") if args.capabilities else ["chat"],
                        "description": args.description or f"ADK agent: {agent_name}",
                        "version": args.version or "0.1.0",
                    },
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
                if resp.status_code in (200, 201):
                    data = resp.json()
                    agent_id = data.get("agent_id", "unknown")
                    print(f"  ✅ Registered: {agent_id}")
                else:
                    error = resp.json() if resp.headers.get(
                        "content-type", ""
                    ).startswith("application/json") else {"error": resp.text}
                    print(f"  ❌ Registration failed: {error}")
                    return 1

                # Upload package (deploy endpoint)
                print("  📤 Uploading package...")
                with open(tmp_path, "rb") as zf:
                    resp = await client.post(
                        f"{gateway}/v1/agents/{agent_id}/deploy",
                        content=zf.read(),
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/zip",
                            "X-Agent-Name": agent_name,
                        },
                    )
                    if resp.status_code in (200, 201):
                        print("  ✅ Deployed successfully!")
                    elif resp.status_code == 404:
                        print("  ⚠️  Deploy endpoint not yet available on gateway.")
                        print("     Agent registered but code deployment coming soon.")
                    else:
                        print(f"  ⚠️  Deploy returned {resp.status_code}: {resp.text[:200]}")

        except Exception as e:
            print(f"  ❌ Error: {e}")
            return 1
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        print(f"\n✅ Agent '{agent_name}' deployed to AitherOS!")
        return 0

    return asyncio.run(_deploy())


def _onboard_agent(agent_name: str, tenant_slug: str, args) -> int:
    """Register a running agent with the portal fleet.

    Usage: adk onboard --agent myapp --tenant customer-acme

    Steps:
        1. Detect running agent on localhost (check /health)
        2. Read agent identity from ~/.aither/agents.json
        3. Register with portal via FederationLiteClient.upsert_agents()
        4. Configure inference (if not already done)
        5. Print fleet dashboard URL
    """
    import asyncio

    async def _do_onboard():
        from adk.config import load_saved_config
        saved = load_saved_config()
        api_key = getattr(args, 'api_key', None) or saved.get("api_key", "") or os.environ.get("AITHER_API_KEY", "")

        if not tenant_slug:
            ts = saved.get("tenant_id", "") or saved.get("tenant_slug", "")
        else:
            ts = tenant_slug

        if not api_key:
            print("  No API key. Run 'adk login' first.")
            return 1

        print()
        print(f"  Onboarding agent: {agent_name}")
        print(f"  Tenant: {ts or '(not set)'}")
        print()

        # 1. Check if agent is running
        agent_url = "http://localhost:8080"
        from adk.agent_registry import get_local_agent
        local_entry = get_local_agent(agent_name)
        if local_entry:
            agent_url = local_entry.get("url", agent_url)
            print(f"  [OK] Found in local registry: {agent_url}")
        else:
            print("  [..] Not in local registry, checking localhost:8080...")

        import httpx
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{agent_url}/api/health")
                if resp.status_code < 300:
                    print(f"  [OK] Agent healthy at {agent_url}")
                else:
                    print(f"  [!!] Agent returned {resp.status_code}")
        except Exception:
            print(f"  [!!] Agent not reachable at {agent_url}")
            print(f"       Start it first: adk run --identity {agent_name}")
            return 1

        # 2. Read instance ID
        instance_id = ""
        if local_entry:
            instance_id = local_entry.get("instance_id", "")
        if not instance_id:
            home = Path.home()
            iid_file = home / ".aither" / "agents" / agent_name / ".aither" / "instance_id"
            if iid_file.exists():
                instance_id = iid_file.read_text(encoding="utf-8").strip()

        # 3. Register with portal
        portal_url = saved.get("portal_url", "") or os.environ.get(
            "AITHER_PORTAL_URL", "https://portal.aitherium.com"
        )
        invoke_url = os.environ.get("AITHER_INVOKE_URL", agent_url)

        try:
            from adk.federation_lite import FederationLiteClient
            fed = FederationLiteClient(
                hub_url=portal_url,
                api_key=api_key,
                node_id=instance_id or agent_name,
            )
            result = await fed.upsert_agents([{
                "name": agent_name,
                "invoke_url": invoke_url,
                "status": "online",
                "tenant_id": ts,
                "instance_id": instance_id,
            }])
            if result.get("error"):
                print(f"  [!!] Portal registration failed: {result}")
            else:
                print("  [OK] Registered with portal fleet")
        except Exception:
            # Fallback: direct HTTP
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.post(
                        f"{portal_url}/v1/agents/upsert",
                        json={
                            "name": agent_name,
                            "scope": {"visibility": "workspace", "tenant_id": ts},
                            "invoke_url": invoke_url,
                            "instance_id": instance_id,
                            "status": "online",
                        },
                        headers={"Authorization": f"Bearer {api_key}"},
                    )
                print("  [OK] Registered with portal fleet (direct)")
            except Exception as e2:
                print(f"  [!!] Portal registration failed: {e2}")

        # 4. Print fleet URL
        print()
        print("  Fleet dashboard: https://portal.aitherium.com/portal/fleet")
        if instance_id:
            print(f"  Instance ID:     {instance_id}")
        print()
        return 0

    return asyncio.run(_do_onboard())


def _onboard_webgpu(args) -> int:
    """Self-bootstrap onto in-browser (WebGPU) inference — no server model.

    The local GUI runs the model entirely in the visitor's browser on their GPU
    (zero server compute, no API cost, private). This sets the on-device-capable UI
    pack as the default and records the choice, then points the user at `adk up`.
    """
    saved_path = None
    try:
        saved_path = save_saved_config({"agent_ui": "llamacpp", "inference_mode": "webgpu"})
    except Exception as e:  # noqa: BLE001 — config write is best-effort; still print guidance
        print(f"  [!] could not persist config ({e}); the GUI still defaults to on-device.")

    print()
    print("  ON-DEVICE (WebGPU) SETUP")
    print("  ────────────────────────")
    print()
    print("  This agent runs inference IN THE BROWSER on the user's GPU — no server")
    print("  model, no API cost, and nothing leaves the machine.")
    print()
    print("  - Default model: Gemma-4-E2B (small QAT model, ~900 MB, cached after first load)")
    print("  - Needs a WebGPU browser (Chrome or Edge).")
    if saved_path:
        print(f"  - Saved: agent_ui=llamacpp, inference_mode=webgpu  ({saved_path})")
    print()
    print("  NEXT:")
    print("    adk up        # open the console; the '(WebGPU) On-device' backend is the default")
    print()
    print("  (Want a server model instead? run:  adk onboard --quick)")
    return 0


def _onboard_quick(args) -> int:
    """One-command onboarding chain: inference -> pack -> enroll.

    Runs synchronously at top level. Each reused cmd_* helper manages its own
    event loop, so this must NOT be called from within a running loop.
    """
    import json as _json
    from pathlib import Path

    # Resolve api_key the same way the interactive scan does.
    api_key = getattr(args, "api_key", "") or os.environ.get("AITHER_API_KEY", "")
    if not api_key:
        config_path = Path.home() / ".aither" / "config.json"
        if config_path.exists():
            try:
                cfg = _json.loads(config_path.read_text(encoding="utf-8"))
                api_key = cfg.get("api_key", "")
            except Exception:
                pass

    pack_name = getattr(args, "pack", "openclaw") or "openclaw"

    print()
    print("  QUICK SETUP")
    print("  ───────────")
    print()

    # Step 1: inference (skip if a cloud key is already configured)
    if not api_key:
        print("  [1/3] Setting up local inference backend...")

        class QuickstartArgs:
            backend = "auto"
            port = 8209
            dry_run = False
            model = None
            api_key = ""

        if cmd_quickstart_local(QuickstartArgs()) != 0:
            print()
            print("  ERROR: Inference setup failed")
            print("  You can retry with: adk quickstart-local")
            return 1
    else:
        print("  [1/3] Cloud API key already configured, skipping local inference")

    # Step 2: install the default pack (non-fatal on failure)
    print()
    print("  [2/3] Installing agent pack...")

    class InstallArgs:
        target = f"pack:{pack_name}"

    if cmd_install(InstallArgs()) != 0:
        print()
        print("  WARNING: Pack installation failed (continuing anyway)")
        print(f"  You can retry with: adk install pack:{pack_name}")

    # Step 3: enroll (best-effort — never blocks onboarding)
    print()
    print("  [3/3] Enrolling with control plane...")

    class EnrollArgs:
        portal = None
        genesis = None
        no_heartbeat = False
        force = False

    enrollment_success = False
    try:
        if cmd_enroll(EnrollArgs()) != 0:
            print()
            print("  WARNING: Enrollment failed (optional, you can retry later)")
            print("  You can retry with: adk enroll --force")
        else:
            enrollment_success = True
    except Exception as e:
        print()
        print(f"  WARNING: Enrollment error: {e} (optional, continuing)")

    # Step 4: Start MicroScheduler heartbeat (so personal agent appears in Fleet)
    if enrollment_success:
        try:
            from adk.microscheduler_heartbeat import start_heartbeat_threaded

            agent_id = start_heartbeat_threaded()

            print()
            print("  [4/3] Starting agent fleet registration...")
            print(f"      Agent ID: {agent_id}")
            print("      Status: Registering with fleet (visible in Portal → Fleet → Live)")
        except Exception as e:
            print()
            print(f"  NOTE: Fleet registration skipped: {e}")

    print()
    print("=" * 60)
    print("  Onboarding Complete!")
    print("=" * 60)
    print()
    print(f"  Pack installed: {pack_name}")
    print("  Next steps:")
    print(f"    adk run --agents {pack_name}      Run this agent")
    print(f"    adk serve --agents {pack_name}    Serve as web service")
    print()
    return 0


def cmd_onboard(args):
    """Interactive onboarding — detect products, configure, integrate."""
    import asyncio
    import json as _json

    agent_name = getattr(args, 'agent', None) or ''
    tenant_slug = getattr(args, 'tenant', None) or os.environ.get('AITHER_TENANT_SLUG', '')

    # ── Agent fleet registration mode ─────────────────────────────────
    if agent_name:
        return _onboard_agent(agent_name, tenant_slug, args)

    # ── Quick mode: one-command chain ─────────────────────────────────
    # Runs at TOP LEVEL, not inside the async _onboard() below: the reused
    # cmd_* helpers call asyncio.run() internally (e.g. cmd_enroll), which
    # would raise "cannot be called from a running event loop" if nested.
    if getattr(args, 'quick', False):
        return _onboard_quick(args)

    # ── WebGPU mode: self-bootstrap onto in-browser inference (no server model) ──
    if getattr(args, 'webgpu', False):
        return _onboard_webgpu(args)

    # ── Discord mode: automated onboarding — deploy the agent as a Discord bot ──
    if getattr(args, 'discord', False):
        from adk.onboard_discord import onboard_discord
        return onboard_discord(args)

    async def _onboard():
        # Inline ProductDetector (no AitherOS lib dependency)
        from pathlib import Path
        import shutil

        home = Path.home()
        aither_dir = home / ".aither"
        openclaw_dir = home / ".openclaw"

        # If tenant provided, write it to config immediately
        if tenant_slug:
            aither_dir.mkdir(parents=True, exist_ok=True)
            config_path = aither_dir / "config.json"
            existing = {}
            if config_path.exists():
                try:
                    existing = _json.loads(config_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            existing["tenant_slug"] = tenant_slug
            existing["tenant_id"] = f"tnt_{tenant_slug.replace('-', '_')}"
            config_path.write_text(_json.dumps(existing, indent=2), encoding="utf-8")

        print()
        print("  AitherOS Onboarding")
        print("  ===================")
        print()

        # ── 1. Detect products ────────────────────────────────
        print("  SCANNING ENVIRONMENT")
        print("  ────────────────────")

        products = []

        # ADK
        aither_bin = shutil.which("aither")
        if aither_bin:
            products.append("awdk")
            print("  [OK] AitherADK — installed")
        else:
            print("  [--] AitherADK — not found (you're running it though!)")

        # Config
        config = {}
        config_path = aither_dir / "config.json"
        if config_path.exists():
            try:
                config = _json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        api_key = args.api_key or os.environ.get("AITHER_API_KEY", "")
        if not api_key:
            api_key = config.get("api_key", "")

        if api_key:
            print(f"  [OK] API key: {api_key[:16]}...")
        else:
            print("  [--] No API key — run 'aither register' for cloud access")

        # Ollama
        ollama_bin = shutil.which("ollama")
        if ollama_bin:
            products.append("ollama")
            print("  [OK] Ollama — installed")

        # vLLM (check via import or docker)
        try:
            import importlib.util
            if importlib.util.find_spec("vllm"):
                products.append("vllm")
                print("  [OK] vLLM — installed (Python)")
        except (ImportError, ModuleNotFoundError):
            pass

        # OpenClaw
        openclaw_detected = openclaw_dir.exists()
        if openclaw_detected:
            products.append("openclaw")
            oc_config = {}
            oc_config_path = openclaw_dir / "openclaw.json"
            if oc_config_path.exists():
                try:
                    oc_config = _json.loads(oc_config_path.read_text(encoding="utf-8"))
                except Exception:
                    pass

            version = oc_config.get("version", "unknown")
            aither_integrated = any(
                "aither" in k.lower()
                for k in oc_config.get("mcpServers", {})
            )

            if aither_integrated:
                print(f"  [OK] OpenClaw v{version} — integrated with AitherOS")
            else:
                print(f"  [!!] OpenClaw v{version} — detected but NOT integrated")
                print("       Run 'adk integrate openclaw' to connect agent fleets")

        # GPU
        gpu_name = ""
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                lines = [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
                if len(lines) > 1:
                    # Multi-GPU: show all, highlight best
                    best_vram = 0
                    total_vram = 0
                    for i, line in enumerate(lines):
                        parts = [p.strip() for p in line.split(",")]
                        g_name = parts[0] if parts else "GPU"
                        g_vram = float(parts[1]) / 1024 if len(parts) > 1 else 0
                        total_vram += g_vram
                        if g_vram > best_vram:
                            best_vram = g_vram
                            gpu_name = g_name
                        print(f"  [OK] GPU {i}: {g_name} ({g_vram:.0f}GB VRAM)")
                    print(f"  [OK] Total VRAM: {total_vram:.0f}GB across {len(lines)} GPUs")
                else:
                    parts = [p.strip() for p in lines[0].split(",")]
                    gpu_name = parts[0].strip()
                    vram = float(parts[1].strip()) / 1024 if len(parts) > 1 else 0
                    print(f"  [OK] GPU: {gpu_name} ({vram:.0f}GB VRAM)")
        except Exception:
            print("  [--] No NVIDIA GPU detected")

        # ── 2. Onboarding plan ────────────────────────────────
        print()
        print("  ONBOARDING PLAN")
        print("  ───────────────")

        step_num = 1

        if not api_key:
            print(f"  {step_num}. Register for Aitherium (free)")
            print("     -> aither register")
            step_num += 1

        if not ollama_bin and not gpu_name:
            print(f"  {step_num}. Set up inference backend")
            print("     -> Install Ollama: https://ollama.com")
            print("     -> Or use cloud: aither register")
            step_num += 1

        if openclaw_detected and not aither_integrated:
            print(f"  {step_num}. Connect OpenClaw to AitherOS agent fleet")
            print("     -> adk integrate openclaw")
            step_num += 1

        print(f"  {step_num}. Create your first agent")
        print("     -> aither init my-agent && cd my-agent && aither run")
        step_num += 1

        if api_key:
            print(f"  {step_num}. Publish to Elysium marketplace (optional)")
            print("     -> aither publish")
            step_num += 1

        # ── 3. Auto-configure IDE MCP servers ────────────────
        print()
        print("  CONFIGURING MCP SERVERS")
        print("  ���─────────────────────")

        mcp_url = "http://localhost:8080"
        mcp_configured = []

        # Claude Code — .mcp.json goes in PROJECT ROOT (CWD), not ~/.claude/
        # Claude Code reads MCP config from the working directory, not global.
        claude_dir = home / ".claude"
        mcp_json = {
            "mcpServers": {
                "aitheros": {
                    "command": "npx",
                    "args": ["-y", "aither-mcp-server"],
                    "disabled": False,
                }
            }
        }

        def _write_mcp(target: Path, label: str):
            """Write or merge MCP config into a .mcp.json file."""
            try:
                if target.exists():
                    existing = _json.loads(target.read_text(encoding="utf-8"))
                    servers = existing.get("mcpServers", {})
                    if "aitheros" not in servers:
                        servers["aitheros"] = mcp_json["mcpServers"]["aitheros"]
                        existing["mcpServers"] = servers
                        target.write_text(_json.dumps(existing, indent=2), encoding="utf-8")
                        print(f"  [OK] {label} — AitherOS MCP added to existing config")
                        return True
                    else:
                        print(f"  [OK] {label} — AitherOS MCP already configured")
                        return True
                else:
                    target.write_text(_json.dumps(mcp_json, indent=2), encoding="utf-8")
                    print(f"  [OK] {label} — MCP configured at {target}")
                    return True
            except Exception as e:
                print(f"  [!!] {label} — failed: {e}")
                return False

        # 1. Write to current project directory (primary — Claude Code reads from CWD)
        cwd_mcp = Path.cwd() / ".mcp.json"
        if _write_mcp(cwd_mcp, "Claude Code (project)"):
            mcp_configured.append("claude-code")

        # 2. Also write to ~/.claude/.mcp.json as global fallback
        if claude_dir.exists():
            _write_mcp(claude_dir / ".mcp.json", "Claude Code (global)")
        else:
            print("  [--] Claude Code global — ~/.claude/ not found (project-level config is sufficient)")

        # Cursor — write to ~/.cursor/mcp.json
        cursor_dir = home / ".cursor"
        if cursor_dir.exists():
            cursor_mcp = cursor_dir / "mcp.json"
            cursor_config = {
                "mcpServers": {
                    "aitheros": {
                        "url": f"{mcp_url}/sse",
                    }
                }
            }
            try:
                if cursor_mcp.exists():
                    existing = _json.loads(cursor_mcp.read_text(encoding="utf-8"))
                    if "aitheros" not in existing.get("mcpServers", {}):
                        existing.setdefault("mcpServers", {})["aitheros"] = cursor_config["mcpServers"]["aitheros"]
                        cursor_mcp.write_text(_json.dumps(existing, indent=2), encoding="utf-8")
                        print("  [OK] Cursor — AitherOS MCP added")
                        mcp_configured.append("cursor")
                    else:
                        print("  [OK] Cursor — AitherOS MCP already configured")
                        mcp_configured.append("cursor")
                else:
                    cursor_mcp.write_text(_json.dumps(cursor_config, indent=2), encoding="utf-8")
                    print(f"  [OK] Cursor — MCP configured at {cursor_mcp}")
                    mcp_configured.append("cursor")
            except Exception as e:
                print(f"  [!!] Cursor — failed to write config: {e}")
        else:
            print("  [--] Cursor — not detected (~/.cursor/ not found)")

        # OpenClaw — use adk integrate openclaw
        if openclaw_detected and not aither_integrated:
            try:
                oc_config_path = openclaw_dir / "openclaw.json"
                if oc_config_path.exists():
                    oc_config = _json.loads(oc_config_path.read_text(encoding="utf-8"))
                    oc_config.setdefault("mcpServers", {})["aither_mcp_configured"] = {
                        "command": "npx",
                        "args": ["-y", "aither-mcp-server"],
                        "disabled": False,
                    }
                    oc_config_path.write_text(_json.dumps(oc_config, indent=2), encoding="utf-8")
                    print("  [OK] OpenClaw — AitherOS MCP added")
                    mcp_configured.append("openclaw")
            except Exception as e:
                print(f"  [!!] OpenClaw — failed to integrate: {e}")
        elif openclaw_detected and aither_integrated:
            print("  [OK] OpenClaw — already integrated")
            mcp_configured.append("openclaw")

        # VS Code — write to .vscode/mcp.json in current dir
        vscode_dir = Path.cwd() / ".vscode"
        if vscode_dir.exists():
            vscode_mcp = vscode_dir / "mcp.json"
            if not vscode_mcp.exists():
                try:
                    vscode_mcp.write_text(_json.dumps({
                        "servers": {
                            "aitheros": {"url": f"{mcp_url}/sse"}
                        }
                    }, indent=2), encoding="utf-8")
                    print("  [OK] VS Code — MCP configured in .vscode/mcp.json")
                    mcp_configured.append("vscode")
                except Exception:
                    pass

        if not mcp_configured:
            print("  [--] No IDE detected — configure manually:")
            print(f"       MCP server URL: {mcp_url}/sse")

        # ── 4. Quick actions ──────────────────────────────────
        print()
        print("  QUICK ACTIONS")
        print("  ─────────────")
        print("  aither register        — Create account + get API key")
        print("  aither connect         — Detect backends + test cloud")
        print("  aither init <name>     — Scaffold new agent project")
        print("  adk integrate       — Connect external tools (OpenClaw, etc.)")
        print("  aither publish         — Submit agent to Elysium marketplace")
        print("  aither aeon            — Multi-agent group chat")
        print()

        # ── 4. Install other products ─────────────────────────
        print("  INSTALL OTHER PRODUCTS")
        print("  ──────────────────────")
        print("  pip install awdk          # CLI + SDK (this package)")
        print("  winget install Aitherium.Desktop # Native desktop app (Windows)")
        print("  brew install --cask aither-desktop  # Desktop (macOS)")
        print("  Chrome Web Store: Awconnect  # Browser extension")
        print()

        if openclaw_detected and not aither_integrated:
            print("  " + "=" * 50)
            print("  OPENCLAW DETECTED — Integration available!")
            print("  Run 'adk integrate openclaw' to connect:")
            print("    - 29 specialized AI agents")
            print("    - 100+ MCP tools (code, memory, search)")
            print("    - Swarm coding (11 agents in parallel)")
            print("    - Memory graph + knowledge base")
            print("  " + "=" * 50)
            print()

        return 0

    return asyncio.run(_onboard())


def cmd_enroll(args) -> int:
    """Register this workstation with the control plane.

    Shows hardware info, available models, and enrollment status.
    Persists node_id to ~/.aither/node_auth.json for future heartbeats.
    """
    import asyncio

    try:
        portal_url = getattr(args, "portal", None) or os.environ.get("AITHER_PORTAL_URL", "https://portal.aitherium.com")
        genesis_url = getattr(args, "genesis", None) or os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001")
        no_heartbeat = getattr(args, "no_heartbeat", False)
        force = getattr(args, "force", False)

        from adk.fleet_enroll import enroll_on_boot, _load_node_auth
        from adk.enrollment import build_registration

        # FAIL CLOSED ON IDENTITY. `_extract_tenant_slug()` (fleet_enroll.py:217) falls back
        # to "personal" when ~/.aither/auth.json is absent, so an unauthenticated `adk enroll`
        # SUCCEEDED while binding the device to a tenant that is not yours. The endpoint then
        # never appears in your workspace and nothing says why — the operation reports success
        # and does the wrong thing, which is the fail-open shape this codebase keeps hitting.
        #
        # Onboarding a device is exactly when identity has to be certain. `--api-key` is the
        # non-interactive path, which is what a phone or a headless VM needs.
        from adk.fleet_enroll import _load_auth_config
        _auth = _load_auth_config()
        _has_identity = bool(
            _auth.get("tenant_slug")
            or (_auth.get("user") or {}).get("tenant_slug")
            or _auth.get("api_key")
            or _auth.get("token")
        )
        if not _has_identity and not os.environ.get("AITHER_NODE_TOKEN"):
            print("✗ Not signed in — refusing to enrol.")
            print()
            print("  Without an identity this device registers under the tenant 'personal'")
            print("  instead of your workspace, and it looks like it worked.")
            print()
            print("  Sign in first, then re-run:")
            print("    adk login                     # browser flow")
            print("    adk login --api-key <key>     # headless / phone / VM")
            print(f"    adk enroll --portal {portal_url}")
            print()
            return 1

        # Check if already enrolled
        existing = _load_node_auth()
        if existing.get("node_id") and not force:
            node_id = existing["node_id"]
            print(f"✓ Already enrolled: {node_id}")
            print()
            print("Node details:")
            print(f"  Tenant: {existing.get('tenant_slug', 'unknown')}")
            print(f"  Hub: {existing.get('hub_url', 'unknown')}")
            print(f"  Mode: {existing.get('mode', 'legacy')}")
            print()
            print("To re-enroll, use: adk enroll --force")
            return 0

        # Perform enrollment
        result = asyncio.run(enroll_on_boot(
            genesis_url=genesis_url,
            portal_url=portal_url,
            enable_heartbeat=not no_heartbeat,
        ))

        if not result.get("enrolled"):
            error_detail = result.get("error", "unknown error")
            print(f"ERROR: Enrollment failed: {error_detail}")
            return 1

        # Build registration for display
        node_id = result.get("node_id")
        reg = build_registration(node_id)

        # Format success response
        print("✓ Enrollment successful")
        print()
        print("Node Information:")
        print(f"  ID: {result.get('node_id', 'unknown')}")
        print("  Hardware:")
        print(f"    CPU: {reg.get('cpu_count', 0)} cores")
        print(f"    RAM: {reg.get('ram_mb', 0)} MB")
        if reg.get("gpu_name"):
            print(f"    GPU: {reg['gpu_name']} ({reg.get('gpu_vram_mb', 0)} MB)")
        else:
            print("    GPU: none")
        models_str = ", ".join(reg.get("available_models", []))[:80]
        print(f"  Available Models: {models_str if models_str else 'none'}")
        print()
        print("Fleet Status:")
        print(f"  Workspace ID: {result.get('workspace_id', 'N/A')}")
        print(f"  Agents Upserted: {result.get('agents_upserted', 0)}")
        if not no_heartbeat:
            print("  Heartbeat: enabled (60s interval)")
        print()
        print(f"View in portal: {portal_url.rstrip('/')}/portal/workstation")
        print()
        return 0
    except Exception as e:
        print(f"ERROR: Enrollment failed: {e}")
        return 1


def _find_cloudflared() -> str | None:
    """Locate the cloudflared binary, checking PATH + common install dirs."""
    import shutil
    cf = shutil.which("cloudflared")
    if cf:
        return cf
    candidates = [
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
        "/usr/local/bin/cloudflared",
        "/opt/homebrew/bin/cloudflared",
        os.path.expanduser("~/.cloudflared/cloudflared"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def _cloudflared_install_hint() -> str:
    if sys.platform == "win32":
        return "  winget install --id Cloudflare.cloudflared"
    if sys.platform == "darwin":
        return "  brew install cloudflared"
    return ("  curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/"
            "download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared && "
            "chmod +x /usr/local/bin/cloudflared")


def _cloudflared_asset() -> str | None:
    """Map this platform to the cloudflared GitHub release asset name."""
    import platform as _plat
    sysname = _plat.system()
    mach = _plat.machine().lower()
    if sysname == "Windows":
        if mach in ("x86", "i386", "i686"):
            return "cloudflared-windows-386.exe"
        return "cloudflared-windows-amd64.exe"
    if sysname == "Linux":
        if mach in ("aarch64", "arm64"):
            return "cloudflared-linux-arm64"
        if mach in ("armv7l", "armv6l", "arm"):
            return "cloudflared-linux-arm"
        return "cloudflared-linux-amd64"
    if sysname == "Darwin":
        if mach in ("arm64", "aarch64"):
            return "cloudflared-darwin-arm64.tgz"
        return "cloudflared-darwin-amd64.tgz"
    return None


def _parse_cloudflared_checksums(body: str) -> dict:
    """Parse 'cloudflared-...: <sha256>' pairs from a cloudflared release body.

    cloudflared publishes checksums as colon-separated lines inside a markdown code
    block (NOT the space-separated ``checksums.sha256`` format the shell binary uses),
    so this is deliberately its own parser.
    """
    out: dict[str, str] = {}
    hexset = set("0123456789abcdefABCDEF")
    for line in (body or "").splitlines():
        s = line.strip().strip("`").strip()
        if ":" not in s:
            continue
        name, _, digest = s.partition(":")
        name = name.strip()
        digest = digest.strip()
        if name.startswith("cloudflared-") and len(digest) == 64 and set(digest) <= hexset:
            out[name] = digest.lower()
    return out


def _ensure_cloudflared() -> str | None:
    """Return a path to cloudflared, downloading it to ~/.aither/bin if missing.

    Secure-by-default: verifies the published SHA256 and refuses to install without
    one unless AITHER_CLOUDFLARED_REQUIRE_CHECKSUM=0. Returns None on any failure.
    """
    existing = _find_cloudflared()
    if existing:
        return existing

    import hashlib
    import shutil as _shutil
    import stat as _stat

    import httpx

    asset = _cloudflared_asset()
    if not asset:
        import platform as _plat
        print(f"  Unsupported platform for cloudflared auto-download: "
              f"{_plat.system()} {_plat.machine()}")
        return None

    cache = Path.home() / ".aither" / "bin"
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")
    require_ck = os.environ.get(
        "AITHER_CLOUDFLARED_REQUIRE_CHECKSUM", "1").lower() not in ("0", "false", "no")
    tmp = cache / (asset + ".part")

    try:
        rel = httpx.get(
            "https://api.github.com/repos/cloudflare/cloudflared/releases/latest",
            follow_redirects=True, timeout=30).json()
        assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
        url = assets.get(asset)
        if not url:
            print(f"  cloudflared asset '{asset}' not in the latest release.")
            return None
        checksums = _parse_cloudflared_checksums(rel.get("body", ""))

        with httpx.stream("GET", url, follow_redirects=True, timeout=180) as dl:
            dl.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in dl.iter_bytes(65536):
                    f.write(chunk)

        expected = checksums.get(asset)
        if expected:
            h = hashlib.sha256()
            with open(tmp, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            if h.hexdigest().lower() != expected:
                tmp.unlink(missing_ok=True)
                print("  cloudflared checksum verification FAILED — not installing.")
                return None
        elif require_ck:
            tmp.unlink(missing_ok=True)
            print("  No published checksum for cloudflared — refusing to install "
                  "(set AITHER_CLOUDFLARED_REQUIRE_CHECKSUM=0 to override).")
            return None

        if asset.endswith(".tgz"):
            import tarfile
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                with tarfile.open(tmp, "r:gz") as tf:
                    tf.extractall(td)  # noqa: S202 — trusted cloudflare release asset
                found = list(Path(td).rglob("cloudflared"))
                if not found:
                    tmp.unlink(missing_ok=True)
                    print("  cloudflared archive did not contain the binary.")
                    return None
                _shutil.copy2(found[0], dest)
            tmp.unlink(missing_ok=True)
        else:
            tmp.replace(dest)

        if sys.platform != "win32":
            dest.chmod(dest.stat().st_mode | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)
        return str(dest)
    except (httpx.HTTPError, OSError, KeyError, ValueError) as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"  cloudflared download failed: {e}")
        return None


def cmd_host(args):
    """One command to host a self-hosted agent and connect it to your fleet.

    Prompts for your model key (stored locally in ~/.aither/provider_keys.json —
    NEVER sent to AitherOS), starts aither-serve on your machine with your chosen
    brain, opens a secure Cloudflare quick tunnel (no account, no inbound ports),
    and registers the agent with the AitherOS control plane. Ctrl+C stops the
    agent + tunnel and deregisters.

    You run the brain (your key), the body (this loop) and the hands (your MCP).
    AitherOS drives it over the tunnel — chat + live trace, tool approval,
    artifacts. The only secret AitherOS holds is the callback bearer it presents
    back to YOUR agent.
    """
    import base64
    import getpass
    import re
    import secrets as _secrets
    import subprocess
    import threading
    import time as _time
    from queue import Empty, Queue

    import httpx

    provider = (getattr(args, "provider", "") or "").strip().lower()
    port = getattr(args, "port", None) or 8080
    name = (getattr(args, "name", "") or "").strip()
    if not name:
        import socket
        name = re.sub(r"[^a-z0-9_-]", "-", f"{socket.gethostname()}-adk".lower())
    identity = getattr(args, "identity", None) or "aither"
    register_url = getattr(args, "register_url", "") or ""
    portal = (getattr(args, "portal", "") or _control_plane()).rstrip("/")
    dry_run = bool(getattr(args, "dry_run", False))

    print()
    print("  Host a self-hosted agent -> AitherOS fleet")
    print("  ==========================================")
    print()

    # ── 1. Provider + model key (local store, never an env var the user types) ──
    if not provider:
        try:
            provider = (input("  Model provider [deepseek/openai/anthropic]: ").strip().lower()
                        or "deepseek")
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
    if provider not in _KNOWN_PROVIDERS:
        print(f"  Unknown provider '{provider}'. Known: {', '.join(sorted(_KNOWN_PROVIDERS))}")
        return 1

    keys = _load_provider_keys()
    env_name = _KNOWN_PROVIDERS[provider]["env"]
    key = keys.get(provider, "") or os.environ.get(env_name, "")
    if key:
        print(f"  [+] {_KNOWN_PROVIDERS[provider]['label']} key found: {_mask_key(key)}")
    else:
        print(f"  Enter your {_KNOWN_PROVIDERS[provider]['label']} API key.")
        print("  It is stored locally (~/.aither/provider_keys.json) and NEVER sent to AitherOS.")
        try:
            key = getpass.getpass(f"  {_KNOWN_PROVIDERS[provider]['label']} API key: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if not key:
            print("  No key entered — aborting.")
            return 1
        ok, msg = _test_provider_key(provider, key)
        print(f"  [{'+' if ok else 'x'}] {msg}")
        if not ok:
            print("  Key failed validation — aborting (not saved).")
            return 1
        keys[provider] = key
        _save_provider_keys(keys)
        print(f"  Saved to {_keys_path()} (local only).")
    # Make the key available to the child server without the user ever exporting it.
    os.environ[env_name] = key

    _default_models = {
        "deepseek": "deepseek-chat",
        "openai": "gpt-4o-mini",
        "anthropic": "claude-sonnet-4-6",
        "gemini": "gemini-2.0-flash",
    }
    model = getattr(args, "model", None) or _default_models.get(provider, "")

    # ── 2. Callback bearer — the ONE secret the control plane presents back to us ──
    auth_token = getattr(args, "auth_token", "") or os.environ.get("AITHER_SERVER_API_KEY", "")
    minted = False
    if not auth_token:
        auth_token = base64.urlsafe_b64encode(_secrets.token_bytes(24)).decode().rstrip("=")
        minted = True

    # ── 3. Control-plane token (for registration) ──
    # Resolution order: explicit --token → env → saved login → DEVICE-FLOW LOGIN.
    # Device flow (RFC 8628) is the autonomous path: no token, no manual steps —
    # print a short code + URL, the human approves once in a browser (or an agent
    # approves it headlessly), and the token arrives. Anyone — including a non-tech
    # user or an autonomous agent on the internet — can self-onboard with ONE command.
    saved = load_saved_config()
    portal_token = (getattr(args, "token", "") or os.environ.get("AITHER_PORTAL_TOKEN", "")
                    or saved.get("api_key", "") or saved.get("access_token", ""))
    will_register = not getattr(args, "no_register", False)
    if will_register and not portal_token and not dry_run:
        login_url = _resolve_identity_url(
            getattr(args, "login_url", "") or os.environ.get("AITHER_PORTAL_URL", "")
            or portal or _DEFAULT_IDENTITY_URL
        )
        print()
        print(f"  Not signed in — starting device-flow login at {login_url} …")
        try:
            dev = _device_flow_login(login_url, client_name=f"adk-host:{name}")
            portal_token = dev.get("access_token", "") or dev.get("token", "")
        except Exception as e:  # noqa: BLE001 — surface a clear next step
            print(f"  Device-flow login failed: {e}")
            print("  Pass --token, run 'adk login', or use --no-register to run locally.")
            return 1
        if not portal_token:
            print("  No token returned from device-flow login. Aborting.")
            return 1
        try:
            save_saved_config({"api_key": portal_token})
        except Exception:  # noqa: BLE001 — persistence is best-effort
            pass
        print("  [+] Signed in (token saved to ~/.aither/config.json).")

    # ── 4. Approval policy ──
    approval = getattr(args, "approve", None)
    if approval is None:
        approval = "file_write,shell_exec,shell"  # sensible default: gate the dangerous ones
    print(f"  Tool approval gating: {approval or '(none)'}")

    # ── 5. cloudflared availability (skip if local-only) ──
    cf = _find_cloudflared()
    if will_register and not cf and not dry_run:
        print("  cloudflared is not installed (needed for the secure tunnel):")
        print(_cloudflared_install_hint())
        print("  Or re-run with --no-register to run locally without a tunnel.")
        return 1

    if dry_run:
        print()
        print("  [dry-run] would:")
        print(f"    1. aither-serve --backend {provider} --port {port} --identity {identity}")
        print(f"    2. cloudflared tunnel --url http://localhost:{port}")
        reg = register_url or f"{portal}/api/genesis/v1/agent/fleet/register"
        from adk.a2a_identity import get_a2a_public_key
        public_key = get_a2a_public_key()
        body = {"name": name, "invoke_url": "https://<tunnel>.trycloudflare.com",
                "reach": "tunnel", "agent_type": "adk-agent", "token": "<callback-bearer>",
                "model": model, "provider_hint": provider, "public_key": public_key}
        print(f"    3. POST {reg}  {json.dumps(body)}")
        return 0

    procs: list[subprocess.Popen] = []

    def _cleanup():
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass

    try:
        # ── 6. Start aither-serve ──
        print()
        print(f"  Starting aither-serve ({provider}, :{port}) …")
        child_env = dict(os.environ)
        child_env["AITHER_SERVER_API_KEY"] = auth_token
        child_env["AITHER_TOOL_APPROVAL"] = approval or ""
        server = subprocess.Popen(
            [sys.executable, "-m", "adk.server", "--backend", provider,
             "--port", str(port), "--identity", identity],
            env=child_env,
        )
        procs.append(server)

        # Wait for /health
        healthy = False
        deadline = _time.time() + 60
        while _time.time() < deadline:
            if server.poll() is not None:
                print("  [x] aither-serve exited during startup.")
                return 1
            try:
                r = httpx.get(f"http://localhost:{port}/health", timeout=3)
                if r.status_code == 200:
                    healthy = True
                    break
            except Exception:
                pass
            _time.sleep(2)
        if not healthy:
            print("  [x] aither-serve did not become healthy in 60s.")
            _cleanup()
            return 1
        print(f"  [+] Agent healthy on :{port} (brain: {model or provider})")

        if not will_register:
            print()
            print(f"  Agent running locally at http://localhost:{port} (no tunnel, --no-register).")
            print("  Ctrl+C to stop.")
            server.wait()
            return 0

        # ── 7. Open the quick tunnel + capture the public URL ──
        print("  Opening a secure Cloudflare quick tunnel …")
        tunnel = subprocess.Popen(
            [cf, "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        procs.append(tunnel)
        url_q: Queue = Queue()

        def _scan():
            pat = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
            for line in iter(tunnel.stdout.readline, ""):
                m = pat.search(line)
                if m:
                    url_q.put(m.group(0))
                    break

        threading.Thread(target=_scan, daemon=True).start()
        try:
            public_url = url_q.get(timeout=45)
        except Empty:
            print("  [x] Could not obtain a trycloudflare.com URL.")
            _cleanup()
            return 1
        print(f"  [+] Tunnel live: {public_url}")

        # ── 8. Register with the control plane ──
        # A stale/expired saved token must NOT dead-end onboarding: on 401/403 we
        # re-authenticate via device flow and retry once. This is what keeps the
        # one-command flow autonomous even when ~/.aither holds an old token.
        reg = register_url or f"{portal}/api/genesis/v1/agent/fleet/register"
        from adk.a2a_identity import get_a2a_public_key
        public_key = get_a2a_public_key()
        body = {"name": name, "invoke_url": public_url, "reach": "tunnel",
                "agent_type": "adk-agent", "token": auth_token,
                "model": model, "provider_hint": provider, "public_key": public_key}

        def _do_register(tok: str):
            hdrs = {"Content-Type": "application/json"}
            if tok:
                hdrs["Authorization"] = f"Bearer {tok}"
            return httpx.post(reg, json=body, headers=hdrs, timeout=20)

        try:
            rr = _do_register(portal_token)
            if rr.status_code in (401, 403):
                login_url = _resolve_identity_url(
                    getattr(args, "login_url", "") or os.environ.get("AITHER_PORTAL_URL", "")
                    or portal or _DEFAULT_IDENTITY_URL
                )
                print(f"  Saved token rejected ({rr.status_code}) — re-authenticating via device flow…")
                dev = _device_flow_login(login_url, client_name=f"adk-host:{name}")
                portal_token = dev.get("access_token", "") or dev.get("token", "")
                if portal_token:
                    try:
                        save_saved_config({"api_key": portal_token})
                    except Exception:  # noqa: BLE001
                        pass
                    rr = _do_register(portal_token)
            if rr.status_code >= 300:
                print(f"  [x] Registration rejected ({rr.status_code}): {rr.text[:300]}")
                _cleanup()
                return 1
        except Exception as e:  # noqa: BLE001
            print(f"  [x] Registration failed: {e}")
            _cleanup()
            return 1
        print(f"  [+] Registered '{name}' with the control plane.")

        if minted:
            print()
            print("  [!] A callback bearer was minted for inbound control-plane calls.")
            print("     It is set on this agent for this run. To make it durable, start with:")
            print(f"       AITHER_SERVER_API_KEY={auth_token}")

        print()
        print("  [OK] Your self-hosted agent is LIVE in the fleet.")
        print(f"     Fleet:  {portal}/portal/fleet")
        print(f"     Chat:   {portal}/settings/agent?agent={name}")
        print("     Your model key never left this machine.")
        print()
        print("  Leave this running. Ctrl+C to stop + deregister.")
        server.wait()
        return 0

    except KeyboardInterrupt:
        print("\n  Stopping …")
        # Best-effort deregister
        if will_register and not getattr(args, "no_register", False) and (portal_token or register_url):
            try:
                reg_base = register_url.rsplit("/fleet/register", 1)[0] if register_url else f"{portal}/api/genesis/v1/agent"
                dh = {"Authorization": f"Bearer {portal_token}"} if portal_token else {}
                httpx.request("DELETE", f"{reg_base}/agent-endpoints/{name}", headers=dh, timeout=10)
                print("  [+] Deregistered from the fleet.")
            except Exception:
                pass
        return 0
    finally:
        _cleanup()


def cmd_integrate(args):
    """Integrate external tools with AitherOS."""

    target = args.target

    if target == "openclaw":
        return _integrate_openclaw(args)
    elif target == "list":
        print()
        print("  Available integrations:")
        print("  ───────────────────────")
        print("  openclaw    — Connect OpenClaw to AitherOS agent fleet")
        print("  (more coming: cursor, windsurf, continue, cline)")
        print()
        print("  Usage: adk integrate <target>")
        return 0
    else:
        print(f"  Unknown integration target: {target}")
        print("  Run 'adk integrate list' to see available integrations")
        return 1


def _integrate_openclaw(args):
    """Run OpenClaw <-> AitherOS integration."""
    import asyncio
    import json as _json
    from pathlib import Path

    async def _run():
        home = Path.home()
        openclaw_dir = home / ".openclaw"
        aither_dir = home / ".aither"

        print()
        print("  OpenClaw <-> AitherOS Integration")
        print("  ==================================")
        print()

        # 1. Detect OpenClaw
        if not openclaw_dir.exists():
            print("  [!!] OpenClaw not found at ~/.openclaw/")
            print()
            print("  Install OpenClaw first: https://openclaw.ai")
            print("  Then run this command again.")
            return 1

        print("  [OK] OpenClaw detected at ~/.openclaw/")

        # Parse config
        oc_config = {}
        oc_config_path = openclaw_dir / "openclaw.json"
        if oc_config_path.exists():
            try:
                oc_config = _json.loads(oc_config_path.read_text(encoding="utf-8"))
                version = oc_config.get("version", "unknown")
                print(f"  [OK] Version: {version}")
            except Exception:
                pass

        # Check workspace
        workspace = openclaw_dir / "workspace"
        if oc_config.get("agent", {}).get("workspace"):
            workspace = Path(oc_config["agent"]["workspace"]).expanduser()

        soul_files = []
        if workspace.exists():
            for f in ["SOUL.md", "IDENTITY.md", "AGENTS.md", "USER.md",
                       "TOOLS.md", "STYLE.md"]:
                if (workspace / f).exists():
                    soul_files.append(f)
            if soul_files:
                print(f"  [OK] Workspace soul files: {', '.join(soul_files)}")

        # Check agents
        agents_dir = openclaw_dir / "agents"
        if agents_dir.exists():
            agent_count = sum(1 for d in agents_dir.iterdir() if d.is_dir())
            if agent_count:
                print(f"  [OK] Agent sessions: {agent_count} agent(s)")

        # Already integrated?
        existing_mcp = oc_config.get("mcpServers", {})
        already = any("aither" in k.lower() for k in existing_mcp)
        if already:
            print()
            print("  [!!] AitherOS MCP servers already configured!")
            if not args.force:
                print("  Use --force to reconfigure")
                return 0

        # 2. Detect mode
        print()
        mode = args.mode or "auto"

        api_key = args.api_key or os.environ.get("AITHER_API_KEY", "")
        if not api_key:
            aither_config = aither_dir / "config.json"
            if aither_config.exists():
                try:
                    cfg = _json.loads(aither_config.read_text(encoding="utf-8"))
                    api_key = cfg.get("api_key", "")
                except Exception:
                    pass

        # Auto-detect local AitherOS
        local_running = False
        try:
            import httpx
            resp = httpx.get("http://localhost:8080/health", timeout=2.0)
            local_running = resp.status_code == 200
        except Exception:
            pass

        if mode == "auto":
            if local_running and api_key:
                mode = "hybrid"
            elif local_running:
                mode = "local"
            elif api_key:
                mode = "cloud"
            else:
                mode = "local"

        print(f"  Integration mode: {mode}")
        if local_running:
            print("  [OK] AitherOS Node running locally (port 8080)")
        if api_key:
            print(f"  [OK] API key: {api_key[:16]}...")

        # 3. Generate MCP config
        print()
        print("  CONFIGURING MCP SERVERS")
        print("  ───────────────────────")

        mcp_servers = {}

        if mode in ("local", "hybrid"):
            mcp_servers["aither-local"] = {
                "url": "http://localhost:8080/mcp/sse",
                "transport": "sse",
                "description": "AitherOS local — 29 agents, 100+ tools",
            }
            print("  [+] aither-local: localhost:8080/mcp/sse")

        if mode in ("cloud", "hybrid"):
            server_cfg = {
                "url": "https://mcp.aitherium.com/mcp/sse",
                "transport": "sse",
                "description": "AitherOS cloud — inference, agents, memory",
            }
            if api_key:
                server_cfg["env"] = {"AITHER_API_KEY": api_key}
                server_cfg["headers"] = {
                    "Authorization": f"Bearer {api_key}",
                }
            mcp_servers["aither-cloud"] = server_cfg
            print("  [+] aither-cloud: mcp.aitherium.com/mcp/sse")

        # A2A endpoint
        mcp_servers["aither-a2a"] = {
            "url": "http://localhost:8766",
            "transport": "a2a",
            "description": "AitherOS A2A — direct agent-to-agent dispatch",
        }
        print("  [+] aither-a2a: localhost:8766 (agent-to-agent)")

        if args.dry_run:
            print()
            print("  DRY RUN — would write:")
            print(_json.dumps({"mcpServers": mcp_servers}, indent=2))
            return 0

        # 4. Write config
        print()
        print("  WRITING CONFIGURATION")
        print("  ─────────────────────")

        existing_mcp.update(mcp_servers)
        oc_config["mcpServers"] = existing_mcp

        try:
            oc_config_path.write_text(
                _json.dumps(oc_config, indent=2), encoding="utf-8"
            )
            print(f"  [OK] Updated {oc_config_path}")
        except OSError as e:
            print(f"  [!!] Failed to write openclaw.json: {e}")
            return 1

        # 5. Write fleet config
        fleet_path = openclaw_dir / "aither-fleet.json"
        fleet_config = {
            "provider": "aitheros",
            "endpoint": "http://localhost:8080",
            "cloud_endpoint": "https://mcp.aitherium.com",
            "agents": [
                {"name": "demiurge", "role": "Code generation & refactoring", "tier": "pro"},
                {"name": "athena", "role": "Security analysis & threat modeling", "tier": "pro"},
                {"name": "hydra", "role": "Multi-perspective code review", "tier": "pro"},
                {"name": "apollo", "role": "Performance optimization", "tier": "pro"},
                {"name": "atlas", "role": "Service discovery & architecture", "tier": "free"},
                {"name": "viviane", "role": "Memory & knowledge recall", "tier": "free"},
                {"name": "scribe", "role": "Documentation generation", "tier": "pro"},
                {"name": "saga", "role": "Creative writing & content", "tier": "free"},
                {"name": "lyra", "role": "Research & web search", "tier": "pro"},
            ],
        }
        try:
            fleet_path.write_text(
                _json.dumps(fleet_config, indent=2), encoding="utf-8"
            )
            print(f"  [OK] Wrote {fleet_path}")
        except OSError as e:
            print(f"  [!!] Failed to write fleet config: {e}")

        # 6. Summary
        print()
        print("  " + "=" * 50)
        print("  INTEGRATION COMPLETE!")
        print()
        print("  Next steps:")
        print("  1. Restart OpenClaw to pick up the new MCP servers")
        print("  2. Try: 'use the aither agent fleet to review my code'")
        print("  3. Try: 'ask demiurge to refactor this function'")
        print("  4. Try: 'use aither swarm to implement feature X'")
        print()

        if not api_key:
            print("  Want cloud agents too?")
            print("    aither register     — Get free API key")
            print("    adk integrate openclaw --mode hybrid")
            print()

        agents_str = ", ".join(a["name"] for a in fleet_config["agents"])
        print(f"  Available agents: {agents_str}")
        print()

        return 0

    return asyncio.run(_run())


def cmd_publish(args):
    """Publish an agent to the Elysium marketplace."""
    import asyncio

    async def _publish():
        project_dir = Path(args.directory or ".").resolve()

        print()
        print("  Elysium Marketplace Publisher")
        print("  =============================")
        print()

        # Check for agent.py
        if not (project_dir / "agent.py").exists():
            print("  [!!] No agent.py found in current directory.")
            print("  Run 'aither init my-agent' to create a project first.")
            return 1

        # Read config
        agent_name = args.name
        config_file = project_dir / "config.yaml"
        if not agent_name and config_file.exists():
            try:
                import yaml
                cfg = yaml.safe_load(config_file.read_text(encoding="utf-8"))
                agent_name = cfg.get("identity", project_dir.name)
            except Exception:
                agent_name = project_dir.name

        if not agent_name:
            agent_name = project_dir.name

        print(f"  Agent: {agent_name}")
        print(f"  Directory: {project_dir}")

        # Get API key
        api_key = args.api_key or os.environ.get("AITHER_API_KEY", "")
        if not api_key:
            saved = load_saved_config()
            api_key = saved.get("api_key", "")

        if not api_key:
            print()
            print("  [!!] No API key found.")
            print("  Run 'aither register' to create an account first.")
            return 1

        # Validate
        print()
        print("  VALIDATION")
        print("  ──────────")

        errors = []
        warnings = []

        if not (project_dir / "agent.py").exists():
            errors.append("Missing agent.py")
        if not config_file.exists():
            warnings.append("No config.yaml — using defaults")
        if not (project_dir / "README.md").exists():
            warnings.append("No README.md — recommended for discoverability")

        # Check for secrets
        for f in project_dir.rglob("*.py"):
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                for pattern in ["sk-", "sk_live_", "PRIVATE_KEY"]:
                    if pattern in content:
                        warnings.append(f"Possible secret in {f.name}")
                        break
            except OSError:
                pass

        for e in errors:
            print(f"  [!!] {e}")
        for w in warnings:
            print(f"  [??] {w}")

        if errors:
            print()
            print("  Fix errors above and try again.")
            return 1

        if not errors:
            print("  [OK] Validation passed")

        if args.dry_run:
            print()
            print("  DRY RUN — would publish to Elysium marketplace")
            return 0

        # Package and submit
        print()
        print("  PUBLISHING")
        print("  ──────────")

        try:
            import httpx
            import tempfile
            import zipfile

            gateway = args.gateway or "https://gateway.aitherium.com"

            # Package
            print("  Packaging project...")
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp_path = tmp.name

            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in project_dir.rglob("*"):
                    if f.is_file() and not any(
                        part.startswith(".") or part == "__pycache__"
                        for part in f.relative_to(project_dir).parts
                    ):
                        zf.write(f, f.relative_to(project_dir))

            zip_size = os.path.getsize(tmp_path)
            print(f"  Package size: {zip_size / 1024:.1f} KB")

            # Register
            print("  Registering with gateway...")
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{gateway}/v1/agents/register",
                    json={
                        "agent_name": agent_name,
                        "description": args.description or f"ADK agent: {agent_name}",
                        "capabilities": (
                            args.capabilities.split(",") if args.capabilities else ["chat"]
                        ),
                        "version": args.version or "0.1.0",
                    },
                    headers={"Authorization": f"Bearer {api_key}"},
                )

                if resp.status_code not in (200, 201):
                    print(f"  [!!] Registration failed: {resp.text[:200]}")
                    return 1

                data = resp.json()
                agent_id = data.get("agent_id", "")
                print(f"  Registered: {agent_id}")

                # Submit listing
                print("  Submitting marketplace listing...")
                resp = await client.post(
                    f"{gateway}/v1/marketplace/listings",
                    json={
                        "agent_id": agent_id,
                        "name": agent_name,
                        "description": args.description or f"ADK agent: {agent_name}",
                        "version": args.version or "0.1.0",
                        "pricing": args.pricing or "free",
                        "tier": args.tier or "agent",
                        "category": args.category or "general",
                    },
                    headers={"Authorization": f"Bearer {api_key}"},
                )

                if resp.status_code in (200, 201):
                    listing = resp.json()
                    print(f"  Listing created: {listing.get('listing_id', '')}")
                elif resp.status_code == 404:
                    print("  [??] Marketplace endpoint not yet available")
                    print("       Agent registered but listing pending.")

            os.unlink(tmp_path)

        except ImportError:
            print("  [!!] httpx not installed. Run: pip install httpx")
            return 1
        except Exception as e:
            print(f"  [!!] Error: {e}")
            return 1

        print()
        print("  " + "=" * 50)
        print(f"  PUBLISHED: {agent_name}")
        print(f"  Marketplace: https://aitherium.com/marketplace/{agent_name}")
        print("  Status: pending_review")
        print()
        print("  Your agent will be reviewed and listed within 24 hours.")
        print()

        return 0

    return asyncio.run(_publish())


def cmd_test(args):
    """Run agent tests."""
    import subprocess
    project_dir = args.directory or "."
    test_dir = os.path.join(project_dir, "tests")
    if not os.path.exists(test_dir):
        print(f"No tests/ directory in {project_dir}")
        print("Create tests/test_agent.py to get started.")
        return 1
    cmd = ["python", "-m", "pytest", test_dir, "-v"]
    if args.coverage:
        cmd.extend(["--cov", project_dir, "--cov-report", "term-missing"])
    result = subprocess.run(cmd)
    return result.returncode


# The port `adk bonsai-local` serves on, and the port its backend preset dials. These were
# four separate literals (docstring, --port default, docker publish, preset URL) and the
# preset's was a fifth number entirely — see the comment on "bonsai-local" below.
BONSAI_LOCAL_PORT = 8090


_BACKEND_PRESETS: dict[str, dict] = {
    # Sovereign local default — Bonsai-27B, reached through MicroScheduler (:8150),
    # which is where every fleet LLM call goes and which routes `bonsai-27b` to the
    # live llama.cpp lane (the 5090 today). Until 2026-08-22 these two presets
    # dialled AitherVLLMSwap on :8201 — a slot that has been OFFLINE for bonsai since
    # 2026-07-25 (catalog row `bonsai-27b-awq`), so `--backend local|bonsai` pointed
    # a sovereign agent at nothing while reading as the sovereign default. The same
    # day the bare id `bonsai-27b` was made to resolve to the live lane (canonical
    # catalog row), so this name is safe to use here. `vllm` is the provider that
    # speaks MicroScheduler's TLS + internal CA; `openai` would refuse the cert.
    "local":   {"provider": "vllm", "base_url": "https://127.0.0.1:8150/v1", "model": "bonsai-27b"},
    "bonsai":  {"provider": "vllm", "base_url": "https://127.0.0.1:8150/v1", "model": "bonsai-27b"},
    # `adk bonsai-local` serves llama.cpp on :8090 — a DIFFERENT backend from the two
    # above, which target AitherVLLMSwap on :8201. That is a fleet service and does not
    # exist on anyone else's machine, so before this preset existed the one-command
    # install had no backend that could reach it: you ran `adk bonsai-local`, got a
    # healthy server, and every `--backend local|bonsai` still dialled :8201 and found
    # nothing. Named after the command that starts it so the pairing is discoverable.
    "bonsai-local": {"provider": "openai",
                     "base_url": f"http://localhost:{BONSAI_LOCAL_PORT}/v1",
                     "model": "bonsai-27b"},
    # On-demand escalation to the mesh / cloud — each with a model the target serves.
    "genesis": {"provider": "openai", "base_url": "http://localhost:8001/v1", "model": "aither-orchestrator"},
    # Cloud inference is the AitherMCPGateway (mcp.aitherium.com) — it fronts the
    # aither-* model roster. gateway.aitherium.com is the API/billing edge, NOT an
    # inference endpoint (its /v1/models is empty), so "managed" routes via mcp too.
    "mcp":     {"provider": "openai", "base_url": "https://mcp.aitherium.com/v1", "model": "aither-orchestrator"},
    "gateway": {"provider": "openai", "base_url": "https://mcp.aitherium.com/v1", "model": "aither-orchestrator"},
    "managed": {"provider": "openai", "base_url": "https://mcp.aitherium.com/v1", "model": "aither-orchestrator"},
    # BYO-key providers — set the key first (`adk keys set <provider> <key>` or the
    # GUI key field); the switch then uses the stored device-local key.
    "claude":   {"provider": "anthropic", "model": "claude-sonnet-4-6"},
    "anthropic": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
    "deepseek": {"provider": "deepseek", "model": "deepseek-chat"},
    # External ACP agent (claude-agent-acp, codex-acp, gemini-cli, ...) as the
    # model. The provider binary is whatever `adk backend add acp --command`
    # stored; this preset only says WHICH provider family to switch to.
    "acp": {"provider": "acp", "model": "acp"},
}


def _cmd_backend_use(preset: str) -> int:
    """Switch the RUNNING agent's backend live (no restart) to a named preset.

    Hits the agent's own POST /admin/llm/switch. From the local box this needs no
    token (loopback-trusted); for a cloud preset the stored portal key is used.
    """
    import httpx

    from adk import agent_daemon as daemon

    cfg = _BACKEND_PRESETS.get(preset)
    if not cfg:
        print(f"  Unknown backend '{preset}'. Options: {', '.join(_BACKEND_PRESETS)}")
        return 1

    st = daemon.read_status() or {}
    port = st.get("port") or 8080
    body = dict(cfg)
    body["persist"] = True
    # Cloud presets authenticate with the saved portal token.
    if preset in ("gateway", "mcp"):
        saved = load_saved_config()
        tok = saved.get("api_key") or saved.get("access_token") or ""
        if tok:
            body["api_key"] = tok

    try:
        r = httpx.post(f"http://127.0.0.1:{port}/admin/llm/switch", json=body, timeout=30)
    except httpx.HTTPError as e:
        print(f"  No running agent on :{port} to switch (start one with `adk up`). {e}")
        return 1
    if r.status_code == 200:
        print(f"  [+] Switched live -> '{preset}'  ({cfg['base_url']}"
              f"{', model ' + cfg['model'] if cfg.get('model') else ''})")
        print("      No restart needed; persisted for next start.")
        return 0
    print(f"  Switch failed ({r.status_code}): {r.text[:200]}")
    return 1


def _fleet_apply_pack(args) -> int:
    """Push+enable a bundled pack on any mesh agent via the running node's proxy.

    `agent` = 'self'/'local' targets this node's /admin/packs/apply; any other
    name targets /mesh/agents/{name}/admin/packs/apply (no SSH).
    """
    import httpx

    from adk import agent_daemon as daemon

    port = (daemon.read_status() or {}).get("port") or 8080
    agent = (getattr(args, "agent", "") or "").strip()
    pack = (getattr(args, "pack", "") or "").strip()
    is_self = agent in ("", "self", "local", "this")
    path = "/admin/packs/apply" if is_self else f"/mesh/agents/{agent}/admin/packs/apply"
    try:
        r = httpx.post(f"http://127.0.0.1:{port}{path}", json={"pack": pack}, timeout=90)
    except httpx.HTTPError as e:
        print(f"  No running agent on :{port} (start one with `adk up`). {e}")
        return 1
    if r.status_code == 200:
        print(f"  [+] Applied pack '{pack}' to '{agent or 'this agent'}' (enabled + reloaded).")
        return 0
    detail = r.text[:200]
    if r.status_code in (403, 404):
        detail += "  (older agent may not support remote pack apply — upgrade it)"
    print(f"  Apply failed ({r.status_code}): {detail}")
    return 1


def _relay_join_base(args) -> str:
    """Resolve the AitherRelay API base (same as `adk relay join`)."""
    if getattr(args, "url", ""):
        return args.url.rstrip("/")
    if getattr(args, "local", False):
        return "https://localhost:8205/v1"
    return "https://relay.aitherium.com/api/relay/v1"


def _relay_acta_base(saved: dict, args) -> str:
    """Resolve the ACTA/portal-gateway base URL (serves /v1/auth/keys)."""
    for cand in (
        getattr(args, "acta_url", "") or "",
        os.environ.get("AITHER_ACTA_URL", ""),
        os.environ.get("AITHERACTA_URL", ""),
        saved.get("acta_url", ""),
    ):
        if cand:
            return cand.rstrip("/")
    if getattr(args, "local", False):
        return "https://localhost:8206"          # portal-gateway (ACTA) on the mesh box
    return "https://mcp.aitherium.com"            # cloud ACTA surface


def _relay_resolve_owner_uid(acta_base: str, owner_key: str, verify) -> str:
    """Return the AUTHENTICATED owner user_id from ACTA (never a caller-supplied id)."""
    import httpx
    try:
        r = httpx.get(
            f"{acta_base}/v1/auth/keys",
            headers={"Authorization": f"Bearer {owner_key}"},
            verify=verify, timeout=10.0, follow_redirects=True,
        )
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            return str(r.json().get("user_id", "")).strip()
    except Exception as exc:  # noqa: BLE001 — surfaced by caller, never crash
        import logging as _logging
        _logging.getLogger("adk.cli").debug("ACTA identity resolve failed: %s", exc)
    return ""


def _relay_enroll_roster(roster_path: Path, nick: str, owner_uid: str) -> tuple[bool, str]:
    """Merge {nick: owner_uid} into the relay fleet-trust roster. Returns (wrote, msg)."""
    try:
        if roster_path.exists():
            data = json.loads(roster_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        else:
            data = {}
        agents = data.get("agents")
        if not isinstance(agents, dict):
            agents = {}
        if agents.get(nick) == owner_uid:
            return True, f"already enrolled ({nick} -> {owner_uid})"
        agents[nick] = owner_uid
        data["agents"] = agents
        roster_path.parent.mkdir(parents=True, exist_ok=True)
        roster_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True, f"enrolled {nick} -> {owner_uid} in {roster_path}"
    except OSError as exc:
        return False, str(exc)


def _relay_internal_token(args) -> str:
    """The service-internal token needed to mint an agent-scoped ACTA key + write the
    vault. Present on-mesh (container/host env); absent for a remote self-service host."""
    return (getattr(args, "internal_token", "") or os.environ.get("AITHER_INTERNAL_SECRET", "")
            or os.environ.get("AITHER_MASTER_KEY", "")).strip()


def _relay_mint_agent_key(acta_base: str, uid: str, nick: str, internal_token: str, verify) -> str:
    """Mint a REVOCABLE, agent_id-scoped ACTA key bound to owner `uid` via the internal
    ACTA endpoint. Returns the raw key, or '' on failure. On-mesh only (needs the token)."""
    import httpx
    try:
        r = httpx.post(
            f"{acta_base}/v1/internal/users/{uid}/api-keys",
            headers={"X-Internal-Token": internal_token},
            json={"name": f"fleet-agent:{nick}", "agent_id": nick, "scopes": ["read"],
                  "created_by": "adk-relay-provision",
                  "metadata": {"purpose": "relay-fleet-agent", "nick": nick}},
            verify=verify, timeout=20.0, follow_redirects=True,
        )
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            return str(r.json().get("api_key", "")).strip()
        print(f"  ! mint failed ({r.status_code}): {r.text[:160]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! mint error: {exc}")
    return ""


def _relay_store_credential(relay_base: str, nick: str, value: str, auth_token: str, verify) -> str:
    """Store the agent's own relay credential in the encrypted lockbox via the relay
    credential endpoint (agent-managed, admin-recoverable). Auth = the agent's own key.
    Returns "" on success, else a short reason (never the secret)."""
    import httpx
    try:
        r = httpx.post(
            f"{relay_base}/agent/credential",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"value": value, "nick": nick, "metadata": {"source": "adk relay provision"}},
            verify=verify, timeout=15.0, follow_redirects=True,
        )
        if r.status_code == 200:
            return ""
        return f"http {r.status_code}"
    except Exception as exc:  # noqa: BLE001 — surface a reason, never the secret
        return f"{type(exc).__name__}"


def _relay_up(args) -> int:
    """Start a sovereign AitherNet relay using Docker compose.

    Writes a docker-compose.yml and .env file, then runs `docker compose up -d`.
    The relay is configured for optional hub federation and directory registration.
    """
    import socket
    import subprocess as _sp

    # Resolve compose file path
    if getattr(args, "compose_file", None):
        compose_path = Path(args.compose_file)
    else:
        # Auto-detect: look in deploy/compose, then awdk/deploy/compose
        candidates = [
            Path("deploy/compose/docker-compose.relay.yml"),
            Path(__file__).resolve().parent.parent / "deploy/compose/docker-compose.relay.yml",
        ]
        compose_path = None
        for candidate in candidates:
            if candidate.exists():
                compose_path = candidate
                break
        if not compose_path:
            print(f"  [x] docker-compose.relay.yml not found in: {[str(c) for c in candidates]}")
            print(f"      Expected at: {candidates[0]} or {candidates[1]}")
            return 1

    # Resolve values: flags > env > defaults
    slug = (getattr(args, "slug", "") or os.environ.get("AITHERNET_NODE_SLUG", "")
            or os.environ.get("AITHER_NODE_SLUG", "")
            or f"relay-{socket.gethostname()}").strip()
    rooms = (getattr(args, "rooms", "") or os.environ.get("AITHERNET_ADVERTISED_ROOMS", "")
             or "#general").strip()
    hub_url = (getattr(args, "hub_url", "") or os.environ.get("AITHERNET_HUB_URL", "")
               or "wss://relay.aitherium.com/ws/chat").strip()
    federation = (not getattr(args, "no_federation", False)
                  and os.environ.get("AITHERNET_FEDERATION", "").lower() not in ("0", "false", "no"))
    directory_url = (getattr(args, "directory_url", "") or os.environ.get("AITHERNET_DIRECTORY_URL", "")
                     or "").strip()
    token = (getattr(args, "token", "") or os.environ.get("AITHER_NODE_TOKEN", "")).strip()
    port = getattr(args, "port", 8205) or 8205
    foreground = bool(getattr(args, "foreground", False))
    dry_run = bool(getattr(args, "dry_run", False))

    # Create working directory structure
    work_dir = Path.cwd()
    env_file = work_dir / ".env.relay"
    env_lines = [
        "# AitherNet Relay Environment",
        f"AITHERNET_NODE_SLUG={slug}",
        f"AITHERNET_ADVERTISED_ROOMS={rooms}",
        f"AITHERNET_HUB_URL={hub_url}",
        f"AITHERNET_FEDERATION={'true' if federation else 'false'}",
        f"RELAY_PORT={port}",
        f"REDIS_PORT={6379}",
        "AITHER_LOG_LEVEL=INFO",
    ]
    public_endpoint = (getattr(args, "public_endpoint", "")
                       or os.environ.get("AITHERNET_PUBLIC_ENDPOINT", "") or "").strip()
    if directory_url:
        env_lines.append(f"AITHERNET_DIRECTORY_URL={directory_url}")
        if not public_endpoint:
            print("  [!] --directory-url set but no --public-endpoint: the relay will "
                  "skip directory registration (nothing to list as your join address)")
    if public_endpoint:
        env_lines.append(f"AITHERNET_PUBLIC_ENDPOINT={public_endpoint}")
    if token:
        env_lines.append(f"AITHER_NODE_TOKEN={token}")

    if dry_run:
        print("DRY RUN: Would write .env and start relay")
        print()
        print("Compose file:", compose_path)
        print("Working dir:", work_dir)
        print(".env file:", env_file)
        print()
        print(".env contents:")
        for line in env_lines:
            print(f"  {line}")
        print()
        print("Command: docker compose -f {compose} --env-file {env} up {flag}".format(
            compose=compose_path.name if compose_path.parent == work_dir else compose_path,
            env=env_file.name,
            flag="-d" if not foreground else ""
        ))
        return 0

    # Write .env file
    try:
        env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        print(f"  [+] Wrote {env_file}")
    except OSError as e:
        print(f"  [x] Failed to write {env_file}: {e}")
        return 1

    # Run docker compose
    compose_cmd = [
        "docker", "compose",
        "-f", str(compose_path),
        "--env-file", str(env_file),
        "up",
    ]
    if not foreground:
        compose_cmd.append("-d")

    print(f"  [+] Starting relay at localhost:{port}...")
    if not foreground:
        print(f"      docker compose -f {compose_path.name} --env-file {env_file.name} up -d")

    try:
        result = _sp.run(compose_cmd, cwd=work_dir)
        if result.returncode != 0:
            print(f"  [x] docker compose failed with exit code {result.returncode}")
            return result.returncode
    except Exception as e:
        print(f"  [x] Failed to run docker compose: {e}")
        return 1

    # Print next steps
    print()
    print("  ✓ Relay started!")
    print()
    print("  Next steps:")
    print(f"    - Local chat:  ws://localhost:{port}/ws/chat")
    if federation:
        print(f"    - Hub URL:     {hub_url}")
        print(f"    - Federation:  enabled (node slug: {slug})")
    if directory_url:
        print(f"    - Directory:   {directory_url} (auto-registering)")
    print()
    print("  Manage:")
    print(f"    - Logs:     docker compose -f {compose_path.name} logs -f relay")
    print(f"    - Stop:     docker compose -f {compose_path.name} down")
    print()
    return 0


def _relay_provision(args) -> int:
    """Provision a fleet agent as a human-facing AitherRelay participant (no manual curling).

    Binds <nick> to your AUTHENTICATED owner identity: resolves your user_id from ACTA,
    enrolls `nick -> user_id` in the relay fleet-trust roster (so it may DM humans, not just
    other agents), saves the relay credential locally so `adk relay join` uses it, and prints
    the next step. The roster bind is identity-checked server-side — a key for a different
    user cannot claim the nick.
    """
    from adk._tls import tls_verify

    nick = (getattr(args, "nick", "") or "").strip()
    if not nick:
        print("Usage: adk relay provision <nick> [--local] [--acta-url URL] [--roster PATH] [--user-id ID]")
        return 1

    saved = load_saved_config()
    owner_key = (getattr(args, "token", "") or os.environ.get("AITHER_API_KEY", "")
                 or saved.get("api_key", "") or saved.get("access_token", ""))
    if not owner_key:
        print("  Not logged in. Run `adk login` first — provisioning binds the agent to YOUR identity.")
        return 1

    verify = tls_verify()
    acta_base = _relay_acta_base(saved, args)

    # 1) Resolve the authenticated owner user_id (server-derived, not caller-claimed).
    owner_uid = (getattr(args, "user_id", "") or "").strip()
    if owner_uid:
        print(f"  Using owner user_id from --user-id: {owner_uid} (asserted).")
    else:
        owner_uid = _relay_resolve_owner_uid(acta_base, owner_key, verify)
        if owner_uid:
            print(f"  Resolved your identity from ACTA ({acta_base}): user_id={owner_uid}")
    if not owner_uid:
        print(f"  Could not resolve your user_id from ACTA at {acta_base}.")
        print("  Re-run with --acta-url <portal-gateway> or pass --user-id <your-user_id> to assert it.")
        return 1

    # 2) Agent credential. Preference order:
    #    a) explicit --agent-key
    #    b) MINT a fresh revocable, agent-scoped key on-mesh (internal token available) +
    #       store it in AitherSecrets — the real self-service path, no manual curling.
    #    c) reuse your authenticated login key (resolves to owner_uid, which the roster needs).
    agent_key = (getattr(args, "agent_key", "") or "").strip()
    cred_kind = "supplied"
    internal_token = _relay_internal_token(args)
    if not agent_key and internal_token and not getattr(args, "no_mint", False):
        minted = _relay_mint_agent_key(acta_base, owner_uid, nick, internal_token, verify)
        if minted:
            agent_key = minted
            cred_kind = "minted"
            # Store the credential in the agent's own encrypted lockbox via the relay
            # (agent-managed + admin-recoverable). Authenticated by the minted key itself.
            relay_base = _relay_join_base(args)
            reason = _relay_store_credential(relay_base, nick, agent_key, agent_key, verify)
            if not reason:
                print("  Minted a revocable agent key and stored it in the agent lockbox "
                      "(recoverable via relay GET /v1/agent/credential).")
            else:
                print(f"  Minted a revocable agent key (lockbox store unavailable [{reason}]; "
                      f"saved to local adk config).")
    if not agent_key:
        agent_key = owner_key
        cred_kind = "reused-login"

    # 3) Enroll the nick in the relay fleet-trust roster (write locally if reachable).
    default_roster = os.environ.get("AITHER_RELAY_FLEET_TRUST_FILE", "") or str(
        Path.cwd() / "AitherOS" / "config" / "relay" / "fleet_trust.json"
    )
    roster_path = Path(getattr(args, "roster", "") or default_roster)
    wrote, msg = _relay_enroll_roster(roster_path, nick, owner_uid)
    if wrote:
        print(f"  Roster: {msg}")
        print("  ! Restart aitheros-communication-core to apply the roster change (config is bind-mounted).")
    else:
        print(f"  Could not write the roster at {roster_path}: {msg}")
        print("  On the relay host, add this to config/relay/fleet_trust.json then restart commcore:")
        print(f'      {{"agents": {{"{nick}": "{owner_uid}"}}}}')

    # 4) Persist the relay credential + nick locally so `adk relay join` picks it up.
    _kind_note = {"minted": " (fresh revocable agent key)",
                  "reused-login": " (reusing your login key — no on-mesh mint token)",
                  "supplied": " (supplied --agent-key)"}.get(cred_kind, "")
    try:
        save_saved_config({"relay_token": agent_key, "relay_nick": nick})
        print(f"  Saved relay credential for '{nick}' to your adk config{_kind_note}.")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! Could not persist relay config: {exc}")

    where = "--local" if getattr(args, "local", False) else ""
    print(f"\n  Done. Bring the agent online:  adk relay join --nick {nick} {where}".rstrip())
    if not wrote:
        print("  (join will succeed but DMs to HUMANS stay quarantined until the roster entry lands + commcore restarts.)")
    return 0


def _relay_notifications(args) -> int:
    """Get and optionally mark notifications as read."""
    import asyncio
    from datetime import datetime, timezone

    # Resolve token + base URL (same pattern as relay join)
    saved = load_saved_config()
    token = (
        getattr(args, "token", "")
        or os.environ.get("AITHER_RELAY_TOKEN", "")
        or saved.get("relay_token", "")
        or saved.get("api_key", "")
        or saved.get("access_token", "")
    )
    if not token:
        print(
            "  No relay credential. Run `adk relay provision <nick>` or `adk login`,"
            " or pass --token / set AITHER_RELAY_TOKEN."
        )
        return 1

    if getattr(args, "url", ""):
        base = args.url.rstrip("/")
    elif getattr(args, "local", False):
        base = "https://localhost:8205/v1"
    else:
        base = "https://relay.aitherium.com/api/relay/v1"

    nick = (
        getattr(args, "nick", "")
        or saved.get("relay_nick", "")
        or saved.get("username", "")
        or "aither"
    )

    async def _fetch():
        import httpx
        from adk._tls import tls_verify

        verify = tls_verify()
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=30, verify=verify
        ) as client:
            headers = {"Authorization": f"Bearer {token}"}
            # GET /v1/notifications/{nick}?unread_only={unread_only}
            unread_only = getattr(args, "unread", False)
            url = f"{base}/notifications/{nick}"
            params = {}
            if unread_only:
                params["unread_only"] = "true"

            r = await client.get(url, headers=headers, params=params)
            if r.status_code != 200:
                print(f"Error: {r.status_code} — {r.text[:200]}")
                return None

            try:
                data = r.json()
            except ValueError:
                print("Error: invalid JSON response")
                return None

            notifs = data.get("notifications", [])
            unread_count = data.get("unread_count", 0)

            # Mark as read if --ack is set
            if notifs and getattr(args, "ack", False):
                ids = [n.get("id") for n in notifs if n.get("id")]
                if ids:
                    r2 = await client.post(
                        f"{base}/notifications/{nick}/read",
                        headers=headers,
                        json={"ids": ids},
                    )
                    if r2.status_code != 200:
                        print(f"Warning: failed to mark as read: {r2.status_code}")

            return notifs, unread_count

    result = asyncio.run(_fetch())
    if result is None:
        return 1

    notifs, unread_count = result

    if not notifs:
        print(f"No notifications (unread: {unread_count}).")
        return 0

    # Print compact table
    print(f"\nNotifications (unread: {unread_count}):")
    print(
        f"  {'ID':<8} {'Type':<12} {'From':<16} {'Channel':<16} {'Age':<8} {'Read':<5}"
    )
    print("  " + "-" * 80)

    now = datetime.now(timezone.utc)
    for notif in notifs:
        notif_id = notif.get("id", "?")[:8]
        notif_type = notif.get("type", "?")[:12]
        from_nick = notif.get("from_nick", "?")[:16]
        channel = notif.get("channel", "?")[:16]
        created_at_str = notif.get("created_at", "")
        read = "yes" if notif.get("read", False) else "no"

        # Parse created_at and compute age
        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            age_s = int((now - created_at).total_seconds())
            if age_s < 60:
                age = f"{age_s}s"
            elif age_s < 3600:
                age = f"{age_s // 60}m"
            else:
                age = f"{age_s // 3600}h"
        except (ValueError, AttributeError):
            age = "?"

        print(
            f"  {notif_id:<8} {notif_type:<12} {from_nick:<16} "
            f"{channel:<16} {age:<8} {read:<5}"
        )

    if getattr(args, "ack", False):
        print(f"\n  Marked {len(notifs)} notification(s) as read.")

    return 0


def cmd_relay(args):
    """Connect this agent to AitherRelay as a chat participant — one command.

    `adk relay up` starts a sovereign relay bundle (Docker compose).
    `adk relay join` joins the relay and serves DMs: a human DMs this agent and
    it replies on its OWN inference (no SSH, no manual curling). Credential and
    URL are auto-resolved from your login/config; override with flags.
    `adk relay provision <nick>` enrolls a fleet agent so it may DM humans.
    `adk relay notifications` fetches stored notifications for the agent.
    """
    import asyncio

    if getattr(args, "relay_command", None) == "up":
        return _relay_up(args)

    if getattr(args, "relay_command", None) == "provision":
        return _relay_provision(args)

    if getattr(args, "relay_command", None) == "notifications":
        return _relay_notifications(args)

    if getattr(args, "relay_command", None) != "join":
        print("Usage: adk relay up [--slug NAME] [--rooms #general,#agents] [--hub-url URL]")
        print("       adk relay join [--nick NAME] [--url BASE] [--local] [--channel #agents]")
        print("       adk relay provision <nick> [--local] [--acta-url URL] [--roster PATH]")
        print("       adk relay notifications [--nick NAME] [--local] [--unread] [--ack]")
        return 1

    from adk import AitherAgent
    from adk.relay_client import RelayClient

    saved = load_saved_config()
    token = (getattr(args, "token", "") or os.environ.get("AITHER_RELAY_TOKEN", "")
             or saved.get("relay_token", "")
             or saved.get("api_key", "") or saved.get("access_token", ""))
    if not token:
        print("  No relay credential. Run `adk relay provision <nick>` or `adk login`,"
              " or pass --token / set AITHER_RELAY_TOKEN.")
        return 1

    if getattr(args, "url", ""):
        base = args.url.rstrip("/")
    elif getattr(args, "local", False):
        base = "https://localhost:8205/v1"       # local fleet AitherRelay (internal CA)
    else:
        base = "https://relay.aitherium.com/api/relay/v1"   # cloud fabric (public cert)

    nick = (getattr(args, "nick", "") or saved.get("relay_nick", "")
            or saved.get("username", "") or "aither")
    channel = getattr(args, "channel", "") or "#agents"

    agent = AitherAgent(nick)
    client = RelayClient(base_url=base, token=token, nick=nick, agent=agent, channel=channel)
    print(f"  Joining AitherRelay as '{nick}' at {base} — serving DMs (Ctrl+C to leave).")
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        print("\n  Left the relay.")
    except Exception as e:  # noqa: BLE001 — surface a clean error, not a traceback
        print(f"  Relay error: {e}")
        return 1
    return 0


def cmd_backend(args):
    """Manage LLM backends — list, set, use, test, switch, status."""
    import asyncio
    import time

    sub = getattr(args, "backend_command", None)

    if sub == "guide":
        from adk.llm.onboarding import guide, provider_menu
        name = getattr(args, "provider", None)
        print(guide(name) if name else provider_menu())
        return 0

    if sub == "use":
        return _cmd_backend_use(getattr(args, "preset", "") or "")

    if sub == "list":
        async def _list():
            from adk.llm import LLMRouter
            from adk.config import Config
            cfg = Config.from_env()
            router = LLMRouter(config=cfg)
            try:
                await router.get_provider()
            except ConnectionError:
                pass
            info = router.get_backends()
            print("LLM Backends")
            print("=" * 40)
            for k, v in info.items():
                print(f"  {k:20s} {v}")
            # Show available providers
            print()
            print("Available:")
            for name in ("ollama", "vllm", "openai", "anthropic", "deepseek",
                         "moonshot", "groq", "together", "gateway", "lmstudio",
                         "genesis", "picolm"):
                print(f"  - {name}")
        asyncio.run(_list())
        return 0

    elif sub == "set":
        provider = getattr(args, "provider", None)
        if not provider:
            print("Usage: adk backend set <provider> [--api-key KEY] [--base-url URL] [--model MODEL]")
            return 1
        data = {"default_backend": provider}
        api_key = getattr(args, "api_key", None)
        base_url = getattr(args, "base_url", None)
        model = getattr(args, "model", None)
        if api_key:
            if provider == "anthropic":
                data["anthropic_api_key"] = api_key
            elif provider == "deepseek":
                data["deepseek_api_key"] = api_key
            elif provider == "moonshot":
                data["moonshot_api_key"] = api_key
            else:
                data["api_key"] = api_key
        if base_url:
            data["inference_url"] = base_url
        if model:
            data["default_model"] = model
        save_saved_config(data)
        print(f"Backend set to: {provider}")
        if base_url:
            print(f"  URL: {base_url}")
        if model:
            print(f"  Model: {model}")
        return 0

    elif sub == "set-reasoning":
        provider = getattr(args, "provider", None)
        if not provider:
            print("Usage: adk backend set-reasoning <provider> [--api-key KEY]")
            return 1
        data = {"reasoning_backend": provider}
        api_key = getattr(args, "api_key", None)
        model = getattr(args, "model", None)
        if api_key:
            data["reasoning_api_key"] = api_key
        if model:
            data["reasoning_model"] = model
        save_saved_config(data)
        print(f"Reasoning backend set to: {provider}")
        return 0

    elif sub == "add":
        kind = getattr(args, "kind", "")
        command = getattr(args, "command", "")
        if kind == "acp":
            if not command or not command.strip():
                print("Usage: adk backend add acp --command <cmd> [--arg ...] [--model NAME]")
                return 1
            data = {
                "default_backend": "acp",
                "acp_command": command.strip(),
                "acp_args": list(getattr(args, "args", []) or []),
            }
            model = getattr(args, "model", None)
            if model:
                data["default_model"] = model
            save_saved_config(data)
            print(f"ACP backend registered: {command.strip()} "
                  f"{' '.join(getattr(args, 'args', []) or [])}".rstrip())
            print("  Switch live now:      adk backend use acp")
            print("  Make it the default:  adk backend set acp")
            return 0
        print(f"Unknown backend kind: {kind} (currently only 'acp' is supported)")
        return 1

    elif sub == "status":
        """Show current backend configuration and connectivity."""
        cfg_dict = load_saved_config()
        current_backend = cfg_dict.get("setup_backend", "unknown")
        inference_url = cfg_dict.get("inference_url", "not configured")
        inference_model = cfg_dict.get(
            "inference_model",
            cfg_dict.get("ollama_model", "not configured"),
        )

        print("\n  Backend Status")
        print("  " + "=" * 50)
        print(f"  Backend:  {current_backend}")
        print(f"  Endpoint: {inference_url}")
        print(f"  Model:    {inference_model}")
        print()

        # Quick connectivity check
        async def _check():
            try:
                import httpx
                async with httpx.AsyncClient(timeout=3.0) as client:
                    # All three backends are OpenAI-compatible — /v1/models is
                    # the uniform liveness probe (Ollama/vLLM/llama.cpp serve it).
                    base = inference_url.rstrip("/")
                    probe = base + "/models" if base.endswith("/v1") else base + "/v1/models"
                    r = await client.get(probe)
                    if r.status_code == 200:
                        print("  Status:   UP (responding)")
                    else:
                        print(f"  Status:   HTTP {r.status_code}")
                    return 0
            except Exception as e:
                print(f"  Status:   DOWN ({type(e).__name__})")
                return 0

        asyncio.run(_check())
        print()
        return 0

    elif sub == "switch":
        """Switch to a different inference backend (ollama, llamacpp, vllm)."""
        target = getattr(args, "target_backend", None)
        if not target or target not in ("ollama", "llamacpp", "vllm"):
            print("Usage: adk backend switch <ollama|llamacpp|vllm>")
            return 1

        print()
        print("  Backend Switch")
        print("  " + "=" * 50)
        print()

        cfg_dict = load_saved_config()
        current_backend = cfg_dict.get("setup_backend", "unknown")

        if target == current_backend:
            print(f"  Already using {target}. No changes needed.")
            return 0

        # Install/ensure the target backend
        print(f"  [1/3] Preparing {target} backend...")

        if target == "ollama":
            from adk.ollama_setup import ensure_installed, ensure_running

            if not ensure_installed():
                print("  ERROR: Failed to install Ollama", file=sys.stderr)
                return 1
            if not ensure_running():
                print("  ERROR: Failed to start Ollama", file=sys.stderr)
                return 1
            endpoint = "http://localhost:11434/v1"
            # Preserve the model if it was ollama before, else use default
            model_name = cfg_dict.get("ollama_model", "gemma4:e2b")

        elif target == "llamacpp":
            from adk import llamacpp_setup

            result = llamacpp_setup.install(quant=None, port=8209, service=True)
            if not result.success:
                print(f"  ERROR: {result.error}", file=sys.stderr)
                return 1
            endpoint = f"http://localhost:{result.port}/v1"
            model_name = result.quant

        elif target == "vllm":
            from adk.setup_cli import cmd_setup

            class SetupArgs:
                shortcut = None
                tier = None
                mode = "auto"
                reasoning_api = None
                reasoning_model = ""
                dgx_spark = None
                stack = None
                dry_run = False
                non_interactive = True
                hf_token = ""
                api_key = ""
                output = "docker-compose.vllm.yml"

            setup_result = cmd_setup(SetupArgs())
            if setup_result != 0:
                print("  ERROR: vLLM setup failed", file=sys.stderr)
                return 1
            endpoint = "http://localhost:8209/v1"
            model_name = "auto"
        else:
            print(f"  ERROR: unknown backend {target}", file=sys.stderr)
            return 1

        # Step 2: Verify with smoke test
        print()
        print("  [2/3] Verifying endpoint...")
        time.sleep(2)  # Give service time to start

        if target in ("llamacpp", "vllm"):
            from adk import llamacpp_setup

            verify_port = 8209
            for _ in range(30):
                if llamacpp_setup.status(port=verify_port).running:
                    break
                time.sleep(1)

            ok = llamacpp_setup.smoke_test(port=verify_port)
            if not ok:
                print(
                    "  ERROR: Smoke test failed - the endpoint did not return a valid "
                    "completion. Check logs and re-run.",
                    file=sys.stderr,
                )
                return 1
        elif target == "ollama":
            from adk.ollama_setup import smoke_test as ollama_smoke_test

            ok = ollama_smoke_test(model_name)
            if not ok:
                print(
                    "  ERROR: Smoke test failed - the endpoint did not respond. "
                    "Check logs and re-run.",
                    file=sys.stderr,
                )
                return 1

        print(f"  Endpoint ready: {endpoint}")

        # Step 3: Persist configuration
        print()
        print("  [3/3] Saving configuration...")
        save_saved_config({
            "setup_backend": target,
            "inference_url": endpoint,
            "inference_model": model_name,
        })
        if target == "ollama":
            save_saved_config({"ollama_model": model_name})

        print()
        print("=" * 60)
        print("  Backend Switched Successfully")
        print("=" * 60)
        print()
        print(f"  Backend:  {target}")
        print(f"  Endpoint: {endpoint}")
        print(f"  Model:    {model_name}")
        print()
        return 0

    elif sub == "test":
        async def _test():
            from adk.llm import LLMRouter
            from adk.config import Config
            cfg = Config.from_env()
            router = LLMRouter(config=cfg)
            try:
                await router.get_provider()
                print(f"Provider: {router.provider_name}")
                resp = await router.chat(
                    [{"role": "user", "content": "Say 'hello' in one word."}],
                    effort=3,
                )
                print(f"Model: {resp.model}")
                print(f"Response: {resp.content[:100]}")
                print(f"Tokens: {resp.tokens_used}")
                print("Status: OK")
            except Exception as e:
                print(f"FAILED: {e}")
                return 1
            return 0
        return asyncio.run(_test())

    print("Usage: adk backend [list|set|set-reasoning|test|switch|status]")
    return 1


def cmd_install(args) -> int:
    """Install ready-made agent packs into user workspace.

    Accepts `adk install` / `adk install list` (list packs), and
    `adk install pack:<name>` or `adk install <name>` (install a pack).
    """
    # Resolve the requested target across the CLI positional and the legacy
    # subcommand arg shape still used by tests.
    target = getattr(args, "target", None)
    if target is None:
        legacy = getattr(args, "install_command", None)
        if legacy == "pack":
            target = getattr(args, "pack_name", None)
        elif legacy:
            target = legacy

    # Get available packs from the bundled packs directory
    packs_dir = Path(__file__).parent / "packs"
    available_packs = {}
    if packs_dir.exists():
        for pack_dir in packs_dir.iterdir():
            if pack_dir.is_dir():
                agent_yaml = pack_dir / "agent.yaml"
                if agent_yaml.exists():
                    try:
                        import yaml
                        data = yaml.safe_load(agent_yaml.read_text(encoding="utf-8")) or {}
                        available_packs[pack_dir.name] = {
                            "path": pack_dir,
                            "name": data.get("name", pack_dir.name),
                            "description": data.get("description", ""),
                            "identity": data.get("identity", pack_dir.name),
                        }
                    except Exception:
                        pass

    if target is None or target == "list":
        """List available bundled packs."""
        if not available_packs:
            print("No agent packs available.")
            return 0

        print("\n  Available Agent Packs")
        print("  " + "=" * 50)
        print()
        for pack_id, info in sorted(available_packs.items()):
            print(f"  {pack_id:<20s} {info['name']}")
            if info["description"]:
                desc = info["description"].split("\n")[0]
                print(f"  {'':20s} {desc[:50]}")
            print()
        print("  Install with: adk install pack:<name>")
        print()
        return 0

    # Install mode — accept 'pack:<name>' or a bare '<name>'.
    pack_name = target[len("pack:"):] if target.startswith("pack:") else target
    pack_name = (pack_name or "").strip()
    if target is not None:
        if not pack_name:
            print("Usage: adk install pack:<name>")
            print()
            print("Available packs:")
            for pid in sorted(available_packs.keys()):
                print(f"  - {pid}")
            return 1

        if pack_name not in available_packs:
            print(f"ERROR: Unknown pack '{pack_name}'")
            print()
            print("Available packs:")
            for pid in sorted(available_packs.keys()):
                print(f"  - {pid}")
            return 1

        pack_info = available_packs[pack_name]
        pack_src = pack_info["path"]

        # Determine install location
        install_base = Path.home() / ".aither" / "agents"
        install_dst = install_base / pack_name

        print()
        print(f"  Installing {pack_name}...")
        print("  " + "=" * 50)
        print()

        # Create install directory
        try:
            install_base.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"  ERROR: Could not create directory {install_base}: {e}")
            return 1

        # Copy pack files
        try:
            import shutil
            if install_dst.exists():
                print(f"  Removing existing installation at {install_dst}")
                shutil.rmtree(install_dst)
            shutil.copytree(pack_src, install_dst)
            print(f"  Installed to: {install_dst}")
        except Exception as e:
            print(f"  ERROR: Failed to install pack: {e}")
            return 1

        # Print instructions
        print()
        print("  Next steps:")
        print(f"    adk run --agents {pack_name}      # Run this agent")
        print(f"    adk serve --agents {pack_name}    # Serve as a web service")
        print()
        return 0

    print("Usage: adk install [list|pack:<name>]")
    return 1


def cmd_packs(args) -> int:
    """Alias for 'adk install list' — list available agent packs."""
    args.install_command = "list"
    return cmd_install(args)


def _load_arc_pack():
    """File-load the bundled arc-brainpack pack and return its tools module.
    Bundled at adk/toolpacks/arc-brainpack — loaded by path so it works from a
    plain `pip install awdk` with the pack dir not on sys.path."""
    import importlib.util
    pack_dir = Path(__file__).resolve().parent / "toolpacks" / "arc-brainpack"
    init = pack_dir / "__init__.py"
    tools = pack_dir / "tools.py"
    target = init if init.is_file() else tools
    if not target.is_file():
        raise FileNotFoundError(f"arc-brainpack pack not found at {pack_dir}")
    spec = importlib.util.spec_from_file_location(
        "_arc_brainpack", target, submodule_search_locations=[str(pack_dir)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_arc_brainpack"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    # register/__init__ may re-export from tools; prefer the tools module for fns
    if not hasattr(mod, "arc_register") and (pack_dir / "tools.py").is_file():
        tspec = importlib.util.spec_from_file_location(
            "_arc_brainpack.tools", pack_dir / "tools.py",
            submodule_search_locations=[str(pack_dir)])
        tmod = importlib.util.module_from_spec(tspec)
        sys.modules["_arc_brainpack.tools"] = tmod
        tspec.loader.exec_module(tmod)  # type: ignore[union-attr]
        return tmod
    return mod


def cmd_contribute(args) -> int:
    """Teach Aither's ARC world model via the bundled arc-brainpack pack.

    Zero-to-contributing with no repo clone:
        pip install 'awdk[arc]'
        adk contribute register
        export ARC_API_KEY=<key from three.arcprize.org>
        adk contribute play
    """
    sub = getattr(args, "contribute_command", None)
    try:
        pack = _load_arc_pack()
    except Exception as exc:
        print(f"✗ could not load the arc-brainpack pack: {exc}")
        return 1

    def _show(result):
        if isinstance(result, (dict, list)):
            print(json.dumps(result, indent=2, default=str))
        elif result is not None:
            print(result)

    try:
        if sub == "register":
            _show(pack.arc_register())
        elif sub == "play":
            fn = getattr(pack, "arc_contribute", None) or getattr(pack, "contribute_random", None)
            if fn is None:
                print("✗ this pack build has no play entrypoint")
                return 1
            _show(fn(args.games or None, n=args.steps))
        elif sub == "status":
            _show(pack.arc_status())
        elif sub == "leaderboard":
            _show(pack.arc_leaderboard(limit=getattr(args, "limit", 20)))
        elif sub == "solo":
            _show(pack.arc_solo())
        else:
            print("Teach Aither's ARC world model (free, open).\n")
            print("  adk contribute register       mint a free wallet + contributor token")
            print("  adk contribute play [games]   play ARC and stream transitions")
            print("  adk contribute status         your accepted count + daily quota")
            print("  adk contribute leaderboard    who has taught it the most")
            print("  adk contribute solo           one-command self-host (train YOUR own model)\n")
            print("Playing needs the arc extra:  pip install 'awdk[arc]'")
            print("and an ARC key:  export ARC_API_KEY=<key from three.arcprize.org>")
            return 0
    except Exception as exc:
        print(f"✗ {sub}: {exc}")
        return 1
    return 0


# ── Phase 1: aither keys — API key management ─────────────────────────────

_KNOWN_PROVIDERS = {
    "openai": {"env": "OPENAI_API_KEY", "test_url": "https://api.openai.com/v1/models", "label": "OpenAI"},
    "anthropic": {"env": "ANTHROPIC_API_KEY", "test_url": "https://api.anthropic.com/v1/messages", "label": "Anthropic"},
    "deepseek": {"env": "DEEPSEEK_API_KEY", "test_url": "https://api.deepseek.com/v1/models", "label": "DeepSeek"},
    "moonshot": {"env": "MOONSHOT_API_KEY", "test_url": "https://api.moonshot.ai/v1/models", "label": "Moonshot Kimi"},
    "google": {"env": "GOOGLE_API_KEY", "test_url": "https://generativelanguage.googleapis.com/v1/models", "label": "Google AI"},
    "openrouter": {"env": "OPENROUTER_API_KEY", "test_url": "https://openrouter.ai/api/v1/models", "label": "OpenRouter"},
    "groq": {"env": "GROQ_API_KEY", "test_url": "https://api.groq.com/openai/v1/models", "label": "Groq"},
    "together": {"env": "TOGETHER_API_KEY", "test_url": "https://api.together.xyz/v1/models", "label": "Together AI"},
}


def _keys_path() -> Path:
    """Path to local provider key store."""
    d = Path.home() / ".aither"
    d.mkdir(parents=True, exist_ok=True)
    return d / "provider_keys.json"


def _load_provider_keys() -> dict:
    p = _keys_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_provider_keys(data: dict) -> None:
    p = _keys_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    # Restrict permissions on Unix
    if sys.platform != "win32":
        os.chmod(p, 0o600)


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return key[:4] + "..." + key[-4:]


def _test_provider_key(provider: str, key: str) -> tuple:
    """Test a provider key with a minimal API call. Returns (success, message)."""
    import urllib.request
    import urllib.error

    info = _KNOWN_PROVIDERS.get(provider)
    if not info:
        return False, f"Unknown provider: {provider}"

    test_url = info["test_url"]
    headers = {"Content-Type": "application/json"}

    if provider == "anthropic":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
        # Anthropic needs a POST to /messages with minimal payload to validate
        try:
            payload = json.dumps({
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            }).encode()
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=payload, headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=15):
                return True, "Key valid"
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return False, "Invalid API key"
            if e.code in (400, 429):
                return True, "Key valid (rate limited or minimal request rejected)"
            return False, f"HTTP {e.code}"
        except Exception as e:
            return False, str(e)
    else:
        headers["Authorization"] = f"Bearer {key}"
        try:
            req = urllib.request.Request(test_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15):
                return True, "Key valid"
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return False, "Invalid API key"
            if e.code == 403:
                return False, "Key forbidden (check permissions)"
            if e.code == 429:
                return True, "Key valid (rate limited)"
            return False, f"HTTP {e.code}"
        except Exception as e:
            return False, str(e)


def _push_key_to_vault(provider: str, key: str) -> bool:
    """Push key to AitherOS via the BYOK endpoint (tenant-scoped) or Secrets vault.

    Tries the proper tenant LLM keys endpoint first (scoped to tenant), falls
    back to raw AitherSecrets (platform-level, owner/fleet boxes with the
    internal secret in env). NEVER fails silently — prints why sync didn't
    happen and how to enable it (owner directive: keys set locally must sync
    to the workspace/tenant in portal.aitherium.com, and errors must guide).
    """
    import httpx

    reasons = []

    # Try 1: Genesis BYOK endpoint (properly scoped to tenant)
    genesis_url = os.environ.get("AITHER_URL", "http://localhost:8001")
    saved = load_saved_config()
    try:
        headers = {"Content-Type": "application/json"}
        if saved.get("tenant_id"):
            headers["X-Tenant-ID"] = saved["tenant_id"]
        if saved.get("api_key"):
            headers["Authorization"] = f"Bearer {saved['api_key']}"
        r = httpx.put(
            f"{genesis_url}/tenants/me/llm-keys/{provider}",
            json={"api_key": key}, headers=headers, timeout=8,
        )
        if r.status_code == 200:
            return True
        reasons.append(f"tenant sync HTTP {r.status_code}"
                       + (" (not logged in — run `aither register` / `adk login`)"
                          if r.status_code in (401, 403) else ""))
    except Exception as e:
        reasons.append(f"tenant endpoint unreachable ({type(e).__name__})")

    # Try 2: Direct AitherSecrets (platform-level; needs the internal secret,
    # present on owner/fleet boxes). Vault is HTTPS with the internal CA.
    info = _KNOWN_PROVIDERS.get(provider, {})
    env_name = info.get("env", f"{provider.upper()}_API_KEY")
    internal = os.environ.get("AITHER_INTERNAL_SECRET") or os.environ.get("AITHER_MASTER_KEY")
    if internal:
        try:
            from adk._tls import tls_verify
            r = httpx.post(
                "https://localhost:8111/secrets",
                json={"name": env_name, "value": key,
                      "secret_type": "api_key", "access_level": "internal"},
                headers={"X-API-Key": internal},
                timeout=8, verify=tls_verify(),
            )
            if r.status_code == 200:
                return True
            reasons.append(f"vault HTTP {r.status_code}")
        except Exception as e:
            reasons.append(f"vault unreachable ({type(e).__name__})")
    else:
        reasons.append("no AITHER_INTERNAL_SECRET in env (platform vault path needs it)")

    print("  NOT synced to AitherOS: " + "; ".join(reasons))
    print("  Key works locally. To sync it to your workspace "
          "(portal.aitherium.com -> Settings -> keys): log in first, then re-run "
          f"`adk keys set {provider} <key>`.")
    return False


def _platform_secret(name: str):
    """Read a PLATFORM-SYSTEM secret as the logged-in owner via the genesis proxy
    (host path). The local keyring is account-scoped only, so a keyring miss
    falls back here. The session bearer is read from disk and never printed."""
    import httpx as _httpx
    url = (os.environ.get("AITHER_SECRETS_URL") or "http://127.0.0.1:8001").rstrip("/")
    bearer = None
    try:
        b = open(os.path.expanduser("~/.aither/session-bearer"), "r", encoding="utf-8").read().strip()
        bearer = b or None
    except Exception:
        pass
    if not bearer:
        return None
    try:
        resp = _httpx.get(
            f"{url}/secrets/{name}",
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        value = data.get("value")
        return value if isinstance(value, str) else None
    except Exception:
        return None


def cmd_secret(args):
    """Manage secrets in encrypted local keyring + sync with AitherSecrets vault."""
    import asyncio as _asyncio

    sub = getattr(args, "secret_command", None)

    if sub == "list":
        from adk.builtin_tools import secret_list
        result = secret_list()
        if result.startswith("{"):
            try:
                import json
                data = json.loads(result)
                if "keys" in data:
                    print(f"\nStored Secrets ({data.get('count', 0)})")
                    print("=" * 50)
                    for key in data["keys"]:
                        print(f"  {key}")
                    return 0
            except Exception:
                pass
        print(result)
        return 0

    elif sub == "get":
        name = getattr(args, "name", "")
        if not name:
            print("Usage: adk secret get <name>")
            return 1
        from adk.builtin_tools import secret_get
        result = secret_get(name)
        # Local keyring miss (the keyring is account-scoped only)? Fall back to
        # the platform vault as the logged-in owner via genesis + session bearer.
        if "not found" in result.lower() or '"error"' in result:
            platform = _platform_secret(name)
            if platform is not None:
                print(platform)
                return 0
        if result.startswith("{"):
            try:
                import json
                data = json.loads(result)
                if "error" in data:
                    print(f"Error: {data['error']}")
                    return 1
            except Exception:
                pass
        print(result)
        return 0

    elif sub == "set":
        name = getattr(args, "name", "")
        value = getattr(args, "value", "")
        if not name or not value:
            print("Usage: adk secret set <name> <value>")
            return 1
        from adk.builtin_tools import secret_set
        result = secret_set(name, value)
        try:
            import json
            data = json.loads(result)
            if data.get("success"):
                print(f"Stored secret '{name}' in encrypted keyring")
                return 0
        except Exception:
            pass
        print(result)
        return 1

    elif sub == "pull":
        secrets_url = getattr(args, "secrets_url", None) or os.environ.get("AITHER_SECRETS_URL")
        gateway_url = getattr(args, "gateway_url", None) or os.environ.get("AITHER_GATEWAY_URL")
        api_key = getattr(args, "api_key", None) or os.environ.get("AITHER_API_KEY")

        from adk.sync.secrets import SecretsSync
        client = SecretsSync(
            api_key=api_key,
            secrets_url=secrets_url,
            gateway_url=gateway_url,
        )

        async def _pull():
            return await client.pull()

        synced = _asyncio.run(_pull())
        print(f"Pulled {len(synced)} secrets from vault")
        for key in synced:
            print(f"  {key}")
        return 0 if synced else 1

    elif sub == "push":
        name = getattr(args, "name", "")
        if not name:
            print("Usage: adk secret push <name>")
            return 1

        from adk.builtin_tools import secret_get
        value = secret_get(name)
        if value.startswith("{"):
            try:
                import json
                data = json.loads(value)
                if "error" in data:
                    print(f"Error: Secret '{name}' not found in local keyring")
                    return 1
            except Exception:
                pass

        secrets_url = getattr(args, "secrets_url", None) or os.environ.get("AITHER_SECRETS_URL")
        gateway_url = getattr(args, "gateway_url", None) or os.environ.get("AITHER_GATEWAY_URL")
        api_key = getattr(args, "api_key", None) or os.environ.get("AITHER_API_KEY")

        if not (secrets_url or gateway_url) or not api_key:
            print("Error: --secrets-url/--gateway-url and --api-key required")
            print("  Or set: AITHER_SECRETS_URL, AITHER_GATEWAY_URL, AITHER_API_KEY")
            return 1

        from adk.sync.secrets import SecretsSync
        client = SecretsSync(
            api_key=api_key,
            secrets_url=secrets_url,
            gateway_url=gateway_url,
        )

        async def _push():
            return await client.push(name, value)

        success = _asyncio.run(_push())
        if success:
            print(f"Pushed secret '{name}' to vault")
            return 0
        else:
            print(f"Failed to push secret '{name}'")
            return 1

    elif sub == "sync":
        secrets_url = getattr(args, "secrets_url", None) or os.environ.get("AITHER_SECRETS_URL")
        gateway_url = getattr(args, "gateway_url", None) or os.environ.get("AITHER_GATEWAY_URL")
        api_key = getattr(args, "api_key", None) or os.environ.get("AITHER_API_KEY")

        from adk.sync.secrets import SecretsSync
        client = SecretsSync(
            api_key=api_key,
            secrets_url=secrets_url,
            gateway_url=gateway_url,
        )

        async def _sync():
            return await client.sync()

        synced = _asyncio.run(_sync())
        print(f"Synced {len(synced)} secrets total")
        for key in synced:
            print(f"  {key}")
        return 0

    else:
        print("Usage: adk secret [list|get|set|pull|push|sync]")
        return 1


def cmd_voice(args):
    """Standalone HTTP voice server for AitherShell."""
    sub = getattr(args, "voice_command", None)

    if sub == "serve":
        from adk.voice_http import run

        port = getattr(args, "port", None)
        host = getattr(args, "host", "127.0.0.1")

        try:
            run(host=host, port=port)
        except KeyboardInterrupt:
            print("\nVoice server stopped.")
            return 0
        except Exception as exc:
            print(f"Error: {exc}")
            return 1
    else:
        print("Usage: adk voice serve [--port N] [--host ADDR]")
        return 1


def cmd_keys(args):
    """Manage cloud provider API keys."""

    sub = getattr(args, "keys_command", None)

    if sub == "set":
        provider = getattr(args, "provider", "").lower()
        key = getattr(args, "key", "") or ""
        if not provider:
            print("Usage: adk keys set <provider> [key]")
            return 1
        if provider not in _KNOWN_PROVIDERS:
            # Accept aliases/model names (kimi → moonshot, claude → anthropic, …)
            try:
                from adk.llm.onboarding import resolve_name
                resolved = resolve_name(provider)
            except Exception:
                resolved = None
            if resolved in _KNOWN_PROVIDERS:
                provider = resolved
            else:
                print(f"Unknown provider: {provider}")
                print(f"Known: {', '.join(sorted(_KNOWN_PROVIDERS))}")
                return 1
        if not key:
            # Guided onboarding: no key on the command line → show where to get
            # one, then prompt with hidden input (keeps the key out of shell
            # history). Empty input or no TTY → guidance only, no dead end.
            try:
                from adk.llm.onboarding import guide
                print(guide(provider))
            except Exception:
                pass
            import getpass
            import sys as _sys
            if _sys.stdin is not None and _sys.stdin.isatty():
                try:
                    key = getpass.getpass(
                        f"Paste your {_KNOWN_PROVIDERS[provider]['label']} API key "
                        "(input hidden, Enter to cancel): "
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    key = ""
            if not key:
                print(f"\nNo key entered. When you have one:  adk keys set {provider} <key>")
                return 1

        keys = _load_provider_keys()
        keys[provider] = key
        _save_provider_keys(keys)

        # Set env var for current session
        env_name = _KNOWN_PROVIDERS[provider]["env"]
        os.environ[env_name] = key
        print(f"  Saved {_KNOWN_PROVIDERS[provider]['label']} key: {_mask_key(key)}")

        # Try to push to vault
        if _push_key_to_vault(provider, key):
            print("  Synced to AitherSecrets vault")
        return 0

    elif sub == "pull":
        # Two-way sync, DOWN direction. Key VALUES never leave the vault (by
        # design) — this pulls per-provider STATUS from the tenant BYOK
        # endpoint so `adk keys list` and agents agree with the portal.
        import httpx
        genesis_url = os.environ.get("AITHER_URL", "http://localhost:8001")
        saved = load_saved_config()
        headers = {}
        if saved.get("tenant_id"):
            headers["X-Tenant-ID"] = saved["tenant_id"]
        if saved.get("api_key"):
            headers["Authorization"] = f"Bearer {saved['api_key']}"
        try:
            r = httpx.get(f"{genesis_url}/tenants/me/llm-keys", headers=headers, timeout=10)
        except Exception as e:
            print(f"Workspace unreachable ({type(e).__name__}). Is the fleet up / are you logged in?")
            return 1
        if r.status_code in (401, 403):
            print("Not authenticated — run `aither register` / `adk login`, then retry `adk keys pull`.")
            return 1
        if r.status_code != 200:
            print(f"Workspace sync failed: HTTP {r.status_code}")
            return 1
        data = r.json()
        print(f"\nWorkspace provider keys (tenant: {data.get('tenant_id', '?')})")
        print("=" * 55)
        for p in data.get("providers", []):
            mark = "[+]" if p.get("has_key") else "[-]"
            src = f" ({p['source']})" if p.get("has_key") else ""
            print(f"  {mark} {p.get('display_name', p.get('provider')):18s}{src}")
        print("\nValues stay in the vault; agents resolve them server-side.")
        return 0

    elif sub == "list":
        keys = _load_provider_keys()
        print("\nCloud Provider API Keys")
        print("=" * 55)
        for pname, info in sorted(_KNOWN_PROVIDERS.items()):
            key = keys.get(pname, "") or os.environ.get(info["env"], "")
            if key:
                status = f"{_mask_key(key)}"
                print(f"  [+] {info['label']:15s} {status}")
            else:
                print(f"  [-] {info['label']:15s} not configured")
        configured = sum(1 for p in _KNOWN_PROVIDERS if keys.get(p) or os.environ.get(_KNOWN_PROVIDERS[p]["env"]))
        print(f"\n  {configured}/{len(_KNOWN_PROVIDERS)} providers configured")
        return 0

    elif sub == "test":
        provider = getattr(args, "provider", None)
        keys = _load_provider_keys()

        providers_to_test = [provider] if provider else list(_KNOWN_PROVIDERS.keys())
        print("\nTesting API Keys")
        print("=" * 50)

        for pname in providers_to_test:
            if pname not in _KNOWN_PROVIDERS:
                print(f"  [?] {pname}: unknown provider")
                continue
            info = _KNOWN_PROVIDERS[pname]
            key = keys.get(pname, "") or os.environ.get(info["env"], "")
            if not key:
                print(f"  [-] {info['label']:15s} no key configured")
                continue
            ok, msg = _test_provider_key(pname, key)
            icon = "+" if ok else "x"
            print(f"  [{icon}] {info['label']:15s} {msg}")
        return 0

    elif sub == "remove":
        provider = getattr(args, "provider", "").lower()
        if not provider:
            print("Usage: adk keys remove <provider>")
            return 1
        keys = _load_provider_keys()
        if provider in keys:
            del keys[provider]
            _save_provider_keys(keys)
            print(f"  Removed {provider} key")
        else:
            print(f"  No key stored for {provider}")
        return 0

    else:
        # Interactive mode — prompt for each provider
        print("\n  Cloud Provider API Key Setup")
        print("  " + "=" * 40)
        print("  Enter API keys for your cloud providers.")
        print("  Press Enter to skip a provider.\n")

        keys = _load_provider_keys()
        changed = False

        for pname, info in _KNOWN_PROVIDERS.items():
            existing = keys.get(pname, "") or os.environ.get(info["env"], "")
            if existing:
                status = f"(configured: {_mask_key(existing)})"
            else:
                status = "(not set)"

            try:
                val = input(f"  {info['label']} API key {status}: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if val:
                keys[pname] = val
                os.environ[info["env"]] = val
                changed = True
                ok, msg = _test_provider_key(pname, val)
                icon = "+" if ok else "x"
                print(f"    [{icon}] {msg}")
                if _push_key_to_vault(pname, val):
                    print("    Synced to vault")

        if changed:
            _save_provider_keys(keys)
            print(f"\n  Keys saved to {_keys_path()}")

        configured = sum(1 for p in _KNOWN_PROVIDERS if keys.get(p) or os.environ.get(_KNOWN_PROVIDERS[p]["env"]))
        print(f"\n  {configured}/{len(_KNOWN_PROVIDERS)} providers ready")
        return 0


# ── Phase 3: aither routing — inference routing management ─────────────────

def _routing_config_path() -> Path:
    """Find inference_routing.yaml — local ~/.aither/ first, then AitherOS config."""
    local = Path.home() / ".aither" / "inference_routing.yaml"
    if local.exists():
        return local
    # Check AitherOS config
    aitheros = Path(os.environ.get("AITHER_ROOT", "")) / "config" / "inference_routing.yaml"
    if aitheros.exists():
        return aitheros
    # Check relative to this file (ADK might be inside repo)
    for candidate in [
        Path(__file__).resolve().parents[2] / "AitherOS" / "config" / "inference_routing.yaml",
        Path.cwd() / "AitherOS" / "config" / "inference_routing.yaml",
        Path.cwd() / "config" / "inference_routing.yaml",
    ]:
        if candidate.exists():
            return candidate
    return local  # default write location


def _load_routing_config() -> dict:
    import yaml
    p = _routing_config_path()
    if p.exists():
        try:
            with open(p) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {"enabled": False, "intent_routing": {}, "presets": {}}


def _save_routing_config(cfg: dict) -> None:
    import yaml
    p = _routing_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)


# Map short intent aliases to config keys
_INTENT_ALIASES = {
    "code": "code_generation",
    "coding": "code_generation",
    "review": "code_review",
    "reasoning": "deep_analysis",
    "analysis": "deep_analysis",
    "planning": "complex_planning",
    "chat": "casual_chat",
    "question": "simple_question",
    "research": "research",
    "search": "web_search",
}

# Map short provider aliases
_PROVIDER_ALIASES = {
    "claude": "anthropic",
    "gpt": "openai",
    "ds": "deepseek",
    "local": "local",
}


def cmd_mesh(args) -> int:
    """Manage AitherMesh overlay and A2A operations."""
    import asyncio

    sub = getattr(args, "mesh_command", None)

    if sub == "onboard":
        async def _onboard():
            from adk.mesh import join, _resolve_conductor_url

            conductor = getattr(args, "conductor", None)
            if not conductor:
                print("ERROR: --conductor required (or set AITHER_CONDUCTOR_URL)")
                return 1

            # Resolve internal hostname fallback to public endpoint
            conductor = _resolve_conductor_url(conductor)
            node_id = getattr(args, "node_id", "") or None
            role = getattr(args, "role", "worker")
            external_ip = getattr(args, "external_ip", "") or None
            headscale = getattr(args, "headscale", False)

            try:
                report = await join(
                    conductor_url=conductor,
                    node_id=node_id,
                    role=role,
                    external_ip=external_ip,
                    headscale=headscale,
                )
                print("\n  Mesh Onboard Complete")
                print("  " + "=" * 50)
                print(f"  Node ID:      {report.get('node_id', 'unknown')}")
                print(f"  Overlay IP:   {report.get('overlay_ip', 'unknown')}")
                print(f"  Transport:    {report.get('transport', 'unknown')}")
                if report.get("transport") == "wireguard":
                    print(f"  Interface:    {report.get('iface', 'unknown')}")
                    print(f"  Handshake:    {'OK' if report.get('handshake') else 'pending'}")
                elif report.get("transport") == "headscale":
                    print(f"  Hostname:     {report.get('hostname', 'unknown')}")
                print()
                return 0
            except Exception as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 1

        return asyncio.run(_onboard())

    elif sub == "ls":
        async def _ls():
            import json

            mesh_url = getattr(args, "mesh_url", None)
            fmt = getattr(args, "format", "table")

            if not mesh_url:
                print("ERROR: --mesh-url required (or set AITHER_MESH_URL)")
                return 1

            # A container-less remote node can't resolve the internal
            # 'aitheros-aithernet' name — the peer directory is reached over the
            # WireGuard overlay via mesh-core, not a public host. Degrade with a
            # clear instruction instead of a raw DNS crash.
            from adk.mesh import _resolve_mesh_url
            resolved = _resolve_mesh_url(mesh_url)
            if resolved is None:
                print(
                    "ERROR: cannot reach the mesh directory at "
                    f"'{mesh_url}' (internal name doesn't resolve from this box).\n"
                    "  The peer directory is served over the WireGuard overlay by "
                    "mesh-core, not a public endpoint. Either:\n"
                    "    1. run 'adk mesh onboard' first to join the overlay, or\n"
                    "    2. pass --mesh-url https://<mesh-core-overlay-ip>:8125 "
                    "(or set AITHER_AITHERNET_URL/AITHER_MESH_URL).")
                return 1
            mesh_url = resolved

            try:
                import httpx
                from adk.mesh import _headers, _verify

                # Query AitherMesh /nodes endpoint for peer directory
                url = mesh_url.rstrip("/") + "/nodes"
                async with httpx.AsyncClient(timeout=10.0, verify=_verify()) as c:
                    r = await c.get(url, headers=_headers(None))
                    r.raise_for_status()
                    nodes = r.json() if r.headers.get("content-type", "").startswith("application/json") else []

                if not isinstance(nodes, list):
                    nodes = nodes.get("nodes", []) if isinstance(nodes, dict) else []

                if fmt == "json":
                    print(json.dumps(nodes, indent=2))
                else:
                    # Table format
                    print("\n  Mesh Peers")
                    print("  " + "=" * 80)
                    if not nodes:
                        print("  (no peers connected)")
                    else:
                        print(f"  {'Node ID':<20} {'Overlay IP':<16} {'Role':<12} {'A2A Services':<20}")
                        print("  " + "-" * 80)
                        for node in nodes:
                            node_id = node.get("node_id", "unknown")[:20]
                            overlay_ip = node.get("overlay_ip", node.get("aithernet_ip", "?"))
                            role = node.get("role", "?")[:12]
                            services = ", ".join(node.get("services", [])[:2])
                            print(f"  {node_id:<20} {overlay_ip:<16} {role:<12} {services:<20}")
                    print()
                return 0
            except Exception as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 1

        return asyncio.run(_ls())

    elif sub == "provide":
        async def _provide():
            from adk.mesh_provider import provide

            inference_url = getattr(args, "inference_url", "").strip()
            model = getattr(args, "model", "").strip()
            peer_id = getattr(args, "peer_id", "").strip() or None
            tenant_id = getattr(args, "tenant_id", "").strip() or None
            wait = getattr(args, "wait", 30)
            strata_url = getattr(args, "strata_url", "").strip() or None
            conductor_url = getattr(args, "conductor_url", "").strip() or None
            auth_token = getattr(args, "auth_token", "").strip() or None

            if not inference_url or not model:
                print("ERROR: --inference-url and --model are required")
                return 1

            try:
                report = await provide(
                    inference_url=inference_url,
                    inference_model=model,
                    peer_id=peer_id,
                    tenant_id=tenant_id,
                    wait_seconds=wait,
                    strata_url=strata_url,
                    conductor_url=conductor_url,
                    auth_token=auth_token,
                )

                # Print report
                print("\n  AitherNet Provider Setup")
                print("  " + "=" * 60)

                for step in report.get("steps", []):
                    step_name = step.get("step", "unknown").upper()
                    if step.get("ok"):
                        print(f"  [OK] {step_name}")
                    else:
                        print(f"  [FAIL] {step_name}: {step.get('error', 'unknown error')}")

                if report.get("message"):
                    print(f"\n  {report['message']}")

                if report.get("next_action"):
                    print(f"\n  Next Step:\n{report['next_action']}")

                print()
                return 0 if report.get("ok") else 1
            except Exception as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 1

        return asyncio.run(_provide())

    elif sub == "serve":
        async def _serve():
            from adk import mesh_serve

            role = getattr(args, "role", "plan")
            dry_run = not getattr(args, "execute", False)
            try:
                if role == "plan":
                    plan = mesh_serve.serve_plan(
                        getattr(args, "nodes", ""), quant=getattr(args, "quant", "auto")
                    )
                    print("\n  Kimi-K3 Mesh Split Plan")
                    print("  " + "=" * 60)
                    print(f"  Quant:        {plan['quant']} "
                          f"({plan['download_gb']}GB download)")
                    print(f"  Pool:         {plan['pool_total_gb']:.0f}GB "
                          f"(need {plan['required_gb']}GB)")
                    def _nid(n):
                        # plan entries are NodeBudget objects (or dicts in tests)
                        if isinstance(n, dict):
                            return n.get("node_id", "?"), n.get("host", "?")
                        return getattr(n, "node_id", "?"), getattr(n, "host", "?")

                    cid, chost = _nid(plan["coordinator"])
                    print(f"  Coordinator:  {cid} ({chost})")
                    for backend in plan["rpc_backends"]:
                        bid, bhost = _nid(backend)
                        print(f"  RPC backend:  {bid} ({bhost})")
                    for skipped in plan.get("skipped_nodes", []):
                        sid, _ = _nid(skipped)
                        print(f"  Skipped:      {sid}")
                    print(f"  Tensor split: {plan['tensor_split']}")
                    print(f"\n  {plan['note']}\n")
                    return 0
                if role == "rpc-backend":
                    report = mesh_serve.serve_rpc_backend(
                        bind=getattr(args, "bind", ""),
                        build_dir=getattr(args, "build_dir", "unsloth-llamacpp"),
                        dry_run=dry_run,
                    )
                elif role == "coordinator":
                    report = await mesh_serve.serve_coordinator(
                        nodes_spec=getattr(args, "nodes", ""),
                        backends=getattr(args, "backends", ""),
                        quant=getattr(args, "quant", "auto"),
                        model_dir=getattr(args, "model_dir", "kimi-k3-model"),
                        build_dir=getattr(args, "build_dir", "unsloth-llamacpp"),
                        bind=getattr(args, "bind", "") or "127.0.0.1",
                        dry_run=dry_run,
                        advertise=not getattr(args, "no_advertise", False),
                        tenant_id=getattr(args, "tenant_id", "").strip() or None,
                    )
                else:
                    print(f"ERROR: unknown role {role}")
                    return 1
                print(json.dumps(report, indent=2, default=str))
                if dry_run:
                    print("\n  (dry-run — re-run with --execute to act)")
                return 0
            except (ValueError, RuntimeError, NotImplementedError) as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 1

        return asyncio.run(_serve())

    elif sub == "federation-token":
        async def _fed_token():
            from adk.mesh_provider import _get_auth_token, _resolve_peer_id, federation_token

            peer_id = getattr(args, "peer_id", "").strip() or None
            conductor_url = getattr(args, "conductor_url", "").strip() or None
            auth_token = getattr(args, "auth_token", "").strip() or _get_auth_token()

            if not peer_id:
                try:
                    peer_id = await _resolve_peer_id()
                except RuntimeError as e:
                    print(f"ERROR: {e}", file=sys.stderr)
                    return 1
            try:
                kwargs = {"peer_id": peer_id, "auth_token": auth_token}
                if conductor_url:
                    kwargs["conductor_url"] = conductor_url
                result = await federation_token(**kwargs)
                if result.get("ok"):
                    print("  [OK] federation node token minted (shown ONCE — store it now):")
                    print(f"       AITHER_NODE_TOKEN={result.get('node_token', '')}")
                    print(f"       AITHERNET_NODE_SLUG={result.get('node_slug', peer_id)}")
                    print(f"       expires in {result.get('expires_in_days')} days")
                    return 0
                print(f"  [FAIL] {result.get('error', 'unknown error')}")
                return 1
            except Exception as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 1

        return asyncio.run(_fed_token())

    elif sub == "leave":
        async def _leave():
            from adk.mesh_provider import _get_auth_token, _resolve_peer_id, leave_pool

            peer_id = getattr(args, "peer_id", "").strip() or None
            conductor_url = getattr(args, "conductor_url", "").strip() or None
            auth_token = getattr(args, "auth_token", "").strip() or _get_auth_token()

            if not peer_id:
                try:
                    peer_id = await _resolve_peer_id()
                except RuntimeError as e:
                    print(f"ERROR: {e}", file=sys.stderr)
                    return 1
            try:
                kwargs = {"peer_id": peer_id, "auth_token": auth_token}
                if conductor_url:
                    kwargs["conductor_url"] = conductor_url
                result = await leave_pool(**kwargs)
                if result.get("ok"):
                    print(f"  [OK] left the community pool "
                          f"(backend {result.get('backend_name', '')} drained)")
                    return 0
                print(f"  [FAIL] {result.get('error', 'unknown error')}")
                return 1
            except Exception as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 1

        return asyncio.run(_leave())

    elif sub == "flux-node":
        async def _flux_node():
            from adk.mesh_provider import flux_node

            flux_image = getattr(args, "flux_image", "").strip() \
                or "ghcr.io/aitherium/mesh-agent:latest"
            flux_port = getattr(args, "flux_port", 8117)
            mesh_src = getattr(args, "mesh_src", "").strip() \
                or "/opt/aitheros/mesh-src"
            node_id = getattr(args, "node_id", "").strip() or None
            aither_internal_secret = getattr(args, "aither_internal_secret",
                                             "").strip() or None

            try:
                result = await flux_node(
                    flux_image=flux_image,
                    flux_port=flux_port,
                    mesh_src=mesh_src,
                    node_id=node_id,
                    aither_internal_secret=aither_internal_secret,
                )

                print("\n  Flux Event-Plane Listener")
                print("  " + "=" * 60)

                if result.get("ok"):
                    print(f"  [OK] {result['message']}")
                    print(f"  Container: {result.get('container', 'aither-flux')}")
                    print(f"  Port:      {result.get('port', flux_port)}")
                    print(f"  Node ID:   {result.get('node_id', node_id)}")
                    if result.get("output"):
                        print("\n  Startup output:")
                        for line in result["output"].split("\n"):
                            print(f"    {line}")
                    print()
                    return 0
                else:
                    print(f"  [FAIL] {result.get('error', 'unknown error')}")
                    if result.get("details"):
                        print(f"\n  Details:\n{result['details']}")
                    print()
                    return 1
            except Exception as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 1

        return asyncio.run(_flux_node())

    elif sub == "create":
        async def _create():
            import json as _json
            import httpx
            from adk.mesh import _resolve_conductor_url, _verify

            name = getattr(args, "name", "").strip()
            tenant_id = getattr(args, "tenant_id", "").strip()
            discoverable = bool(getattr(args, "discoverable", False))
            federation_role = getattr(args, "federation_role", "standalone")
            conductor = getattr(args, "conductor_url", "").strip() or os.getenv("AITHER_CONDUCTOR_URL", "")
            auth_token = getattr(args, "auth_token", "").strip()

            if not name:
                print("ERROR: --name required")
                return 1
            if not conductor:
                print("ERROR: --conductor-url required (or set AITHER_CONDUCTOR_URL)")
                return 1
            # Bearer resolution: flag -> env -> ~/.aither/config.json (never fabricate).
            if not auth_token:
                auth_token = os.getenv("AITHER_AUTH_TOKEN", "").strip()
            if not auth_token:
                try:
                    cfg = os.path.expanduser("~/.aither/config.json")
                    if os.path.exists(cfg):
                        with open(cfg) as f:
                            auth_token = (_json.load(f).get("auth_token") or "").strip()
                except Exception:  # noqa: BLE001
                    auth_token = ""
            if not auth_token:
                print("ERROR: no auth token — a mesh is created under YOUR authenticated tenant, "
                      "never anonymously. Set AITHER_AUTH_TOKEN, pass --auth-token, or run 'adk auth'.")
                return 1
            if not tenant_id:
                print("ERROR: --tenant-id required (or set AITHER_TENANT_ID) — verified against "
                      "your token (fail-closed; you can only create under your own tenant).")
                return 1

            conductor = _resolve_conductor_url(conductor)
            payload = {"tenant_id": tenant_id, "name": name,
                       "discoverable": discoverable, "federation_role": federation_role}
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {auth_token}"}
            try:
                async with httpx.AsyncClient(verify=_verify()) as client:
                    r = await client.post(f"{conductor}/v1/mesh/create", json=payload,
                                          headers=headers, timeout=45.0)
            except Exception as e:
                print(f"ERROR: could not reach conductor at {conductor}: {e}", file=sys.stderr)
                return 1
            if r.status_code != 200:
                print(f"ERROR: mesh create failed ({r.status_code}): {r.text[:300]}", file=sys.stderr)
                return 1
            rep = r.json()
            print("\n  AitherNet Mesh Created")
            print("  " + "=" * 55)
            print(f"  Mesh ID:       {rep.get('mesh_id','')}")
            print(f"  Name:          {rep.get('name','')}")
            print(f"  Tenant:        {rep.get('tenant_id','')}")
            print(f"  Overlay CIDR:  {rep.get('cidr','')}")
            print(f"  Headscale key: {'issued' if rep.get('headscale_key_issued') else 'not issued'}")
            print(f"  Discoverable:  {rep.get('discoverable', False)}")
            print(f"  ACL entry:     {'applied' if rep.get('acl_tenant_entry_applied') else 'skipped'}")
            for step in rep.get("next_steps", []):
                print(f"    -> {step}")
            print()
            return 0

        return asyncio.run(_create())

    elif sub == "link":
        async def _link():
            import json as _json
            import httpx
            from adk.mesh import _resolve_conductor_url, _verify

            action = getattr(args, "link_action", "") or ""
            tenant_id = getattr(args, "tenant_id", "").strip()
            conductor = getattr(args, "conductor_url", "").strip() or os.getenv("AITHER_CONDUCTOR_URL", "")
            auth_token = getattr(args, "auth_token", "").strip()
            if not conductor:
                print("ERROR: --conductor-url required (or set AITHER_CONDUCTOR_URL)")
                return 1
            # Bearer resolution: flag -> env -> ~/.aither/config.json (never fabricate).
            if not auth_token:
                auth_token = os.getenv("AITHER_AUTH_TOKEN", "").strip()
            if not auth_token:
                try:
                    cfg = os.path.expanduser("~/.aither/config.json")
                    if os.path.exists(cfg):
                        with open(cfg) as f:
                            auth_token = (_json.load(f).get("auth_token") or "").strip()
                except Exception:  # noqa: BLE001
                    auth_token = ""
            if not auth_token:
                print("ERROR: no auth token — mesh links act under YOUR authenticated tenant. "
                      "Set AITHER_AUTH_TOKEN, pass --auth-token, or run 'adk auth'.")
                return 1
            if not tenant_id:
                print("ERROR: --tenant-id required (or set AITHER_TENANT_ID).")
                return 1

            conductor = _resolve_conductor_url(conductor)
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {auth_token}"}
            try:
                async with httpx.AsyncClient(verify=_verify()) as client:
                    if action == "request":
                        src = getattr(args, "source_mesh", "").strip()
                        tgt = getattr(args, "target_mesh", "").strip()
                        if not src or not tgt:
                            print("ERROR: --source-mesh and --target-mesh required")
                            return 1
                        r = await client.post(
                            f"{conductor}/v1/mesh/link/request",
                            json={"tenant_id": tenant_id, "source_mesh": src, "target_mesh": tgt},
                            headers=headers, timeout=45.0)
                    elif action == "approve":
                        lid = getattr(args, "link_id", "").strip()
                        if not lid:
                            print("ERROR: --link-id required")
                            return 1
                        r = await client.post(
                            f"{conductor}/v1/mesh/link/approve",
                            json={"tenant_id": tenant_id, "link_id": lid},
                            headers=headers, timeout=45.0)
                    elif action == "revoke":
                        lid = getattr(args, "link_id", "").strip()
                        if not lid:
                            print("ERROR: --link-id required")
                            return 1
                        r = await client.post(
                            f"{conductor}/v1/mesh/link/revoke",
                            json={"tenant_id": tenant_id, "link_id": lid},
                            headers=headers, timeout=45.0)
                    elif action == "list":
                        r = await client.get(
                            f"{conductor}/v1/mesh/link/list",
                            params={"tenant_id": tenant_id}, headers=headers, timeout=45.0)
                    else:
                        print("Usage: adk mesh link [request|approve|revoke|list]")
                        return 1
            except Exception as e:
                print(f"ERROR: could not reach conductor at {conductor}: {e}", file=sys.stderr)
                return 1
            if r.status_code != 200:
                print(f"ERROR: mesh link {action} failed ({r.status_code}): {r.text[:300]}", file=sys.stderr)
                return 1
            rep = r.json()
            if action == "list":
                links = rep.get("links", [])
                print(f"\n  Mesh Links ({len(links)})")
                print("  " + "=" * 55)
                for lk in links:
                    print(f"  {lk.get('link_id','')}  {lk.get('status','')}  "
                          f"{lk.get('source_mesh','')} -> {lk.get('target_mesh','')}")
                print()
            else:
                print(f"\n  Mesh link {action}: {rep.get('status', rep)}")
                if rep.get("link_id"):
                    print(f"  Link ID: {rep.get('link_id')}")
                print()
            return 0

        return asyncio.run(_link())

    else:
        print("Usage: adk mesh [onboard|ls|provide|create|link]")
        return 1


def cmd_routing(args):
    """Manage per-intent model routing."""
    sub = getattr(args, "routing_command", None)

    if sub == "preset":
        preset_name = getattr(args, "preset_name", "")
        if not preset_name:
            print("Usage: adk routing preset <budget|balanced|quality>")
            return 1

        cfg = _load_routing_config()
        presets = cfg.get("presets", {})
        if preset_name not in presets:
            print(f"Unknown preset: {preset_name}")
            print(f"Available: {', '.join(presets.keys())}")
            return 1

        preset = presets[preset_name]
        desc = preset.get("description", "")
        print(f"\n  Applying preset: {preset_name}")
        if desc:
            print(f"  {desc}")

        # Apply preset to intent routing
        routing = cfg.get("intent_routing", {})
        dp = preset.get("default_provider", "local")
        df = preset.get("default_fallback", "")
        rp = preset.get("reasoning_provider", "")
        rm = preset.get("reasoning_model", "")

        # Update quick intents to use default provider
        for intent in ("casual_chat", "simple_question", "web_search"):
            if intent not in routing:
                routing[intent] = {}
            routing[intent]["provider"] = dp
            if df:
                routing[intent]["fallback_provider"] = df

        # Update reasoning intents
        for intent in ("deep_analysis", "complex_planning"):
            if rp:
                if intent not in routing:
                    routing[intent] = {}
                routing[intent]["provider"] = rp
                if rm:
                    routing[intent]["model"] = rm

        cfg["intent_routing"] = routing
        cfg["enabled"] = True
        _save_routing_config(cfg)

        # Also save to ADK config for backend selection
        save_data = {"routing_preset": preset_name}
        if rp and rm:
            save_data["reasoning_backend"] = rp
            save_data["reasoning_model"] = rm
        save_saved_config(save_data)

        print(f"  Routing preset '{preset_name}' applied and enabled")
        return 0

    elif sub == "set":
        intent_alias = getattr(args, "intent", "")
        provider_alias = getattr(args, "provider", "")
        model = getattr(args, "model", None)
        if not intent_alias or not provider_alias:
            print("Usage: adk routing set <intent> <provider> [--model MODEL]")
            return 1

        intent_key = _INTENT_ALIASES.get(intent_alias, intent_alias)
        provider_key = _PROVIDER_ALIASES.get(provider_alias, provider_alias)

        cfg = _load_routing_config()
        routing = cfg.get("intent_routing", {})
        if intent_key not in routing:
            routing[intent_key] = {}
        routing[intent_key]["provider"] = provider_key
        if model:
            routing[intent_key]["model"] = model

        cfg["intent_routing"] = routing
        cfg["enabled"] = True
        _save_routing_config(cfg)
        print(f"  {intent_key} -> {provider_key}" + (f" ({model})" if model else ""))
        return 0

    elif sub == "reset":
        cfg = _load_routing_config()
        cfg["enabled"] = False
        _save_routing_config(cfg)
        print("  Intent routing disabled — using effort-based defaults")
        return 0

    else:
        # Show current routing
        cfg = _load_routing_config()
        enabled = cfg.get("enabled", False)
        routing = cfg.get("intent_routing", {})

        print(f"\nInference Routing {'(ACTIVE)' if enabled else '(disabled — effort-based)'}")
        print("=" * 55)

        if not routing:
            print("  No intent overrides configured.")
            print("  Use 'adk routing preset balanced' to get started.")
        else:
            for intent, config in sorted(routing.items()):
                provider = config.get("provider", "?")
                model = config.get("model", "")
                fb = config.get("fallback_provider", "")
                line = f"  {intent:25s} -> {provider}"
                if model:
                    line += f" ({model})"
                if fb:
                    line += f"  [fallback: {fb}]"
                print(line)

        presets = cfg.get("presets", {})
        if presets:
            print(f"\n  Presets: {', '.join(presets.keys())}")
        print(f"\n  Config: {_routing_config_path()}")
        return 0


# ── Phase 3.5: adk grid — manage grid distributed infrastructure ──────────

_GRID_CONFIG_STRATA_PATH = "grid/config.json"


def cmd_grid(args) -> int:
    """Manage grid distributed inference nodes."""
    import asyncio

    sub = getattr(args, "grid_command", None)
    saved = load_saved_config()

    # Grid config lives under a "grid_nodes" key in ~/.aither/config.json
    grid_nodes = saved.get("grid_nodes", {})
    # grid_nodes = { "reasoning": {"host": "...", "port": 8121, "model": "..."},
    #                "cluster": [{"host": "...", "port": 8121, "model": "..."}] }

    if sub == "status" or sub is None:
        print()
        print("  Grid Topology")
        print("  " + "=" * 55)

        # Primary (local GPU)
        backend = saved.get("backend", "")
        base_url = saved.get("base_url", "")
        model = saved.get("model", "")
        if backend:
            print(f"  [GPU]     {base_url or 'auto-detect':30s}  {model or 'auto'}")
        else:
            print("  [GPU]     not configured (run: adk deploy grid)")

        # Reasoning node
        r_node = grid_nodes.get("reasoning")
        r_url = saved.get("reasoning_url", "")
        r_model = saved.get("reasoning_model", "")
        if r_node:
            r_display = f"{r_node['host']}:{r_node.get('port', 8121)}"
            print(f"  [reason]  {r_display:30s}  {r_node.get('model', r_model or 'auto')}")
        elif r_url:
            print(f"  [reason]  {r_url:30s}  {r_model or 'auto'}")
        else:
            print("  [reason]  not configured (run: adk grid add reasoning <ip>)")

        # Cluster nodes
        c_nodes = grid_nodes.get("cluster", [])
        c_url = saved.get("cluster_url", "")
        c_model = saved.get("cluster_model", "")
        if c_nodes:
            for i, node in enumerate(c_nodes):
                c_display = f"{node['host']}:{node.get('port', 8121)}"
                print(f"  [cpu.{i}]   {c_display:30s}  {node.get('model', c_model or 'auto')}")
        elif c_url:
            print(f"  [cpu.0]   {c_url:30s}  {c_model or 'auto'}")
        else:
            print("  [cpu]     not configured (run: adk grid add cluster <ip>)")

        # Auth status
        print()
        api_key = saved.get("api_key", "")
        tenant = saved.get("tenant_id", "")
        username = saved.get("username", "")
        if api_key:
            print(f"  Auth:     {username or 'logged in'} (tenant: {tenant or 'default'})")
            print("  Sync:     adk grid sync → portal.aitherium.com")
        else:
            print("  Account:  none (everything works locally without one)")
            print("  Optional: adk login → free account, enables config sync across machines")
            print("            https://portal.aitherium.com/signup")

        # Health check all nodes
        print()
        print("  Health")
        print("  " + "-" * 55)
        _grid_health_check(saved, grid_nodes)

        print()
        return 0

    elif sub == "add":
        role = args.role
        host = args.host
        port = getattr(args, "port", 8121) or 8121
        model_override = getattr(args, "model", None)

        node_entry = {"host": host, "port": port}
        if model_override:
            node_entry["model"] = model_override

        if role == "reasoning":
            grid_nodes["reasoning"] = node_entry
            # Also update the flat config for LLM router
            update = {
                "reasoning_backend": "openai",
                "reasoning_url": f"http://{host}:{port}/v1",
                "reasoning_model": model_override or "deepseek-r1-8b",
                "grid_nodes": grid_nodes,
            }
        else:  # cluster
            cluster_list = grid_nodes.get("cluster", [])
            # Deduplicate by host
            cluster_list = [n for n in cluster_list if n["host"] != host]
            cluster_list.append(node_entry)
            grid_nodes["cluster"] = cluster_list
            # Use the first cluster node for routing
            first = cluster_list[0]
            update = {
                "cluster_backend": "openai",
                "cluster_url": f"http://{first['host']}:{first.get('port', 8121)}/v1",
                "cluster_model": model_override or "qwen2.5-32b",
                "grid_nodes": grid_nodes,
            }

        save_saved_config(update)
        print(f"  Added {role} node: {host}:{port}")

        # Quick health check
        _grid_test_node(host, port)

        print("\n  Config saved to ~/.aither/config.json")
        print("  Sync to cloud: adk grid sync")
        return 0

    elif sub == "remove":
        host = args.host
        removed = False

        r_node = grid_nodes.get("reasoning")
        if r_node and r_node.get("host") == host:
            del grid_nodes["reasoning"]
            save_saved_config({
                "reasoning_backend": "",
                "reasoning_url": "",
                "reasoning_model": "",
                "grid_nodes": grid_nodes,
            })
            removed = True
            print(f"  Removed reasoning node: {host}")

        c_nodes = grid_nodes.get("cluster", [])
        new_cluster = [n for n in c_nodes if n.get("host") != host]
        if len(new_cluster) < len(c_nodes):
            grid_nodes["cluster"] = new_cluster
            update = {"grid_nodes": grid_nodes}
            if new_cluster:
                first = new_cluster[0]
                update["cluster_url"] = f"http://{first['host']}:{first.get('port', 8121)}/v1"
            else:
                update["cluster_backend"] = ""
                update["cluster_url"] = ""
                update["cluster_model"] = ""
            save_saved_config(update)
            removed = True
            print(f"  Removed cluster node: {host}")

        if not removed:
            print(f"  No node found with host: {host}")
            return 1
        return 0

    elif sub == "test":
        target = getattr(args, "host", None)
        print()
        print("  Grid Node Tests")
        print("  " + "=" * 50)
        _grid_health_check(saved, grid_nodes, target_host=target)
        print()
        return 0

    elif sub == "sync":
        api_key = saved.get("api_key", "")
        tenant = saved.get("tenant_id", "")
        if not api_key:
            print("  Not logged in. Run: adk login")
            print("  Grid sync requires authentication to store config in your workspace.")
            return 1

        grid_data = {
            "profile": saved.get("profile", ""),
            "backend": saved.get("backend", ""),
            "base_url": saved.get("base_url", ""),
            "model": saved.get("model", ""),
            "reasoning_backend": saved.get("reasoning_backend", ""),
            "reasoning_url": saved.get("reasoning_url", ""),
            "reasoning_model": saved.get("reasoning_model", ""),
            "cluster_backend": saved.get("cluster_backend", ""),
            "cluster_url": saved.get("cluster_url", ""),
            "cluster_model": saved.get("cluster_model", ""),
            "grid_nodes": grid_nodes,
        }

        async def _sync():
            from adk.strata import get_strata
            strata = get_strata()
            import json as _json
            ok = await strata.write(
                _GRID_CONFIG_STRATA_PATH,
                _json.dumps(grid_data, indent=2),
            )
            if ok:
                print(f"  Grid config synced to workspace (tenant: {tenant or 'default'})")
                print("  Pull on another machine: adk grid pull")
            else:
                # Fallback: try direct gateway API
                try:
                    import httpx
                    gateway = saved.get("gateway_url", "https://gateway.aitherium.com")
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.put(
                            f"{gateway}/api/v1/config/grid",
                            json=grid_data,
                            headers={"Authorization": f"Bearer {api_key}"},
                        )
                        if resp.status_code in (200, 201):
                            print("  Grid config synced via gateway")
                            return
                except Exception:
                    pass
                print("  Sync failed — Strata not available and gateway unreachable.")
                print("  Config is saved locally at ~/.aither/config.json")

        asyncio.run(_sync())
        return 0

    elif sub == "pull":
        api_key = saved.get("api_key", "")
        if not api_key:
            print("  Not logged in. Run: adk login")
            return 1

        async def _pull():
            import json as _json
            from adk.strata import get_strata
            strata = get_strata()
            data = await strata.read_text(_GRID_CONFIG_STRATA_PATH)
            if data:
                grid_data = _json.loads(data)
                save_saved_config(grid_data)
                print("  Grid config pulled from workspace and saved locally.")
                print("  Run: adk grid status")
                return

            # Fallback: try gateway API
            try:
                import httpx
                gateway = saved.get("gateway_url", "https://gateway.aitherium.com")
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"{gateway}/api/v1/config/grid",
                        headers={"Authorization": f"Bearer {api_key}"},
                    )
                    if resp.status_code == 200:
                        grid_data = resp.json()
                        save_saved_config(grid_data)
                        print("  Grid config pulled from gateway and saved locally.")
                        print("  Run: adk grid status")
                        return
            except Exception:
                pass
            print("  No grid config found in workspace. Run: adk grid sync (from configured machine)")

        asyncio.run(_pull())
        return 0

    elif sub == "enroll":
        ttl = max(60, min(int(getattr(args, "ttl", 1.0) * 3600), 86400))
        body = {"ttl_seconds": ttl, "label": getattr(args, "label", ""), "tenant": getattr(args, "tenant", "")}
        # Self-service first (owner ruling 2026-08-31): the device-flow bearer
        # from `adk login` mints for the caller's OWN tenant, subscription-
        # gated server-side. The admin key remains the fallback for platform
        # operators minting on behalf of a tenant.
        _cfg = load_saved_config()
        _bearer = (_cfg.get("access_token") or _cfg.get("api_key")
                   or os.environ.get("AITHER_API_KEY", "")).strip()
        ok, data = _grid_mesh_request("POST", "/nodes/enroll-token", body=body,
                                      need_key=not bool(_bearer), bearer=_bearer or None)
        if not ok:
            print(f"  {data}")
            return 1
        tok = data.get("enroll_token", "")
        # The server's own `install` string wins. The FALLBACK used to name a
        # retired cluster host whose installer returns 503 "Origin
        # unavailable" — its origin (aitheros-gateway:8777) was never migrated to
        # podman, so the tunnel resolves and then finds nothing behind it. This
        # line ships to PyPI, so the fallback was handing strangers a command
        # that cannot work. Point it at the maintained bootstrap instead — the
        # same command `POST /nodes/enroll-command` emits.
        base = (getattr(args, "url", "") or "https://portal.aitherium.com").rstrip("/")
        install = data.get("install") or (
            f'curl -fsSL {base}/bootstrap/join.sh | '
            f'AITHER_NODE_NAME=$(hostname) AITHER_ENROLL_TOKEN={tok} '
            f'AITHER_ENROLL_URL={base} bash'
        )
        tslug = getattr(args, "tenant", "")
        print()
        print(f"  Enrollment token minted (expires in {ttl // 60} min, single-use)"
              + (f", tenant={tslug}" if tslug else ""))
        print("\n  Run this on the target node:")
        print(f"    {install}")
        print("\n  It registers through the tunnel, installs a reboot-safe service, and starts heartbeating.")
        print()
        return 0

    elif sub == "ls":
        ok, data = _grid_mesh_request("GET", "/nodes")
        if not ok:
            print(f"  {data}")
            return 1
        nodes = data.get("nodes", [])
        if not nodes:
            print("  No mesh nodes registered.")
            return 0
        print()
        print("  Mesh Nodes")
        print("  " + "=" * 55)
        for n in nodes:
            hw = n.get("hardware") or {}
            dot = "online " if n.get("status") == "online" else "offline"
            gpu = hw.get("gpu") or "—"
            mem = hw.get("memory_gb")
            mem_s = f"{round(mem)}GB" if isinstance(mem, (int, float)) else "—"
            ctr = len(n.get("services") or n.get("containers") or [])
            tenant = n.get("tenant") or (n.get("labels") or {}).get("tenant") or ""
            name = n.get("name") or n.get("node_id") or "?"
            line = f"  [{dot}] {name:18s} {str(gpu):14s} {mem_s:>6s}  {ctr} ctr"
            if tenant:
                line += f"  tenant={tenant}"
            print(line)
        print()
        return 0

    elif sub == "deregister":
        node_id = args.node_id
        ok, data = _grid_mesh_request("DELETE", f"/nodes/{node_id}", need_key=True)
        if not ok:
            print(f"  {data}")
            return 1
        print(f"  Removed {data.get('name') or node_id} ({data.get('remaining', '?')} remaining)")
        return 0

    else:
        print()
        print("  adk grid — Manage distributed inference nodes")
        print()
        print("  Commands:")
        print("    adk grid status              Show topology + health")
        print("    adk grid add reasoning <ip>  Add Mac/reasoning node")
        print("    adk grid add cluster <ip>    Add CPU cluster node")
        print("    adk grid remove <ip>         Remove a node")
        print("    adk grid test                Test all nodes")
        print("    adk grid test <ip>           Test specific node")
        print("    adk grid sync                Push config to your Aitherium workspace")
        print("    adk grid pull                Pull config from workspace (new machine)")
        print()
        print("  Mesh registry (enrolled nodes via the gateway tunnel):")
        print("    adk grid ls                  List enrolled mesh nodes")
        print("    adk grid enroll [--tenant]   Mint a token to onboard a remote node")
        print("    adk grid deregister <id>     Remove a node from the registry")
        print()
        print("  Setup:")
        print("    adk deploy grid              Deploy vLLM + configure grid")
        print("    adk login                    Auth for cloud sync")
        print()
        return 0


def _grid_mesh_request(method: str, path: str, body: dict | None = None, need_key: bool = False,
                       bearer: str | None = None):
    """Call the gateway node registry through the public Cloudflare tunnel.

    GET /nodes is open; minting/removing are gated by admin credentials (need_key=True,
    sourced from AITHER_INTERNAL_SECRET/AITHER_MASTER_KEY env vars). Since 2026-08-31 the
    enroll-token mint also accepts an authenticated identity (the device-flow bearer from
    `adk login`) for the caller's OWN tenant — pass it via `bearer`. Returns (ok, data|msg).
    Uses a browser UA — Cloudflare 403s the default python-urllib agent.
    """
    import json as _json
    import os as _os
    import ssl as _ssl
    import urllib.request as _ur

    gateway = ""
    if bearer:
        # The self-service (bearer) lane rides the PUBLIC portal proxy: the
        # gateway itself has NO public ingress (tunnel-ingress.yaml: aitheros-
        # gateway:8777 appears only in comments), and cluster.aitherium.com
        # serves PORTAL HTML for every path (measured 2026-08-31) — which is
        # how a "working" mint used to hand a web page back to the adk. The
        # portal route /api/nodes/enroll-token forwards the caller to the
        # gateway in-network; the mint is the security boundary.
        gateway = (_os.environ.get("AITHER_NODE_GATEWAY_URL")
                   or "https://portal.aitherium.com/api").rstrip("/")
    else:
        # Admin-key lanes stay OFF the public surface — the X-API-Key IS the
        # internal secret, so only an operator-set in-network URL may carry
        # it. There is deliberately NO public default here: cluster.
        # aitherium.com serves PORTAL HTML for every path (measured
        # 2026-08-31), so a "working" default was a web page. Refuse with the
        # remedy instead of dialing a host that cannot answer.
        gateway = (_os.environ.get("AITHER_NODE_GATEWAY_URL")
                   or _os.environ.get("AITHER_PUBLIC_CLUSTER_URL") or "").rstrip("/")
        if not gateway:
            return False, ("No gateway URL for the node registry. Set "
                           "AITHER_NODE_GATEWAY_URL (operator, in-network) — "
                           "or use the device-flow bearer via `adk login` "
                           "where supported. cluster.aitherium.com serves a "
                           "web page, not the API.")
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AitherADK/1.0)",
        "Content-Type": "application/json",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    elif need_key:
        key = (_os.environ.get("AITHER_INTERNAL_SECRET") or _os.environ.get("AITHER_MASTER_KEY") or "").strip()
        if not key:
            return False, "Admin credential required: set AITHER_INTERNAL_SECRET or AITHER_MASTER_KEY to mint or remove nodes."
        headers["X-API-Key"] = key
    data = _json.dumps(body).encode() if body is not None else None
    req = _ur.Request(f"{gateway}{path}", data=data, method=method, headers=headers)
    ctx = _ssl.create_default_context()
    if gateway.startswith("https://aitheros-") or "localhost" in gateway or "127.0.0.1" in gateway:
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
    try:
        with _ur.urlopen(req, context=ctx, timeout=15) as resp:
            return True, _json.loads(resp.read().decode() or "{}")
    except _ur.HTTPError as e:  # noqa: PERF203
        if e.code == 404:
            return False, f"Not found: {path.rsplit('/', 1)[-1]}"
        return False, f"Gateway returned {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, f"Gateway unreachable: {str(e)[:120]}"


def _grid_test_node(host: str, port: int) -> bool:
    """Test connectivity and API compatibility of a single grid node."""
    from urllib.request import Request, urlopen

    try:
        req = Request(
            f"http://{host}:{port}/health",
            headers={"User-Agent": "AitherADK/1.0"},
        )
        with urlopen(req, timeout=5):
            pass
    except Exception:
        print(f"  [x] {host}:{port} — unreachable")
        return False

    try:
        req = Request(
            f"http://{host}:{port}/v1/models",
            headers={"User-Agent": "AitherADK/1.0"},
        )
        with urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                import json as _json
                data = _json.loads(resp.read())
                models = [m.get("id", "") for m in data.get("data", [])]
                print(f"  [+] {host}:{port} — healthy, models: {', '.join(models[:3]) or 'default'}")
                return True
    except Exception:
        print(f"  [!] {host}:{port} — healthy but no /v1 API (missing --api-oai?)")
        return False

    return False


def _grid_health_check(saved: dict, grid_nodes: dict, target_host: str | None = None):
    """Run health checks on all or a specific grid node."""
    checked = False

    # Reasoning node
    r_node = grid_nodes.get("reasoning")
    if r_node and (target_host is None or target_host == r_node.get("host")):
        _grid_test_node(r_node["host"], r_node.get("port", 8121))
        checked = True

    # Cluster nodes
    for node in grid_nodes.get("cluster", []):
        if target_host is None or target_host == node.get("host"):
            _grid_test_node(node["host"], node.get("port", 8121))
            checked = True

    # Fallback: check flat config URLs if no grid_nodes
    if not checked and not target_host:
        r_url = saved.get("reasoning_url", "")
        if r_url:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(r_url)
                host = parsed.hostname or ""
                port = parsed.port or 8121
                if host:
                    _grid_test_node(host, port)
            except Exception:
                pass

        c_url = saved.get("cluster_url", "")
        if c_url:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(c_url)
                host = parsed.hostname or ""
                port = parsed.port or 8121
                if host:
                    _grid_test_node(host, port)
            except Exception:
                pass

    if not checked and target_host:
        print(f"  No node found with host: {target_host}")


# ── Phase 3.6: adk explore — marketplace browser ─────────────────────────


def cmd_explore(args) -> int:
    """Browse packs, agents, and skills in the Aitherium marketplace."""
    genesis_url = _get_genesis_url()
    category = getattr(args, "category", "all").lower()
    free_only = getattr(args, "free", False)

    catalog = _load_pack_catalog(genesis_url)

    if not catalog:
        print("\n  No catalog available. Install awdk or connect to Genesis.\n")
        return 1

    # Filter
    filtered = catalog
    if category == "agents":
        filtered = [p for p in catalog if p.get("type") == "agent_pack"]
    elif category == "tools":
        filtered = [p for p in catalog if p.get("type") == "tool_pack"]
    elif category == "skills":
        filtered = [p for p in catalog if p.get("type") == "skill_pack"]
    elif category == "grid":
        filtered = [p for p in catalog if "grid" in p.get("tags", []) or "distributed" in p.get("tags", [])]

    if free_only:
        filtered = [p for p in filtered if p.get("tier") == "free"]

    # Check installed
    packs_dir = Path.home() / ".aitheros" / "packs"
    installed_ids = set()
    if packs_dir.is_dir():
        for child in packs_dir.iterdir():
            if child.is_dir() and (child / ".toolpack.yaml").exists():
                installed_ids.add(child.name)

    # Group by type
    groups: dict[str, list] = {}
    for p in filtered:
        ptype = p.get("type", "other").replace("_pack", "").replace("_", " ").title()
        groups.setdefault(ptype, []).append(p)

    print()
    print("  Aitherium Marketplace")
    print("  " + "=" * 60)

    for gname in sorted(groups):
        packs = groups[gname]
        print(f"\n  {gname} Packs ({len(packs)})")
        print("  " + "-" * 55)
        for p in packs:
            pid = p.get("id", "?")
            name = p.get("name", pid)
            desc = p.get("description", "")[:70]
            tier = p.get("tier", "free")
            installed = pid in installed_ids
            pricing = p.get("pricing", {})

            icon = "[+]" if installed else "[ ]"
            tier_label = tier
            price = ""
            if pricing.get("subscription_cents"):
                price = f"${int(pricing['subscription_cents']) / 100:.0f}/mo"
            elif pricing.get("one_time_cents"):
                price = f"${int(pricing['one_time_cents']) / 100:.0f}"

            status = "installed" if installed else tier_label
            print(f"  {icon} {name}")
            if desc:
                print(f"      {desc}")
            parts = [status]
            if price:
                parts.append(price)
            if p.get("install_command"):
                parts.append(p["install_command"])
            print(f"      {' | '.join(parts)}")

    total = len(filtered)
    free_count = sum(1 for p in filtered if p.get("tier") == "free")
    inst_count = sum(1 for p in filtered if p.get("id") in installed_ids)

    print(f"\n  {total} packs shown ({free_count} free, {inst_count} installed)")
    print()
    print("  Quick actions:")
    print("    adk explore agents          Browse agent packs")
    print("    adk explore tools --free    Free tool packs only")
    print("    adk explore grid            Grid infrastructure")
    print("    adk pack install <id>       Install a pack")
    print("    adk upgrade <id>            Open checkout page")
    print()
    print("  Full catalog: https://portal.aitherium.com/marketplace")
    print()
    return 0


# ── Phase 3.7: adk upgrade — checkout shortcut ──────────────────────────


_UPGRADE_URLS: dict[str, tuple[str, str]] = {
    # The ONLY marketplace detail route that exists is
    # /marketplace/app/[app_id] (src/app/marketplace/app/[app_id]).
    # The bare /marketplace/grid and /marketplace/agent.* shapes 307 to the
    # login gate and then 404 — measured 2026-08-31, gate RSU001. The id
    # slug is catalog data; if a slug ever stops resolving, the detail page
    # renders its own not-found state instead of a route-level 404.
    "managed": ("https://portal.aitherium.com/marketplace/app/grid?sku=grid_managed_monthly", "Grid Managed ($49/mo)"),
    "setup": ("https://portal.aitherium.com/marketplace/app/grid?sku=grid_setup_onetime", "Grid Setup Call ($199)"),
    "grid": ("https://portal.aitherium.com/marketplace/app/grid", "Grid Distributed Inference"),
    "demiurge": ("https://portal.aitherium.com/marketplace/app/agent.demiurge", "Demiurge — Code Architect"),
    "hydra": ("https://portal.aitherium.com/marketplace/app/agent.hydra", "Hydra — Code Guardian"),
    "athena": ("https://portal.aitherium.com/marketplace/app/agent.athena", "Athena — Security Oracle"),
    "lyra": ("https://portal.aitherium.com/marketplace/app/agent.lyra", "Lyra — Research Muse"),
    "pro": ("https://portal.aitherium.com/pricing", "Professional Plan"),
}


def cmd_upgrade(args) -> int:
    """Open upgrade/checkout page for a pack or plan."""
    target = getattr(args, "target", "").lower().strip()

    if not target:
        print("\n  Upgrade Options\n")
        for key, (url, label) in _UPGRADE_URLS.items():
            print(f"    {key:15s} {label}")
        print()
        print("  Usage: adk upgrade managed")
        print("         adk upgrade demiurge")
        print("         adk upgrade pro")
        print()
        return 0

    if target in _UPGRADE_URLS:
        url, label = _UPGRADE_URLS[target]
    else:
        url = f"https://portal.aitherium.com/marketplace/{target}"
        label = target

    print(f"\n  Opening: {label}")
    print(f"  {url}\n")

    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        print("  (Could not open browser — copy the URL above)")

    return 0


# ── Version check (runs once per day, non-blocking) ─────────────────────


def _version_is_newer(latest: str, current: str) -> bool:
    """True only when `latest` is strictly newer than `current`.

    Uses packaging.version when available (correct pre-release/post-release
    ordering) and falls back to a numeric-tuple compare so this never becomes a
    hard dependency — a version check must never break the CLI it decorates.
    Unparseable input returns False: stay quiet rather than nag wrongly.
    """
    if not latest or not current:
        return False
    try:
        from packaging.version import InvalidVersion, Version
        try:
            return Version(latest) > Version(current)
        except InvalidVersion:
            return False
    except ImportError:
        pass

    def _parts(v: str) -> tuple:
        out = []
        for chunk in v.split("."):
            digits = "".join(ch for ch in chunk if ch.isdigit())
            if not digits:
                break
            out.append(int(digits))
        return tuple(out)

    lp, cp = _parts(latest), _parts(current)
    return bool(lp and cp and lp > cp)


def _check_for_updates() -> None:
    """Check PyPI for newer awdk version. Runs at most once per day."""
    marker = Path.home() / ".aither" / ".last_update_check"
    now = time.time()

    # Check at most once per day
    if marker.exists():
        try:
            last = float(marker.read_text(encoding="utf-8").strip())
            if now - last < 86400:
                return
        except (ValueError, OSError):
            pass

    try:
        from urllib.request import urlopen
        resp = urlopen("https://pypi.org/pypi/awdk/json", timeout=3)
        data = json.loads(resp.read())
        latest = data.get("info", {}).get("version", "")

        # Read current version
        try:
            from importlib.metadata import version as pkg_version
            current = pkg_version("awdk")
        except Exception:
            current = ""

        # Only nag when the published version is genuinely NEWER. This used to be a
        # bare `latest != current`, which fires in BOTH directions: a user running a
        # newer build than PyPI (a dev install, or the minutes-long window right after
        # a release while the index propagates) was told
        #   "Update available: awdk 2.36.0 -> 2.35.0"
        # i.e. instructed to DOWNGRADE — and this prints on `adk join`, the first
        # command a new contributor ever runs. Seen live on the DGX 2026-07-24.
        if latest and current and _version_is_newer(latest, current):
            print(f"\n  Update available: awdk {current} -> {latest}")
            print("  Run: pip install --upgrade awdk\n")

        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(now), encoding="utf-8")
    except Exception:
        pass  # Best effort, never crash


# ── Phase 4: aither costs — token economy visibility ──────────────────────

def cmd_costs(args):
    """Show cloud inference costs and savings."""
    sub = getattr(args, "costs_command", None)

    # Try Genesis API first
    def _genesis_get(path: str) -> dict | None:
        import urllib.request
        import urllib.error
        try:
            url = os.environ.get("AITHER_URL", "http://localhost:8001")
            req = urllib.request.Request(f"{url.rstrip('/')}{path}")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception:
            return None

    if sub == "budget":
        amount = getattr(args, "amount", 0)
        if amount <= 0:
            print("Usage: adk costs budget <amount_usd>")
            return 1
        result = _genesis_get(f"/costs/budget?monthly_usd={amount}")
        if result:
            print(f"  Monthly budget set to ${amount:.2f}")
        else:
            # Save locally
            budget_path = Path.home() / ".aither" / "cost_budget.json"
            budget_path.parent.mkdir(parents=True, exist_ok=True)
            budget_path.write_text(json.dumps({"monthly_budget_usd": amount}))
            print(f"  Monthly budget set to ${amount:.2f} (local — Genesis not running)")
        return 0

    elif sub == "compare":
        period = getattr(args, "period", "week")
        result = _genesis_get(f"/costs/compare?period={period}")
        if result:
            print(f"\n  Cost Comparison ({result.get('period', period)})")
            print("  " + "=" * 45)
            print(f"  Actual spend:          ${result.get('actual_cost_usd', 0):.4f}")
            print(f"  Est. raw API cost:     ${result.get('estimated_raw_api_cost_usd', 0):.4f}")
            print(f"  Savings:               ${result.get('savings_usd', 0):.4f} ({result.get('savings_percent', 0):.1f}%)")
            print(f"  Local requests:        {result.get('local_requests', 0)}")
            print(f"  Cloud requests:        {result.get('cloud_requests', 0)}")
        else:
            # Read local cost log
            _show_local_costs("compare", getattr(args, "period", "week"))
        return 0

    else:
        # Default: summary
        period = getattr(args, "period", "day")
        result = _genesis_get(f"/costs/summary?period={period}")
        if result:
            print(f"\n  Cost Summary ({result.get('period', period)})")
            print("  " + "=" * 45)
            print(f"  Total spend:     ${result.get('total_cost_usd', 0):.4f}")
            print(f"  Requests:        {result.get('total_requests', 0)} ({result.get('cloud_requests', 0)} cloud, {result.get('local_requests', 0)} local)")
            print(f"  Tokens:          {result.get('total_tokens', 0):,}")

            by_provider = result.get("by_provider", {})
            if by_provider:
                print("\n  By Provider:")
                for prov, cost in by_provider.items():
                    print(f"    {prov:15s} ${cost:.4f}")

            budget = result.get("monthly_budget_usd", 0)
            remaining = result.get("budget_remaining_usd")
            if budget:
                print(f"\n  Budget: ${budget:.2f}/mo  Remaining: ${remaining:.2f}")
        else:
            _show_local_costs("summary", period)
        return 0


def _show_local_costs(mode: str, period: str):
    """Fallback: read local cost JSONL when Genesis is not running."""
    from datetime import datetime, timedelta, timezone

    days_map = {"day": 1, "week": 7, "month": 30}
    days = days_map.get(period, 1)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Search for cost log
    candidates = [
        Path.home() / ".aither" / "cloud_costs.jsonl",
        Path.cwd() / "data" / "cloud_costs.jsonl",
        Path.cwd() / "AitherOS" / "data" / "cloud_costs.jsonl",
    ]
    log_path = None
    for c in candidates:
        if c.exists():
            log_path = c
            break

    if not log_path:
        print("\n  No cost data found (Genesis not running, no local cost log)")
        print("  Costs are tracked when cloud providers are used via AitherOS")
        return

    total = 0.0
    count = 0
    local_count = 0
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts = entry.get("timestamp", "")
                    if ts:
                        entry_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if entry_time < cutoff:
                            continue
                    cost = entry.get("cost_usd", 0) or 0
                    total += cost
                    if entry.get("provider") == "local":
                        local_count += 1
                    else:
                        count += 1
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass

    print(f"\n  Cost Summary — {period} (local log)")
    print("  " + "=" * 40)
    print(f"  Total spend:  ${total:.4f}")
    print(f"  Cloud reqs:   {count}")
    print(f"  Local reqs:   {local_count}")


def cmd_tools(args):
    """Dispatch tools subcommands (list, sync)."""
    # Dispatch to sync subcommand if specified
    if getattr(args, "tools_command", None) == "sync":
        return cmd_tools_sync(args)

    # Default to list behavior (including when tools_command is 'list' or None)
    import asyncio

    async def _tools():
        from adk.builtin_tools import get_builtin_registry

        # Local built-in tools
        reg = get_builtin_registry()
        local_tools = reg.list_tools()

        print("Local Tools")
        print("=" * 50)
        for t in sorted(local_tools, key=lambda x: x.name):
            desc = (t.description or "")[:60]
            print(f"  {t.name:30s} {desc}")
        print(f"\n  Total: {len(local_tools)} local tools")

        # MCP tools (if connected)
        api_key = os.environ.get("AITHER_API_KEY", "")
        if not api_key:
            saved = load_saved_config()
            api_key = saved.get("api_key", "")

        if api_key:
            try:
                from adk.mcp import MCPBridge
                bridge = MCPBridge(api_key=api_key)
                mcp_tools = await bridge.list_tools()
                print("\nMCP Tools (cloud)")
                print("=" * 50)
                for t in sorted(mcp_tools, key=lambda x: x.get("name", ""))[:20]:
                    name = t.get("name", "?")
                    desc = t.get("description", "")[:50]
                    tier = t.get("tier", "")
                    marker = f" [{tier}]" if tier else ""
                    print(f"  {name:30s} {desc}{marker}")
                print(f"\n  Total: {len(mcp_tools)} MCP tools")
                if getattr(args, "upgrade", False):
                    print("\n  Upgrade at: https://portal.aitherium.com/pricing")
            except Exception as e:
                print(f"\n  MCP: not available ({e})")
        else:
            print("\n  MCP: no API key (run 'adk login' for cloud tools)")

    asyncio.run(_tools())
    return 0


def cmd_tools_sync(args):
    """Sync entitled tools from platform to local cache (~/.aither/tools-manifest.json)."""
    import asyncio
    import stat

    async def _sync_tools():
        import httpx
        from adk.auth import resolve_credentials
        from adk.config import Config

        # Resolve credentials and gateway URL
        try:
            creds = resolve_credentials()
        except Exception as e:
            print(f"Error: Failed to resolve credentials: {e}")
            return 1

        if not creds.access_token:
            print("Error: No API key found. Run 'adk login' first.")
            return 1

        config = Config.from_env()
        gateway_url = config.gateway_url.rstrip("/")
        url = f"{gateway_url}/tools/manifest"

        # Prepare authorization header
        auth_header = f"Bearer {creds.access_token}"

        # Check for cached manifest and build If-None-Match header
        cache_path = Path.home() / ".aither" / "tools-manifest.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        headers = {"Authorization": auth_header}
        if cache_path.exists():
            try:
                cached_data = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(cached_data, dict) and "version" in cached_data:
                    headers["If-None-Match"] = cached_data["version"]
                    if getattr(args, "verbose", False):
                        print(f"Using cached version: {cached_data['version']}")
            except (json.JSONDecodeError, OSError):
                pass

        # Fetch tools manifest
        try:
            async with httpx.AsyncClient(timeout=30, verify=tls_verify()) as client:
                response = await client.get(url, headers=headers)
        except Exception as e:
            print(f"Error: Failed to fetch tools manifest: {e}")
            return 1

        # Handle response status codes
        if response.status_code == 304:
            # Not Modified — cache is current
            print("Tools manifest is up to date (cached).")
            return 0
        elif response.status_code == 401:
            print("Error: Authentication failed. Check your API key (adk login).")
            return 1
        elif response.status_code == 503:
            print("Error: Platform unavailable. Please try again later.")
            return 1
        elif response.status_code == 200:
            # Success — parse and cache the manifest
            try:
                manifest = response.json()
            except Exception as e:
                print(f"Error: Failed to parse tools manifest: {e}")
                return 1

            # Validate response structure
            if not isinstance(manifest, dict):
                print("Error: Unexpected response format from server.")
                return 1

            tools_list = manifest.get("tools", [])
            if not isinstance(tools_list, list):
                print("Error: Invalid tools list in response.")
                return 1

            # Write to cache with restricted permissions (0600)
            try:
                cache_path.write_text(
                    json.dumps(manifest, indent=2),
                    encoding="utf-8"
                )
                cache_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
                tool_count = len(tools_list)
                print(f"Synced {tool_count} tools to ~/.aither/tools-manifest.json")
                if getattr(args, "verbose", False):
                    print(f"Manifest version: {manifest.get('version', 'unknown')}")
                return 0
            except Exception as e:
                print(f"Error: Failed to write tools manifest to cache: {e}")
                return 1
        else:
            print(
                f"Error: Server returned {response.status_code}: "
                f"{response.text[:200]}"
            )
            return 1

    return asyncio.run(_sync_tools())


def cmd_backup(args):
    """Backup all ~/.aither/ data."""
    import tarfile
    import time as _time

    data_dir = Path.home() / ".aither"
    if not data_dir.exists():
        print("Nothing to backup — ~/.aither/ does not exist")
        return 1

    ts = _time.strftime("%Y%m%d-%H%M%S")
    output = getattr(args, "output", None) or f"aither-backup-{ts}.tar.gz"
    output_path = Path(output)

    # Count files
    files = list(data_dir.rglob("*"))
    file_count = sum(1 for f in files if f.is_file())

    print(f"Backing up {file_count} files from ~/.aither/")

    with tarfile.open(str(output_path), "w:gz") as tar:
        tar.add(str(data_dir), arcname=".aither")

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"Saved: {output_path} ({size_mb:.1f}MB)")
    return 0


def cmd_ingest(args):
    """Ingest files into the agent's knowledge graph with optional brain sync."""
    import asyncio

    target = Path(args.path or ".")
    agent_name = getattr(args, "agent", "default")
    brain_sync = getattr(args, "brain", False)
    brain_url = getattr(args, "brain_url", "")
    classification = getattr(args, "classification", "internal")
    chunk_size = getattr(args, "chunk_size", 2000)
    chunk_overlap = getattr(args, "chunk_overlap", 200)
    workspace_id = getattr(args, "workspace", "default")
    skip_embeddings = getattr(args, "skip_embeddings", False)
    dry_run = getattr(args, "dry_run", False)

    async def _ingest():
        from adk.ingest import ingest_files

        if not target.exists():
            print(f"Error: path not found: {target}")
            return 1

        print(f"Ingesting files from {target}...")
        if brain_sync:
            print(f"  Brain sync: ENABLED (workspace: {workspace_id})")
        else:
            print("  Brain sync: disabled (local only)")
        print(f"  Classification: {classification}")
        print(f"  Chunk size: {chunk_size} bytes, overlap: {chunk_overlap} bytes")

        result = await ingest_files(
            path=target,
            classification=classification,
            brain_sync=brain_sync,
            brain_url=brain_url,
            workspace_id=workspace_id,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            skip_embeddings=skip_embeddings,
            dry_run=dry_run,
            agent_name=agent_name,
        )

        # Print summary
        print()
        print("=" * 60)
        print("Ingest Summary")
        print("=" * 60)
        print(f"Total files scanned:      {result.files_total}")
        print(f"Files ingested:           {result.files_ingested}")
        print(f"Files skipped:            {result.files_skipped}")
        if result.skipped_files:
            for path, reason in result.skipped_files[:5]:
                print(f"  - {path}: {reason}")
            if len(result.skipped_files) > 5:
                print(f"  ... and {len(result.skipped_files) - 5} more")
        print()
        print(f"Chunks created:           {result.chunks_created}")
        print(f"Chunks embedded:          {result.chunks_embedded}")
        if result.embedding_degraded:
            print("  WARNING: Embedding degraded (fallback dimension)")
        print()
        if brain_sync:
            print(f"Brain sync status:        {'SUCCESS' if result.brain_synced else 'FAILED'}")
            print(f"Chunks synced to hub:     {result.chunks_synced}")
        else:
            print("Brain sync:               disabled")
        print()
        if result.errors:
            print("Errors encountered:")
            for error in result.errors:
                print(f"  ! {error}")
        print("=" * 60)

        # Return appropriate exit code
        if result.errors and result.files_ingested == 0:
            return 1
        return 0

    return asyncio.run(_ingest())


def cmd_sync(args):
    """AitherDrive — bidirectional file sync with AitherOS platform."""
    import asyncio

    action = getattr(args, "sync_action", None)
    if not action:
        # Default: show status
        action = "status"

    def _load_config():
        """Load auth config from ~/.aither/config.json."""
        cfg_path = Path.home() / ".aither" / "config.json"
        if not cfg_path.exists():
            print("Not logged in. Run `adk login` first.")
            sys.exit(1)
        import json
        return json.loads(cfg_path.read_text(encoding="utf-8"))

    async def _run():
        from adk.sync import SyncManager, SyncManifest, MANIFEST_FILE
        from adk.client.services.strata import StrataClient
        from adk.client.services.data_plane import DataPlaneClient

        if action == "init":
            cfg = _load_config()
            tenant_id = cfg.get("tenant_id", "")
            if not tenant_id:
                print("No tenant_id in config. Run `adk login` first.")
                return 1

            sync_dir = Path(getattr(args, "directory", ".")).resolve()
            if not sync_dir.is_dir():
                print(f"Directory not found: {sync_dir}")
                return 1

            # Check if already initialized
            if (sync_dir / MANIFEST_FILE).exists():
                print(f"Already initialized: {sync_dir / MANIFEST_FILE}")
                return 0

            import httpx
            token = cfg.get("access_token", cfg.get("api_key", ""))
            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            if tenant_id:
                headers["X-Tenant-ID"] = tenant_id
            async with httpx.AsyncClient(timeout=30.0, headers=headers) as http:
                async def get_client():
                    return http

                strata_url = cfg.get("strata_url", os.environ.get(
                    "AITHER_STRATA_URL", "http://localhost:8136"))
                dp_url = cfg.get("data_plane_url", os.environ.get(
                    "AITHER_DATAPLANE_URL", "http://localhost:8170"))
                strata = StrataClient(strata_url, get_client)
                data_plane = DataPlaneClient(dp_url, get_client)
                mgr = SyncManager(sync_dir, strata, data_plane, tenant_id)
                result = await mgr.init()

            if result.get("status") == "initialized":
                print(f"Sync root initialized at {sync_dir}")
                print(f"  Node ID:    {result['node_id']}")
                print(f"  Source ID:  {result.get('source_id', 'n/a')}")
                print(f"  Files:      {result['files_scanned']}")
                print()
                print("Run `adk sync push` to upload or `adk sync watch` to auto-sync.")
            else:
                print(f"Already initialized (node: {result.get('node_id', '?')})")
            return 0

        # All other actions require an existing manifest
        sync_dir = Path(".").resolve()
        manifest_path = sync_dir / MANIFEST_FILE
        if not manifest_path.exists():
            # Walk up to find manifest
            for parent in sync_dir.parents:
                if (parent / MANIFEST_FILE).exists():
                    sync_dir = parent
                    manifest_path = parent / MANIFEST_FILE
                    break
            else:
                print("Not a sync root. Run `adk sync init` first.")
                return 1

        manifest = SyncManifest(sync_dir)
        manifest.load()

        cfg = _load_config()
        token = cfg.get("access_token", cfg.get("api_key", ""))
        tenant_id = manifest.tenant_id or cfg.get("tenant_id", "")

        import httpx
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if tenant_id:
            headers["X-Tenant-ID"] = tenant_id

        async with httpx.AsyncClient(timeout=60.0, headers=headers) as http:
            async def get_client():
                return http

            strata_url = cfg.get("strata_url", os.environ.get(
                "AITHER_STRATA_URL", "http://localhost:8136"))
            dp_url = cfg.get("data_plane_url", os.environ.get(
                "AITHER_DATAPLANE_URL", "http://localhost:8170"))
            strata = StrataClient(strata_url, get_client)
            data_plane = DataPlaneClient(dp_url, get_client)
            mgr = SyncManager(sync_dir, strata, data_plane, tenant_id, manifest.node_id)
            mgr.manifest = manifest

            if action == "status":
                st = mgr.status()
                print(f"Sync root: {sync_dir}")
                print(f"Node:      {manifest.node_id}")
                print(f"Last sync: {manifest.last_sync_at or 'never'}")
                print(f"Status:    {st.summary()}")
                if st.new:
                    for f in st.new[:10]:
                        print(f"  + {f}")
                    if len(st.new) > 10:
                        print(f"  ... and {len(st.new) - 10} more")
                if st.changed:
                    for f in st.changed[:10]:
                        print(f"  ~ {f}")
                    if len(st.changed) > 10:
                        print(f"  ... and {len(st.changed) - 10} more")
                if st.deleted:
                    for f in st.deleted[:10]:
                        print(f"  - {f}")
                    if len(st.deleted) > 10:
                        print(f"  ... and {len(st.deleted) - 10} more")

            elif action == "push":
                print("Pushing local changes...")
                result = await mgr.push()
                print(f"Uploaded: {result['uploaded']}  Deleted: {result['deleted']}")
                if result["errors"]:
                    for e in result["errors"][:5]:
                        print(f"  Error: {e}")

            elif action == "pull":
                print("Pulling remote changes...")
                result = await mgr.pull()
                print(f"Downloaded: {result['downloaded']}")
                if result.get("errors"):
                    for e in result["errors"][:5]:
                        print(f"  Error: {e}")

            elif action == "watch":
                started = await mgr.watch()
                if not started:
                    print("watchdog not installed. Install with:")
                    print("  pip install awdk[sync]")
                    return 1
                print(f"Watching {sync_dir} for changes (Ctrl+C to stop)...")
                try:
                    while True:
                        await asyncio.sleep(1)
                except KeyboardInterrupt:
                    mgr.stop()
                    print("\nWatcher stopped.")

            elif action == "stop":
                mgr.stop()
                print("Watcher stopped.")

            elif action == "ignore":
                pattern = getattr(args, "pattern", "")
                if pattern:
                    mgr.add_ignore(pattern)
                    print(f"Added ignore pattern: {pattern}")

            elif action == "config":
                print(f"Sync root:     {sync_dir}")
                print(f"Node ID:       {manifest.node_id}")
                print(f"Tenant ID:     {manifest.tenant_id}")
                print(f"Source ID:     {manifest.source_id}")
                print(f"Strata prefix: {manifest.strata_prefix}")
                print(f"Conflict:      {manifest.conflict_strategy}")
                print(f"Settings sync: {manifest.settings_sync}")
                print(f"Max file size: {manifest.max_file_size // (1024*1024)}MB")
                print(f"Ignore:        {', '.join(manifest.ignore)}")
                print(f"Tracked files: {len(manifest.files)}")

        return 0

    return asyncio.run(_run())


def cmd_quickstart(args):
    """Unified first-run wizard — setup + auth + shell in one command."""

    cloud_mode = getattr(args, "cloud", False)

    print()
    print("  AitherADK Quickstart")
    print("  ====================")
    print()

    # Step 1: Check if already set up
    saved = load_saved_config()
    if saved.get("setup_backend"):
        print(f"  Already configured: {saved.get('setup_backend')} backend")
        print("  Run 'adk doctor' to check health, or 'adk start' to begin.")
        print()
        return 0

    # Step 1.5: Check for brain_pack.yaml in marketplace/local projects
    brain_pack_path = None
    if Path("brain_pack.yaml").exists():
        brain_pack_path = Path("brain_pack.yaml")
    elif Path("config/brain_pack.yaml").exists():
        brain_pack_path = Path("config/brain_pack.yaml")

    if brain_pack_path:
        print(f"  Found brain pack: {brain_pack_path}")
        try:
            import yaml
            with open(brain_pack_path) as f:
                brain = yaml.safe_load(f) or {}
            agent_name = brain.get("agent_name", "agent")
            model = brain.get("model", "")
            llm_backend = brain.get("llm_backend", "auto")
            print(f"    Agent: {agent_name}")
            print(f"    Model: {model or 'auto'}")
            print(f"    Backend: {llm_backend}")
            print()

            if model and llm_backend == "local":
                print(f"  Preparing model: {model}")
        except Exception as e:
            print(f"  Warning: could not parse brain pack: {e}")
            brain_pack_path = None
            print()

    if cloud_mode:
        # ── Cloud-only quickstart ──────────────────────────────────────
        print("  Cloud-Only Setup (no GPU required)")
        print("  " + "-" * 38)
        print()

        # Step 1: API key setup
        print("  Step 1: Connect Cloud Providers")
        print("  " + "-" * 38)
        print("  Enter API keys for at least one cloud provider.")
        print("  Press Enter to skip.\n")

        keys = _load_provider_keys()
        configured_providers = []

        for pname in ("openai", "anthropic", "deepseek"):
            info = _KNOWN_PROVIDERS[pname]
            existing = keys.get(pname, "") or os.environ.get(info["env"], "")
            if existing:
                print(f"  {info['label']}: already configured ({_mask_key(existing)})")
                configured_providers.append(pname)
                continue
            try:
                val = input(f"  {info['label']} API key: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if val:
                keys[pname] = val
                os.environ[info["env"]] = val
                ok, msg = _test_provider_key(pname, val)
                icon = "+" if ok else "x"
                print(f"    [{icon}] {msg}")
                if ok:
                    configured_providers.append(pname)
                _push_key_to_vault(pname, val)

        _save_provider_keys(keys)

        if not configured_providers:
            print("\n  No providers configured. At least one API key is required for cloud mode.")
            print("  Run: adk keys set openai sk-...")
            return 1

        # Step 2: Auto-select routing preset
        print()
        print("  Step 2: Routing Configuration")
        print("  " + "-" * 38)

        if set(configured_providers) >= {"openai", "anthropic", "deepseek"}:
            preset = "quality"
        elif "anthropic" in configured_providers and "deepseek" in configured_providers:
            preset = "balanced"
        elif "deepseek" in configured_providers:
            preset = "budget"
        else:
            preset = "balanced"

        print(f"  Auto-selected preset: {preset}")
        print("  (Change later with: adk routing preset <budget|balanced|quality>)")

        # Apply preset
        class RoutingArgs:
            routing_command = "preset"
            preset_name = preset
        cmd_routing(RoutingArgs())

        # Set cloud mode in ADK config
        save_saved_config({
            "setup_backend": "cloud",
            "cloud_mode": "cloud_first",
            "configured_providers": configured_providers,
        })

        # Step 2.5: Test cloud memory
        print()
        print("  Step 2.5: Cloud Memory")
        print("  " + "-" * 38)
        gateway_url = "https://gateway.aitherium.com"
        try:
            import httpx as _httpx
            _mem_resp = _httpx.post(
                f"{gateway_url}/v1/memory/teach",
                json={"content": "adk_quickstart_test", "category": "system"},
                timeout=5.0,
            )
            if _mem_resp.status_code in (200, 201):
                print("  Cloud memory: connected")
                save_saved_config({
                    "spirit_url": gateway_url,
                    "spirit_teach_path": "/v1/memory/teach",
                    "spirit_recall_path": "/v1/memory/recall",
                })
            else:
                print("  Cloud memory: not available (memories will be local-only)")
        except Exception:
            print("  Cloud memory: not available (memories will be local-only)")

        # Step 3: Cost estimate
        print()
        print("  Step 3: Ready!")
        print("  " + "-" * 38)
        print()
        print(f"  Configured providers: {', '.join(configured_providers)}")
        print(f"  Routing preset: {preset}")
        print()
        print("  Estimated costs per 1,000 requests:")
        print("    Budget preset:    ~$0.50 - $2.00")
        print("    Balanced preset:  ~$2.00 - $8.00")
        print("    Quality preset:   ~$5.00 - $20.00")
        print()
        print("  Set a budget:  adk costs budget 50")
        print("  View costs:    adk costs")
        print()
        print("  Next steps:")
        print("    adk start            Start chatting with your codebase")
        print("    adk shell            Interactive terminal with agents")
        print("    adk explore          Browse 47 packs (agents, tools, skills)")
        print("    adk deploy grid      Distributed inference (GPU + Mac + cluster)")
        print("    adk doctor           Check system health")
        print()

        if brain_pack_path:
            print("  Marketplace pack shortcuts:")
            print("    adk run              Launch your packaged agent")
            print("    docker compose up -d")
            print()
            print("  To register with fleet ($5/mo):")
            print("    adk deploy --register-fleet")
            print()
            print("  To add cloud MCP tools:")
            print("    adk mcp add mcp.aitherium.com --api-key <your-key>")
            print()

        # Offer local orchestrator
        try:
            answer = input("  Set up a local orchestrator to reduce costs further? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("y", "yes"):
            from adk.setup_cli import cmd_setup
            class SetupArgs:
                shortcut = None
                tier = None
                mode = "hybrid"
                reasoning_api = None
                reasoning_model = ""
                dgx_spark = None
                stack = None
                dry_run = False
                non_interactive = False
                hf_token = ""
                api_key = getattr(args, "api_key", "") or ""
                output = "docker-compose.vllm.yml"
                force = False
            cmd_setup(SetupArgs())

        return 0

    # ── Standard quickstart (GPU-based) ────────────────────────────────

    # Step 2: Run setup wizard
    print("  Step 1: GPU + Inference Setup")
    print("  " + "-" * 38)
    from adk.setup_cli import cmd_setup

    class SetupArgs:
        shortcut = None
        tier = None
        mode = "auto"
        reasoning_api = None
        reasoning_model = ""
        dgx_spark = None
        stack = None
        dry_run = False
        non_interactive = False
        hf_token = ""
        api_key = getattr(args, "api_key", "") or ""
        output = "docker-compose.vllm.yml"
        force = False

    setup_result = cmd_setup(SetupArgs())
    if setup_result != 0:
        print("  Setup had issues — but you may still be able to use ADK.")
        print()

    # Step 3: Auth (optional)
    print()
    print("  Step 2: Aitherium Account (optional)")
    print("  " + "-" * 38)

    try:
        answer = input("  Connect to Aitherium for cloud tools? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer in ("y", "yes"):
        class LoginArgs:
            email = None
            password = None
            api_key = None
            portal_url = ""
        cmd_login(LoginArgs())

    # Step 4: Shell
    print()
    print("  Step 3: Ready!")
    print("  " + "-" * 38)
    print()
    print("  Your agent system is configured. Next steps:")
    print("    adk start            Start chatting with your codebase")
    print("    adk shell            Launch AitherShell interactive terminal")
    print("    adk run              Start the agent server")
    print("    adk doctor           Check system health")
    print()

    if brain_pack_path:
        print("  Marketplace pack shortcuts:")
        print("    adk run              Launch your packaged agent")
        print("    docker compose up -d")
        print()
        print("  To register with fleet ($5/mo):")
        print("    adk deploy --register-fleet")
        print()
        print("  To add cloud MCP tools:")
        print("    adk mcp add mcp.aitherium.com --api-key <your-key>")
        print()

    return 0


def cmd_quickstart_local(args):
    """
    One-command local inference quickstart.

    Detects hardware → picks backend (llamacpp/ollama/vllm) →
    installs & verifies → launches agent.
    """
    from adk import llamacpp_setup
    from adk.local_backends import pick_backend, docker_available
    from adk.ollama_setup import (
        DEFAULT_OLLAMA_MODEL,
        ensure_running,
        pull,
        register_config as ollama_register_config,
        smoke_test as ollama_smoke_test,
    )

    print()
    print("  AitherADK Local Inference Quickstart")
    print("  ====================================")
    print()

    # Step 1: Detect accelerator
    print("  [1/5] Detecting hardware...")
    accel = llamacpp_setup.detect_accel()
    print(
        f"  Hardware: {accel.os_family}/{accel.arch}, "
        f"accel={accel.kind}, GPU={accel.name}, "
        f"VRAM={accel.vram_gb:.1f}GB, RAM={accel.ram_gb:.1f}GB"
    )

    # Step 2: Pick backend
    print()
    print("  [2/5] Selecting backend...")
    backend = pick_backend(
        accel,
        prefer=getattr(args, "backend", "auto"),
        docker_available_override=docker_available(),
    )
    print(f"  Backend: {backend}")
    if backend == "vllm":
        print("    (NVIDIA CUDA + Docker: optimal throughput)")
    elif backend == "ollama":
        print("    (Ollama installed: convenient & portable)")
    else:
        print("    (llama.cpp: pure stdlib, no dependencies)")

    # Step 3: Install backend
    print()
    print("  [3/5] Installing inference backend...")

    if backend == "llamacpp":
        result = llamacpp_setup.install(
            quant=None,  # auto-detect from hardware
            port=getattr(args, "port", llamacpp_setup.DEFAULT_PORT),
            service=True,
            dry_run=getattr(args, "dry_run", False),
        )
        if not result.success:
            print(
                f"  ERROR: {result.error}",
                file=sys.stderr,
            )
            return 1
        endpoint = f"http://localhost:{result.port}/v1"
        model_name = result.quant

    elif backend == "ollama":
        if not ensure_running():
            print(
                "  ERROR: Could not start Ollama. "
                "Install from https://ollama.ai",
                file=sys.stderr,
            )
            return 1

        # NB: args.model exists but defaults to None, so getattr's fallback never
        # fires — use `or` to apply the default when the flag was omitted.
        model = getattr(args, "model", None) or DEFAULT_OLLAMA_MODEL
        if getattr(args, "dry_run", False):
            print(f"  [DRY] Would pull model: {model}")
        else:
            if not pull(model):
                print(f"  ERROR: Failed to pull model {model}", file=sys.stderr)
                return 1
        ollama_register_config(model)
        endpoint = "http://localhost:11434/v1"
        model_name = model

    elif backend == "vllm":
        # Use setup_cli's cmd_setup for Docker-based vLLM
        from adk.setup_cli import cmd_setup

        class SetupArgs:
            shortcut = None
            tier = None
            mode = "auto"
            reasoning_api = None
            reasoning_model = ""
            dgx_spark = None
            stack = None
            dry_run = getattr(args, "dry_run", False)
            non_interactive = True
            hf_token = ""
            api_key = getattr(args, "api_key", "") or ""
            output = "docker-compose.vllm.yml"

        setup_result = cmd_setup(SetupArgs())
        if setup_result != 0:
            print("  ERROR: vLLM setup failed", file=sys.stderr)
            return 1
        endpoint = "http://localhost:8209/v1"
        model_name = "auto"
    else:
        print(f"  ERROR: unknown backend {backend}", file=sys.stderr)
        return 1

    # Step 4: Verify with smoke test. A failed verification FAILS the command —
    # proving inference actually works is the entire point of "quickstart".
    print()
    print("  [4/5] Verifying endpoint...")
    if getattr(args, "dry_run", False):
        print("  [DRY] Skipping smoke test")
    else:
        if backend in ("llamacpp", "vllm"):
            import time

            verify_port = getattr(args, "port", 8209) if backend == "llamacpp" else 8209
            # Wait for the freshly-started service to bind + load the model.
            for _ in range(60):
                if llamacpp_setup.status(port=verify_port).running:
                    break
                time.sleep(2)
            ok = llamacpp_setup.smoke_test(port=verify_port)
        else:  # ollama
            ok = ollama_smoke_test(model)
        if not ok:
            print(
                "  ERROR: Smoke test failed — the endpoint did not return a valid "
                "completion. The backend may have failed to start or load the model. "
                "Check logs and re-run.",
                file=sys.stderr,
            )
            return 1
    print(f"  Endpoint ready: {endpoint}")

    # Step 5: Persist config
    print()
    print("  [5/5] Saving configuration...")
    save_saved_config({
        "setup_backend": backend,
        "inference_url": endpoint,
        "inference_model": model_name,
    })
    print("  Config saved to ~/.aither/config.json")

    # Banner
    print()
    print("=" * 60)
    print("  Local Inference Backend Ready!")
    print("=" * 60)
    print()
    print(f"  Endpoint:  {endpoint}")
    print(f"  Model:     {model_name}")
    print(f"  Backend:   {backend}")
    print()
    print("  Next steps:")
    print("    adk start              Chat with your codebase")
    print("    adk shell              Interactive terminal")
    print("    adk doctor             Check system health")
    print()

    return 0


def cmd_status(args):
    """Show the running agent (adk up) plus backend and service status."""
    import asyncio

    from adk import agent_daemon as daemon

    # ── Local agent (from `adk up`) ──
    st = daemon.read_status()
    agent_state = None
    if st:
        alive = daemon.pid_alive(st.get("server_pid"))
        healthy = daemon.wait_for_health(st.get("port", 8080), timeout=3) if alive else False
        agent_state = {
            **st,
            "running": alive,
            "health": "healthy" if healthy else ("stale" if not alive else "unhealthy"),
            "tunnel_running": daemon.pid_alive(st.get("tunnel_pid")),
        }

    if getattr(args, "json", False):
        print(json.dumps(agent_state or {"running": False}, indent=2))
        return 0

    if agent_state:
        print("AitherADK Agent")
        print("=" * 50)
        icon = "+" if agent_state["running"] else "-"
        print(f"  [{icon}] {agent_state.get('identity', 'aither'):12s} "
              f":{agent_state.get('port')} ({agent_state['health']})")
        if agent_state.get("invoke_url"):
            print(f"      tunnel: {agent_state['invoke_url']} "
                  f"({'up' if agent_state['tunnel_running'] else 'down'})")
        print(f"      registered: {agent_state.get('registered')}   "
              f"autostart: {agent_state.get('autostart') or 'no'}")
        print()

    async def _status():
        import httpx
        from adk.config import load_saved_config

        checks = {
            "Genesis": os.environ.get("AITHER_URL", "http://localhost:8001"),
            "vLLM": os.environ.get("AITHER_VLLM_URL", os.environ.get("VLLM_URL", "http://localhost:8209")),
            "Ollama": _fix_ollama_host(os.environ.get("OLLAMA_HOST", "")),
            "awnode": "http://localhost:8090",
            "Gateway": os.environ.get("AITHER_GATEWAY_URL", "https://gateway.aitherium.com"),
        }

        # Add Qdrant if configured
        qdrant_url = os.environ.get("AITHER_FLEET_QDRANT_URL", "")
        if not qdrant_url:
            try:
                saved = load_saved_config()
                qdrant_url = saved.get("qdrant_url", "")
            except Exception:
                pass
        if qdrant_url:
            checks["Qdrant"] = qdrant_url

        print("AitherADK Backend Status")
        print("=" * 50)
        for name, url in checks.items():
            try:
                async with httpx.AsyncClient(timeout=3.0) as c:
                    hp = "/api/tags" if name == "Ollama" else "/health"
                    if name == "Qdrant":
                        hp = "/healthz"
                    r = await c.get(f"{url.rstrip('/')}{hp}")
                    status = "UP" if r.status_code == 200 else f"HTTP {r.status_code}"
            except Exception:
                status = "DOWN"
            icon = "+" if status == "UP" else "-"
            print(f"  [{icon}] {name:12s} {url:45s} {status}")

        # Scan additional vLLM ports
        for extra_port in [8201, 8202, 8203, 8209]:
            try:
                async with httpx.AsyncClient(timeout=2.0) as c:
                    r = await c.get(f"http://localhost:{extra_port}/health")
                    if r.status_code == 200:
                        url = f"http://localhost:{extra_port}"
                        print(f"  [+] {'vLLM':12s} {url:45s} UP")
            except Exception:
                pass

        # API key check
        saved = {}
        try:
            saved = load_saved_config()
        except Exception:
            pass

        api_key = os.environ.get("AITHER_API_KEY", "")
        if api_key:
            print(f"\n  Portal API Key: {api_key[:16]}...{api_key[-4:]}")
        elif saved.get("api_key"):
            print(f"\n  Portal API Key (saved): {saved['api_key'][:16]}...")
        else:
            print("\n  Portal: No API key. Run: adk connect --api-key <key>")

        # Qdrant API key check
        qdrant_key = os.environ.get("AITHER_FLEET_QDRANT_API_KEY", "")
        if qdrant_key:
            print(f"  Qdrant API Key: {qdrant_key[:16]}...{qdrant_key[-4:]}")
        elif saved.get("qdrant_api_key"):
            print(f"  Qdrant API Key (saved): {saved['qdrant_api_key'][:16]}...")
        else:
            if qdrant_url:
                print("  Qdrant: No API key configured. Run: adk stack qdrant")

    asyncio.run(_status())
    return 0


def cmd_start(args):
    """Zero-config agent start — index, connect, chat. Works for anyone."""
    import asyncio
    import time as _time

    target = os.path.abspath(args.path or ".")
    project_name = os.path.basename(target)

    # ── Banner ──────────────────────────────────────────────────────
    print()
    print("  AitherADK")
    print("  =========")
    print()

    # ── Step 1: Detect project ──────────────────────────────────────
    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv",
                 ".tox", "dist", "build", ".mypy_cache", "site-packages"}

    def _count_files(root, ext):
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            count += sum(1 for f in filenames if f.endswith(ext))
            if count > 5000:
                break  # Good enough
        return count

    py_count = _count_files(target, ".py")
    ts_count = _count_files(target, ".ts")
    js_count = _count_files(target, ".js")

    # Count all file types for a richer picture
    md_count = _count_files(target, ".md")
    txt_count = _count_files(target, ".txt")
    json_count = _count_files(target, ".json")
    yaml_count = _count_files(target, ".yaml") + _count_files(target, ".yml")
    total_files = py_count + ts_count + js_count + md_count + txt_count + json_count + yaml_count

    # Classify workspace type
    lang = None
    if py_count >= ts_count and py_count >= js_count and py_count > 5:
        lang = "Python"
    elif ts_count > 5:
        lang = "TypeScript"
    elif js_count > 5:
        lang = "JavaScript"

    workspace_parts = []
    if lang:
        workspace_parts.append(f"{lang} ({py_count or ts_count or js_count} files)")
    if md_count > 0:
        workspace_parts.append(f"{md_count} docs")
    if json_count + yaml_count > 0:
        workspace_parts.append(f"{json_count + yaml_count} configs")
    if txt_count > 0:
        workspace_parts.append(f"{txt_count} text files")

    if workspace_parts:
        print(f"  Workspace:  {project_name} -- {', '.join(workspace_parts)}")
    else:
        print(f"  Workspace:  {project_name} (empty or no recognized files)")
    print(f"  Directory:  {target}")

    # ── Step 2: Detect LLM backend ──────────────────────────────────
    explicit_model = getattr(args, "model", None)
    explicit_provider = getattr(args, "provider", None)
    if explicit_model or explicit_provider:
        llm_info = _resolve_explicit_backend(explicit_provider, explicit_model)
    else:
        llm_info = _detect_llm_backend()
    print(f"  LLM:        {llm_info['display']}")

    # ── Step 3: Index codebase (if applicable) ────────────────────
    code_graph = None
    memory_graph = None  # pre-existing REPL /stats,/memory refs were undefined (NameError)
    if lang == "Python" and py_count > 0:
        from adk.faculties.code_graph import CodeGraph
        code_graph = CodeGraph()
        print()
        print(f"  Indexing {py_count} Python files...", end="", flush=True)
        t0 = _time.perf_counter()
        stats = asyncio.run(code_graph.index_codebase(target))
        elapsed = _time.perf_counter() - t0
        print(f" {stats['total_chunks']:,} chunks in {elapsed:.1f}s")
    elif total_files > 0:
        print("  Code index: Skipped (no Python files -- code search works with Python)")
    else:
        print("  Code index: Skipped (empty directory)")

    # ── Step 4: Set up memory ───────────────────────────────────────
    # Suppress noisy warnings for casual use
    _logging = __import__("logging")
    _logging.getLogger("adk.graph_memory").setLevel(_logging.ERROR)
    _logging.getLogger("adk.identity").setLevel(_logging.ERROR)

    from adk.graph_memory import GraphMemory
    graph = GraphMemory(agent_name=project_name)
    mem_stats = asyncio.run(graph.get_stats())
    if mem_stats.get("nodes", 0) > 0:
        print(f"  Memory:     {mem_stats['nodes']} memories restored from previous sessions")
    else:
        print("  Memory:     New (will persist across sessions)")

    # ── Step 5: Build agent ─────────────────────────────────────────
    print()

    from adk.agent import AitherAgent
    from adk.llm import LLMRouter

    llm_kwargs = {}
    if llm_info.get("provider"):
        llm_kwargs["provider"] = llm_info["provider"]
    if llm_info.get("base_url"):
        llm_kwargs["base_url"] = llm_info["base_url"]
    if llm_info.get("model"):
        llm_kwargs["model"] = llm_info["model"]
    if llm_info.get("api_key"):
        llm_kwargs["api_key"] = llm_info["api_key"]

    llm = LLMRouter(**llm_kwargs) if llm_kwargs else None

    # Build system prompt based on what's available
    prompt_parts = [
        f"You are a helpful assistant for the '{project_name}' workspace.",
        f"The workspace is at: {target}",
    ]
    if code_graph:
        prompt_parts.append(
            "You have code_search and code_context tools — ALWAYS search before answering code questions."
        )
    prompt_parts.append(
        "You have remember/recall tools for persistent memory across sessions. "
        "Use them proactively to store important findings and user preferences."
    )
    prompt_parts.append(
        "You also have file tools (read_file, write_file, search_files, list_directory) "
        "for working with any files in the workspace."
    )
    prompt_parts.append(
        "Be direct and helpful. If you're unsure, search first, then answer."
    )

    agent = AitherAgent(
        name=project_name,
        llm=llm,
        system_prompt=" ".join(prompt_parts),
    )

    if code_graph:
        agent.set_code_graph(code_graph)
    # GraphMemory is already wired via agent._graph in __init__

    # ── Step 6: Interactive chat loop ───────────────────────────────
    print()
    capabilities = []
    if code_graph:
        capabilities.append("search your code")
    capabilities.append("read/write files")
    capabilities.append("remember things across sessions")
    print(f"  Ready! I can {', '.join(capabilities)}.")
    print("  Just ask a question. Type /help for commands, /quit to exit.")
    print()

    session_id = agent.new_session()

    async def _chat_loop():
        while True:
            try:
                user_input = input("  You > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user_input:
                continue

            if user_input.lower() in ("/quit", "/exit", "/q"):
                break

            if user_input.lower() == "/help":
                print()
                print("  /quit     Exit")
                print("  /stats    Show index stats")
                print("  /memory   Show memory stats")
                print("  /forget   Clear session memory")
                print("  /reindex  Re-index the codebase")
                print()
                continue

            if user_input.lower() == "/stats":
                if code_graph:
                    print(f"  Code index: {len(code_graph.chunks):,} chunks, "
                          f"{code_graph.total_files} files, "
                          f"{code_graph.memory_usage_mb:.1f}MB")
                if memory_graph:
                    ms = memory_graph.get_stats()
                    print(f"  Memory:     {ms['nodes']} memories, {ms['edges']} connections")
                print(f"  Workspace:  {target}")
                continue

            if user_input.lower() == "/memory":
                if memory_graph:
                    ms = memory_graph.get_stats()
                    print(f"  Nodes: {ms['nodes']}, Edges: {ms['edges']}, "
                          f"Embeddings: {ms['embeddings_cached']}")
                else:
                    print("  (memory graph not initialized)")
                continue

            if user_input.lower() == "/forget":
                agent.new_session()
                print("  Session cleared.")
                continue

            if user_input.lower() == "/reindex":
                if code_graph:
                    print("  Re-indexing...", end="", flush=True)
                    stats = await code_graph.index_codebase(target)
                    print(f" {stats['total_chunks']:,} chunks")
                continue

            # Chat
            try:
                response = await agent.chat(
                    user_input,
                    session_id=session_id,
                    effort=5,
                )
                print()
                print(f"  {response.content}")
                print()
                if response.tool_calls_made:
                    tools_used = ", ".join(set(
                        t.split("[")[0] for t in response.tool_calls_made
                    ))
                    print(f"  [tools: {tools_used}]")
                    print()
            except Exception as e:
                print(f"\n  Error: {e}\n")

    asyncio.run(_chat_loop())

    # Save memory on exit (suppress noisy HMAC warnings)
    logging = __import__("logging")
    logging.getLogger("adk.faculties").setLevel(logging.ERROR)
    if memory_graph:
        memory_graph.save()
    print("  Memory saved. Goodbye!")
    return 0


def _resolve_explicit_backend(provider: str = None, model: str = None) -> dict:
    """Resolve an explicitly requested provider/model to connection info."""
    provider_keys_path = Path.home() / ".aither" / "provider_keys.json"
    keys = {}
    if provider_keys_path.exists():
        try:
            import json as _j
            keys = _j.loads(provider_keys_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Shortcut names
    shortcuts = {
        "deepseek-flash": ("deepseek", "deepseek-v4-flash"),
        "deepseek-pro": ("deepseek", "deepseek-v4-pro"),
        "deepseek": ("deepseek", "deepseek-v4-flash"),
        "openrouter": ("openrouter", "deepseek/deepseek-v4-flash"),
        "ollama": ("ollama", "llama3.2:latest"),
    }

    if model and model in shortcuts:
        provider, model = shortcuts[model]
    if not provider:
        provider = "deepseek"

    configs = {
        "deepseek": {
            "provider": "openai",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": keys.get("deepseek", os.environ.get("DEEPSEEK_API_KEY", "")),
            "model": model or "deepseek-v4-flash",
            "display": f"DeepSeek ({model or 'deepseek-v4-flash'})",
        },
        "openrouter": {
            "provider": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": keys.get("openrouter", os.environ.get("OPENROUTER_API_KEY", "")),
            "model": model or "deepseek/deepseek-v4-flash",
            "display": f"OpenRouter ({model or 'deepseek/deepseek-v4-flash'})",
        },
        "ollama": {
            "provider": "ollama",
            "model": model or "llama3.2:latest",
            "display": f"Ollama ({model or 'llama3.2:latest'})",
        },
        "openai": {
            "provider": "openai",
            "api_key": keys.get("openai", os.environ.get("OPENAI_API_KEY", "")),
            "model": model or "gpt-4o-mini",
            "display": f"OpenAI ({model or 'gpt-4o-mini'})",
        },
        "anthropic": {
            "provider": "anthropic",
            "api_key": keys.get("anthropic", os.environ.get("ANTHROPIC_API_KEY", "")),
            "model": model or "claude-sonnet-4-6",
            "display": f"Anthropic ({model or 'claude-sonnet-4-6'})",
        },
    }

    info = configs.get(provider, configs["deepseek"])
    if not info.get("api_key") and provider not in ("ollama",):
        info["display"] += " [NO KEY — run: adk keys set " + provider + "]"
    return info


def _detect_llm_backend():
    """Detect available LLM backend. Returns dict with provider info."""
    import shutil

    # 1. Check for Ollama
    ollama_bin = shutil.which("ollama")
    if ollama_bin:
        try:
            import httpx
            resp = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                model_names = [m["name"] for m in models]
                # Pick best available model
                preferred = [
                    "llama3.2:latest", "llama3.2:3b", "llama3.1:8b",
                    "mistral:latest", "qwen2.5:7b",
                ]
                chosen = None
                for p in preferred:
                    if p in model_names:
                        chosen = p
                        break
                if not chosen and model_names:
                    chosen = model_names[0]
                if chosen:
                    return {
                        "provider": "ollama",
                        "model": chosen,
                        "display": f"Ollama ({chosen})",
                    }
                else:
                    return {
                        "provider": "ollama",
                        "display": "Ollama (no models pulled — run: ollama pull llama3.2)",
                    }
        except Exception:
            pass

    # 2. Check for vLLM
    try:
        import httpx
        for port in (8201, 8202, 8203, 8209, 8000):
            try:
                resp = httpx.get(f"http://localhost:{port}/v1/models", timeout=1.0)
                if resp.status_code == 200:
                    data = resp.json()
                    model_id = data["data"][0]["id"] if data.get("data") else "unknown"
                    return {
                        "provider": "openai",
                        "base_url": f"http://localhost:{port}/v1",
                        "model": model_id,
                        "api_key": "not-needed",
                        "display": f"vLLM ({model_id})",
                    }
            except Exception:
                continue
    except ImportError:
        pass

    # 3. Check for Elysium API key
    api_key = os.environ.get("AITHER_API_KEY", "")
    if not api_key:
        config_path = Path.home() / ".aither" / "config.json"
        if config_path.exists():
            try:
                import json as _j
                cfg = _j.loads(config_path.read_text(encoding="utf-8"))
                api_key = cfg.get("api_key", "")
            except Exception:
                pass
    if api_key:
        return {
            "provider": "gateway",
            "base_url": "https://mcp.aitherium.com/v1",
            "api_key": api_key,
            "model": "aither-orchestrator",
            "display": "Elysium Cloud (aither-orchestrator)",
        }

    # 4. Check for provider keys (DeepSeek, OpenRouter, etc.)
    provider_keys_path = Path.home() / ".aither" / "provider_keys.json"
    if provider_keys_path.exists():
        try:
            import json as _j
            pkeys = _j.loads(provider_keys_path.read_text(encoding="utf-8"))
            if pkeys.get("deepseek"):
                return {
                    "provider": "openai",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": pkeys["deepseek"],
                    "model": "deepseek-v4-flash",
                    "display": "DeepSeek V4 Flash (1M context)",
                }
            if pkeys.get("openrouter"):
                return {
                    "provider": "openai",
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key": pkeys["openrouter"],
                    "model": "deepseek/deepseek-v4-flash",
                    "display": "OpenRouter (DeepSeek V4 Flash)",
                }
        except Exception:
            pass

    # 5. Check for OpenAI key
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key:
        return {
            "provider": "openai",
            "api_key": openai_key,
            "model": "gpt-4o-mini",
            "display": "OpenAI (gpt-4o-mini)",
        }

    # 5. Check for Anthropic key
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        return {
            "provider": "anthropic",
            "api_key": anthropic_key,
            "model": "claude-sonnet-4-20250514",
            "display": "Anthropic (claude-sonnet-4-20250514)",
        }

    return {
        "display": "None detected! Install Ollama (ollama.com) or set AITHER_API_KEY",
    }


def cmd_index(args):
    """Index a codebase for code search via CodeGraph."""
    import asyncio
    import time as _time

    target = os.path.abspath(args.path)
    if not os.path.isdir(target):
        print(f"Error: {target} is not a directory")
        return 1

    print(f"Indexing: {target}")
    print()

    from adk.faculties.code_graph import CodeGraph

    cg = CodeGraph()

    def on_progress(frac, msg):
        bar_len = 30
        filled = int(bar_len * frac)
        bar = "#" * filled + "-" * (bar_len - filled)
        print(f"\r  [{bar}] {frac*100:5.1f}%  {msg:<50}", end="", flush=True)

    t0 = _time.perf_counter()
    stats = asyncio.run(cg.index_codebase(target, on_progress=on_progress))
    elapsed = _time.perf_counter() - t0
    print()  # newline after progress bar
    print()
    print(f"  Files:      {stats['total_files']:,}")
    print(f"  Functions:  {stats['functions']:,}")
    print(f"  Methods:    {stats['methods']:,}")
    print(f"  Classes:    {stats['classes']:,}")
    print(f"  Total:      {stats['total_chunks']:,} chunks in {elapsed:.1f}s")

    if args.embed:
        print()
        print("Generating embeddings...")
        try:
            embed_stats = asyncio.run(cg.embed_chunks(on_progress=on_progress))
            print()
            print(f"  Embedded:   {embed_stats.get('new', 0)} new, {embed_stats.get('cached', 0)} cached")
            print(f"  Backend:    {embed_stats.get('model', 'unknown')}")
        except Exception as e:
            print(f"\n  Embedding failed: {e}")
            print("  (Install sentence-transformers for local embeddings, or set AITHER_API_KEY for cloud)")

    if args.stats:
        print()
        metrics = cg.get_python_metrics()
        print(f"  Total lines:      {metrics['total_py_lines']:,}")
        print(f"  Avg complexity:   {metrics['avg_complexity']}")
        print(f"  Test functions:   {metrics['test_functions']:,}")
        if metrics.get("top_complex_files"):
            print("  Most complex:")
            for name, cx in metrics["top_complex_files"][:5]:
                print(f"    {name}: {cx}")

    # Test a sample query
    print()
    sample_results = asyncio.run(cg.query("main", max_results=3))
    if sample_results:
        print("  Sample query 'main':")
        for r in sample_results:
            print(f"    {r.chunk_type.value}: {r.name} @ {Path(r.source_path).name}:{r.start_line}")

    print()
    print("Done! Use in your agent:")
    print()
    print("    from adk.faculties import CodeGraph")
    print("    cg = CodeGraph()")
    print(f"    await cg.index_codebase(\"{target}\")")
    print("    agent.set_code_graph(cg)")
    return 0


def _connect_elysium(args):
    """Connect to a desktop AitherOS instance via --elysium flag."""
    import asyncio

    async def _run():
        from adk.elysium_connect import connect_to_desktop

        url = args.elysium
        token = getattr(args, "token", None)

        print()
        print("  AitherOS Desktop Connect (Elysium)")
        print("  ===================================")
        print()
        print(f"  Desktop: {url}")

        result = await connect_to_desktop(url, token=token)

        if not result.get("ok"):
            print(f"  [!!] Connection failed: {result.get('error', 'unknown')}")
            return 1

        print("  [OK] Genesis reachable")

        if result.get("token"):
            print(f"  [OK] Node token: {result['token'][:16]}...")

        if result.get("mesh_joined"):
            print(f"  [OK] Mesh joined (node: {result.get('node_id', 'unknown')[:16]})")
        else:
            print("  [--] Mesh join: skipped or failed")

        if result.get("wireguard"):
            print("  [OK] WireGuard tunnel active")
        else:
            print("  [--] WireGuard: not configured (direct LAN is fine)")

        print(f"  [OK] Remote inference: {result.get('core_llm_url', 'N/A')}")
        print(f"  [OK] Config saved to {result.get('config_saved', '~/.aither/config.json')}")

        print()
        print("  Next steps:")
        print("    adk run              # Start agent server")
        print("    adk run --mesh       # Start with mesh hosting (share your tools)")
        print("    adk status           # Check backend status")
        print()

        return 0

    return asyncio.run(_run())


def cmd_admin(args):
    """Administration commands."""

    admin_cmd = getattr(args, "admin_command", None)

    if admin_cmd == "create-token":
        return _admin_create_token(args)
    else:
        print("  Usage: adk admin create-token --name <name> --url <genesis-url>")
        return 1


def _admin_create_token(args):
    """Create a node token on the desktop for mesh enrollment."""
    import asyncio
    import platform as plat

    async def _run():
        import httpx

        url = args.url.rstrip("/")
        name = args.name or plat.node()

        print()
        print("  AitherOS Admin — Create Node Token")
        print("  ===================================")
        print()
        print(f"  Genesis: {url}")
        print(f"  Node name: {name}")

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{url}/admin/nodes/create",
                    json={
                        "node_name": name,
                        "capabilities": ["mcp", "inference"],
                    },
                )
                if resp.status_code in (200, 201):
                    data = resp.json()
                    token = data.get("token") or data.get("node_token", "")
                    node_id = data.get("node_id", "")

                    print()
                    print("  [OK] Token created!")
                    print(f"  Node ID: {node_id}")
                    print(f"  Token:   {token}")
                    print()
                    print("  Use on the laptop:")
                    print(f"    adk connect --elysium {url} --token {token}")
                    print()
                    print("  Or set environment variables:")
                    print(f"    export AITHER_CORE_URL={url}")
                    print(f"    export AITHER_NODE_TOKEN={token}")
                    print()

                    # Save to local config
                    save_saved_config({
                        "admin_last_token": token,
                        "admin_last_node_id": node_id,
                    })

                    return 0
                else:
                    print(f"  [!!] Failed: HTTP {resp.status_code}")
                    print(f"       {resp.text[:200]}")
                    return 1
        except Exception as e:
            print(f"  [!!] Error: {e}")
            return 1

    return asyncio.run(_run())


def cmd_disconnect(args):
    """Disconnect from desktop AitherOS mesh."""
    import asyncio

    async def _run():
        from adk.elysium_connect import disconnect_from_desktop

        print()
        print("  Disconnecting from desktop mesh...")

        result = await disconnect_from_desktop()

        if result.get("mesh_left"):
            print("  [OK] Left mesh")
        if result.get("wireguard_down"):
            print("  [OK] WireGuard tunnel torn down")
        if result.get("config_cleared"):
            print("  [OK] Elysium config cleared")

        print("  Done.")
        print()
        return 0

    return asyncio.run(_run())


def cmd_jobs(args) -> int:
    """Manage background jobs.

    LOCAL by default (jobs run on THIS machine, tracked in ~/.aither/jobs.db);
    pass --remote to operate on cloud expeditions via genesis/portal instead.
    """
    import asyncio

    jobs_cmd = getattr(args, "jobs_command", None)
    remote = bool(getattr(args, "remote", False))

    # Local engine subcommands (no server required).
    if jobs_cmd == "run":
        return _jobs_run_local(args)
    elif jobs_cmd == "start":
        return _jobs_start_local(args)
    elif jobs_cmd == "cancel":
        return _jobs_cancel_local(args)
    elif jobs_cmd == "sync":
        return _jobs_sync_local(args)
    elif jobs_cmd == "_exec":
        from adk.local_jobs import _execute
        _execute(args.id)
        return 0

    # Read/steer: remote (cloud) when --remote, else the local store.
    if jobs_cmd == "list":
        return asyncio.run(_jobs_list(args)) if remote else _jobs_list_local(args)
    elif jobs_cmd == "status":
        return asyncio.run(_jobs_status(args)) if remote else _jobs_status_local(args)
    elif jobs_cmd == "steer":
        return asyncio.run(_jobs_steer(args)) if remote else _jobs_steer_local(args, "append")
    elif jobs_cmd == "hint":
        return asyncio.run(_jobs_hint(args)) if remote else _jobs_steer_local(args, "hint")
    elif jobs_cmd == "watch":
        return asyncio.run(_jobs_watch(args))  # SSE watch is cloud-only
    else:
        print("Usage: adk jobs [run|start|list|status|steer|hint|cancel|watch|sync]")
        print("  Local by default; add --remote to list/status/steer against the cloud.")
        return 1


# ── Local job engine handlers (~/.aither/jobs.db) ─────────────────────────────

def _jobs_run_local(args) -> int:
    """Run a job locally in the foreground and print its result."""
    from adk.local_jobs import run_foreground
    query = " ".join(args.query) if isinstance(args.query, list) else args.query
    print(f"  Running locally: {query[:70]}")
    row = run_foreground(query, agent=getattr(args, "agent", "aither"))
    status = row.get("status", "?")
    if status == "completed":
        print(f"\n  [done] job {row.get('id', '')[:8]}\n")
        print(row.get("result", "") or "  (no output)")
    else:
        print(f"  [{status}] {row.get('error', '') or ''}")
    return 0 if status == "completed" else 1


def _jobs_start_local(args) -> int:
    """Start a local job in the background (detached) and return its id."""
    from adk.local_jobs import spawn
    query = " ".join(args.query) if isinstance(args.query, list) else args.query
    jid = spawn(query, agent=getattr(args, "agent", "aither"))
    print(f"  Started local job {jid[:8]} in the background.")
    print(f"  Status: adk jobs status {jid[:8]}   ·   Cancel: adk jobs cancel {jid[:8]}")
    return 0


def _jobs_list_local(args) -> int:
    """List local jobs from ~/.aither/jobs.db."""
    from adk.local_jobs import get_store
    jobs = get_store().list()
    if not jobs:
        print("  No local jobs. Start one: adk jobs start \"<task>\"")
        return 0
    print("\n  Local Jobs")
    print("  " + "=" * 70)
    print(f"  {'ID':<10s} {'Status':<11s} {'Synced':<7s} {'Query':<40s}")
    print("  " + "-" * 70)
    for j in jobs:
        synced = "yes" if j.get("remote_id") else "-"
        print(f"  {j['id'][:8]:<10s} {j['status']:<11s} {synced:<7s} {(j['query'] or '')[:40]:<40s}")
    print()
    return 0


def _jobs_status_local(args) -> int:
    """Show a local job's status + result."""
    from adk.local_jobs import get_store
    job = get_store().get(args.id)
    if not job:
        print(f"  Local job not found: {args.id}")
        return 1
    print(f"\n  Job {job['id'][:8]}  [{job['status']}]")
    print(f"  Query: {job['query']}")
    if job.get("remote_id"):
        print(f"  Synced → {job.get('remote_url', '')}/expedition/{job['remote_id']}")
    if job.get("result"):
        print("\n" + job["result"])
    if job.get("error"):
        print(f"\n  Error: {job['error']}")
    return 0


def _jobs_steer_local(args, action: str) -> int:
    """Queue a steering/hint nudge for a running local job."""
    from adk.local_jobs import steer
    ok = steer(args.id, args.message, action)
    print(f"  {'Steered' if ok else 'Job not found:'} {args.id}" + ("" if ok else ""))
    return 0 if ok else 1


def _jobs_cancel_local(args) -> int:
    """Cancel a running local job."""
    from adk.local_jobs import cancel
    ok = cancel(args.id)
    print(f"  {'Cancelled' if ok else 'Not cancellable (missing or already finished):'} {args.id}")
    return 0 if ok else 1


def _jobs_sync_local(args) -> int:
    """Push a local job up to the portal, or pull its remote status back."""
    from adk.local_jobs import pull, push
    if args.direction == "push":
        res = push(args.id)
        if res.get("ok"):
            print(f"  Pushed local job {args.id} → portal expedition {res.get('remote_id', '')[:8]}")
            return 0
        print(f"  Push failed: {res.get('error')}")
        return 1
    else:
        res = pull(args.id)
        if res.get("ok"):
            print(f"  Pulled: local job {args.id} is now '{res.get('status')}'")
            return 0
        print(f"  Pull failed: {res.get('error')}")
        return 1


async def _jobs_list(args) -> int:
    """List all jobs and expeditions."""
    import httpx

    genesis_url = os.environ.get(
        "AITHER_GENESIS_URL", os.environ.get("AITHER_URL", "http://localhost:8001")
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{genesis_url.rstrip('/')}/expedition/list")
            if resp.status_code == 200:
                data = resp.json()
                expeditions = data.get("expeditions", [])

                if not expeditions:
                    print("  No jobs found.")
                    return 0

                print("\n  Expeditions / Jobs")
                print("  " + "=" * 70)
                print(
                    f"  {'ID':<20s} {'Status':<12s} {'Title':<36s}"
                )
                print("  " + "-" * 70)
                for exp in expeditions:
                    exp_id = exp.get("id", "unknown")[:20]
                    status = exp.get("status", "unknown")[:12]
                    title = exp.get("title", "")[:36]
                    print(f"  {exp_id:<20s} {status:<12s} {title:<36s}")
                print()
                return 0
            elif resp.status_code == 404:
                print("  Error: Genesis /expedition/list not found (404)")
                return 1
            else:
                print(
                    f"  Error: HTTP {resp.status_code} from {genesis_url}"
                )
                return 1
    except Exception as e:
        print(f"  Error: Could not reach Genesis at {genesis_url}")
        print(f"         {e}")
        return 1


async def _jobs_status(args) -> int:
    """Show status of a job."""
    import httpx

    genesis_url = os.environ.get(
        "AITHER_GENESIS_URL", os.environ.get("AITHER_URL", "http://localhost:8001")
    )
    exp_id = args.id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Get expedition status
            status_resp = await client.get(
                f"{genesis_url.rstrip('/')}/expedition/{exp_id}/status"
            )
            if status_resp.status_code == 404:
                print(f"  Error: Expedition '{exp_id}' not found")
                return 1
            elif status_resp.status_code != 200:
                print(
                    f"  Error: HTTP {status_resp.status_code} from {genesis_url}"
                )
                return 1

            status_data = status_resp.json()

            # Get expedition tasks
            tasks_resp = await client.get(
                f"{genesis_url.rstrip('/')}/expedition/{exp_id}/tasks"
            )
            tasks = []
            if tasks_resp.status_code == 200:
                tasks = tasks_resp.json().get("tasks", [])

            # Print status
            print()
            print(f"  Expedition: {exp_id}")
            print("  " + "=" * 70)
            for key, value in status_data.items():
                if key != "id":
                    print(f"  {key:.<20s} {value}")

            # Print tasks if available
            if tasks:
                print()
                print("  Tasks:")
                print("  " + "-" * 70)
                for task in tasks:
                    task_id = task.get("id", "unknown")
                    task_status = task.get("status", "unknown")
                    task_title = task.get("title", "")
                    print(f"    [{task_status:>8s}] {task_id:.<24s} {task_title}")
                    if task.get("result_summary"):
                        print(f"             Result: {task['result_summary'][:50]}")
                    if task.get("error"):
                        print(f"             Error: {task['error'][:50]}")
            print()
            return 0
    except Exception as e:
        print(f"  Error: Could not reach Genesis at {genesis_url}")
        print(f"         {e}")
        return 1


async def _jobs_steer(args) -> int:
    """Send a follow-up message to a job."""
    import httpx

    genesis_url = os.environ.get(
        "AITHER_GENESIS_URL", os.environ.get("AITHER_URL", "http://localhost:8001")
    )
    exp_id = args.id
    message = args.message

    if not message:
        print("  Error: message is required")
        return 1

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{genesis_url.rstrip('/')}/expedition/{exp_id}/steer",
                json={"message": message, "action": "append"},
            )
            if resp.status_code == 404:
                print(f"  Error: Expedition '{exp_id}' not found")
                return 1
            elif resp.status_code == 400:
                print("  Error: Invalid request (400)")
                return 1
            elif resp.status_code in (200, 201):
                data = resp.json()
                print(f"  [OK] Message sent to {exp_id}")
                if data.get("status"):
                    print(f"       Status: {data['status']}")
                return 0
            else:
                print(
                    f"  Error: HTTP {resp.status_code} from {genesis_url}"
                )
                return 1
    except Exception as e:
        print(f"  Error: Could not reach Genesis at {genesis_url}")
        print(f"         {e}")
        return 1


async def _jobs_hint(args) -> int:
    """Send an invisible hint to a job."""
    import httpx

    genesis_url = os.environ.get(
        "AITHER_GENESIS_URL", os.environ.get("AITHER_URL", "http://localhost:8001")
    )
    exp_id = args.id
    message = args.message

    if not message:
        print("  Error: message is required")
        return 1

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{genesis_url.rstrip('/')}/expedition/{exp_id}/steer",
                json={"message": message, "action": "hint"},
            )
            if resp.status_code == 404:
                print(f"  Error: Expedition '{exp_id}' not found")
                return 1
            elif resp.status_code == 400:
                print("  Error: Invalid request (400)")
                return 1
            elif resp.status_code in (200, 201):
                data = resp.json()
                print(f"  [OK] Hint sent to {exp_id}")
                if data.get("status"):
                    print(f"       Status: {data['status']}")
                return 0
            else:
                print(
                    f"  Error: HTTP {resp.status_code} from {genesis_url}"
                )
                return 1
    except Exception as e:
        print(f"  Error: Could not reach Genesis at {genesis_url}")
        print(f"         {e}")
        return 1


async def _jobs_watch(args) -> int:
    """Watch a job's progress in real-time via SSE."""
    import httpx

    genesis_url = os.environ.get(
        "AITHER_GENESIS_URL", os.environ.get("AITHER_URL", "http://localhost:8001")
    )
    exp_id = args.id

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET",
                f"{genesis_url.rstrip('/')}/expedition/{exp_id}/stream",
            ) as resp:
                if resp.status_code == 404:
                    print(f"  Error: Expedition '{exp_id}' not found")
                    return 1
                elif resp.status_code != 200:
                    print(
                        f"  Error: HTTP {resp.status_code} from {genesis_url}"
                    )
                    return 1

                print(f"\n  Watching expedition {exp_id}...")
                print("  " + "=" * 70)

                try:
                    async for line in resp.aiter_lines():
                        if not line or line.startswith(":"):
                            # Skip empty lines and comments
                            continue
                        if line.startswith("data: "):
                            # Parse SSE data line
                            data_str = line[6:].strip()
                            if data_str:
                                try:
                                    import json as _json

                                    data = _json.loads(data_str)
                                    # Print event details
                                    event_type = data.get("type", "event")
                                    msg = data.get("message", "")
                                    if msg:
                                        print(f"  [{event_type}] {msg}")
                                except (ValueError, AttributeError):
                                    # Fallback for unparseable JSON
                                    print(f"  {data_str}")
                except KeyboardInterrupt:
                    print("\n  Watching stopped.")
                    return 0

                print("  " + "=" * 70)
                return 0
    except Exception as e:
        print("  Error: Could not watch expedition")
        print(f"         {e}")
        return 1


def _cmd_cron(args) -> int:
    """Handle `adk cron` subcommands."""
    sub = getattr(args, "cron_command", None)
    try:
        from adk.cron import CronScheduler
    except ImportError:
        print("Cron module not available.")
        return 1

    sched = CronScheduler()

    if sub == "list" or sub is None:
        jobs = sched.list_jobs()
        if not jobs:
            print("No cron jobs configured.")
            return 0
        print(f"{'Name':<30} {'Expression':<20} {'Enabled'}")
        print("-" * 60)
        for j in jobs:
            print(f"{j.name:<30} {j.expression:<20} {'yes' if j.enabled else 'no'}")
        return 0

    if sub == "add":
        expr = args.expression
        name = args.task_name
        # Register a placeholder -- actual task binding happens programmatically
        sched.add(expr, task=None, name=name)
        print(f"Added cron job '{name}' with schedule: {expr}")
        print("Note: bind a task function programmatically via CronScheduler.add()")
        return 0

    if sub == "remove":
        if sched.remove(args.name):
            print(f"Removed cron job '{args.name}'")
        else:
            print(f"Job '{args.name}' not found")
        return 0

    print("Usage: adk cron [list|add|remove]")
    return 1


def _cmd_addon(args) -> int:
    """Handle `adk addon` subcommands."""
    import asyncio

    sub = getattr(args, "addon_command", None)

    if sub == "list" or sub is None:
        from adk.addon_manager import load_all_manifests, AddonManager
        manifests = load_all_manifests()
        mgr = AddonManager()
        states = {s.get("addon_id"): s for s in [inst.to_dict() for inst in asyncio.run(mgr.status())]}

        if not manifests:
            print("No addon manifests found.")
            return 0

        print(f"{'Addon':<20} {'Type':<10} {'Port':<7} {'Plan':<12} {'Status':<10} {'Pack'}")
        print("-" * 80)
        for m in manifests:
            aid = m["id"]
            state = states.get(aid, {})
            status = state.get("status", "disabled")
            health = " (healthy)" if state.get("health_ok") else ""
            print(
                f"{aid:<20} {m.get('type', '?'):<10} {m.get('default_port', '?'):<7} "
                f"{m.get('requires_plan', 'free'):<12} {status + health:<10} "
                f"{m.get('pack_id', '-')}"
            )
        return 0

    elif sub == "enable":
        from adk.addon_manager import AddonManager
        addon_id = args.addon_id
        config = {}
        if getattr(args, "endpoint", None):
            config["endpoint"] = args.endpoint
        mgr = AddonManager()
        try:
            inst = asyncio.run(mgr.enable(addon_id, config=config))
            print(f"Addon {addon_id} enabled")
            print(f"  Status:   {inst.status}")
            print(f"  Endpoint: {inst.endpoint}")
            print(f"  Health:   {'OK' if inst.health_ok else 'checking...'}")
            if inst.error_message:
                print(f"  Error:    {inst.error_message}")
        except ValueError as e:
            print(f"Error: {e}")
            return 1
        except Exception as e:
            print(f"Failed to enable {addon_id}: {e}")
            return 1
        return 0

    elif sub == "disable":
        from adk.addon_manager import AddonManager
        mgr = AddonManager()
        asyncio.run(mgr.disable(args.addon_id))
        print(f"Addon {args.addon_id} disabled")
        return 0

    elif sub == "status":
        from adk.addon_manager import AddonManager
        mgr = AddonManager()
        addon_id = getattr(args, "addon_id", None)
        instances = asyncio.run(mgr.status(addon_id))
        if not instances:
            print("No addons enabled." if not addon_id else f"Addon {addon_id} not found.")
            return 0
        for inst in instances:
            print(f"Addon: {inst.addon_id}")
            print(f"  Status:    {inst.status}")
            print(f"  Type:      {inst.addon_type}")
            print(f"  Endpoint:  {inst.endpoint}")
            print(f"  Health:    {'OK' if inst.health_ok else 'FAIL'}")
            if inst.container_id:
                print(f"  Container: {inst.container_id[:12]}")
            if inst.error_message:
                print(f"  Error:     {inst.error_message}")
            print()
        return 0

    elif sub == "logs":
        from adk.addon_manager import AddonManager, _load_state
        state = _load_state()
        addon_state = state.get(args.addon_id, {})
        cid = addon_state.get("container_id", "")
        if not cid:
            print(f"No container found for {args.addon_id}")
            return 1
        from adk.addon_docker import container_logs
        lines = getattr(args, "lines", 100)
        print(container_logs(cid, tail=lines))
        return 0

    elif sub == "update":
        from adk.addon_manager import AddonManager, _load_state, load_addon_manifest
        from adk.addon_docker import pull_image
        state = _load_state()
        if not state:
            print("No addons enabled.")
            return 0
        for addon_id, s in state.items():
            manifest = load_addon_manifest(addon_id)
            if not manifest or manifest.get("type") != "docker":
                continue
            image = manifest.get("image", "")
            if image:
                print(f"Pulling latest: {image}")
                try:
                    pull_image(image)
                    print(f"  Updated {addon_id}")
                except Exception as e:
                    print(f"  Failed: {e}")
        return 0

    else:
        print("Usage: adk addon [list|enable|disable|status|logs|update]")
        return 1


def _load_pack_catalog(genesis_url: str) -> list[dict]:
    """Load pack catalog: Genesis API first, bundled offline catalog as fallback.

    The bundled catalog at ``adk/data/packs_catalog.json`` is auto-generated
    from ``AitherOS/config/packs_catalog.yaml`` by the ADK sync workflow.
    This ensures ``adk pack list`` and ``adk pack search`` always work,
    even without internet or a running Genesis.
    """
    catalog: list[dict] = []

    # Try Genesis API
    try:
        import httpx
        with httpx.Client(base_url=genesis_url, timeout=5) as c:
            resp = c.get("/api/v1/catalog/packs")
            if resp.status_code == 200:
                catalog = resp.json().get("packs", [])
            else:
                resp = c.get("/v1/packs/catalog")
                if resp.status_code == 200:
                    catalog = resp.json().get("packs", [])
    except Exception:
        pass

    # Fallback: bundled offline catalog
    if not catalog:
        try:
            bundled = Path(__file__).parent / "data" / "packs_catalog.json"
            if bundled.exists():
                catalog = json.loads(bundled.read_text(encoding="utf-8")).get("packs", [])
        except Exception:
            pass

    return catalog


def _cmd_fleet(args) -> int:
    """Create & manage a fleet of agents across runtimes (local | managed | hosted | cloud-run)."""
    import json as _json

    from adk.fleet_manager import FleetManager

    cmd = getattr(args, "fleet_command", None)

    # apply-pack targets a mesh agent through the running node's proxy, not the
    # FleetManager lifecycle store — handle it before constructing the manager.
    if cmd == "apply-pack":
        return _fleet_apply_pack(args)

    mgr = FleetManager()

    def _emit(obj, human):
        if getattr(args, "json_output", False):
            print(_json.dumps(obj, indent=2))
        else:
            print(human)

    if cmd == "create":
        opts = {
            "pack": args.pack or "",
            "port": args.port,
            "mcp_url": getattr(args, "mcp_url", "") or "",
            "model": args.model or "",
            "preset": getattr(args, "preset", "") or "",
            "brain": getattr(args, "brain", "") or "",
            "loop": getattr(args, "loop", "") or "",
            "hands": getattr(args, "hands", "") or "",
            "image": getattr(args, "image", "") or "",
        }
        m = mgr.create(args.runtime, args.name, **{k: v for k, v in opts.items() if v != ""})
        status_icon = {"running": "✓", "pending_runtime": "…", "failed": "✗"}.get(m.status, "•")
        _emit(m.to_dict(),
              f"{status_icon} {m.runtime} agent '{m.name}' [{m.id}] → {m.status}"
              + (f"  {m.endpoint}" if m.endpoint else "")
              + (f"  {m.ref}" if m.ref else "")
              + (f"\n  error: {m.error}" if m.error else ""))
        return 1 if m.status == "failed" else 0

    if cmd == "list":
        members = mgr.list_members()
        if getattr(args, "json_output", False):
            print(_json.dumps([m.to_dict() for m in members], indent=2))
        elif not members:
            print("No fleet members. Create one: adk fleet create <name> --runtime managed --pack <id>")
        else:
            print(f"{'ID':<14}{'RUNTIME':<12}{'STATUS':<16}{'NAME'}")
            for m in members:
                print(f"{m.id:<14}{m.runtime:<12}{m.status:<16}{m.name}")
        return 0

    if cmd == "status":
        m = mgr.refresh(args.member_id)
        if m is None:
            _emit({"error": "not found"}, f"No fleet member '{args.member_id}'")
            return 1
        _emit(m.to_dict(), f"{m.id}  {m.runtime}  {m.status}  {m.name}"
              + (f"  {m.endpoint}" if m.endpoint else ""))
        return 0

    if cmd == "rm":
        ok = mgr.remove(args.member_id)
        print(f"Removed {args.member_id}" if ok else f"No fleet member '{args.member_id}'")
        return 0 if ok else 1

    if cmd == "connect-local":
        from adk.fleet_manager import connect_local_agent
        result = connect_local_agent(args.agent_name, args.mcp_url)
        if result.get("ok"):
            endpoint_info = result.get("endpoint", {})
            human = f"✓ Registered local agent '{args.agent_name}' → {args.mcp_url}"
            if endpoint_info.get("name"):
                human += f"\n  name: {endpoint_info.get('name')}"
            if endpoint_info.get("auth_vault_key"):
                human += f"\n  auth (vault key): {endpoint_info.get('auth_vault_key')}"
            _emit(result, human)
            return 0
        else:
            error_msg = result.get("error", "unknown error")
            human = f"✗ Failed to register local agent: {error_msg}"
            _emit(result, human)
            return 1

    print("Usage: adk fleet {create|list|status|rm|connect-local}. See `adk fleet -h`.")
    return 1


def _cmd_instance(args) -> int:
    """`adk instance` — thin client of Genesis /v1/instances.

    Every verb prints the gateway's real answer. A 4xx/5xx is printed with its
    body and exits 1; nothing here 'gracefully degrades' into a pending state —
    that is how the old cloud-run driver hid a route that never existed.
    """
    import json as _json

    import httpx

    from adk.fleet_manager import _api_key, _gateway_base

    cmd = getattr(args, "instance_command", None)
    base = f"{_gateway_base()}/v1/instances"
    headers = {"Authorization": f"Bearer {_api_key()}"} if _api_key() else {}
    as_json = bool(getattr(args, "json_output", False))

    def _call(method: str, url: str, body: dict | None = None, timeout: float = 60.0):
        try:
            r = httpx.request(method, url, json=body, headers=headers, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - the transport error IS the answer
            print(f"✗ gateway unreachable ({_gateway_base()}): {exc}")
            return None
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:400]}
        if r.status_code >= 400:
            print(f"✗ {method} {url} -> {r.status_code}")
            print(_json.dumps(data, indent=2)[:1500])
            return None
        return data

    def _show(inst: dict) -> None:
        if as_json:
            print(_json.dumps(inst, indent=2))
            return
        icon = {"ready": "✓", "provisioning": "…", "stopped": "■", "failed": "✗",
                "suspended": "⏸", "destroyed": "—"}.get(inst.get("status", ""), "•")
        print(f"{icon} {inst.get('id')}  {inst.get('name')}  {inst.get('status')}"
              f"  {inst.get('endpoint_url') or ''}")
        if inst.get("ready_ms", -1) >= 0:
            print(f"   ready in {inst['ready_ms']} ms · placement "
                  f"brain={inst.get('brain')} loop={inst.get('loop')} hands={inst.get('hands')}")
        if inst.get("error"):
            print(f"   error: {inst['error']}")

    if cmd == "create":
        body: dict = {"name": args.name, "preset": args.preset or "hosted"}
        for plane in ("brain", "loop", "hands"):
            if getattr(args, plane, ""):
                body[plane] = getattr(args, plane)
        if args.image:
            body["image"] = args.image
        env: dict = {}
        for kv in args.env or []:
            k, _, v = kv.partition("=")
            if k:
                env[k] = v
        if env:
            body["env"] = env
        data = _call("POST", base, body, timeout=180.0)
        if not data:
            return 1
        _show(data.get("instance") or {})
        return 0
    if cmd == "list":
        data = _call("GET", base)
        if data is None:
            return 1
        items = data.get("instances") or []
        if as_json:
            print(_json.dumps(items, indent=2))
        elif not items:
            print("No instances. Create one: adk instance create <name>")
        else:
            for inst in items:
                _show(inst)
        return 0
    if cmd in ("status", "connect", "stop", "start", "rm"):
        iid = args.instance_id
        if cmd == "status":
            data = _call("GET", f"{base}/{iid}")
            if data is None:
                return 1
            _show(data.get("instance") or {})
            return 0
        if cmd == "connect":
            data = _call("GET", f"{base}/{iid}/connect-info")
            if data is None:
                return 1
            print(_json.dumps(data, indent=2))
            return 0
        if cmd == "rm":
            data = _call("DELETE", f"{base}/{iid}", timeout=120.0)
        else:
            data = _call("POST", f"{base}/{iid}/{cmd}", timeout=180.0)
        if data is None:
            return 1
        _show(data.get("instance") or {})
        return 0
    print("Usage: adk instance {create|list|status|connect|stop|start|rm}. See `adk instance -h`.")
    return 1


def _cmd_pack(args) -> int:
    """Handle `adk pack` subcommands."""
    sub = getattr(args, "pack_command", None)
    genesis_url = _get_genesis_url()

    if sub == "list" or sub is None:
        # Fetch catalog: Genesis API → bundled offline catalog fallback
        catalog = _load_pack_catalog(genesis_url)

        # Check local packs
        packs_dir = Path.home() / ".aitheros" / "packs"
        local_ids = set()
        if packs_dir.is_dir():
            for child in packs_dir.iterdir():
                if child.is_dir() and (child / ".toolpack.yaml").exists():
                    local_ids.add(child.name)

        # Merge local-only
        catalog_ids = {p["id"] for p in catalog}
        for lid in sorted(local_ids - catalog_ids):
            catalog.append({"id": lid, "name": lid, "version": "local", "tier": "free", "installed": True})

        for entry in catalog:
            if entry["id"] in local_ids:
                entry["installed"] = True

        if not catalog:
            print("No packs found.")
            return 0

        print(f"{'Pack':<25} {'Type':<12} {'Tier':<14} {'Status':<12} {'Version'}")
        print("-" * 75)
        for p in catalog:
            pid = p.get("id", "?")
            ver = p.get("version", "?")
            ptype = p.get("type", "?").replace("_pack", "").replace("_", " ")
            tier = p.get("tier", "free")
            installed = p.get("installed", False)
            status = "installed" if installed else "available"

            print(f"{pid:<25} {ptype:<12} {tier:<14} {status:<12} {ver}")
        print(f"\n{len(catalog)} pack(s) total")
        return 0

    if sub == "search":
        query = getattr(args, "query", "")
        if not query:
            print("Usage: adk pack search <query>")
            return 1

        catalog = _load_pack_catalog(genesis_url)

        q = query.lower()
        matches = [
            p for p in catalog
            if q in p.get("name", "").lower()
            or q in p.get("description", "").lower()
            or q in p.get("id", "").lower()
            or any(q in tag.lower() for tag in p.get("tags", []))
        ]

        if getattr(args, "json_output", False):
            print(json.dumps({"query": query, "count": len(matches), "packs": matches}, indent=2))
            return 0

        if not matches:
            print(f"No packs matching '{query}'")
            return 0

        print(f"{'Pack':<25} {'Version':<10} {'Status':<14} {'Price'}")
        print("-" * 65)

        # Check local packs
        packs_dir = Path.home() / ".aitheros" / "packs"
        local_ids = set()
        if packs_dir.is_dir():
            for child in packs_dir.iterdir():
                if child.is_dir() and (child / ".toolpack.yaml").exists():
                    local_ids.add(child.name)

        for p in matches:
            pid = p.get("id", "?")
            ver = p.get("version", "?")
            pricing = p.get("pricing", {})
            is_free = not pricing
            installed = p.get("installed", False) or pid in local_ids
            licensed = p.get("licensed", False)

            if installed and (is_free or licensed):
                status = "installed"
            elif licensed:
                status = "licensed"
            elif installed:
                status = "installed"
            else:
                status = "available"

            if is_free:
                price = "free"
            else:
                cents = pricing.get("subscription_cents", 0)
                price = f"${int(cents) / 100:.0f}/mo" if cents else "paid"

            print(f"{pid:<25} {ver:<10} {status:<14} {price}")

        print(f"\n{len(matches)} pack(s) matching '{query}'")
        return 0

    if sub == "sync":
        # License-driven convergence: install every entitled pack not present.
        # Delegates to the shared PacksPlugin sync (download → verify → extract).
        try:
            from adk.shell.plugins.builtins.packs import PacksPlugin
        except Exception as e:  # noqa: BLE001
            print(f"pack sync unavailable: {e}")
            return 1
        plugin = PacksPlugin()
        # Customer node talks to its portal/gateway; the v1 routes live there.
        portal = (
            os.environ.get("AITHER_PORTAL_URL")
            or os.environ.get("AITHER_ELYSIUM_URL")
            or genesis_url
            or "https://portal.aitherium.com"
        ).rstrip("/")
        plugin._base_url = portal
        cfg = load_saved_config()
        token = cfg.get("api_key") or cfg.get("access_token") or os.environ.get("AITHER_API_KEY", "")
        if token:
            plugin.auth.set_auth(token, cfg.get("tenant_id"))
        sync_args = ["--dry-run"] if getattr(args, "dry_run", False) else []
        print(plugin._sync(sync_args))
        return 0

    if sub in ("buy", "negotiate"):
        import httpx as _httpx
        portal = (
            os.environ.get("AITHER_PORTAL_URL")
            or os.environ.get("AITHER_ELYSIUM_URL")
            or genesis_url
            or "https://portal.aitherium.com"
        ).rstrip("/")
        cfg = load_saved_config()
        token = cfg.get("api_key") or cfg.get("access_token") or os.environ.get("AITHER_API_KEY", "")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-Aither-Api-Key"] = token
        try:
            if sub == "negotiate":
                body = {"listing_id": args.pack_id, "offer_credits": int(args.offer),
                        "rationale": getattr(args, "why", "")}
                with _httpx.Client(timeout=30.0, verify=tls_verify()) as c:
                    r = c.post(f"{portal}/v1/marketplace/negotiate", json=body, headers=headers)
                data = r.json() if r.content else {}
                print(json.dumps(data, indent=2))
                if data.get("decision") == "accept" and data.get("negotiation_token"):
                    print(f"\nAccepted at {data.get('agreed_credits')} credits. Buy it with:")
                    print(f"  adk pack buy {args.pack_id} --token {data['negotiation_token']} --install")
                return 0
            # buy
            body = {"listing_id": args.pack_id}
            if getattr(args, "token", ""):
                body["negotiation_token"] = args.token
            with _httpx.Client(timeout=60.0, verify=tls_verify()) as c:
                r = c.post(f"{portal}/v1/marketplace/purchase", json=body, headers=headers)
            data = r.json() if r.content else {}
            print(json.dumps(data, indent=2))
            if r.status_code == 200 and data.get("ok") and getattr(args, "install", False):
                from adk.shell.plugins.builtins.packs import PacksPlugin
                p = PacksPlugin()
                p._base_url = portal
                if token:
                    p.auth.set_auth(token, cfg.get("tenant_id"))
                print("\n" + p._sync([]))
            return 0 if r.status_code == 200 else 1
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: {type(e).__name__}: {e}")
            return 1

    if sub == "install":
        pack_id = args.pack_id

        # Deploy-based packs (grid, sovereign) — redirect to deploy command
        deploy_packs = {
            "grid-distributed": ("grid", "adk deploy grid"),
            "grid": ("grid", "adk deploy grid"),
        }
        if pack_id in deploy_packs:
            component, hint = deploy_packs[pack_id]
            print(f"\n  {pack_id} is an infrastructure pack — installing via deploy.\n")
            print(f"  Running: {hint}")
            print()
            # Delegate to deploy
            args.component = component
            args.dry_run = getattr(args, "dry_run", False)
            from adk.deploy import cmd_deploy_component
            return cmd_deploy_component(args)

        import httpx
        import hashlib
        import tarfile
        import shutil
        from io import BytesIO

        packs_dir = Path.home() / ".aitheros" / "packs"
        target = packs_dir / pack_id

        try:
            with httpx.Client(base_url=genesis_url, timeout=60) as c:
                resp = c.get(f"/v1/packs/{pack_id}/download")

            if resp.status_code == 402:
                detail = resp.json().get("detail", "License required")
                if isinstance(detail, dict):
                    detail = detail.get("message", "License required")
                print(f"License required: {detail}")
                print("Purchase at: https://portal.aitherium.com/marketplace")
                return 2

            if resp.status_code == 404:
                print(f"Pack '{pack_id}' not found")
                return 1

            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            print(f"Download failed: {e}")
            return 1
        except Exception as e:
            print(f"Cannot reach Genesis: {e}")
            return 1

        # Verify SHA
        expected_sha = resp.headers.get("X-Pack-SHA256", "")
        actual_sha = hashlib.sha256(resp.content).hexdigest()
        if expected_sha and actual_sha != expected_sha:
            print(f"SHA256 mismatch: expected {expected_sha[:16]}..., got {actual_sha[:16]}...")
            return 1

        # Extract
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)

        buf = BytesIO(resp.content)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in member.name:
                    print(f"Unsafe path in archive: {member.name}")
                    return 1
            tar.extractall(target, filter="data")

        version = resp.headers.get("X-Pack-Version", "unknown")

        # Trigger MCP reload so new tools appear without manual restart
        mcp_reloaded = False
        try:
            httpx.post(f"{genesis_url}/mcp/reload", timeout=5)
            mcp_reloaded = True
        except Exception:
            pass  # Non-fatal — user can restart manually

        print(f"Pack '{pack_id}' v{version} installed to {target}")
        if mcp_reloaded:
            print("MCP tools reloaded — new tools are available now.")
        else:
            print("Restart `adk mcp serve` to activate.")

        # Auto-register with portal if pack has agent.yaml
        agent_yaml = target / pack_id / "agent.yaml"
        if not agent_yaml.exists():
            agent_yaml = target / "agent.yaml"
        if agent_yaml.exists():
            try:
                import yaml
                spec = yaml.safe_load(agent_yaml.read_text(encoding="utf-8")) or {}
                if spec.get("portal"):
                    import asyncio
                    from adk.registration import register_with_portal
                    success = asyncio.run(register_with_portal(spec))
                    if success:
                        print(f"Registered '{spec.get('name', pack_id)}' with portal.")
                    else:
                        print("Portal registration skipped (portal unreachable).")
            except Exception as e:
                print(f"Portal registration skipped: {e}")

        return 0

    if sub == "remove":
        import shutil
        target = Path.home() / ".aitheros" / "packs" / args.pack_id
        if not target.is_dir():
            print(f"Pack '{args.pack_id}' is not installed")
            return 1
        shutil.rmtree(target)

        # Trigger MCP reload so removed tools disappear without manual restart
        mcp_reloaded = False
        try:
            import httpx
            httpx.post(f"{genesis_url}/mcp/reload", timeout=5)
            mcp_reloaded = True
        except Exception:
            pass  # Non-fatal — user can restart manually

        if mcp_reloaded:
            print(f"Pack '{args.pack_id}' removed. MCP tools reloaded.")
        else:
            print(f"Pack '{args.pack_id}' removed. Restart `adk mcp serve` to take effect.")
        return 0

    if sub == "info":
        import httpx
        pack_id = args.pack_id
        try:
            with httpx.Client(base_url=genesis_url, timeout=10) as c:
                # Try new catalog API first
                resp = c.get(f"/api/v1/catalog/packs/{pack_id}")
                if resp.status_code == 404:
                    # Fallback to old API
                    resp = c.get(f"/v1/packs/{pack_id}/manifest")
                if resp.status_code == 404:
                    print(f"Pack '{pack_id}' not found")
                    return 1
                resp.raise_for_status()
                data = resp.json()
                # New API wraps in {"pack": {...}}
                if "pack" in data:
                    data = data["pack"]
        except Exception as e:
            print(f"Cannot fetch pack info: {e}")
            return 1

        print(f"Pack:        {data.get('name', pack_id)}")
        print(f"ID:          {data.get('id', pack_id)}")
        print(f"Version:     {data.get('version', '?')}")
        print(f"Type:        {data.get('type', '?')}")
        print(f"Tier:        {data.get('tier', 'free')}")
        print(f"Category:    {data.get('category', '?')}")
        print(f"Description: {data.get('description', '')}")

        price = data.get("price_monthly_usd")
        if price is None:
            print("Price:       included in tier")
        elif price == 0:
            print("Price:       free")
        else:
            print(f"Price:       ${price}/mo add-on")

        deps = data.get("depends_on", [])
        if deps:
            print(f"Depends on:  {', '.join(deps)}")

        includes = data.get("includes", [])
        if includes:
            print(f"Includes:    {', '.join(includes)}")

        agents = data.get("agents", [])
        if agents:
            print(f"\nAgents ({len(agents)}):")
            for a in agents:
                identity = a.get("identity", "?").replace(".yaml", "")
                print(f"  - {identity} (effort cap: {a.get('effort_cap', '?')})")

        skills = data.get("skills", [])
        if skills:
            print(f"Skills:      {', '.join(skills)}")

        mcp_modules = data.get("mcp_modules", [])
        if mcp_modules:
            print(f"\nMCP Modules ({len(mcp_modules)}):")
            for m in mcp_modules:
                print(f"  - {m}")

        tool_count = data.get("tool_count")
        if tool_count:
            print(f"Tool count:  {tool_count}")

        skill_files = data.get("skill_files", [])
        if skill_files:
            print(f"\nSkill Files ({len(skill_files)}):")
            for sf in skill_files:
                print(f"  - {sf}")

        services = data.get("services", [])
        if services:
            print(f"\nServices ({len(services)}):")
            for s in services:
                print(f"  - {s.get('addon_id', '?')} (port {s.get('port', '?')})")

        # Fetch dependency tree
        try:
            with httpx.Client(base_url=genesis_url, timeout=10) as c:
                deps_resp = c.get(f"/api/v1/catalog/packs/{pack_id}/deps")
                if deps_resp.status_code == 200:
                    deps_data = deps_resp.json()
                    order = deps_data.get("install_order", [])
                    if len(order) > 1:
                        print(f"\nInstall order: {' -> '.join(order)}")
        except Exception:
            pass

        return 0

    if sub == "update":
        import httpx
        pack_id = getattr(args, "pack_id", None)
        packs_dir = Path.home() / ".aitheros" / "packs"

        if pack_id:
            # Update specific pack
            target = packs_dir / pack_id
            if not target.is_dir():
                print(f"Pack '{pack_id}' is not installed")
                return 1
            print(f"Updating {pack_id}...")
            args.pack_id = pack_id
            # Re-use install logic
            return _cmd_pack(type("Args", (), {"pack_command": "install", "pack_id": pack_id})())
        else:
            # Update all installed packs
            if not packs_dir.is_dir():
                print("No packs installed")
                return 0
            installed = [
                d.name for d in packs_dir.iterdir()
                if d.is_dir() and (d / ".toolpack.yaml").exists()
            ]
            if not installed:
                print("No packs installed")
                return 0
            print(f"Updating {len(installed)} pack(s)...")
            for pid in installed:
                print(f"  Updating {pid}...")
                _cmd_pack(type("Args", (), {"pack_command": "install", "pack_id": pid})())
            print("All packs updated.")
            return 0

    if sub == "export":
        pack_ids = getattr(args, "pack_ids", "").split(",")
        output = getattr(args, "output", ".")
        if not pack_ids or not pack_ids[0]:
            print("Usage: adk pack export <pack-ids> [-o output_dir]")
            return 1

        import httpx
        try:
            with httpx.Client(base_url=genesis_url, timeout=30) as c:
                resp = c.post("/api/v1/catalog/resolve", json={"packs": pack_ids})
                if resp.status_code != 200:
                    print(f"Failed to resolve packs: {resp.text}")
                    return 1
                resolved = resp.json()
        except Exception as e:
            print(f"Cannot reach Genesis: {e}")
            return 1

        order = resolved.get("install_order", [])
        print(f"Resolved {len(order)} packs: {', '.join(order)}")
        print(f"Export to: {output}")
        print("(Offline export bundles are an enterprise feature — contact sales@aitherium.com)")
        return 0

    if sub == "customize":
        import yaml
        from adk.pack_discovery import load_agent_spec

        pack_name = args.pack_id
        pack_dir = Path.home() / ".aither" / "agents" / pack_name

        if not pack_dir.exists():
            print(f"ERROR: Pack '{pack_name}' not installed.")
            print(f"Install it with: adk install pack:{pack_name}")
            return 1

        # Parse options
        system_prompt = None
        capabilities = None
        show_spec = getattr(args, "show", False)

        if getattr(args, "system_prompt_file", None):
            try:
                sp = Path(getattr(args, "system_prompt_file")).expanduser()
                system_prompt = sp.read_text(encoding="utf-8")
            except Exception as e:
                print(f"ERROR: Failed to read system prompt file: {e}")
                return 1

        if getattr(args, "system_prompt", None):
            system_prompt = args.system_prompt

        if getattr(args, "capabilities", None):
            capabilities = [c.strip() for c in args.capabilities.split(",")]

        # If --show and no overrides, just display current spec
        if show_spec and not system_prompt and not capabilities:
            spec = load_agent_spec(pack_dir / "agent.yaml")
            if not spec:
                print("No agent.yaml found in pack")
                return 1
            print(f"Pack '{pack_name}' current spec:")
            print()
            for key, value in sorted(spec.items()):
                if isinstance(value, (list, tuple)):
                    val_str = "[" + ", ".join(str(v) for v in value) + "]"
                elif isinstance(value, dict):
                    val_str = "{...}"
                elif isinstance(value, str) and len(value) > 80:
                    val_str = value[:80] + "..."
                else:
                    val_str = str(value)
                print(f"  {key}: {val_str}")
            return 0

        # Build overlay
        if not system_prompt and not capabilities:
            print("ERROR: No options provided. Use --system-prompt, --system-prompt-file, --capabilities, or --show")
            return 1

        overlay = {}
        if system_prompt:
            if len(system_prompt) > 8000:
                print("ERROR: system_prompt exceeds 8000 character limit")
                return 1
            overlay["system_prompt"] = system_prompt

        if capabilities:
            overlay["capabilities"] = capabilities

        # Write agent.yaml.local
        local_yaml = pack_dir / "agent.yaml.local"
        try:
            local_yaml.write_text(yaml.dump(overlay, default_flow_style=False), encoding="utf-8")
        except Exception as e:
            print(f"ERROR: Failed to write agent.yaml.local: {e}")
            return 1

        # Show confirmation
        print(f"✓ Customized pack '{pack_name}'")
        print()
        print(f"Overrides saved to: {local_yaml}")
        print()
        if system_prompt:
            prompt_preview = system_prompt[:100].replace("\n", " ")
            print(f"  system_prompt: {prompt_preview}...")
        if capabilities:
            print(f"  capabilities: {', '.join(capabilities)}")
        print()
        print(f"Apply with: adk run --agents {pack_name}")
        return 0

    if sub == "import":
        # Import an external agent (e.g., Eve) to an AitherADK pack
        from adk.importers.eve import import_eve_agent

        agent_path = getattr(args, "agent_path", None)
        if not agent_path:
            print("Usage: adk pack import <eve_agent_path>")
            print()
            print("Converts an Eve agent to an AitherADK pack.")
            print()
            print("Example:")
            print("  adk pack import ./my-eve-agent")
            print()
            print("Requirements:")
            print("  - Eve agent must have a .compiled-manifest.json")
            print("  - Compiled manifest version must be 35")
            return 1

        try:
            result = import_eve_agent(agent_path)
            pack_id = result["pack_id"]
            pack_dir = result["pack_dir"]
            status = result["status"]

            print("✓ Eve agent imported to AitherADK pack!")
            print()
            print(f"  Pack ID:  {pack_id}")
            print(f"  Location: {pack_dir}")
            print(f"  Status:   {status}")
            print()
            print("Next steps:")
            print(f"  adk pack info {pack_id}")
            print(f"  adk pack install {pack_id}")
            return 0

        except FileNotFoundError as e:
            print(f"Error: {e}")
            return 1
        except ValueError as e:
            print(f"Error: {e}")
            return 1
        except Exception as e:  # noqa: BLE001
            print(f"Import failed: {type(e).__name__}: {e}")
            return 1

    print("Usage: adk pack [list|search|install|remove|info|update|export|customize|import]")
    return 1


def _cmd_skills(args) -> int:
    """Handle `adk skills` subcommands."""
    import json as _json

    sub = getattr(args, "skills_command", None)
    try:
        from adk.skills import SkillStore
    except ImportError:
        print("Skills module not available.")
        return 1

    store = SkillStore()

    if sub == "list" or sub is None:
        skills = store.list_all()
        if not skills:
            print("No skills learned yet.")
            return 0
        print(f"{'Name':<30} {'Uses':<8} {'Tags'}")
        print("-" * 60)
        for s in skills:
            tags = ", ".join(s.tags[:3]) if s.tags else ""
            print(f"{s.name:<30} {s.success_count:<8} {tags}")
        return 0

    if sub == "search":
        results = store.search(args.query)
        if not results:
            print(f"No skills matching '{args.query}'")
            return 0
        for s in results:
            print(f"  {s.name}: {s.description}")
        return 0

    if sub == "export":
        data = store.export_agentskills()
        print(_json.dumps(data, indent=2))
        return 0

    print("Usage: adk skills [list|search|export]")
    return 1


def _cmd_listen(args) -> int:
    """Handle `adk listen` subcommands — real-time audio intelligence."""
    sub = getattr(args, "listen_command", None)

    genesis_url = os.environ.get("AITHER_GENESIS_URL",
                                  os.environ.get("AITHER_URL", "http://localhost:8001"))

    if sub in ("audiobook", "meeting", "note"):
        try:
            import httpx
        except ImportError:
            print("httpx required: pip install httpx")
            return 1

        if sub == "audiobook":
            body = {
                "mode": "audiobook",
                "book_title": getattr(args, "title", ""),
                "author": getattr(args, "author", ""),
                "genre": getattr(args, "genre", "litrpg"),
                "capture_backend": getattr(args, "backend", "wasapi"),
                "audio_source": getattr(args, "audio_file", None),
                "workspace_id": getattr(args, "workspace", None),
            }
        elif sub == "meeting":
            mt = getattr(args, "meeting_type", "meeting")
            body = {
                "mode": "lecture" if mt == "lecture" else "meeting",
                "meeting_title": getattr(args, "title", ""),
                "meeting_type": mt,
                "participants": getattr(args, "participants", []),
                "capture_backend": getattr(args, "backend", "wasapi"),
                "workspace_id": getattr(args, "workspace", None),
            }
        else:  # note
            body = {
                "mode": "voice_note",
                "meeting_title": getattr(args, "title", "Voice Note"),
                "capture_backend": getattr(args, "backend", "wasapi"),
                "workspace_id": getattr(args, "workspace", None),
            }

        with httpx.Client(base_url=genesis_url, timeout=30) as c:
            resp = c.post("/audiobook/start", json=body)
            if resp.status_code != 200:
                print(f"Error: {resp.text}")
                return 1
            data = resp.json()

        sid = data.get("session_id", "")
        mode_label = {"audiobook": "Audiobook companion",
                      "meeting": "Meeting transcription",
                      "note": "Voice note"}[sub]
        print(f"{mode_label} started: {sid[:12]}")
        print(f"  Stop:   adk listen stop {sid[:12]}")
        print(f"  Export: adk listen export {sid[:12]}")
        return 0

    elif sub == "sessions":
        import httpx
        with httpx.Client(base_url=genesis_url, timeout=10) as c:
            resp = c.get("/audiobook/sessions")
            if resp.status_code != 200:
                print(f"Error: {resp.text}")
                return 1
            sessions = resp.json().get("sessions", [])

        if not sessions:
            print("No active sessions.")
        for s in sessions:
            sid = s.get("session_id", "")[:12]
            title = s.get("book_title", "Untitled")
            chunks = s.get("chunks_processed", 0)
            print(f"  {sid}  {title:<30}  {chunks} chunks")
        return 0

    elif sub == "stop":
        import httpx
        session_id = args.session_id
        # Partial ID matching
        if len(session_id) < 36:
            with httpx.Client(base_url=genesis_url, timeout=10) as c:
                resp = c.get("/audiobook/sessions")
                if resp.status_code == 200:
                    sessions = resp.json().get("sessions", [])
                    matches = [s for s in sessions
                               if s["session_id"].startswith(session_id)]
                    if len(matches) == 1:
                        session_id = matches[0]["session_id"]
                    elif len(matches) > 1:
                        print(f"Ambiguous ID '{session_id}' — {len(matches)} matches.")
                        return 1

        with httpx.Client(base_url=genesis_url, timeout=15) as c:
            resp = c.post(f"/audiobook/{session_id}/stop")
            if resp.status_code != 200:
                print(f"Error: {resp.text}")
                return 1
        print(f"Session stopped: {session_id[:12]}")
        return 0

    elif sub == "export":
        import httpx
        session_id = args.session_id
        fmt = getattr(args, "fmt", "notes")
        output = getattr(args, "output", None)

        with httpx.Client(base_url=genesis_url, timeout=15) as c:
            resp = c.get(f"/audiobook/{session_id}/export/{fmt}")
            if resp.status_code != 200:
                print(f"Error: {resp.text}")
                return 1
            data = resp.json()

        content = data.get("content") or data.get("transcript", "")
        if output:
            with open(output, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"Exported to {output}")
        else:
            print(content)
        return 0

    print("Usage: adk listen [audiobook|meeting|note|sessions|stop|export]")
    return 1


def _cmd_soul(args) -> int:
    """Handle `adk soul` subcommands."""
    sub = getattr(args, "soul_command", None)

    if sub == "import":
        from pathlib import Path
        from adk.identity import load_soul_md, Identity

        path = Path(args.path)
        if not path.exists():
            print(f"File not found: {path}")
            return 1

        config = load_soul_md(path)
        identity = Identity(**{k: v for k, v in config.items() if k != "knowledge"})
        print(f"Imported identity: {identity.name}")
        if identity.description:
            print(f"  Description: {identity.description}")
        if identity.skills:
            print(f"  Skills: {', '.join(identity.skills)}")
        if config.get("knowledge"):
            print(f"  Knowledge: {len(config['knowledge'])} chars")

        # Save as YAML
        import yaml
        out_path = Path("identities") / f"{identity.name}.yaml"
        out_path.parent.mkdir(exist_ok=True)
        data = {
            "name": identity.name,
            "role": identity.role,
            "description": identity.description,
            "skills": identity.skills,
        }
        if identity.system_prompt:
            data["system_prompt"] = identity.system_prompt
        out_path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
        print(f"  Saved to: {out_path}")
        return 0

    if sub == "export":
        from adk.identity import load_identity, export_soul_md

        identity = load_identity(args.name)
        print(export_soul_md(identity))
        return 0

    print("Usage: adk soul [import|export]")
    return 1


# ---------------------------------------------------------------------------
# adk train — training pipeline management
# ---------------------------------------------------------------------------

def _get_genesis_url() -> str:
    """Resolve the training API URL — Genesis, ADK server, or Aitherium cloud.

    Priority:
    1. AITHER_GENESIS_URL env var (explicit Genesis)
    2. elysium_url from saved config (remote connected desktop)
    3. Local Genesis on :8001 (if reachable)
    4. Local ADK server on configured port (if reachable)
    5. Aitherium cloud gateway (if API key available)
    6. Fallback to localhost:8001 (user gets a clear error)
    """
    import urllib.request
    import urllib.error

    cfg = load_saved_config()

    # Explicit env var
    explicit = os.environ.get("AITHER_GENESIS_URL", "")
    if explicit:
        return explicit.rstrip("/")

    # Remote connected instance
    elysium = cfg.get("elysium_url") or cfg.get("aither_gateway_url", "")
    if elysium:
        return elysium.rstrip("/")

    # Probe local Genesis
    try:
        req = urllib.request.Request("http://localhost:8001/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                return "http://localhost:8001"
    except (urllib.error.URLError, ConnectionError, OSError):
        pass

    # Probe local ADK server
    server_port = cfg.get("server_port", 8080)
    try:
        req = urllib.request.Request(f"http://localhost:{server_port}/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                return f"http://localhost:{server_port}"
    except (urllib.error.URLError, ConnectionError, OSError):
        pass

    # Cloud gateway (for users without local Genesis)
    api_key = cfg.get("api_key") or os.environ.get("AITHER_API_KEY", "")
    if api_key:
        return "https://gateway.aitherium.com"

    # Fallback — will produce a clear error when the call fails
    return "http://localhost:8001"


def _train_headers() -> dict:
    """Build auth headers for Genesis training API calls."""
    cfg = load_saved_config()
    api_key = cfg.get("api_key") or os.environ.get("AITHER_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    tenant_id = cfg.get("tenant_id") or os.environ.get("AITHER_TENANT_ID", "")
    if tenant_id:
        headers["X-Tenant-ID"] = tenant_id
    return headers


def _cmd_train(args) -> int:
    import json as _json
    import urllib.request
    import urllib.error

    genesis = _get_genesis_url()
    headers = _train_headers()
    sub = getattr(args, "train_command", None)

    def _api(method: str, path: str, body: dict | None = None) -> dict:
        data = _json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            f"{genesis}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return _json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                err = _json.loads(exc.read())
                detail = err.get("detail", str(exc))
            except Exception:
                detail = str(exc)
            print(f"  Error ({exc.code}): {detail}")
            return {"error": detail}
        except (urllib.error.URLError, OSError) as exc:
            print(f"  Cannot reach Genesis at {genesis}: {exc}")
            return {"error": str(exc)}

    if sub == "status":
        print(f"  Connecting to {genesis}...")
        dashboard = _api("GET", "/training/dashboard")
        if "error" in dashboard:
            return 1
        readiness = _api("GET", "/training/pipeline/readiness")

        print()
        print("  Training Dashboard")
        print(f"  {'='*40}")
        print(f"  Total Runs:     {dashboard.get('total_runs', 0)}")
        print(f"  Active Runs:    {dashboard.get('active_runs', 0)}")
        print(f"  Successful:     {dashboard.get('successful_runs', 0)}")
        print(f"  Failed:         {dashboard.get('failed_runs', 0)}")
        print(f"  Total Cost:     ${dashboard.get('total_cost_usd', 0):.2f}")
        print(f"  GPU Hours:      {dashboard.get('total_gpu_hours', 0):.1f}")
        print()
        ready = readiness.get("ready", False)
        count = readiness.get("count", 0)
        threshold = readiness.get("threshold", 50)
        status_icon = "READY" if ready else "NOT READY"
        print(f"  Corpus: {status_icon} ({count}/{threshold} examples)")
        return 0

    elif sub == "launch":
        body = {
            "model_preset": args.preset,
            "target_gpu": args.gpu,
            "epochs": args.epochs,
            "lora_r": args.lora_r,
            "max_gpu_price": args.max_price,
            "auto_benchmark": not args.no_benchmark,
            "auto_deploy": args.auto_deploy,
        }
        if args.dataset:
            body["dataset_url"] = args.dataset

        print(f"  Launching training: {args.preset} on {args.gpu}...")
        result = _api("POST", "/training/orchestrate", body)
        if "error" in result:
            return 1
        run_id = result.get("run_id", "???")
        print(f"  Training run started: {run_id}")
        print(f"  Monitor: adk train logs {run_id[:12]}")
        return 0

    elif sub == "logs":
        run_id = args.run_id
        result = _api("GET", f"/training/runs/{run_id}/logs?lines={args.lines}")
        if "error" in result:
            return 1
        logs = result.get("logs", result.get("stdout", ""))
        if logs:
            print(logs)
        else:
            print("  No logs available yet.")
        return 0

    elif sub == "cancel":
        run_id = args.run_id
        result = _api("POST", f"/training/runs/{run_id}/cancel")
        if "error" in result:
            return 1
        print(f"  Training run {run_id} cancelled.")
        return 0

    elif sub == "runs":
        result = _api("GET", "/training/runs")
        if "error" in result:
            return 1
        runs = result.get("runs", result) if isinstance(result, dict) else result
        if not isinstance(runs, list):
            runs = []
        if not runs:
            print("  No training runs found.")
            return 0

        status_filter = getattr(args, "status", None)
        if status_filter:
            runs = [r for r in runs if r.get("status") == status_filter]

        print()
        print(f"  {'RUN ID':<20} {'STATUS':<15} {'MODEL':<25} {'GPU':<15} {'COST':>8}")
        print(f"  {'-'*20} {'-'*15} {'-'*25} {'-'*15} {'-'*8}")
        for run in runs[:20]:
            rid = (run.get("run_id", "")[:18] or "???")
            status = run.get("status", "?")
            model = (run.get("model_preset", "?")[:23] or "?")
            gpu = (run.get("gpu_name", "") or run.get("target_gpu", "?"))[:13]
            cost = run.get("cost_usd", 0)
            print(f"  {rid:<20} {status:<15} {model:<25} {gpu:<15} ${cost:>7.2f}")
        return 0

    elif sub == "register-gpu":
        host = args.host
        port = args.port
        gpu_model = getattr(args, "gpu_model", None) or ""
        vram = getattr(args, "vram", None) or 0

        # Auto-detect GPU if not specified
        if not gpu_model or not vram:
            try:
                import torch
                if torch.cuda.is_available():
                    props = torch.cuda.get_device_properties(0)
                    if not gpu_model:
                        gpu_model = props.name
                    if not vram:
                        vram = props.total_mem // (1024 ** 3)
                    print(f"  Detected: {gpu_model} ({vram}GB)")
                else:
                    print("  Warning: No CUDA GPU detected locally.")
            except ImportError:
                print("  Warning: PyTorch not installed, cannot auto-detect GPU.")

        body = {
            "name": f"adk-{host}-gpu",
            "host": host,
            "port": port,
            "capabilities": ["training", "inference"],
            "gpu_model": gpu_model,
            "gpu_vram_gb": vram,
            "node_type": "gpu_node",
            "location": "workstation",
        }
        result = _api("POST", "/compute/nodes/register", body)
        if "error" in result:
            # Try gateway mesh as fallback
            result = _api("POST", "/gateway/nodes/register", body)
            if "error" in result:
                print("  Failed to register GPU node.")
                return 1

        node_id = result.get("node_id", result.get("id", ""))
        print(f"  GPU registered: {gpu_model} ({vram}GB) at {host}:{port}")
        if node_id:
            print(f"  Node ID: {node_id}")
        print("  This GPU is now available for training via 'adk train launch --gpu customer'")
        return 0

    else:
        print("Usage: adk train [status|launch|logs|cancel|runs|register-gpu]")
        print()
        print("  status         Check training readiness and dashboard stats")
        print("  launch         Launch a new training run")
        print("  logs <run_id>  Stream training logs")
        print("  cancel <id>    Cancel an active run")
        print("  runs           List recent training runs")
        print("  register-gpu   Register your local GPU for remote training")
        return 1


# ---------------------------------------------------------------------------
# Slash-command manifest — auto-generates from argparse for AitherShell
# ---------------------------------------------------------------------------

def build_command_manifest() -> list[dict]:
    """Return a structured manifest of all CLI commands for AitherShell slash-command auto-discovery.

    AitherShell queries GET /slash-commands on the ADK server, gets this manifest,
    and registers each command as a /name slash command with tab-completion.

    Returns list of: {name, help, args: [{name, flags, required, type, default, help, choices}], subcommands: [...]}
    """
    # Build a temporary parser just for introspection (never calls parse_args)
    p = argparse.ArgumentParser(prog="adk")
    sub = p.add_subparsers(dest="command")

    # Import and call the registration block — we'll inline a lightweight
    # version that just registers the parser structure, not the dispatch.
    # This avoids import-time side effects.
    _register_commands(sub)

    commands = []
    for action in p._subparsers._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, subparser in action.choices.items():
            cmd = {"name": name, "help": "", "args": [], "subcommands": []}
            for ca in action._choices_actions:
                if ca.dest == name:
                    cmd["help"] = ca.help or ""
                    break
            for act in subparser._actions:
                if isinstance(act, argparse._HelpAction):
                    continue
                if isinstance(act, argparse._SubParsersAction):
                    for sn, sp in act.choices.items():
                        sc = {"name": sn, "help": "", "args": []}
                        for sca in act._choices_actions:
                            if sca.dest == sn:
                                sc["help"] = sca.help or ""
                                break
                        for sa in sp._actions:
                            if isinstance(sa, (argparse._HelpAction, argparse._SubParsersAction)):
                                continue
                            ai = _extract_arg(sa)
                            sc["args"].append(ai)
                        cmd["subcommands"].append(sc)
                    continue
                cmd["args"].append(_extract_arg(act))
            commands.append(cmd)
    return commands


def _extract_arg(act) -> dict:
    """Extract argument info from an argparse action."""
    # required is computed, never read from argparse's attribute: 3.10 and
    # 3.12 DISAGREE on the value for nargs='*'/REMAINDER positionals (3.10
    # reports True, 3.12 False — measured 2026-08-28 on `image prompt`,
    # `decide_args`, `shell_args`, `platform_args`), so the doc generated
    # under one interpreter went STALE in CI under the other. A positional
    # is required only when nargs is absent (or a positive count); '*' '?'
    # and REMAINDER are all zero-or-more.
    ai = {
        "name": act.dest,
        "flags": act.option_strings or [],
        "required": (act.required if bool(act.option_strings)
                     else (act.nargs not in ("?", "*", argparse.REMAINDER))),
        "type": act.type.__name__ if act.type else "str",
        "default": act.default if act.default != argparse.SUPPRESS else None,
        "help": act.help or "",
    }
    if act.choices:
        ai["choices"] = list(act.choices)
    return ai


_cached_parser: argparse.ArgumentParser | None = None


def get_parser() -> argparse.ArgumentParser:
    """Return the full CLI parser (cached).

    The name and docstring promise a REAL parser, and an empty-returning
    helper is a trap: until 2026-08-31 this returned a bare ArgumentParser
    with NO subcommands, populated only as a side effect of main() running —
    so any caller that asked before main() got a parser that accepted
    nothing, and the failure read as "unrecognized arguments" (measured: a
    smoke test hit exactly that). No source caller exists today (the
    manifest builder and main() construct their own), but the function is
    part of the package surface, so it must do what its name says.
    """
    global _cached_parser
    if _cached_parser is None:
        _cached_parser = argparse.ArgumentParser(
            prog="adk",
            description="AitherADK — Build AI agent fleets with any LLM backend",
        )
        _register_commands(_cached_parser.add_subparsers(dest="command"))
    return _cached_parser


def _register_commands(sub):
    """Register all CLI subcommands on the given subparsers group.

    Shared between main() (for execution) and build_command_manifest() (for introspection).
    """
    # adk start — the main entry point for everyone
    start_p = sub.add_parser("start", help="Start chatting with your codebase (zero config)")
    start_p.add_argument("path", nargs="?", default=".", help="Project directory (default: current)")
    start_p.add_argument("--model", help="Model: deepseek-flash, deepseek-pro, ollama, openrouter, or a model slug")
    start_p.add_argument("--provider", help="Provider: deepseek, openrouter, ollama, openai, anthropic")
    start_p.add_argument("--mcp", action="store_true", help="Connect to AitherOS MCP gateway (1200+ tools)")

    # aither init
    init_p = sub.add_parser("init", help="Scaffold a new agent project")
    init_p.add_argument("name", nargs="?", default="my-agent", help="Project/agent name")
    init_p.add_argument("-d", "--directory", help="Target directory (default: ./<name>)")

    # aither new <template> — scaffold a full template app (e.g. deep-research)
    new_p = sub.add_parser("new", help="Scaffold a full template app (e.g. deep-research)")
    new_p.add_argument("template", help="Template name, e.g. deep-research")
    new_p.add_argument("-d", "--directory", help="Target directory (default: ./<template>)")

    # aither run
    run_p = sub.add_parser("run", help="Start the agent server")
    run_p.add_argument("-i", "--identity", help="Agent identity")
    run_p.add_argument("-p", "--port", type=int, help="Server port")
    run_p.add_argument("--host", help="Server host")
    run_p.add_argument("-b", "--backend", help="LLM backend")
    run_p.add_argument("-m", "--model", help="Model name")
    run_p.add_argument("-f", "--fleet", help="Fleet YAML config")
    run_p.add_argument("-a", "--agents", help="Comma-separated agent identities")
    run_p.add_argument("--mesh", action="store_true",
                       help="Enable mesh hosting (advertise tools/inference to connected desktop)")

    # adk image — generate on local backends (ComfyUI/Sana/SD.Next). The handler
    # lives in adk/images.py (cmd_image) so the daemon and the CLI share one
    # implementation; this was the wiring the local-image-generation skill
    # advertised for months while nothing registered it (ONB006, 2026-08-25).
    image_p = sub.add_parser(
        "image", help="Generate an image on a local backend (ComfyUI/Sana/SD.Next)")
    image_p.add_argument("--backends", action="store_true",
                         help="list which lanes can actually generate")
    image_p.add_argument("prompt", nargs="*", help="what to draw")
    image_p.add_argument("--width", type=int, default=768)
    image_p.add_argument("--height", type=int, default=768)
    image_p.add_argument("--steps", type=int, default=20)
    image_p.add_argument("--cfg", type=float, default=6.0)
    image_p.add_argument("--seed", type=int, default=None)
    image_p.add_argument("--model", default="", help="checkpoint to use")
    image_p.add_argument("--backend", default="", help="force a lane id")
    image_p.add_argument("--negative", default="")
    image_p.add_argument("--out", default="", help="output PNG path")

    # adk up — one command: run a persistent, fleet-connected agent
    up_p = sub.add_parser(
        "up", help="Run a persistent agent connected to your AitherOS fleet (one command)")
    up_p.add_argument("--identity", default="aither", help="Agent identity (default: aither)")
    up_p.add_argument("--name", help="Fleet label for this agent (default: <hostname>-adk)")
    up_p.add_argument("--provider", help="Cloud provider if no local LLM: deepseek/openai/anthropic")
    up_p.add_argument("--model", help="Model name (default: the provider's default)")
    up_p.add_argument("--port", type=int, default=8080, help="Local port for aither-serve (default: 8080)")
    up_p.add_argument("--yes", "--non-interactive", action="store_true", dest="yes",
                      help="Non-interactive: zero prompts, machine-readable JSON (for AI agents/CI)")
    up_p.add_argument("--foreground", action="store_true",
                      help="Block the terminal instead of detaching")
    up_p.add_argument("--no-persist", action="store_true",
                      help="Do not install a reboot-autostart entry")
    up_p.add_argument("--force", action="store_true", help="Restart even if an agent is already running")
    up_p.add_argument("--offline", action="store_true",
                      help="Sovereign/local-only: no tunnel, no portal (or set AITHER_OFFLINE=1)")
    up_p.add_argument("--no-register", action="store_true",
                      help="Run locally only — no tunnel, no fleet registration")
    up_p.add_argument("--require-register", action="store_true",
                      help="Fail (non-zero) if the fleet registration cannot complete")
    up_p.add_argument("--token", help="Portal token for registration (else 'adk login' / $AITHER_PORTAL_TOKEN)")
    up_p.add_argument("--auth-token", help="Callback bearer the control plane presents back (minted if omitted)")
    up_p.add_argument("--passphrase", "--pin", dest="passphrase",
                      help="Memorable secret to authenticate remote chat (else a random token is minted). "
                           "Enter it on the chat page's access gate from your phone.")
    up_p.add_argument("--email", help="Email the phone-ready access link to this address once the "
                                      "tunnel is up (uses configured SMTP; else saved notify_email).")
    up_p.add_argument("--approve", help="Comma-list of tools that pause for approval (default: file_write,shell_exec,shell)")
    up_p.add_argument("--portal", default=_control_plane(), help="Control-plane base URL")
    up_p.add_argument("--login-url", help="Device-flow login base URL")
    up_p.add_argument("--register-url", help="Full fleet-register URL (overrides --portal)")
    up_p.add_argument("--reach", choices=["tunnel", "mesh"], default="tunnel",
                      help="Connectivity mode: tunnel (Cloudflare, default) or mesh (overlay IP)")
    up_p.add_argument("--dry-run", action="store_true", help="Show what would happen without starting anything")

    # adk bonsai-local — one command: run Bonsai-27B locally on :8090 (the ladder's local tier)
    # adk publish-preflight — every check that can fail BEFORE an upload.
    # Separate from any publish command on purpose: an upload is the only
    # irreversible step, and this deliberately does not perform it.
    pf_p = sub.add_parser(
        "publish-preflight",
        help="Check a package can actually be published: an interpreter that "
             "meets requires-python, and a wheel that installs AND imports")
    pf_p.add_argument("path", nargs="?", default=".",
                      help="Package directory (default: the current one)")
    pf_p.add_argument("--import-name", default="",
                      help="Module to import, when it legitimately differs "
                           "from the distribution name")
    pf_p.add_argument("--diagnose", default="",
                      help="Translate a publish error into its cause instead "
                           "of running the checks")

    bonsai_p = sub.add_parser(
        "bonsai-local",
        help="Run Bonsai-27B on your own hardware (:8090) — GPU or CPU; aitherium.com then chats locally")
    bonsai_p.add_argument("--port", type=int, default=BONSAI_LOCAL_PORT,
                          help=f"Host port to serve on (default: {BONSAI_LOCAL_PORT})")
    bonsai_p.add_argument("--dry-run", action="store_true", help="Show what would run without starting anything")
    bonsai_p.add_argument("--stop", action="store_true", help="Stop and remove the local Bonsai container")

    # adk down — stop the agent started by `adk up`
    # adk sandbox — self-host AitherSandbox and link it to your portal (optional)
    sb_p = sub.add_parser("sandbox",
                          help="Self-host AitherSandbox + link it to your portal (optional safe-testing)")
    sb_sub = sb_p.add_subparsers(dest="sandbox_action")
    sb_up = sb_sub.add_parser("up", help="Deploy the sandbox container, open a tunnel, register with the portal")
    sb_up.add_argument("--name", help="Portal label (default: <hostname>-sandbox)")
    sb_up.add_argument("--port", type=int, default=8131, help="Local port (default: 8131)")
    sb_up.add_argument("--yes", "--non-interactive", action="store_true", dest="yes",
                       help="Non-interactive: machine-readable JSON")
    sb_up.add_argument("--no-register", action="store_true",
                       help="Local-only — no tunnel, no portal registration")
    sb_up.add_argument("--token", help="Portal token for registration (else $AITHER_PORTAL_TOKEN)")
    sb_up.add_argument("--portal", default=_control_plane(), help="Control-plane base URL")
    sb_sub.add_parser("down", help="Stop the sandbox container + tunnel")
    sb_sub.add_parser("status", help="Show sandbox state + linked URL")

    down_p = sub.add_parser("down", help="Stop the agent + tunnel and remove its autostart")
    down_p.add_argument("--keep-autostart", action="store_true",
                        help="Stop now but leave the reboot-autostart entry in place")

    # adk reregister — re-register endpoints with their A2A public keys
    reregister_p = sub.add_parser(
        "reregister",
        help="Re-register endpoint(s) with A2A public keys (backfill for existing endpoints)")
    reregister_group = reregister_p.add_mutually_exclusive_group(required=True)
    reregister_group.add_argument("--name", help="Re-register one endpoint by name")
    reregister_group.add_argument("--all", action="store_true",
                                 help="Re-register all endpoints for this agent")
    reregister_p.add_argument("--token", help="Portal token (or $AITHER_PORTAL_TOKEN / 'adk login')")
    reregister_p.add_argument("--portal", default=_control_plane(),
                              help="Portal URL (default: veil.aitherium.com)")

    # adk stack — supervise the consumer stack (Room + Ollama); was `adk up`
    stack_p = sub.add_parser("stack", help="Start the consumer stack (Room + Ollama) as native processes")
    stack_p.add_argument("service", nargs="?", default="default",
                         help="Service to start: default (Room+Ollama), qdrant (local Qdrant)")
    stack_p.add_argument("--interval", type=int, default=10,
                         help="Health check interval in seconds (default: 10)")
    stack_p.add_argument("--no-sync", action="store_true",
                         help="Skip the license-driven pack sync before starting")

    # adk wizard — first-run setup
    wizard_p = sub.add_parser("wizard", help="First-run wizard — hardware detection, setup recommendations, auth token")
    wizard_p.add_argument("--yes", action="store_true",
                         help="Non-interactive mode: accept defaults without prompts")
    wizard_p.add_argument("--gui", action="store_true",
                         help="Launch the point-and-click wizard window (no terminal needed)")

    # aither register
    register_p = sub.add_parser("register", help="Create a new Aitherium account")
    register_p.add_argument("--email", help="Account email (prompted if omitted)")
    register_p.add_argument("--password", help="Account password (prompted if omitted)")

    # adk login
    login_p = sub.add_parser("login", help="Authenticate with Aitherium (browser device flow)")
    login_p.add_argument("--email", help="Use email/password instead of browser flow")
    login_p.add_argument("--password", help="Password (prompted if --email given without it)")
    login_p.add_argument("--github", action="store_true",
                         help="Sign in with your GitHub identity (device flow)")
    login_p.add_argument("--api-key", help="Save an API key directly (no login flow)")
    login_p.add_argument("--no-sync", action="store_true",
                         help="Skip auto-syncing your secrets vault after login")
    login_p.add_argument("--portal-url", default="",
                         help="Portal/Identity URL (default: portal.aitherium.com)")

    # adk pair — node-initiated pairing (the code IS the credential; no login here)
    pair_p = sub.add_parser(
        "pair",
        help="Pair this machine with the portal as an inference node (6-char code from the portal)")
    pair_p.add_argument("code", help="Pairing code shown in the signed-in portal tab")
    pair_p.add_argument("--portal", default="",
                        help="Portal base URL (default: https://portal.aitherium.com)")

    # adk whoami
    _whoami = sub.add_parser(
        "whoami", help="Show current auth status, config and entitlement tier")
    # The portal's quickstart advertises a "verify auth + entitlement" step. It
    # advertised `aither entitlement --refresh --json`, which was wrong twice
    # over: there is no `aither` console script (the wheel declares adk, adk-py,
    # awdk, adk-serve, adk-workspace, adk-bug, adk-shell) and no
    # `entitlement` subcommand. Rather than downgrade the docs to a command that
    # does not answer the question, whoami now answers it.
    _whoami.add_argument("--json", action="store_true",
                         help="machine-readable output (exit 0 = authenticated)")
    # Deliberately no --refresh: see cmd_whoami. There is no endpoint to
    # refresh from, and a flag that silently does nothing is worse than its
    # absence for the one user who runs it right after paying.

    # adk logout
    sub.add_parser("logout", help="Clear saved auth tokens")

    # adk balance — show Aitherium credit account info
    sub.add_parser("balance", help="Show your Aitherium credit balance and earnings")

    # adk ambient — terminal sensor for the ambient expertise loop
    ambient_p = sub.add_parser(
        "ambient",
        help="Make the agent an expert on what you're doing in this terminal",
    )
    ambient_sub = ambient_p.add_subparsers(dest="ambient_command")
    amb_install = ambient_sub.add_parser(
        "install", help="Add the shell hook to your profile (opt-in)"
    )
    amb_install.add_argument(
        "--shell", choices=["powershell", "bash"],
        default="powershell" if os.name == "nt" else "bash",
    )
    amb_uninstall = ambient_sub.add_parser("uninstall", help="Remove the shell hook")
    amb_uninstall.add_argument(
        "--shell", choices=["powershell", "bash"],
        default="powershell" if os.name == "nt" else "bash",
    )
    amb_report = ambient_sub.add_parser(
        "report", help="Report one finished command (called by the shell hook)"
    )
    amb_report.add_argument("--payload", required=True, help="JSON from the hook")
    amb_brief = ambient_sub.add_parser(
        "brief", help="What does the agent already know about this?"
    )
    amb_brief.add_argument("locator", help="Command, URL, or window title")
    amb_brief.add_argument("--surface", default="terminal")
    ambient_sub.add_parser("status", help="Show ambient loop stats and engine health")

    # adk x-session — bootstrap / inspect the browser-transport X session
    x_session_p = sub.add_parser(
        "x-session", help="Bootstrap the autonomous X poster's logged-in session"
    )
    x_session_sub = x_session_p.add_subparsers(dest="x_session_command")
    x_import_p = x_session_sub.add_parser(
        "import", help="Verify and store an exported logged-in x.com session"
    )
    x_import_p.add_argument(
        "--state", required=True, help="Path to a Playwright storage_state JSON"
    )
    x_session_sub.add_parser("status", help="Is the stored X session logged in right now?")

    # adk decide — decision cards: a structured ask instead of a wall of prose.
    # Registered with REMAINDER so every flag stays defined in exactly one place
    # (adk/decisions/cli.py). Duplicating them here would let the two drift, and a
    # flag that silently does nothing is worse than a flag that does not exist.
    decide_p = sub.add_parser(
        "decide",
        help="Decision cards — raise a structured ask, list what is waiting, answer it",
        add_help=False,
    )
    decide_p.add_argument("decide_args", nargs=argparse.REMAINDER,
                          help="ask | list | show | answer | cancel | watch | sweep")

    # adk storage — the awstorage brick (scan / inventory / diff / propose / apply
    # with a reversible quarantine). Same REMAINDER pass-through shape as `decide`:
    # awstorage owns its own parser, so no flag is defined twice.
    storage_p = sub.add_parser(
        "storage",
        help="Storage inventory — scan a drive, rank what fills it, diff, propose, apply",
        add_help=False,
    )
    storage_p.add_argument("storage_args", nargs=argparse.REMAINDER,
                           help="scan | inventory | diff | propose | approve | apply | "
                                "quarantine | revert | graph")

    # adk harness — AitherShell core: one shell that drives every coding shell
    shell_p = sub.add_parser(
        "harness",
        help="AitherShell — drive Claude Code, other coding harnesses, agents and real terminals",
    )
    shell_p.add_argument("--url", default="", help="Harness daemon URL (default 127.0.0.1:8362)")
    shell_p.add_argument("--token", default="", help="Daemon bearer token")
    shell_sub = shell_p.add_subparsers(dest="shell_command")

    sh_serve = shell_sub.add_parser("serve", help="Run the harness daemon")
    sh_serve.add_argument("--host", default="", help="Bind host (default 127.0.0.1)")
    sh_serve.add_argument("--port", type=int, default=0, help="Bind port (default 8362)")

    sh_harnesses = shell_sub.add_parser("harnesses", help="What this box can drive")
    sh_harnesses.add_argument("--versions", action="store_true", help="Probe versions too")

    shell_sub.add_parser("agents", help="Sovereign agent roster")
    shell_sub.add_parser("profiles", help="Model profiles usable per session")
    shell_sub.add_parser("list", help="Live sessions")

    sh_new = shell_sub.add_parser("new", help="Start a session")
    sh_new.add_argument("--harness", default="claude",
                        help="claude|gemini|terminal|sandbox|aither|group")
    sh_new.add_argument("--cwd", default="", help="Working directory")
    sh_new.add_argument("--model-profile", dest="model_profile", default="",
                        help="Per-session model profile")
    sh_new.add_argument("--model", default="", help="Explicit model id")
    sh_new.add_argument("--permission-mode", dest="permission_mode", default="",
                        help="Harness permission mode")
    sh_new.add_argument("--title", default="", help="Tab title")
    sh_new.add_argument("--agent", default="", help="Sovereign agent id (harness=aither)")
    sh_new.add_argument("--participants", default="", help="Comma list of agents (harness=group)")
    sh_new.add_argument("--target", default="", help="Container name (harness=sandbox)")
    sh_new.add_argument("--attach", action="store_true", help="Attach after creating")

    sh_send = shell_sub.add_parser("send", help="Send a turn to a session")
    sh_send.add_argument("session_id")
    sh_send.add_argument("text")

    sh_attach = shell_sub.add_parser("attach", help="Follow a session's event stream")
    sh_attach.add_argument("session_id")
    sh_attach.add_argument("--since", type=int, default=0, help="Resume from this seq")
    sh_attach.add_argument("--follow", action="store_true", help="Keep following past turn end")

    sh_kill = shell_sub.add_parser("kill", help="Stop a session")
    sh_kill.add_argument("session_id")

    sh_wrap = shell_sub.add_parser("wrap", help="Terminal-resident daemon session (bridge stdin/stdout to daemon)")
    sh_wrap.add_argument("--harness", default="claude", help="Harness type (default: claude)")
    sh_wrap.add_argument("--cwd", default="", help="Working directory")
    sh_wrap.add_argument("--model", default="", help="Model profile or id")
    sh_wrap.add_argument("--resume", default="", help="Resume a previous session by id")
    sh_wrap.add_argument("--title", default="", help="Session title in daemon")

    # adk claude-model — switch Claude Code backend (DeepSeek/Kimi/local/Anthropic)
    claude_model_p = sub.add_parser(
        "claude-model",
        help="Switch Claude Code between DeepSeek, Kimi, local AitherOS models, and Anthropic",
    )
    claude_model_sub = claude_model_p.add_subparsers(dest="claude_model_command")
    claude_model_sub.add_parser("list", help="Show available model profiles")

    cm_use_p = claude_model_sub.add_parser("use", help="Switch Claude Code to a profile")
    cm_use_p.add_argument("profile", help="Profile name (e.g., deepseek-pro, aither-best, anthropic)")
    cm_use_p.add_argument("--project", action="store_true", help="Write to project settings instead of global")

    claude_model_sub.add_parser("status", help="Show the active profile")

    cm_check_p = claude_model_sub.add_parser("check", help="Prove the active profile answers a real turn")
    cm_check_p.add_argument("--timeout", type=float, default=120.0, help="Timeout in seconds")

    cm_bridge_p = claude_model_sub.add_parser("bridge", help="Manage the translation bridge")
    cm_bridge_p.add_argument("bridge_action", choices=["start", "status", "stop"], help="Bridge action")

    cm_auto_p = claude_model_sub.add_parser("auto", help="One-shot: start bridge + switch + verify")
    cm_auto_p.add_argument("profile", help="Profile name")

    claude_model_sub.add_parser("failover", help="Test current; if broken, switch to next working provider")

    cm_watch_p = claude_model_sub.add_parser("watch", help="Auto-switch on rate limit (daemon)")
    cm_watch_p.add_argument("--daemon", action="store_true", help="Run in background")
    cm_watch_p.add_argument("--stop", action="store_true", help="Stop the background daemon")

    # Workflow shortcuts — instant model switching by role
    claude_model_sub.add_parser("plan", help="→ Anthropic Opus 5 (architecture, design, review)")
    claude_model_sub.add_parser("code", help="→ DeepSeek Flash (fast ultracode, 1M context)")
    claude_model_sub.add_parser("reason", help="→ DeepSeek Pro (deep reasoning, 1M context)")
    claude_model_sub.add_parser("kimi", help="→ Kimi K3 (1M context, thinking always on)")
    claude_model_sub.add_parser("local", help="→ qwen3.6-27b on DGX")
    claude_model_sub.add_parser("fast", help="→ gemma4-12b for trivial tasks")

    # adk claude-account — manage multiple Claude Code logins
    claude_account_p = sub.add_parser(
        "claude-account",
        help="Manage multiple Claude Code (Anthropic) account profiles",
    )
    claude_account_sub = claude_account_p.add_subparsers(dest="claude_account_command")

    ca_save_p = claude_account_sub.add_parser("save", help="Save current Claude Code login")
    ca_save_p.add_argument("name", help="Profile name (e.g., personal, work)")
    ca_save_p.add_argument("--force", action="store_true", help="Overwrite existing profile without asking")

    claude_account_sub.add_parser("list", help="List saved profiles")

    ca_switch_p = claude_account_sub.add_parser("switch", help="Switch to a saved profile")
    ca_switch_p.add_argument("name", help="Profile name")

    claude_account_sub.add_parser("current", help="Show current profile name")

    ca_remove_p = claude_account_sub.add_parser("remove", help="Delete a saved profile")
    ca_remove_p.add_argument("name", help="Profile name")

    claude_account_sub.add_parser(
        "usage", help="Show multi-account usage and scheduling status"
    )

    # adk claude — scoped headless Claude Code subagent runner
    claude_p = sub.add_parser(
        "claude",
        help="Run scoped headless Claude Code subagents (serve/spawn/runs/kill)",
    )
    claude_sub = claude_p.add_subparsers(dest="claude_command")

    cl_serve_p = claude_sub.add_parser("serve", help="Run the subagent runner daemon")
    cl_serve_p.add_argument("--host", default="", help="Bind host (default 127.0.0.1)")
    cl_serve_p.add_argument("--port", type=int, default=0, help="Bind port (default 8365)")
    cl_serve_p.add_argument("--token", default="", help="Bearer token (default: resolved/generated)")
    cl_serve_p.add_argument("--register", action="store_true", help="Register as durable service")
    cl_serve_p.add_argument("--unregister", action="store_true", help="Unregister durable service")
    cl_serve_p.add_argument("--status", action="store_true", help="Report service status")
    cl_serve_p.add_argument("--force", action="store_true", help="Skip confirmation prompts")

    cl_spawn_p = claude_sub.add_parser("spawn", help="Spawn a scoped subagent run")
    cl_spawn_p.add_argument("--task", default="", help="Task prompt text")
    cl_spawn_p.add_argument("--task-file", default="", help="Read task prompt from file")
    cl_spawn_p.add_argument("--allow", default="", help="Comma list of allowed tools (REQUIRED)")
    cl_spawn_p.add_argument("--deny", default="", help="Comma list of disallowed tools")
    cl_spawn_p.add_argument(
        "--allow-dangerous",
        action="store_true",
        help="Lift the runner-side tool ceiling for this run (secrets, money, "
             "destructive fleet ops). Off by default; see claude_runner.ceiling_deny_rules.",
    )
    cl_spawn_p.add_argument("--model", default="", help="Model override (e.g. haiku)")
    cl_spawn_p.add_argument("--append-system-prompt", default="", help="Appended system prompt")
    cl_spawn_p.add_argument("--mcp-config", default="", help="Path to scoped MCP config JSON")
    cl_spawn_p.add_argument("--cwd", default="", help="Working directory (default: isolated workdir)")
    cl_spawn_p.add_argument("--timeout", type=int, default=0, help="Run timeout seconds (default 600)")
    cl_spawn_p.add_argument("--budget-usd", type=float, default=0, help="Max spend for this run")
    cl_spawn_p.add_argument("--account", default="", help="Saved claude-account profile for this run")
    cl_spawn_p.add_argument(
        "--resume",
        default="",
        help="Continue an existing session by id (requires --cwd, excludes --account)",
    )
    cl_spawn_p.add_argument("--goal", default="", help="Goal id to attribute this run to")
    cl_spawn_p.add_argument("--url", default="", help="Runner URL (default http://127.0.0.1:8365)")
    cl_spawn_p.add_argument("--token", default="", help="Bearer token")
    cl_spawn_p.add_argument("--no-wait", action="store_true", help="Return run_id immediately")

    cl_runs_p = claude_sub.add_parser("runs", help="List subagent runs")
    cl_runs_p.add_argument("--url", default="", help="Runner URL")
    cl_runs_p.add_argument("--token", default="", help="Bearer token")

    cl_kill_p = claude_sub.add_parser("kill", help="Cancel a run")
    cl_kill_p.add_argument("run_id", help="Run id to cancel")
    cl_kill_p.add_argument("--url", default="", help="Runner URL")
    cl_kill_p.add_argument("--token", default="", help="Bearer token")

    # adk agent-prompt
    ap_p = sub.add_parser("agent-prompt", help="Print the setup prompt for AI coding agents")
    ap_p.add_argument("--raw", action="store_true", help="Print raw prompt without footer")

    # aither connect
    connect_p = sub.add_parser("connect", help="Connect to AitherOS — detect LLMs, set up gateway, or join desktop mesh")
    connect_p.add_argument("--api-key", help="AITHER_API_KEY for cloud inference")
    connect_p.add_argument("--elysium", metavar="URL",
                           help="Connect to desktop AitherOS (e.g. http://192.168.1.10:8001)")
    connect_p.add_argument("--token", help="Node token for desktop mesh authentication")
    connect_p.add_argument("--save", action="store_true", default=True,
                           help="Save config to ~/.aither/config.json (default: true)")
    connect_p.add_argument("--no-save", action="store_false", dest="save",
                           help="Don't save config")

    # aither setup
    # adk setup-all — one command to install ALL client products (adk, shell, node, connect, +stack)
    setup_all_p = sub.add_parser(
        "setup-all", help="Install/set up all AitherOS client products (adk + shell + node + connect)")
    setup_all_p.add_argument("--only", default="",
                             help="Comma list — install ONLY these (adk,shell,node,connect,aitherzero)")
    setup_all_p.add_argument("--skip", default="",
                             help="Comma list — skip these products")
    setup_all_p.add_argument("--with-stack", dest="with_stack", default="", metavar="PROFILE",
                             help="Also deploy the AitherZero stack via `adk setup --stack` (e.g. core, full)")
    setup_all_p.add_argument("--dev", action="store_true",
                             help="Editable install of awdk from the local checkout (pip -e)")
    setup_all_p.add_argument("--dry-run", dest="dry_run", action="store_true",
                             help="Print the install plan without doing anything")
    setup_all_p.add_argument("--strict", action="store_true",
                             help="Abort on the first failed product (default: best-effort, continue)")
    setup_all_p.add_argument("--yes", "--non-interactive", dest="yes", action="store_true",
                             help="Non-interactive")

    setup_p = sub.add_parser("setup", help="Interactive GPU setup wizard (vLLM/Ollama) + optional AitherOS stack")
    setup_p.add_argument("shortcut", nargs="?", default=None,
                         help="Quick setup: 'nemotron' (--tier lite), 'llamacpp' / 'local' / 'endpoint' (native local orchestrator, no Docker)")
    setup_p.add_argument("--mode", choices=["auto", "cloud", "hybrid"],
                         default="auto",
                         help="Setup mode: auto (detect GPU), cloud (cloud-only, no GPU), hybrid (local + cloud reasoning)")
    setup_p.add_argument("--tier", choices=["nano", "lite", "standard", "standard-tq4", "full", "hybrid", "hybrid-tq4", "ollama", "llamacpp"],
                         help="Force a specific tier (default: auto-detect from GPU). 'llamacpp' = native local Nemotron-Orchestrator-8B for endpoints (Windows/macOS/Linux, no Docker)")
    setup_p.add_argument("--backend", choices=["vllm", "ollama", "llamacpp"], default=None,
                         help="Backend engine override (default inferred from --tier)")
    setup_p.add_argument("--llamacpp-quant", default=None,
                         help="llama.cpp GGUF quant (e.g. Q4_K_M, Q5_K_M, Q8_0). Default: auto-pick from VRAM/RAM")
    setup_p.add_argument("--llamacpp-port", type=int, default=None,
                         help="llama.cpp server port (default: 8209)")
    setup_p.add_argument("--no-service", action="store_true",
                         help="llama.cpp: skip installing system service (systemd/launchd/scheduled task)")
    setup_p.add_argument("--reasoning-api", choices=["anthropic", "openai", "deepseek", "gateway"],
                         help="Cloud API for reasoning (effort 7+) — hybrid mode")
    setup_p.add_argument("--reasoning-model", default="",
                         help="Specific model for reasoning backend")
    setup_p.add_argument("--dgx-spark", metavar="URL",
                         help="DGX Spark / remote vLLM URL (e.g. http://192.168.0.33:8000)")
    setup_p.add_argument("--stack", choices=["minimal", "core", "full", "headless", "gpu", "agents"],
                         help="Also deploy AitherOS services via AitherZero")
    setup_p.add_argument("--dry-run", action="store_true",
                         help="Show what would happen without making changes")
    setup_p.add_argument("--non-interactive", action="store_true",
                         help="No prompts — auto-accept defaults (for CI/automation)")
    setup_p.add_argument("--hf-token", default="",
                         help="HuggingFace token for gated models")
    setup_p.add_argument("--api-key", help="AITHER_API_KEY for cloud + stack deployment")
    setup_p.add_argument("--output", default="docker-compose.vllm.yml",
                         help="Output compose file path (default: docker-compose.vllm.yml)")
    setup_p.add_argument("--force", action="store_true",
                         help="Start new containers even if inference is already running")

    # aither ui — swappable agent web UI packs
    ui_p = sub.add_parser("ui", help="Manage the agent's web UI pack (ls / set / path)")
    ui_sub = ui_p.add_subparsers(dest="ui_command")
    ui_sub.add_parser("ls", help="List available UI packs (marks the selected one)")
    ui_set_p = ui_sub.add_parser("set", help="Select a UI pack (persists to ~/.aither/config.json)")
    ui_set_p.add_argument("name", help="Pack name (e.g. console, minimal, llamacpp)")
    ui_sub.add_parser("path", help="Show the selected pack + drop-in dir + whether it resolves")

    # aither agents — discover agents in the mesh
    agents_p = sub.add_parser("agents", help="Discover agents in the mesh (ls)")
    agents_sub = agents_p.add_subparsers(dest="agents_command")
    agents_ls = agents_sub.add_parser("ls", help="List every agent in the mesh + its inference backend")
    agents_ls.add_argument("--format", choices=["table", "json"], default="table")

    # adk agent — run/manage host-tier agent loops
    agent_p = sub.add_parser("agent", help="Run/manage host-tier agent loops (run, list, status, stop)")
    agent_sub = agent_p.add_subparsers(dest="agent_command")

    # adk agent run <name>
    agent_run_p = agent_sub.add_parser("run", help="Start a host-tier agent loop")
    agent_run_p.add_argument("name", help="Agent name (e.g. watch, monitor)")
    agent_run_p.add_argument("--daemon-url", default="http://127.0.0.1:8362",
                             help="Harness daemon URL (default: http://127.0.0.1:8362)")
    agent_run_p.add_argument("--token", help="Bearer token for daemon (or $AITHER_HARNESS_TOKEN)")
    agent_run_p.add_argument("--room", default="main", help="Room name (default: main)")
    agent_run_p.add_argument("--interval", type=float, default=5.0,
                             help="Loop interval in seconds (default: 5.0)")
    agent_run_p.add_argument("--foreground", action="store_true",
                             help="Block terminal (default: detach)")

    # adk agent list
    agent_sub.add_parser("list", help="List running agent loops")

    # adk agent status
    agent_status_p = agent_sub.add_parser("status", help="Show status of an agent loop")
    agent_status_p.add_argument("name", help="Agent name")

    # adk agent stop
    agent_stop_p = agent_sub.add_parser("stop", help="Stop a running agent loop")
    agent_stop_p.add_argument("name", help="Agent name")

    # aither chat — chat with a mesh agent by name (responds on its own inference)
    chat_p = sub.add_parser("chat", help="Chat with a mesh agent by name (adk chat <agent> [msg])")
    chat_p.add_argument("agent", help="Agent name from `adk agents ls`")
    chat_p.add_argument("message", nargs="?", help="Message (omit for an interactive loop)")

    # adk invoke — command & control: run a TOOL on a mesh agent over signed A2A
    invoke_p = sub.add_parser(
        "invoke", help="Invoke a tool on a mesh agent over signed A2A (adk invoke <agent> <skill>)")
    invoke_p.add_argument("agent", help="Agent name from `adk agents ls`")
    invoke_p.add_argument("skill", help="Tool/skill name to invoke on the remote agent")
    invoke_p.add_argument("--arg", action="append", metavar="k=v",
                          help="Tool argument (repeatable); value is JSON-parsed when possible")
    invoke_p.add_argument("--as-agent", dest="as_agent",
                          help="Name to sign as (keypair ~/.aither/agent_key.<name>.pem)")

    # aither aeon
    aeon_p = sub.add_parser("aeon", help="Multi-agent group chat")
    aeon_p.add_argument("-p", "--preset", help="Preset: balanced, creative, technical, security, minimal, duo_code, research")
    aeon_p.add_argument("-a", "--agents", help="Comma-separated agent names (e.g. demiurge,athena)")
    aeon_p.add_argument("-r", "--rounds", type=int, default=1, help="Discussion rounds per message (default: 1)")
    aeon_p.add_argument("--no-synthesize", action="store_true", help="Skip orchestrator synthesis")

    # adk create-app — scaffold a full awkit workspace app
    ca_p = sub.add_parser("create-app",
                          help="Scaffold a awkit workspace app")
    ca_p.add_argument("name", help="App name (e.g. 'ACME Assistant')")
    ca_p.add_argument("-o", "--output", help="Output directory (default: ./<slug>)")
    ca_p.add_argument("--company", default="", help="Company name")
    ca_p.add_argument("--industry", default="general", help="Industry vertical")
    ca_p.add_argument("--description", default="", help="What this app does")
    ca_p.add_argument("--subdomain", default="", help="URL slug (auto-derived from name)")
    ca_p.add_argument("--color", default="#6366f1", help="Primary brand color")
    ca_p.add_argument("--template", default="default",
                      choices=["default", "basic", "advanced", "custom"],
                      help="Base template (default: default)")
    ca_p.add_argument("--llm-provider", dest="llm_provider", default="aitheros",
                      choices=["aitheros", "ollama", "portal", "deepseek", "openai", "anthropic", "gemini"],
                      help="LLM provider (default: aitheros)")
    ca_p.add_argument("--force", action="store_true", help="Overwrite existing directory")

    # aither deploy — component deployment OR agent deployment
    deploy_p = sub.add_parser("deploy", help="Deploy AitherOS components or agents")
    deploy_sub = deploy_p.add_subparsers(dest="component")

    # aither deploy ollama
    d_ollama = deploy_sub.add_parser("ollama", help="Install Ollama + pull models for your GPU")
    d_ollama.add_argument("--models", help="Comma-separated model list (default: auto-select by GPU)")
    d_ollama.add_argument("--dry-run", action="store_true", help="Show what would happen")

    # aither deploy vllm
    d_vllm = deploy_sub.add_parser("vllm", help="Deploy vLLM containers directly (use 'adk setup' for guided wizard)")
    d_vllm.add_argument("--tier", choices=["nano", "lite", "standard", "full"],
                        help="Force a specific tier (default: auto-detect)")
    d_vllm.add_argument("--hf-token", default="", help="HuggingFace token for gated models")
    d_vllm.add_argument("--dry-run", action="store_true", help="Show what would happen")

    # aither deploy node
    d_node = deploy_sub.add_parser(
        "node",
        help="Deploy agent node (default: ADK-native lightweight; --genesis for full stack)",
    )
    d_node.add_argument("--genesis", action="store_true",
                         help="Use full Genesis stack (14+ containers) instead of ADK-native (2 containers)")
    d_node.add_argument("--gpu", action="store_true", help="Enable GPU-accelerated vLLM")
    d_node.add_argument("--dashboard", action="store_true", help="Enable workspace dashboard (port 3000)")
    d_node.add_argument("--mesh", action="store_true", help="Enable mesh networking (Genesis mode only)")
    d_node.add_argument("--memory", action="store_true", help="Enable persistent vector memory (Spirit)")
    d_node.add_argument("--addons", help="Comma-separated addon IDs to co-deploy (e.g. qdrant,knowledge-rag)")
    d_node.add_argument("--tag", default="latest", help="Docker image tag (default: latest)")
    d_node.add_argument("--api-key", help="AITHER_API_KEY (or set env var)")
    d_node.add_argument("--dry-run", action="store_true", help="Show what would happen")
    d_node.add_argument("--sovereign", action="store_true",
                         help="Register with Aitherium hub after deployment (federation)")
    d_node.add_argument("--hub", default="https://portal.aitherium.com",
                         help="Hub URL for federation (default: portal.aitherium.com)")
    d_node.add_argument("--tenant", help="Tenant slug for federation registration")
    d_node.add_argument("--federate", action="store_true",
                         help="Also start AitherFederate (port 8094) — agent-fleet "
                              "registration, product-catalog sync, and knowledge "
                              "ingestion routing to portal.aitherium.com. Requires "
                              "--portal-token or AITHER_PORTAL_TOKEN.")
    d_node.add_argument("--portal-token", help="AITHER_PORTAL_TOKEN for --federate (or set env var)")

    # aither deploy core
    d_core = deploy_sub.add_parser("core", help="Core services (Node, Pulse, Watch, Genesis, Veil)")
    d_core.add_argument("--addons", help="Comma-separated addon IDs to co-deploy")
    d_core.add_argument("--tag", default="latest", help="Docker image tag (default: latest)")
    d_core.add_argument("--api-key", help="AITHER_API_KEY (or set env var)")
    d_core.add_argument("--dry-run", action="store_true", help="Show what would happen")

    # aither deploy full
    d_full = deploy_sub.add_parser("full", help="Full AitherOS stack (~31 containers)")
    d_full.add_argument("--profile", default="chat-agents",
                        choices=["chat-minimal", "chat-full", "chat-agents"],
                        help="Docker Compose profile (default: chat-agents)")
    d_full.add_argument("--addons", help="Comma-separated addon IDs to co-deploy")
    d_full.add_argument("--tag", default="latest", help="Docker image tag (default: latest)")
    d_full.add_argument("--api-key", help="AITHER_API_KEY (or set env var)")
    d_full.add_argument("--dry-run", action="store_true", help="Show what would happen")

    # aither deploy fleet-refresh
    d_fleet = deploy_sub.add_parser(
        "fleet-refresh",
        help="Rebuild all lib-baking Python images + safe rolling recreate of the running fleet")
    d_fleet.add_argument("--build-only", action="store_true", help="Rebuild images only")
    d_fleet.add_argument("--recreate-only", action="store_true", help="Recreate from existing images")
    d_fleet.add_argument("--dry-run", action="store_true", help="Print the target list, do nothing")

    # aither deploy addons
    d_addons = deploy_sub.add_parser("addons", help="Deploy self-hosted addon services")
    d_addons.add_argument("addon_ids", nargs="*", help="Addon IDs (default: all available)")
    d_addons.add_argument("--list", dest="list_addons", action="store_true",
                          help="List available addons without deploying")
    d_addons.add_argument("--tag", default="latest", help="Docker image tag (default: latest)")
    d_addons.add_argument("--api-key", help="AITHER_API_KEY (or set env var)")
    d_addons.add_argument("--dry-run", action="store_true", help="Show what would happen")
    d_addons.add_argument("--sovereign", action="store_true",
                          help="Register with federation hub after deployment")
    d_addons.add_argument("--hub", default="https://portal.aitherium.com",
                          help="Hub URL for federation")
    d_addons.add_argument("--tenant", help="Tenant slug for federation registration")

    # aither deploy connect
    d_connect = deploy_sub.add_parser("connect", help="Awconnect browser extension")
    d_connect.add_argument("--api-key", help="AITHER_API_KEY (or set env var)")
    d_connect.add_argument("--dry-run", action="store_true", help="Show what would happen")

    # aither deploy desktop
    d_desktop = deploy_sub.add_parser("desktop", help="AitherDesktop native application")
    d_desktop.add_argument("--api-key", help="AITHER_API_KEY (or set env var)")
    d_desktop.add_argument("--dry-run", action="store_true", help="Show what would happen")

    # aither deploy grid
    d_grid = deploy_sub.add_parser(
        "grid",
        help="Deploy grid distributed stack (GPU + Mac + cluster)",
    )
    d_grid.add_argument("--mac-host", help="Mac Mini IP for Ollama reasoning")
    d_grid.add_argument("--cluster-nodes", help="JSON array of cluster node IPs")
    d_grid.add_argument("--hf-token", default="", help="HuggingFace token for gated models")
    d_grid.add_argument("--skip-health", action="store_true",
                           help="Skip remote node health checks")
    d_grid.add_argument("--dry-run", action="store_true", help="Show what would happen")

    # aither deploy stop <component>
    d_stop = deploy_sub.add_parser("stop", help="Stop a running deployment")
    d_stop.add_argument("stop_target", nargs="?",
                        help="Component to stop: ollama, vllm, node, core, full, all")

    # aither deploy agent — download + configure + start a tenant agent, OR upload to gateway
    d_agent = deploy_sub.add_parser("agent", help="Deploy a tenant agent to this machine (or upload to gateway)")
    d_agent.add_argument("name", nargs="?", help="Agent name/slug (e.g. my-agent)")
    d_agent.add_argument("-d", "--directory", help="Project directory (default: .)")
    d_agent.add_argument("--api-key", help="AITHER_API_KEY")
    d_agent.add_argument("--gateway", help="Gateway URL (default: gateway.aitherium.com)")
    d_agent.add_argument("--capabilities", help="Comma-separated capabilities")
    d_agent.add_argument("--description", help="Agent description")
    d_agent.add_argument("--version", help="Agent version")
    d_agent.add_argument("--target", choices=["gateway", "docker", "kubernetes", "systemd", "cloud-gpu"],
                          default="gateway", help="Deploy target (default: gateway)")
    d_agent.add_argument("--strategy", choices=["rolling", "blue-green", "canary", "recreate"],
                          default="rolling", help="Deployment strategy (for container targets)")
    d_agent.add_argument("--tenant", help="Tenant slug — triggers download+run mode (e.g. acme-consulting)")
    d_agent.add_argument("--inference", choices=["local", "cloud", "hybrid"],
                          help="Inference mode for this endpoint")
    d_agent.add_argument("--from", dest="from_url", help="Direct download URL for the agent package")

    # adk workspace — manage dev workspaces on AitherOS tunnel
    ssh_p = sub.add_parser(
        "ssh", help="Open a remote terminal into a prod/dev environment via the tunnel")
    ssh_p.add_argument("container", nargs="?", default=None,
                       help="Dev-workspace container to attach to (optional)")
    ssh_p.add_argument("--container", dest="container_opt", default=None,
                       help="Dev-workspace container (alternative to positional)")
    ssh_p.add_argument("--tunnel-url", default="tunnel.aitherium.com",
                       help="Tunnel host (default: tunnel.aitherium.com)")
    ssh_p.add_argument("-x", "--exec", dest="exec_cmd", action="append", metavar="CMD",
                       help="Run CMD headlessly (no TTY) and exit with its code; repeatable")
    ssh_p.add_argument("--timeout", type=float, default=120,
                       help="Headless run cap in seconds (default 120)")
    ssh_p.add_argument("--json", action="store_true",
                       help="Headless: print {code, output, reason} as JSON instead of streaming")
    ssh_p.add_argument("trailing", nargs=argparse.REMAINDER,
                       help="After `--`: one command line to run headlessly")

    sshcert_p = sub.add_parser(
        "ssh-cert",
        help="Fetch a short-lived SSH certificate from the AitherCert SSH CA (GitHub org SSH)")
    sshcert_p.add_argument("--github-user", dest="github_user", default=None,
                           help="GitHub login the cert is bound to")
    sshcert_p.add_argument("--key", default=None,
                           help="Public key to certify (default: ~/.ssh/id_ed25519.pub)")
    sshcert_p.add_argument("--ttl-hours", dest="ttl_hours", type=int, default=24,
                           help="Certificate lifetime in hours, max 168 (default: 24)")
    sshcert_p.add_argument("--cert-url", dest="cert_url", default=None,
                           help="AitherCert base URL (default: $AITHER_CERT_URL or https://localhost:8113)")

    ws_p = sub.add_parser("workspace", help="Manage dev workspaces on AitherOS tunnel")
    ws_sub = ws_p.add_subparsers(dest="ws_command")

    ws_create = ws_sub.add_parser("create", help="Create a cloud dev workspace")
    ws_create.add_argument("--scope", default="fullstack",
                           help="Scope template: fullstack, veil, portal, frontend, backend, node, etc.")
    ws_create.add_argument("--tunnel-url", default="https://tunnel.aitherium.com",
                           help="Tunnel URL (default: tunnel.aitherium.com)")

    ws_bundle = ws_sub.add_parser("bundle", help="Download a dev workspace bundle (docker-compose + WireGuard)")
    ws_bundle.add_argument("--scope", default="fullstack",
                           help="Scope template")
    ws_bundle.add_argument("-o", "--output", default="aitheros-devws.zip",
                           help="Output zip file path")
    ws_bundle.add_argument("--tunnel-url", default="https://tunnel.aitherium.com",
                           help="Tunnel URL")

    ws_list = ws_sub.add_parser("list", help="List your active workspaces")
    ws_list.add_argument("--tunnel-url", default="https://tunnel.aitherium.com",
                         help="Tunnel URL")

    ws_submit = ws_sub.add_parser("submit", help="Submit changes from workspace (commit + PR)")
    ws_submit.add_argument("message", help="Commit message")
    ws_submit.add_argument("--workspace", help="Workspace container name (auto-detected if in one)")
    ws_submit.add_argument("--tunnel-url", default="https://tunnel.aitherium.com",
                           help="Tunnel URL")

    ws_sub.add_parser("scopes", help="List available scope templates")

    # aither onboard — interactive onboarding wizard
    onboard_p = sub.add_parser("onboard", help="Interactive onboarding — detect, configure, integrate")
    onboard_p.add_argument("--api-key", help="AITHER_API_KEY")
    onboard_p.add_argument("--tenant", help="Tenant slug to associate this node with")
    onboard_p.add_argument("--agent", help="Register a running agent with the portal fleet")
    onboard_p.add_argument("--non-interactive", action="store_true", help="Skip prompts, use defaults")
    onboard_p.add_argument("--quick", action="store_true",
                           help="One-command: auto-run inference, install default pack, enroll")
    onboard_p.add_argument("--pack", default="openclaw", help="Pack to install (default: openclaw)")
    onboard_p.add_argument("--webgpu", action="store_true",
        help="Self-bootstrap onto in-browser WebGPU inference (no server model — the GUI runs the model on the user's GPU)")
    onboard_p.add_argument("--discord", action="store_true",
        help="Automated onboarding: deploy your agent as a Discord bot (validate token live, print invite link, verify identity/tools, optional --run)")
    onboard_p.add_argument("--identity", default=None,
        help="Agent identity for the Discord bot (default: $ADK_AGENT or 'aither')")
    onboard_p.add_argument("--token", default=None,
        help="Discord bot token (or set DISCORD_BOT_TOKEN)")
    onboard_p.add_argument("--tools-module", default=None,
        help="optional module path whose @tool tools register (e.g. pack.tools.shop)")
    onboard_p.add_argument("--run", action="store_true",
        help="launch the Discord bot after onboarding (stays connected)")
    onboard_p.add_argument("--skip-pack-install", action="store_true",
        help="don't `adk install` the --pack (assume it's already installed)")

    # adk enroll — register workstation with control plane
    enroll_p = sub.add_parser("enroll", help="Register this workstation with the control plane")
    enroll_p.add_argument("--portal", help="Portal URL (default: portal.aitherium.com)")
    enroll_p.add_argument("--genesis", help="Genesis URL (default: localhost:8001)")
    enroll_p.add_argument("--no-heartbeat", action="store_true", help="Skip background heartbeat")
    enroll_p.add_argument("--force", action="store_true", help="Re-enroll even if already registered")

    # aither host — one command: serve a self-hosted agent + connect it to your fleet
    host_p = sub.add_parser(
        "host",
        help="Host a self-hosted agent (your model key) + connect it to your fleet — one command",
    )
    host_p.add_argument("--provider", help="Model provider: deepseek/openai/anthropic (prompted if omitted)")
    host_p.add_argument("--model", help="Model name (default: the provider's default)")
    host_p.add_argument("--name", help="Fleet label for this agent (default: <hostname>-adk)")
    host_p.add_argument("--identity", default="aither", help="Agent identity to load (default: aither)")
    host_p.add_argument("--port", type=int, default=8080, help="Local port for aither-serve (default: 8080)")
    host_p.add_argument("--approve", help="Comma-list of tools that pause for approval, or '*' (default: file_write,shell_exec,shell)")
    host_p.add_argument("--token", help="Control-plane token for registration (else 'adk login' / $AITHER_PORTAL_TOKEN)")
    host_p.add_argument("--auth-token", help="Callback bearer the control plane presents back to your agent (minted if omitted)")
    host_p.add_argument("--portal", default=_control_plane(), help="Control-plane base URL")
    host_p.add_argument("--login-url", help="Device-flow login base URL (default: --portal, then portal.aitherium.com)")
    host_p.add_argument("--register-url", help="Full fleet-register URL (overrides --portal; e.g. http://localhost:8001/v1/agent/fleet/register)")
    host_p.add_argument("--no-register", action="store_true", help="Run locally only — no tunnel, no fleet registration")
    host_p.add_argument("--dry-run", action="store_true", help="Show what would happen without starting anything")

    # adk integrate — connect external tools
    integrate_p = sub.add_parser("integrate", help="Connect external tools (OpenClaw, etc.)")
    integrate_p.add_argument("target", nargs="?", default="list",
                             help="Integration target: openclaw, list")
    integrate_p.add_argument("--mode", choices=["local", "cloud", "hybrid", "auto"],
                             help="Integration mode (default: auto-detect)")
    integrate_p.add_argument("--api-key", help="AITHER_API_KEY for cloud mode")
    integrate_p.add_argument("--dry-run", action="store_true",
                             help="Show config without writing")
    integrate_p.add_argument("--force", action="store_true",
                             help="Overwrite existing integration config")

    # adk index — index a codebase for CodeGraph
    index_p = sub.add_parser("index", help="Index a codebase for code search (CodeGraph)")
    index_p.add_argument("path", nargs="?", default=".", help="Path to index (default: current directory)")
    index_p.add_argument("--embed", action="store_true", help="Also generate embeddings for semantic search")
    index_p.add_argument("--stats", action="store_true", help="Show Python metrics after indexing")

    # adk test
    test_p = sub.add_parser("test", help="Run agent tests")
    test_p.add_argument("-d", "--directory", help="Project directory (default: .)")
    test_p.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    test_p.add_argument("--coverage", action="store_true", help="Show coverage report")

    # adk status
    status_p = sub.add_parser("status", help="Show backend and service status")
    status_p.add_argument("--json", action="store_true",
                          help="Machine-readable JSON (agent state) for AI agents/CI")

    # adk publish — submit to Elysium marketplace
    publish_p = sub.add_parser("publish", help="Publish agent to Elysium marketplace")
    publish_p.add_argument("name", nargs="?", help="Agent name (default: from config.yaml)")
    publish_p.add_argument("-d", "--directory", help="Project directory (default: .)")
    publish_p.add_argument("--api-key", help="AITHER_API_KEY")
    publish_p.add_argument("--gateway", help="Gateway URL (default: gateway.aitherium.com)")
    publish_p.add_argument("--description", help="Agent description for marketplace")
    publish_p.add_argument("--capabilities", help="Comma-separated capabilities")
    publish_p.add_argument("--version", help="Agent version (default: 0.1.0)")
    publish_p.add_argument("--pricing", default="free",
                           help="Pricing model: free, per_request, flat_monthly")
    publish_p.add_argument("--tier", default="agent",
                           help="Agent tier: reflex, agent, reasoning, orchestrator")
    publish_p.add_argument("--category", default="general",
                           help="Category: general, engineering, content, research, security")
    publish_p.add_argument("--dry-run", action="store_true",
                           help="Validate without publishing")

    # adk admin — administration commands
    admin_p = sub.add_parser("admin", help="Administration commands")
    admin_sub = admin_p.add_subparsers(dest="admin_command")
    admin_token_p = admin_sub.add_parser("create-token",
                                          help="Create a node token on the desktop for mesh enrollment")
    admin_token_p.add_argument("--name", default="", help="Node name (default: hostname)")
    admin_token_p.add_argument("--url", default="http://localhost:8001",
                               help="Genesis URL (default: http://localhost:8001)")

    # adk disconnect — leave desktop mesh
    sub.add_parser("disconnect", help="Disconnect from desktop AitherOS mesh")

    # adk backend — manage LLM backends
    relay_p = sub.add_parser("relay", help="Connect this agent to AitherRelay chat (join + serve DMs)")
    relay_sub = relay_p.add_subparsers(dest="relay_command")
    relay_join_p = relay_sub.add_parser("join", help="Join AitherRelay and answer DMs on this agent's own inference")
    relay_join_p.add_argument("--nick", help="Agent nick on the relay (default: your login username)")
    relay_join_p.add_argument("--url", help="Relay API base (default: cloud relay.aitherium.com)")
    relay_join_p.add_argument("--local", action="store_true", help="Use the local fleet relay (https://localhost:8205/v1)")
    relay_join_p.add_argument("--channel", default="#agents", help="Channel to join (default: #agents)")
    relay_join_p.add_argument("--token", help="Bearer credential (default: provisioned/saved login / $AITHER_RELAY_TOKEN)")

    relay_prov_p = relay_sub.add_parser(
        "provision", help="Enroll a fleet agent so it may DM humans (binds nick -> your owner identity)")
    relay_prov_p.add_argument("nick", help="Agent nick to enroll (e.g. optiplex-agent)")
    relay_prov_p.add_argument("--local", action="store_true", help="Use the local mesh ACTA/portal-gateway (https://localhost:8206)")
    relay_prov_p.add_argument("--acta-url", help="ACTA/portal-gateway base URL (serves /v1/auth/keys)")
    relay_prov_p.add_argument("--roster", help="Path to the relay fleet_trust.json (default: ./AitherOS/config/relay/fleet_trust.json or $AITHER_RELAY_FLEET_TRUST_FILE)")
    relay_prov_p.add_argument("--user-id", help="Assert your owner user_id (skips ACTA lookup)")
    relay_prov_p.add_argument("--agent-key", help="Pre-minted agent-scoped ACTA key (skip minting)")
    relay_prov_p.add_argument("--token", help="Owner bearer credential (default: your saved login / $AITHER_API_KEY)")
    relay_prov_p.add_argument("--internal-token", help="Service-internal token to MINT a revocable agent key + write the vault (default: $AITHER_INTERNAL_SECRET; on-mesh only)")
    relay_prov_p.add_argument("--secrets-url", help="AitherSecrets base URL for storing the minted key (default: on-mesh vault)")
    relay_prov_p.add_argument("--no-mint", action="store_true", help="Do not mint; reuse your login key even if an internal token is present")

    relay_notif_p = relay_sub.add_parser(
        "notifications", help="Get stored notifications for this agent (one-shot)")
    relay_notif_p.add_argument("--nick", help="Agent nick on the relay (default: your login username)")
    relay_notif_p.add_argument("--url", help="Relay API base (default: cloud relay.aitherium.com)")
    relay_notif_p.add_argument("--local", action="store_true", help="Use the local fleet relay (https://localhost:8205/v1)")
    relay_notif_p.add_argument("--unread", action="store_true", help="Only show unread notifications")
    relay_notif_p.add_argument("--ack", action="store_true", help="Mark retrieved notifications as read")
    relay_notif_p.add_argument("--token", help="Bearer credential (default: provisioned/saved login / $AITHER_RELAY_TOKEN)")

    relay_up_p = relay_sub.add_parser("up", help="Start a sovereign AitherNet relay (Docker compose bundle)")
    relay_up_p.add_argument("--slug", help="Node identifier for this relay (default: relay-<hostname>)")
    relay_up_p.add_argument("--rooms", help="Comma-separated advertised rooms (default: #general)")
    relay_up_p.add_argument("--hub-url", help="Hub relay endpoint for federation (default: wss://relay.aitherium.com/ws/chat)")
    relay_up_p.add_argument("--no-federation", action="store_true", help="Disable hub federation (default: enabled)")
    relay_up_p.add_argument("--directory-url", help="Directory service URL for registration (optional)")
    relay_up_p.add_argument("--public-endpoint", help="This relay's own public wss:// or https:// address, listed in the community directory (required for directory registration)")
    relay_up_p.add_argument("--token", help="Node token for hub auth (default: $AITHER_NODE_TOKEN)")
    relay_up_p.add_argument("--port", type=int, default=8205, help="Local relay port (default: 8205)")
    relay_up_p.add_argument("--compose-file", help="Path to docker-compose file (default: auto-detect)")
    relay_up_p.add_argument("--foreground", action="store_true", help="Run in foreground (default: detached)")
    relay_up_p.add_argument("--dry-run", action="store_true", help="Show what would run, don't start")

    backend_p = sub.add_parser("backend", help="Manage LLM backends (list, set, test, switch, status)")
    backend_sub = backend_p.add_subparsers(dest="backend_command")
    backend_sub.add_parser("list", help="Show detected and configured backends")
    backend_guide_p = backend_sub.add_parser("guide", help="Step-by-step setup guide for a backend (no arg = menu)")
    backend_guide_p.add_argument("provider", nargs="?", help="Backend or model name (e.g. moonshot, ollama, kimi-k3, gemma4)")
    backend_set_p = backend_sub.add_parser("set", help="Set default backend")
    backend_set_p.add_argument("provider", help="Provider: ollama, vllm, openai, anthropic, deepseek, moonshot, groq, together, gateway")
    backend_set_p.add_argument("--api-key", help="API key for the provider")
    backend_set_p.add_argument("--base-url", help="Custom base URL")
    backend_set_p.add_argument("--model", help="Default model")
    backend_add_p = backend_sub.add_parser(
        "add", help="Register a custom backend (acp: drive an external ACP agent)"
    )
    backend_add_p.add_argument(
        "kind", choices=["acp"], help="Custom backend kind (currently: acp)"
    )
    backend_add_p.add_argument(
        "--command", required=True,
        help="Command that starts the external ACP agent "
             "(e.g. 'claude' for claude-agent-acp, 'codex' for codex-acp)",
    )
    backend_add_p.add_argument(
        "--arg", dest="args", action="append", default=[],
        help="Argument passed to the agent command (repeatable)",
    )
    backend_add_p.add_argument(
        "--model", help="Model name reported for this backend (default: acp)"
    )
    backend_reason_p = backend_sub.add_parser("set-reasoning", help="Set reasoning-only backend (effort 7+)")
    backend_reason_p.add_argument("provider", help="Provider for reasoning tasks")
    backend_reason_p.add_argument("--api-key", help="API key")
    backend_reason_p.add_argument("--model", help="Reasoning model")
    backend_sub.add_parser("test", help="Test current backend with a simple prompt")
    backend_switch_p = backend_sub.add_parser("switch", help="Switch to a different inference backend")
    backend_switch_p.add_argument("target_backend", choices=["ollama", "llamacpp", "vllm"],
                                  help="Target backend to switch to")
    backend_use_p = backend_sub.add_parser(
        "use", help="Switch the RUNNING agent live to a preset (no restart)")
    backend_use_p.add_argument(
        "preset",
        choices=["local", "bonsai", "genesis", "mcp", "gateway", "managed", "claude", "anthropic", "deepseek", "acp"],
        help="local/bonsai=sovereign Bonsai-27B; genesis/mcp/gateway/managed=mesh/cloud; "
             "claude/deepseek=BYO-key (set key first); acp=external ACP agent (adk backend add acp first)")
    backend_sub.add_parser("status", help="Show current backend configuration and connectivity")

    # adk keys — manage cloud provider API keys
    keys_p = sub.add_parser("keys", help="Manage cloud provider API keys (set, list, test, remove)")
    keys_sub = keys_p.add_subparsers(dest="keys_command")
    keys_set_p = keys_sub.add_parser("set", help="Set a provider API key")
    keys_set_p.add_argument("provider", help="Provider: openai, anthropic, deepseek, moonshot, google, openrouter, groq, together")
    keys_set_p.add_argument("key", nargs="?", help="API key value (omit to be guided + prompted securely)")
    keys_sub.add_parser("list", help="Show configured provider keys and status")
    keys_sub.add_parser("pull", help="Sync DOWN from AitherOS: show which providers have keys in your workspace vault")
    keys_test_p = keys_sub.add_parser("test", help="Test API keys (all or specific)")
    keys_test_p.add_argument("provider", nargs="?", help="Specific provider to test (default: all)")
    keys_rm_p = keys_sub.add_parser("remove", help="Remove a provider key")
    keys_rm_p.add_argument("provider", help="Provider to remove")

    # adk secret — manage secrets in encrypted local keyring + sync with vault
    secret_p = sub.add_parser("secret", help="Manage secrets (list, get, set, pull, push, sync)")
    secret_sub = secret_p.add_subparsers(dest="secret_command")
    secret_sub.add_parser("list", help="List all stored secret keys (values not shown)")
    secret_get_p = secret_sub.add_parser("get", help="Get a secret value")
    secret_get_p.add_argument("name", help="Secret name")
    secret_set_p = secret_sub.add_parser("set", help="Store a secret in encrypted keyring")
    secret_set_p.add_argument("name", help="Secret name")
    secret_set_p.add_argument("value", help="Secret value")
    secret_pull_p = secret_sub.add_parser("pull", help="Pull secrets from platform vault")
    secret_pull_p.add_argument("--secrets-url", help="AitherSecrets URL (default: from env)")
    secret_pull_p.add_argument("--gateway-url", help="Gateway URL (fallback, default: from env)")
    secret_pull_p.add_argument("--api-key", help="API key (default: from env)")
    secret_push_p = secret_sub.add_parser("push", help="Push a secret to the platform vault")
    secret_push_p.add_argument("name", help="Secret name to push")
    secret_push_p.add_argument("--secrets-url", help="AitherSecrets URL (default: from env)")
    secret_push_p.add_argument("--gateway-url", help="Gateway URL (fallback, default: from env)")
    secret_push_p.add_argument("--api-key", help="API key (default: from env)")
    secret_sync_p = secret_sub.add_parser("sync", help="Bidirectional sync (pull + push local-only)")
    secret_sync_p.add_argument("--secrets-url", help="AitherSecrets URL (default: from env)")
    secret_sync_p.add_argument("--gateway-url", help="Gateway URL (fallback, default: from env)")
    secret_sync_p.add_argument("--api-key", help="API key (default: from env)")

    # adk vault — on-demand lockbox over the LIVE AitherSecrets vault. The master
    # key is sealed in the OS keychain (unlocked by your OS login); values copy to
    # the clipboard by default so they never hit scrollback.
    vault_p = sub.add_parser("vault", help="Lockbox for the live secrets vault (setup, ls, get, search, rotate, lock)")
    vault_sub = vault_p.add_subparsers(dest="vault_command")
    vault_gui_p = vault_sub.add_parser("gui", help="Open the vault in a browser (starts the console, opens the panel)")
    vault_gui_p.add_argument("--port", type=int, help="Serve on this port (default: an auto-picked free port, remembered)")
    vault_setup_p = vault_sub.add_parser("setup", help="One-time: seal the vault master key into the OS keychain")
    vault_setup_p.add_argument("--from-env", dest="from_env", action="store_true", help="Import AITHER_INTERNAL_SECRET from .env instead of pasting it")
    vault_setup_p.add_argument("--env-file", dest="env_file", help="Path to the .env holding AITHER_INTERNAL_SECRET")
    vault_setup_p.add_argument("--url", help="Vault base URL (default: from env or 127.0.0.1:8111)")
    vault_setup_p.add_argument("--pin", action="store_true", help="Also set a PIN that guards value reveals")
    vault_sub.add_parser("status", help="Show setup + reachability + secret count")
    vault_ls_p = vault_sub.add_parser("ls", help="List secret names + metadata (never values)")
    vault_ls_p.add_argument("--filter", help="Only names containing this text (searches all scopes)")
    vault_ls_p.add_argument("--scope", choices=["mine", "tenant", "providers", "platform", "all"],
                            default="mine", help="Which tier to show (default: mine — hides platform/system)")
    vault_get_p = vault_sub.add_parser("get", help="Reveal one secret (clipboard by default)")
    vault_get_p.add_argument("name", help="Secret name")
    vault_get_p.add_argument("--show", action="store_true", help="Print the value to the terminal (PIN-gated if set)")
    vault_get_p.add_argument("--copy", action="store_true", help="Copy to clipboard (default when not --show)")
    vault_search_p = vault_sub.add_parser("search", help="Fuzzy-search secret names")
    vault_search_p.add_argument("term", help="Substring to search for")
    vault_scope_p = vault_sub.add_parser("scope", help="Re-file a secret's scope/owner (overlay — non-destructive)")
    vault_scope_p.add_argument("name", help="Secret name")
    vault_scope_p.add_argument("scope", choices=["mine", "workspace", "tenant", "provider", "platform", "host"],
                               help="Scope tier to file it under")
    vault_scope_p.add_argument("--owner", help="Optional owner label")
    vault_rotate_p = vault_sub.add_parser("rotate", help="Mint a fresh strong value for a secret and store it")
    vault_rotate_p.add_argument("name", help="Secret name to rotate")
    vault_rotate_p.add_argument("--length", type=int, default=28, help="New value length (default 28)")
    vault_rotate_p.add_argument("--show", action="store_true", help="Print the new value")
    vault_lock_p = vault_sub.add_parser("lock", help="Drop the PIN session (--forget wipes the sealed key)")
    vault_lock_p.add_argument("--forget", action="store_true", help="Also delete the master key + PIN from the keychain")

    # adk voice serve — standalone HTTP voice server for AitherShell
    voice_p = sub.add_parser("voice", help="Voice services (serve standalone HTTP server)")
    voice_sub = voice_p.add_subparsers(dest="voice_command")
    voice_serve_p = voice_sub.add_parser("serve", help="Start the HTTP voice server (default port 8085)")
    voice_serve_p.add_argument("--port", type=int, default=None, help="Port number (default: AITHER_VOICE_HTTP_PORT or 8085)")
    voice_serve_p.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1 — localhost only)")

    # adk grid — manage grid distributed infrastructure
    grid_p = sub.add_parser("grid", help="Manage grid distributed nodes (add, remove, list, test, sync)")
    grid_sub = grid_p.add_subparsers(dest="grid_command")
    grid_sub.add_parser("status", help="Show grid topology and health of all nodes")
    grid_add_p = grid_sub.add_parser("add", help="Add a node to the grid")
    grid_add_p.add_argument("role", choices=["reasoning", "cluster"], help="Node role")
    grid_add_p.add_argument("host", help="Hostname or IP address")
    grid_add_p.add_argument("--port", type=int, default=8121, help="llama.cpp port (default: 8121)")
    grid_add_p.add_argument("--model", help="Model name override")
    grid_rm_p = grid_sub.add_parser("remove", help="Remove a node from the grid")
    grid_rm_p.add_argument("host", help="Hostname or IP to remove")
    grid_test_p = grid_sub.add_parser("test", help="Test connectivity to all or specific nodes")
    grid_test_p.add_argument("host", nargs="?", help="Specific host to test (default: all)")
    grid_sub.add_parser("sync", help="Sync grid config to your Aitherium workspace (requires login)")
    grid_sub.add_parser("pull", help="Pull grid config from your Aitherium workspace")
    # Mesh registry (the platform's enrolled nodes, via the AitherGateway tunnel)
    grid_enroll_p = grid_sub.add_parser("enroll", help="Mint a single-use token to onboard a remote machine as a mesh node")
    grid_enroll_p.add_argument("--ttl", type=float, default=1.0, help="Token lifetime in hours (default 1, max 24)")
    grid_enroll_p.add_argument("--tenant", default="", help="Attribute the node to a tenant slug")
    grid_enroll_p.add_argument("--label", default="", help="Human label for the node")
    grid_sub.add_parser("ls", help="List enrolled mesh nodes (GPU, memory, containers, status)")
    grid_rm_mesh_p = grid_sub.add_parser("deregister", help="Remove an enrolled mesh node from the registry")
    grid_rm_mesh_p.add_argument("node_id", help="Node id or name to deregister")

    # adk approvals — decide the permission cards blocking federated agents.
    # Same cards as the portal tray and the Awconnect popup; approving here
    # clears them there.
    appr_p = sub.add_parser(
        "approvals",
        help="List/approve/deny A2A permission cards blocking federated agents",
    )
    appr_p.add_argument(
        "--url",
        help="A2A gateway base URL (default $AITHER_A2A_URL or https://127.0.0.1:8766)",
    )
    appr_p.add_argument("--json", action="store_true", help="Emit raw JSON")
    appr_sub = appr_p.add_subparsers(dest="approvals_command")
    appr_ls = appr_sub.add_parser("list", help="Show pending permission cards")
    appr_ls.add_argument("--tenant", help="Only cards raised in this tenant")
    for _name, _help in (
        ("approve", "Approve a card and mint its one-time grant token"),
        ("deny", "Refuse a card; no token is minted"),
    ):
        _p = appr_sub.add_parser(_name, help=_help)
        _p.add_argument("request_id", help="Card id (areq_…)")
        _p.add_argument("--approver", help="Who is deciding (audited)")
        _p.add_argument("--reason", help="Recorded with the decision")
        if _name == "approve":
            _p.add_argument(
                "--ttl", type=int, default=60,
                help="Grant lifetime in minutes (default 60)",
            )
            _p.add_argument(
                "--tenant", default="platform",
                help="Approving tenant authority (default platform)",
            )

    # adk join — one-command community node onboarding (GitHub → hardware →
    # serve → mesh → earn)
    join_p = sub.add_parser(
        "join",
        help="One-command community node onboarding (GitHub auth + hardware "
             "detection + serve + mesh join + earnings)"
    )
    join_p.add_argument(
        "--no-github", action="store_true",
        help="Skip GitHub auth (use existing token or env)"
    )
    join_p.add_argument(
        "--cloud-provider",
        choices=["aws", "gcp", "azure", "vast"],
        help="Cloud provider for remote deployment (deferred to P2)"
    )
    join_p.add_argument(
        "--model",
        help="Override the resolved inference model"
    )
    join_p.add_argument(
        "--no-browser", action="store_true",
        help="Do not attempt browser open for GitHub auth"
    )
    join_p.add_argument(
        "--dry-run", action="store_true",
        help="Walk the full plan without side effects"
    )

    # adk mesh — AitherMesh overlay and A2A operations
    mesh_p = sub.add_parser("mesh", help="AitherMesh overlay operations (onboard, list peers)")
    mesh_sub = mesh_p.add_subparsers(dest="mesh_command")
    mesh_onboard_p = mesh_sub.add_parser("onboard", help="Onboard this node into AitherMesh overlay (WireGuard)")
    mesh_onboard_p.add_argument(
        "--conductor",
        default=os.getenv("AITHER_CONDUCTOR_URL", "https://gateway.aitherium.com"),
        help="Conductor URL (default: $AITHER_CONDUCTOR_URL or internal address)")
    mesh_onboard_p.add_argument(
        "--node-id",
        default=os.getenv("AITHER_NODE_ID", ""),
        help="Node ID (default: auto-generated)")
    mesh_onboard_p.add_argument(
        "--role",
        default=os.getenv("AITHER_MESH_ROLE", "worker"),
        help="Node role (default: worker)")
    mesh_onboard_p.add_argument(
        "--external-ip",
        default=os.getenv("AITHER_EXTERNAL_IP", ""),
        help="External IP address for WireGuard endpoint")
    mesh_onboard_p.add_argument(
        "--headscale",
        action="store_true",
        default=os.getenv("AITHER_MESH_TRANSPORT", "").lower() == "headscale",
        help="Use Headscale transport (NAT-friendly)")
    mesh_ls_p = mesh_sub.add_parser("ls", help="List peer agents in the mesh and their A2A services")
    mesh_ls_p.add_argument(
        "--mesh-url",
        default=os.getenv("AITHER_MESH_URL", "https://gateway.aitherium.com"),
        help="AitherMesh directory URL (default: $AITHER_MESH_URL or internal)")
    mesh_ls_p.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)")

    # adk mesh provide — one-command community inference provider setup
    mesh_provide_p = mesh_sub.add_parser(
        "provide",
        help="Become a community inference provider (advertise → consent → await operator trust)"
    )
    mesh_provide_p.add_argument(
        "--inference-url",
        required=True,
        help="OpenAI-compatible inference server URL (e.g., http://10.77.x.x:8000/v1)"
    )
    mesh_provide_p.add_argument(
        "--model",
        required=True,
        help="Model name advertised (e.g., gemma4-12b)"
    )
    mesh_provide_p.add_argument(
        "--peer-id",
        default=os.getenv("AITHER_PEER_ID", ""),
        help="Peer ID (auto-resolved if not given; must have run 'adk mesh onboard' first)"
    )
    mesh_provide_p.add_argument(
        "--tenant-id",
        default=os.getenv("AITHER_TENANT_ID", ""),
        help="Owner tenant ID (authenticated identity; required for fail-closed gate)"
    )
    mesh_provide_p.add_argument(
        "--wait",
        type=int,
        default=30,
        help="Timeout for polling operator trust grant (default: 30s; 0=no wait)"
    )
    mesh_provide_p.add_argument(
        "--strata-url",
        default=os.getenv("AITHER_STRATA_URL", "https://gateway.aitherium.com"),
        help="Strata endpoint override"
    )
    mesh_provide_p.add_argument(
        "--conductor-url",
        default=os.getenv("AITHER_CONDUCTOR_URL", "https://gateway.aitherium.com"),
        help="Conductor endpoint override"
    )
    mesh_provide_p.add_argument(
        "--auth-token",
        default=os.getenv("AITHER_AUTH_TOKEN", ""),
        help="Bearer token for API calls (auto-resolved from ~/.aither/config.json if not given)"
    )

    # adk mesh serve — serve Kimi-K3 from this mesh (intra-mesh llama.cpp RPC split)
    mesh_serve_p = mesh_sub.add_parser(
        "serve",
        help="Serve Kimi-K3 from this mesh (plan / rpc-backend / coordinator roles)"
    )
    mesh_serve_p.add_argument(
        "model_name",
        choices=["kimi-k3"],
        help="Model to serve (kimi-k3 only for now)"
    )
    mesh_serve_p.add_argument(
        "--role",
        choices=["plan", "rpc-backend", "coordinator"],
        default="plan",
        help="This node's role: plan (default, print only), rpc-backend, coordinator"
    )
    mesh_serve_p.add_argument(
        "--nodes",
        default="",
        help="Participating nodes as id:host:ram_gb:vram_gb,... (plan/coordinator roles)"
    )
    mesh_serve_p.add_argument(
        "--quant",
        default="auto",
        help="Quant name (UD-IQ1_S..UD-Q8_K_XL) or 'auto' (largest that fits the pool)"
    )
    mesh_serve_p.add_argument(
        "--bind",
        default="",
        help="Overlay/private IP to bind (rpc-backend/coordinator; public IPs refused)"
    )
    mesh_serve_p.add_argument(
        "--backends",
        default="",
        help="Comma-separated ip:port of RUNNING rpc-servers (coordinator role)"
    )
    mesh_serve_p.add_argument(
        "--model-dir",
        default=os.getenv("AITHER_KIMI_MODEL_DIR", "kimi-k3-model"),
        help="Directory for the GGUF shards + mmproj (594GB+ for UD-IQ1_S)"
    )
    mesh_serve_p.add_argument(
        "--build-dir",
        default=os.getenv("AITHER_UNSLOTH_BUILD_DIR", "unsloth-llamacpp"),
        help="Unsloth llama.cpp fork checkout/build directory"
    )
    mesh_serve_p.add_argument(
        "--tenant-id",
        default=os.getenv("AITHER_TENANT_ID", ""),
        help="Owner tenant ID (for the community-market advertise step)"
    )
    mesh_serve_p.add_argument(
        "--no-advertise",
        action="store_true",
        help="Serve without advertising into the community market"
    )
    mesh_serve_p.add_argument(
        "--execute",
        action="store_true",
        help="Actually run (default is a dry-run that prints the plan/steps)"
    )

    # adk mesh leave — self-service pool exit (drain this node's community backend)
    mesh_leave_p = mesh_sub.add_parser(
        "leave",
        help="Leave the community inference pool (drain your node's backend from routing)"
    )
    mesh_leave_p.add_argument(
        "--peer-id",
        default=os.getenv("AITHER_PEER_ID", ""),
        help="Peer ID (auto-resolved if not given)"
    )
    mesh_leave_p.add_argument(
        "--conductor-url",
        default=os.getenv("AITHER_CONDUCTOR_URL", "https://gateway.aitherium.com"),
        help="Conductor endpoint override"
    )
    mesh_leave_p.add_argument(
        "--auth-token",
        default=os.getenv("AITHER_AUTH_TOKEN", ""),
        help="Bearer token for API calls (auto-resolved from ~/.aither/config.json if not given)"
    )

    # adk mesh federation-token — mint the relay AITHER_NODE_TOKEN (self-service)
    mesh_fedtok_p = mesh_sub.add_parser(
        "federation-token",
        help="Mint your node's relay-federation token (AITHER_NODE_TOKEN for the community hub)"
    )
    mesh_fedtok_p.add_argument(
        "--peer-id",
        default=os.getenv("AITHER_PEER_ID", ""),
        help="Peer ID (auto-resolved if not given); also becomes AITHERNET_NODE_SLUG"
    )
    mesh_fedtok_p.add_argument(
        "--conductor-url",
        default=os.getenv("AITHER_CONDUCTOR_URL", "https://gateway.aitherium.com"),
        help="Conductor endpoint override"
    )
    mesh_fedtok_p.add_argument(
        "--auth-token",
        default=os.getenv("AITHER_AUTH_TOKEN", ""),
        help="Bearer token for API calls (auto-resolved from ~/.aither/config.json if not given)"
    )

    # adk mesh flux-node — start a Flux event-plane listener on this node
    mesh_flux_p = mesh_sub.add_parser(
        "flux-node",
        help="Start a Flux event-plane listener on this node (participates in AitherMesh)"
    )
    mesh_flux_p.add_argument(
        "--flux-image",
        default=os.getenv("FLUX_IMAGE", "ghcr.io/aitherium/mesh-agent:latest"),
        help="Docker image to run (default: ghcr.io/aitherium/mesh-agent:latest)"
    )
    mesh_flux_p.add_argument(
        "--flux-port",
        type=int,
        default=int(os.getenv("FLUX_PORT", "8117")),
        help="Port to bind the listener (default: 8117)"
    )
    mesh_flux_p.add_argument(
        "--mesh-src",
        default=os.getenv("MESH_SRC", "/opt/aitheros/mesh-src"),
        help="Host path to mount as /app (default: /opt/aitheros/mesh-src)"
    )
    mesh_flux_p.add_argument(
        "--node-id",
        default=os.getenv("AITHER_NODE_ID", ""),
        help="Mesh node identifier (required; e.g., spark-dgx)"
    )
    mesh_flux_p.add_argument(
        "--aither-internal-secret",
        default=os.getenv("AITHER_INTERNAL_SECRET", ""),
        help="Service-internal secret from vault (required; never echoed in output)"
    )

    # adk mesh create — one-command self-service: mint your OWN isolated mesh
    mesh_create_p = mesh_sub.add_parser(
        "create",
        help="Create your OWN isolated mesh (per-tenant overlay CIDR + Headscale key + registry)"
    )
    mesh_create_p.add_argument("--name", required=True, help="Human name for the mesh")
    mesh_create_p.add_argument(
        "--tenant-id",
        default=os.getenv("AITHER_TENANT_ID", ""),
        help="Owner tenant ID (authenticated identity; fail-closed — must match your token)"
    )
    mesh_create_p.add_argument(
        "--discoverable", action="store_true",
        help="Opt this mesh into the AitherNet public directory (others can discover + link)"
    )
    mesh_create_p.add_argument(
        "--federation-role", default="standalone",
        choices=["standalone", "hub", "spoke"],
        help="Federation role for mesh-to-mesh linking (default: standalone)"
    )
    mesh_create_p.add_argument(
        "--conductor-url",
        default=os.getenv("AITHER_CONDUCTOR_URL", "https://gateway.aitherium.com"),
        help="Conductor endpoint (default: $AITHER_CONDUCTOR_URL)"
    )
    mesh_create_p.add_argument(
        "--auth-token",
        default=os.getenv("AITHER_AUTH_TOKEN", ""),
        help="Bearer token (auto-resolved from ~/.aither/config.json if not given)"
    )

    # adk mesh link — federate two meshes into ONE inference pool (owner-consented both sides)
    mesh_link_p = mesh_sub.add_parser(
        "link",
        help="Link your mesh with another (both owners consent -> shared inference pool)"
    )
    mesh_link_sub = mesh_link_p.add_subparsers(dest="link_action")
    for _act, _help in (
        ("request", "Offer your mesh into a link with a discovered target mesh"),
        ("approve", "Approve an incoming link request (consent to federate)"),
        ("revoke", "Revoke a link (either side; drops it from the pool)"),
        ("list", "List links your tenant is a party to"),
    ):
        _p = mesh_link_sub.add_parser(_act, help=_help)
        _p.add_argument("--tenant-id", default=os.getenv("AITHER_TENANT_ID", ""),
                        help="Your authenticated tenant ID (fail-closed; must match your token)")
        _p.add_argument("--conductor-url",
                        default=os.getenv("AITHER_CONDUCTOR_URL", "https://gateway.aitherium.com"),
                        help="Conductor endpoint (default: $AITHER_CONDUCTOR_URL)")
        _p.add_argument("--auth-token", default=os.getenv("AITHER_AUTH_TOKEN", ""),
                        help="Bearer token (auto-resolved from ~/.aither/config.json if not given)")
        if _act == "request":
            _p.add_argument("--source-mesh", required=True, help="YOUR mesh_id to offer into the link")
            _p.add_argument("--target-mesh", required=True,
                            help="Target mesh_id (from 'adk mesh' discovery / AitherNet directory)")
        if _act in ("approve", "revoke"):
            _p.add_argument("--link-id", required=True, help="The link_id to act on")

    # adk routing — per-intent model routing
    routing_p = sub.add_parser("routing", help="Manage per-intent model routing (which model handles which task)")
    routing_sub = routing_p.add_subparsers(dest="routing_command")
    routing_preset_p = routing_sub.add_parser("preset", help="Apply a routing preset (budget, balanced, quality)")
    routing_preset_p.add_argument("preset_name", help="Preset: budget, balanced, quality")
    routing_set_p = routing_sub.add_parser("set", help="Set model for an intent type")
    routing_set_p.add_argument("intent", help="Intent: code, reasoning, chat, research, review, planning, search")
    routing_set_p.add_argument("provider", help="Provider: openai, anthropic, deepseek, local")
    routing_set_p.add_argument("--model", help="Specific model name")
    routing_sub.add_parser("reset", help="Reset to effort-based routing (disable intent overrides)")

    # adk costs — token economy visibility
    costs_p = sub.add_parser("costs", help="Show cloud inference costs, savings, and budget")
    costs_sub = costs_p.add_subparsers(dest="costs_command")
    costs_sub.add_parser("summary", help="Show cost summary (default)")
    costs_compare_p = costs_sub.add_parser("compare", help="Compare AitherOS vs raw API costs")
    costs_compare_p.add_argument("--period", default="week", choices=["day", "week", "month"])
    costs_budget_p = costs_sub.add_parser("budget", help="Set monthly spending budget")
    costs_budget_p.add_argument("amount", type=float, help="Monthly budget in USD (0=unlimited)")
    costs_p.add_argument("--period", default="day", choices=["day", "week", "month"], help="Time period")

    # adk tools — list and sync available tools
    tools_p = sub.add_parser("tools", help="Manage available tools (list, sync from platform)")
    tools_sub = tools_p.add_subparsers(dest="tools_command", help="Tools subcommands")

    # adk tools list (default) — list available tools
    tools_list_p = tools_sub.add_parser("list", help="List available tools (local + MCP)")
    tools_list_p.add_argument("--upgrade", action="store_true", help="Show what pro/enterprise unlocks")

    # adk tools sync — sync entitled tools from platform
    tools_sync_p = tools_sub.add_parser("sync", help="Sync entitled tools from platform")
    tools_sync_p.add_argument("--verbose", action="store_true", help="Verbose output with version info")

    # adk quickstart — unified first-run wizard
    quickstart_p = sub.add_parser("quickstart", help="One-command setup: GPU + auth + shell")
    quickstart_p.add_argument("--api-key", help="AITHER_API_KEY")
    quickstart_p.add_argument("--cloud", action="store_true", help="Cloud-only setup (no GPU required)")

    # adk quickstart-local — local-only inference quickstart
    quickstart_local_p = sub.add_parser(
        "quickstart-local",
        help="Local inference quickstart (no cloud required)"
    )
    quickstart_local_p.add_argument(
        "--backend",
        choices=["auto", "llamacpp", "ollama", "vllm"],
        default="auto",
        help="Inference backend (auto = detect best fit)"
    )
    quickstart_local_p.add_argument(
        "--model",
        help="Model name/ID (for Ollama; others auto-detected)"
    )
    quickstart_local_p.add_argument(
        "--port",
        type=int,
        default=8209,
        help="Port for local inference endpoint (default: 8209)"
    )
    quickstart_local_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes"
    )
    quickstart_local_p.add_argument("--api-key", help="AITHER_API_KEY")

    # adk backup — export all ~/.aither/ data
    backup_p = sub.add_parser("backup", help="Backup all agent data (memory, graphs, config)")
    backup_p.add_argument("-o", "--output", help="Output file path (default: aither-backup-<timestamp>.tar.gz)")

    # adk ingest — manually ingest files into knowledge graph
    ingest_p = sub.add_parser("ingest", help="Ingest files into the agent's knowledge graph")
    ingest_p.add_argument("path", nargs="?", default=".", help="File or directory to ingest")
    ingest_p.add_argument("--agent", default="default", help="Agent name for the graph")
    ingest_p.add_argument("--brain", action="store_true",
                         help="Enable sync to CompanyBrain hub (default: local only)")
    ingest_p.add_argument("--brain-url", default="",
                         help="Override brain hub URL (default: from env/config)")
    ingest_p.add_argument("--classification", default="internal",
                         choices=["public", "internal", "confidential", "restricted"],
                         help="Classification level for ingested content (default: internal)")
    ingest_p.add_argument("--chunk-size", type=int, default=2000,
                         help="Bytes per chunk (default: 2000)")
    ingest_p.add_argument("--chunk-overlap", type=int, default=200,
                         help="Overlap bytes between chunks (default: 200)")
    ingest_p.add_argument("--workspace", default="default",
                         help="Workspace ID for brain sync (default: default)")
    ingest_p.add_argument("--skip-embeddings", action="store_true",
                         help="Skip embedding if brain unreachable")
    ingest_p.add_argument("--dry-run", action="store_true",
                         help="Print what would be ingested without persisting")

    # adk doctor — system health checks
    sub.add_parser("doctor", help="Check system health (Python, GPU, LLM backends, API keys)")

    # adk gobbonet — run the GobboNet UI with keyless search, in one command
    gobbo_p = sub.add_parser(
        "gobbonet",
        help="Run GobboNet with keyless web search (clones the UI if needed)",
    )
    gobbo_p.add_argument("--ui", help="existing GobboNet checkout (default: find or clone)")
    gobbo_p.add_argument("--port", type=int, default=11434)
    gobbo_p.add_argument("--host", default="127.0.0.1")
    gobbo_p.add_argument("--no-open", action="store_true", help="do not open a browser")
    gobbo_p.add_argument("--setup-model", action="store_true",
                         help="install llama.cpp + a model sized to this machine")
    gobbo_p.add_argument("--backend",
                         help="pin an OpenAI-compatible server (e.g. http://127.0.0.1:8000)")
    gobbo_p.add_argument("--plain", action="store_true",
                         help="passthrough chat instead of the adk agent loop")

    # adk gateway — multi-channel agent gateway
    gateway_p = sub.add_parser("gateway", help="Run agent across messaging platforms")
    gateway_p.add_argument("-a", "--agent", default="assistant", help="Agent identity (default: assistant)")
    gateway_p.add_argument("--telegram", action="store_true", help="Enable Telegram (TELEGRAM_BOT_TOKEN)")
    gateway_p.add_argument("--discord", action="store_true", help="Enable Discord (DISCORD_BOT_TOKEN)")
    gateway_p.add_argument("--slack", action="store_true", help="Enable Slack (SLACK_BOT_TOKEN + SLACK_APP_TOKEN)")
    gateway_p.add_argument("--webhook", action="store_true", help="Enable webhook endpoint")
    gateway_p.add_argument("--webhook-port", type=int, default=9000, help="Webhook port (default: 9000)")

    # adk cron — cron scheduler
    cron_p = sub.add_parser("cron", help="Manage scheduled tasks")
    cron_sub = cron_p.add_subparsers(dest="cron_command")
    cron_sub.add_parser("list", help="List scheduled jobs")
    cron_add_p = cron_sub.add_parser("add", help="Add a cron job")
    cron_add_p.add_argument("expression", help="Cron expression (e.g. '0 9 * * *')")
    cron_add_p.add_argument("task_name", help="Task name / description")
    cron_rm_p = cron_sub.add_parser("remove", help="Remove a cron job")
    cron_rm_p.add_argument("name", help="Job name to remove")

    # adk skills — skill management
    skills_p = sub.add_parser("skills", help="Manage learned skills")
    skills_sub = skills_p.add_subparsers(dest="skills_command")
    skills_sub.add_parser("list", help="List all learned skills")
    skills_search_p = skills_sub.add_parser("search", help="Search skills")
    skills_search_p.add_argument("query", help="Search query")
    skills_sub.add_parser("export", help="Export skills in agentskills.io format")

    # adk addon — self-hosted addon management
    # `component`/`components` are ALIASES, not a second implementation: an addon
    # manifest IS the component declaration every host reads (2026-09-06).
    addon_p = sub.add_parser(
        "addon", aliases=["component", "components"],
        help="Manage self-hosted addons / components (Qdrant, RAG, awgym, awdesk, ...)",
    )
    addon_sub = addon_p.add_subparsers(dest="addon_command")
    addon_sub.add_parser("list", help="Show available addons + status")
    addon_enable_p = addon_sub.add_parser("enable", help="Pull image, start container, register with portal")
    addon_enable_p.add_argument("addon_id", help="Addon ID to enable (e.g. qdrant, knowledge-rag)")
    addon_enable_p.add_argument("--endpoint", help="Endpoint URL (for external type addons)")
    addon_disable_p = addon_sub.add_parser("disable", help="Stop container, deregister")
    addon_disable_p.add_argument("addon_id", help="Addon ID to disable")
    addon_status_p = addon_sub.add_parser("status", help="Health + metrics for addons")
    addon_status_p.add_argument("addon_id", nargs="?", help="Specific addon (default: all)")
    addon_logs_p = addon_sub.add_parser("logs", help="Tail container logs")
    addon_logs_p.add_argument("addon_id", help="Addon ID")
    addon_logs_p.add_argument("--lines", type=int, default=100, help="Number of log lines (default: 100)")
    addon_sub.add_parser("update", help="Pull latest images for all enabled addons")

    # adk install — install agent packs and extensions
    install_p = sub.add_parser("install", help="Install an agent pack (e.g. adk install pack:openclaw)")
    install_p.add_argument(
        "target", nargs="?", default=None,
        help="'list', 'pack:<name>', or a pack name (openclaw, hermes, claude-code)",
    )

    # adk packs — alias for list
    sub.add_parser("packs", help="List available agent packs")

    # adk contribute — teach the ARC world model (bundled arc-brainpack pack)
    contribute_p = sub.add_parser(
        "contribute",
        help="Teach Aither's ARC world model — enroll, then play & stream transitions (free)",
    )
    contribute_sub = contribute_p.add_subparsers(dest="contribute_command")
    contribute_sub.add_parser(
        "register", help="Mint a free wallet + contributor token (idempotent)")
    c_play = contribute_sub.add_parser(
        "play", help="Play ARC games and stream every transition (needs: pip install 'awdk[arc]')")
    c_play.add_argument("games", nargs="*", default=[],
                        help="ARC game ids (default: a starter set)")
    c_play.add_argument("-n", "--steps", type=int, default=200,
                        help="Max transitions per game (default 200)")
    contribute_sub.add_parser("status", help="Your accepted count + daily quota")
    c_lb = contribute_sub.add_parser("leaderboard", help="Who has taught it the most")
    c_lb.add_argument("--limit", type=int, default=20, help="Rows to show (default 20)")
    contribute_sub.add_parser("solo", help="Print the one-command self-host (train YOUR own model)")

    # adk pack — tool pack management
    pack_p = sub.add_parser("pack", help="Manage ToolPack extensions (list, search, install, remove, info)")
    pack_sub = pack_p.add_subparsers(dest="pack_command")
    pack_sub.add_parser("list", help="List available and installed packs")
    pack_search_p = pack_sub.add_parser("search", help="Search packs by name, description, or tags")
    pack_search_p.add_argument("query", help="Search query")
    pack_search_p.add_argument("--json", dest="json_output", action="store_true", help="JSON output")
    pack_install_p = pack_sub.add_parser("install", help="Install a tool pack")
    pack_install_p.add_argument("pack_id", help="Pack ID to install")
    pack_sync_p = pack_sub.add_parser(
        "sync", help="Install every entitled pack not already present (license-driven)")
    pack_sync_p.add_argument("--dry-run", "-n", action="store_true",
                             help="Preview what would be installed without installing")
    pack_buy_p = pack_sub.add_parser(
        "buy", help="Autonomously buy a pack with Aitherium credits (no Stripe)")
    pack_buy_p.add_argument("pack_id", help="Listing id to buy")
    pack_buy_p.add_argument("--token", default="", help="Negotiation token (agreed price)")
    pack_buy_p.add_argument("--install", action="store_true", help="Run pack sync after buying")
    pack_neg_p = pack_sub.add_parser(
        "negotiate", help="Haggle with the seller Broker for a better price")
    pack_neg_p.add_argument("pack_id", help="Listing id to negotiate")
    pack_neg_p.add_argument("--offer", type=int, required=True, help="Your offer in Aitherium credits")
    pack_neg_p.add_argument("--why", default="", help="Optional rationale for the Broker")
    pack_remove_p = pack_sub.add_parser("remove", help="Remove an installed pack")
    pack_remove_p.add_argument("pack_id", help="Pack ID to remove")
    pack_update_p = pack_sub.add_parser("update", help="Update one or all installed packs")
    pack_update_p.add_argument("pack_id", nargs="?", help="Pack ID to update (omit for all)")
    pack_export_p = pack_sub.add_parser("export", help="Export offline bundle (.tar.gz)")
    pack_export_p.add_argument("pack_ids", help="Comma-separated pack IDs")
    pack_export_p.add_argument("-o", "--output", default=".", help="Output directory")
    pack_info_p = pack_sub.add_parser("info", help="Show pack details")
    pack_info_p.add_argument("pack_id", help="Pack ID to inspect")
    pack_customize_p = pack_sub.add_parser("customize", help="Customize installed pack (system_prompt, capabilities, domains)")
    pack_customize_p.add_argument("pack_id", help="Pack name to customize")
    pack_customize_p.add_argument("--system-prompt", help="Override system prompt")
    pack_customize_p.add_argument("--system-prompt-file", help="Load system prompt from file")
    pack_customize_p.add_argument("--capabilities", help="Override capabilities (comma-separated)")
    pack_customize_p.add_argument("--show", action="store_true", help="Show current spec")

    # adk pack import — import external agents (e.g., Eve agents)
    pack_import_p = pack_sub.add_parser("import", help="Import an external agent (e.g., Eve) to AitherADK pack")
    pack_import_p.add_argument("agent_path", help="Path to agent directory (must have .compiled-manifest.json)")

    # adk fleet — create & manage agents across runtimes (local | managed | hosted | cloud-run)
    fleet_p = sub.add_parser("fleet", help="Create & manage a fleet of agents (local | managed | hosted | cloud-run)")
    fleet_sub = fleet_p.add_subparsers(dest="fleet_command")
    fleet_create_p = fleet_sub.add_parser("create", help="Create an agent in a runtime")
    fleet_create_p.add_argument("name", help="Agent name")
    fleet_create_p.add_argument("--runtime", "-r", default="local",
                                choices=["local", "managed", "hosted", "cloud-run"],
                                help="Where the agent runs (default: local)")
    fleet_create_p.add_argument("--pack", default="", help="Pack/identity to run (e.g. an eve-import id)")
    fleet_create_p.add_argument("--port", type=int, default=8080, help="Local runtime port")
    fleet_create_p.add_argument("--mcp-url", dest="mcp_url", default="", help="Gateway MCP url (managed)")
    fleet_create_p.add_argument("--model", default="", help="Model override (managed)")
    fleet_create_p.add_argument("--preset", default="",
                                help="hosted placement preset: all-local | local-loop-rented-brain | "
                                     "hosted | all-cloud (hosted runtime only)")
    for _plane in ("brain", "loop", "hands"):
        fleet_create_p.add_argument(f"--{_plane}", default="", choices=["", "local", "rented", "cloud"],
                                    help=f"{_plane} placement override (hosted runtime only)")
    fleet_create_p.add_argument("--image", default="", help="OCI image for a hosted instance")
    fleet_create_p.add_argument("--json", dest="json_output", action="store_true", help="JSON output")
    fleet_list_p = fleet_sub.add_parser("list", help="List fleet members")
    fleet_list_p.add_argument("--json", dest="json_output", action="store_true", help="JSON output")
    fleet_status_p = fleet_sub.add_parser("status", help="Refresh & show one member's status")
    fleet_status_p.add_argument("member_id", help="Fleet member id")
    fleet_status_p.add_argument("--json", dest="json_output", action="store_true", help="JSON output")
    fleet_rm_p = fleet_sub.add_parser("rm", help="Remove a member (teardown + drop record)")
    fleet_rm_p.add_argument("member_id", help="Fleet member id")
    fleet_connect_p = fleet_sub.add_parser("connect-local", help="Register this machine's local agent MCP endpoint with the gateway (bidirectional)")
    fleet_connect_p.add_argument("agent_name", help="Name/identifier for the local agent")
    fleet_connect_p.add_argument("mcp_url", help="Public URL where the local agent's MCP is reachable")
    fleet_connect_p.add_argument("--json", dest="json_output", action="store_true", help="JSON output")
    fleet_applypack_p = fleet_sub.add_parser(
        "apply-pack", help="Push+enable a bundled pack on a mesh agent (no SSH; 'self' = this node)")
    fleet_applypack_p.add_argument("agent", help="Agent name (or 'self' for this node)")
    fleet_applypack_p.add_argument("pack", help="Bundled pack name")

    # adk instance — an Aitherium Instance: a long-lived, singleton, addressable agent
    # runtime on the platform fleet (the "Cloud Run instance" primitive). Thin over
    # Genesis /v1/instances; every verb prints what the gateway actually said.
    inst_p = sub.add_parser(
        "instance", help="Create & manage Aitherium Instances (always-on agent with a stable URL)")
    inst_sub = inst_p.add_subparsers(dest="instance_command")
    inst_create = inst_sub.add_parser("create", help="Create an instance (admin of your tenant)")
    inst_create.add_argument("name", help="Instance name (a-z, 0-9, hyphens; becomes the hostname)")
    inst_create.add_argument("--preset", default="hosted",
                             help="all-local | local-loop-rented-brain | hosted | all-cloud")
    for _plane in ("brain", "loop", "hands"):
        inst_create.add_argument(f"--{_plane}", default="", choices=["", "local", "rented", "cloud"],
                                 help=f"{_plane} placement override")
    inst_create.add_argument("--image", default="", help="OCI image (default: the aitherd image)")
    inst_create.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                             help="Environment for the loop (repeatable)")
    inst_create.add_argument("--json", dest="json_output", action="store_true")
    inst_list = inst_sub.add_parser("list", help="List your tenant's instances")
    inst_list.add_argument("--json", dest="json_output", action="store_true")
    for verb, help_text in (("status", "Show one instance"),
                            ("connect", "Print how to reach an instance"),
                            ("stop", "Stop the loop (hostname + record kept; meter stops)"),
                            ("start", "Re-create the loop of a stopped instance"),
                            ("rm", "Destroy: loop, hostname, meter")):
        vp = inst_sub.add_parser(verb, help=help_text)
        vp.add_argument("instance_id", help="Instance id (inst-...)")
        vp.add_argument("--json", dest="json_output", action="store_true")

    # adk support — help and community links
    sub.add_parser("support", help="Get help — Discord, GitHub, docs")

    # adk explore — browse marketplace catalog
    explore_p = sub.add_parser("explore", help="Browse packs, agents, and skills in the Aitherium marketplace")
    explore_p.add_argument("category", nargs="?", default="all",
                           help="Filter: agents, tools, skills, grid, all (default: all)")
    explore_p.add_argument("--free", action="store_true", help="Show only free packs")

    # adk upgrade — open checkout page
    upgrade_p = sub.add_parser("upgrade", help="Open upgrade/checkout page for a pack or plan")
    upgrade_p.add_argument("target", nargs="?", default="",
                           help="Pack ID or plan: managed, setup, grid, demiurge, pro")

    # adk soul — SOUL.md import/export
    soul_p = sub.add_parser("soul", help="Import/export SOUL.md identity files")
    soul_sub = soul_p.add_subparsers(dest="soul_command")
    soul_import_p = soul_sub.add_parser("import", help="Import a SOUL.md file")
    soul_import_p.add_argument("path", help="Path to SOUL.md file")
    soul_export_p = soul_sub.add_parser("export", help="Export identity as SOUL.md")
    soul_export_p.add_argument("name", help="Identity name to export")

    # adk doc — encrypted document storage
    doc_p = sub.add_parser("doc", help="Manage encrypted documents (upload, list, download, delete)")
    doc_sub = doc_p.add_subparsers(dest="doc_command")
    doc_upload_p = doc_sub.add_parser("upload", help="Upload and encrypt a document")
    doc_upload_p.add_argument("path", help="Path to document file")
    doc_upload_p.add_argument("--type", help="Document MIME type (optional, e.g. application/pdf)")
    doc_upload_p.add_argument("--gateway", help="Gateway/Genesis URL (default: http://localhost:8001)")
    doc_upload_p.add_argument("--api-key", help="API key (or set AITHER_API_KEY env var)")
    doc_list_p = doc_sub.add_parser("list", help="List your encrypted documents")
    doc_list_p.add_argument("--gateway", help="Gateway/Genesis URL (default: http://localhost:8001)")
    doc_list_p.add_argument("--api-key", help="API key (or set AITHER_API_KEY env var)")
    doc_get_p = doc_sub.add_parser("get", help="Download and decrypt a document")
    doc_get_p.add_argument("doc_id", help="Document UUID")
    doc_get_p.add_argument("-o", "--output", help="Output file path (required)")
    doc_get_p.add_argument("--gateway", help="Gateway/Genesis URL (default: http://localhost:8001)")
    doc_get_p.add_argument("--api-key", help="API key (or set AITHER_API_KEY env var)")
    doc_delete_p = doc_sub.add_parser("delete", help="Delete a document")
    doc_delete_p.add_argument("doc_id", help="Document UUID")
    doc_delete_p.add_argument("--gateway", help="Gateway/Genesis URL (default: http://localhost:8001)")
    doc_delete_p.add_argument("--api-key", help="API key (or set AITHER_API_KEY env var)")

    # adk mcp — MCP server (stdio for Claude Code, or config helper)
    mcp_p = sub.add_parser("mcp", help="MCP server, IDE setup, and cloud gateway connection")
    mcp_sub = mcp_p.add_subparsers(dest="mcp_command")
    mcp_serve_p = mcp_sub.add_parser("serve", help="Start stdio MCP server (for Claude Code)")
    mcp_serve_p.add_argument("-d", "--directory", default=".", help="Agent project directory")
    mcp_serve_p.add_argument("-p", "--port", type=int,
                             help="Print HTTP config for a running server instead of stdio")
    mcp_config_p = mcp_sub.add_parser("config", help="Print MCP client configuration")
    mcp_config_p.add_argument("-p", "--port", type=int, default=8080,
                              help="ADK server port (default: 8080)")
    mcp_config_p.add_argument("-m", "--mode", choices=["stdio", "http"], default="stdio",
                              help="Transport mode (default: stdio)")
    # adk mcp setup — generate IDE config for cloud or local MCP gateway
    mcp_setup_p = mcp_sub.add_parser("setup", help="Generate IDE config (.mcp.json) for MCP gateway")
    mcp_setup_p.add_argument("--mode", choices=["local", "remote"], default="local",
                             help="local = Docker gateway (8182), remote = mcp.aitherium.com")
    mcp_setup_p.add_argument("--ide", choices=["claude-code", "cursor", "windsurf", "vscode"],
                             default="claude-code", help="Target IDE")
    mcp_setup_p.add_argument("--project-dir", default=".", help="Project directory for config file")
    mcp_setup_p.add_argument("--bake-token", action="store_true",
                             help="Bake auth token into headers (fallback for IDEs without OAuth)")
    # adk mcp node — lightweight local MCP server
    mcp_node_p = mcp_sub.add_parser("node", help="Start lightweight local MCP server")
    mcp_node_p.add_argument("--mode", choices=["proxy", "standalone"], default="proxy",
                            help="proxy = forward to cloud, standalone = local tools only")
    mcp_node_p.add_argument("-p", "--port", type=int, default=8182, help="Port (default: 8182)")
    # adk mcp status — check gateway connectivity
    mcp_sub.add_parser("status", help="Check MCP gateway connectivity and tier")

    # adk eval — MCP evaluation harness. Test tools and packs against a gateway.
    eval_p = sub.add_parser(
        "eval",
        help="Evaluate MCP tools and packs on a connected gateway",
    )
    eval_sub = eval_p.add_subparsers(dest="eval_command")
    eval_tools_p = eval_sub.add_parser(
        "tools", help="Evaluate all available tools"
    )
    eval_tools_p.add_argument(
        "--gateway", default="", help="MCP gateway URL (default: mcp.aitherium.com)"
    )
    eval_tools_p.add_argument(
        "--api-key", default="", help="API key (or set AITHER_API_KEY env var)"
    )
    eval_tools_p.add_argument(
        "--json", action="store_true", help="Output as JSON instead of human-readable"
    )
    eval_tools_p.add_argument(
        "--invoke", action="store_true", help="Smoke-invoke safe tools (slow)"
    )
    eval_pack_p = eval_sub.add_parser(
        "pack", help="Evaluate a specific pack's declared tools"
    )
    eval_pack_p.add_argument(
        "pack", help="Pack name or path"
    )
    eval_pack_p.add_argument(
        "--gateway", default="", help="MCP gateway URL"
    )
    eval_pack_p.add_argument(
        "--api-key", default="", help="API key"
    )
    eval_pack_p.add_argument(
        "--json", action="store_true", help="Output as JSON"
    )
    eval_sub.add_parser(
        "self-test", help="Run offline self-test (proves the harness can fail)"
    )

    # adk acp — Agent Client Protocol. Two directions:
    #   serve    — expose an AitherOS agent to ACP editors (JetBrains/Zed/...).
    #   connect/ — drive an EXTERNAL ACP agent (claude-agent-acp, codex-acp, ...)
    #   prompt/    via the v2 client.
    #   list-sessions/config
    acp_p = sub.add_parser(
        "acp",
        help="Agent Client Protocol: serve an agent to ACP editors, or drive an external ACP agent",
    )
    acp_sub = acp_p.add_subparsers(dest="acp_command")
    acp_serve_p = acp_sub.add_parser(
        "serve", help="Serve an AitherOS agent over ACP stdio (the editor-facing entrypoint)"
    )
    acp_serve_p.add_argument(
        "--name", default="aither-agent", help="Agent name advertised in initialize"
    )
    acp_serve_p.add_argument(
        "--version", default="2.0.0", help="Agent version advertised in initialize"
    )
    acp_serve_p.add_argument(
        "--model", default=None, help="LLM model/backend for the served agent (default: auto-detect)"
    )
    # Terminal Auth (AUTHENTICATION.md): an ACP client re-launches the agent
    # with the authMethod's `args` instead of its normal ones. `aither-terminal`
    # advertises ["acp", "login"], so this subcommand MUST exist — a method
    # naming a command that is not there fails as "command not found" inside the
    # editor, where nobody sees it.
    acp_sub.add_parser(
        "login", help="Interactive AitherIdentity sign-in (ACP Terminal Auth entrypoint)"
    )
    acp_connect_p = acp_sub.add_parser(
        "connect", help="Connect to an external ACP agent and report its identity"
    )
    acp_connect_p.add_argument("--command", dest="agent_command", required=True, help="Command that starts the agent (e.g. claude)")
    acp_connect_p.add_argument("--arg", dest="arg", action="append", default=[], help="Agent argument (repeatable)")
    acp_prompt_p = acp_sub.add_parser(
        "prompt", help="Prompt an external ACP agent once and print its reply"
    )
    acp_prompt_p.add_argument("--command", dest="agent_command", required=True, help="Command that starts the agent (e.g. claude)")
    acp_prompt_p.add_argument("--arg", dest="arg", action="append", default=[], help="Agent argument (repeatable)")
    acp_prompt_p.add_argument("--timeout", type=float, default=2.0, help="Drain timeout for trailing updates")
    acp_prompt_p.add_argument("message", help="The prompt text")
    acp_ls_p = acp_sub.add_parser("list-sessions", help="List sessions of an external ACP agent")
    acp_ls_p.add_argument("--command", dest="agent_command", required=True, help="Command that starts the agent (e.g. claude)")
    acp_ls_p.add_argument("--arg", dest="arg", action="append", default=[], help="Agent argument (repeatable)")
    acp_config_p = acp_sub.add_parser(
        "config", help="Emit editor config that runs `adk acp serve` as an ACP agent"
    )
    acp_config_p.add_argument("ide", choices=["zed", "jetbrains", "vscode", "neovim"],
                              help="Target editor")
    acp_config_p.add_argument(
        "--command", dest="agent_command", default=None,
        help="Override the serve command embedded in the config "
             "(default: the running python + adk.cli acp serve)",
    )

    # adk shell — download/launch AitherShell interactive terminal
    shell_p = sub.add_parser("shell", help="Launch AitherShell interactive terminal")
    shell_p.add_argument("--install", action="store_true", help="Download/update the AitherShell binary")
    shell_p.add_argument("--api-url", dest="api_url", help="Backend URL (Genesis or ADK server)")
    shell_p.add_argument("--genesis", help="Legacy alias for --api-url")
    shell_p.add_argument("shell_args", nargs=argparse.REMAINDER, help="Arguments to pass to AitherShell")

    # adk platform — internal platform toolkit commands (merged from aither-platform)
    platform_p = sub.add_parser("platform", help="Internal platform toolkit (merged from aither-platform)")
    platform_p.add_argument("platform_args", nargs=argparse.REMAINDER, help="Platform subcommand args")

    # adk listen — real-time audio intelligence (audiobook, meeting, voice notes)
    listen_p = sub.add_parser("listen", help="Real-time audio intelligence — audiobook, meeting, voice notes")
    listen_sub = listen_p.add_subparsers(dest="listen_command")

    listen_audiobook_p = listen_sub.add_parser("audiobook", help="Audiobook companion — track characters, stats, spells")
    listen_audiobook_p.add_argument("title", nargs="?", default="", help="Book title")
    listen_audiobook_p.add_argument("--author", default="", help="Author name")
    listen_audiobook_p.add_argument("--genre", default="litrpg", choices=["litrpg", "fantasy", "scifi", "general"])
    listen_audiobook_p.add_argument("--backend", default="wasapi", choices=["wasapi", "pulse", "sounddevice", "file"])
    listen_audiobook_p.add_argument("--file", dest="audio_file", help="Audio file path (for file backend)")
    listen_audiobook_p.add_argument("--workspace", help="Auto-save to workspace ID")

    listen_meeting_p = listen_sub.add_parser("meeting", help="Meeting transcription — action items, decisions, key points")
    listen_meeting_p.add_argument("title", nargs="?", default="", help="Meeting title")
    listen_meeting_p.add_argument("--type", dest="meeting_type", default="meeting",
                                  choices=["meeting", "lecture", "interview", "brainstorm"])
    listen_meeting_p.add_argument("--participants", "-p", nargs="*", default=[], help="Participant names")
    listen_meeting_p.add_argument("--backend", default="wasapi", choices=["wasapi", "pulse", "sounddevice"])
    listen_meeting_p.add_argument("--workspace", help="Auto-save to workspace ID")

    listen_note_p = listen_sub.add_parser("note", help="Voice note — quick dictation with key point extraction")
    listen_note_p.add_argument("title", nargs="?", default="Voice Note", help="Note title")
    listen_note_p.add_argument("--backend", default="wasapi", choices=["wasapi", "pulse", "sounddevice"])
    listen_note_p.add_argument("--workspace", help="Auto-save to workspace ID")

    _ = listen_sub.add_parser("sessions", help="List active listening sessions")
    listen_stop_p = listen_sub.add_parser("stop", help="Stop a listening session")
    listen_stop_p.add_argument("session_id", help="Session ID (partial match supported)")

    listen_export_p = listen_sub.add_parser("export", help="Export session as markdown notes or transcript")
    listen_export_p.add_argument("session_id", help="Session ID")
    listen_export_p.add_argument("--format", dest="fmt", default="notes", choices=["notes", "transcript"])
    listen_export_p.add_argument("--output", "-o", help="Write to file instead of stdout")

    # adk sync — bidirectional file sync (AitherDrive)
    sync_p = sub.add_parser("sync", help="Sync local directory with AitherOS platform")
    sync_sub = sync_p.add_subparsers(dest="sync_action")
    sync_init_p = sync_sub.add_parser("init", help="Initialize sync root")
    sync_init_p.add_argument("directory", nargs="?", default=".", help="Directory to sync")
    sync_sub.add_parser("status", help="Show sync status (changed/new/deleted)")
    sync_sub.add_parser("push", help="Upload local changes to platform")
    sync_sub.add_parser("pull", help="Download remote changes")
    sync_sub.add_parser("watch", help="Auto-sync on file changes (requires watchdog)")
    sync_sub.add_parser("stop", help="Stop background watcher")
    sync_ignore_p = sync_sub.add_parser("ignore", help="Add ignore pattern")
    sync_ignore_p.add_argument("pattern", help="Glob pattern to ignore")
    sync_sub.add_parser("config", help="Show sync configuration")

    # adk train — training pipeline management
    train_p = sub.add_parser("train", help="Manage model training (launch, monitor, cancel)")
    train_sub = train_p.add_subparsers(dest="train_command")

    _ = train_sub.add_parser("status", help="Check training readiness and active runs")

    train_launch_p = train_sub.add_parser("launch", help="Launch a training run")
    train_launch_p.add_argument("--preset", "-p", default="nemotron-orchestrator-8b",
                                help="Model preset (default: nemotron-orchestrator-8b)")
    train_launch_p.add_argument("--gpu", "-g", default="auto",
                                choices=["auto", "local", "dgx", "vast.ai", "customer"],
                                help="GPU target (default: auto)")
    train_launch_p.add_argument("--epochs", type=int, default=2, help="Training epochs (default: 2)")
    train_launch_p.add_argument("--lora-r", type=int, default=32, help="LoRA rank (default: 32)")
    train_launch_p.add_argument("--max-price", type=float, default=0.50,
                                help="Max GPU price $/hr for cloud (default: 0.50)")
    train_launch_p.add_argument("--dataset", help="HuggingFace dataset URL or local path")
    train_launch_p.add_argument("--no-benchmark", action="store_true",
                                help="Skip auto-benchmarking after training")
    train_launch_p.add_argument("--auto-deploy", action="store_true",
                                help="Auto-deploy if benchmark passes")

    train_logs_p = train_sub.add_parser("logs", help="Stream training logs for a run")
    train_logs_p.add_argument("run_id", help="Training run ID (partial match supported)")
    train_logs_p.add_argument("--lines", type=int, default=100, help="Number of log lines")

    train_cancel_p = train_sub.add_parser("cancel", help="Cancel an active training run")
    train_cancel_p.add_argument("run_id", help="Training run ID to cancel")

    train_runs_p = train_sub.add_parser("runs", help="List recent training runs")
    train_runs_p.add_argument("--status", help="Filter by status (e.g. training, completed, failed)")

    train_register_gpu_p = train_sub.add_parser("register-gpu",
                                                help="Register your local GPU for training")
    train_register_gpu_p.add_argument("--host", default="localhost", help="SSH host (default: localhost)")
    train_register_gpu_p.add_argument("--port", type=int, default=22, help="SSH port (default: 22)")
    train_register_gpu_p.add_argument("--gpu-model", help="GPU model name (auto-detected if omitted)")
    train_register_gpu_p.add_argument("--vram", type=int, help="GPU VRAM in GB (auto-detected if omitted)")

    # adk jobs — manage background jobs and expeditions
    jobs_p = sub.add_parser(
        "jobs",
        help="Manage background jobs — LOCAL by default, --remote for the portal/cloud",
    )
    jobs_sub = jobs_p.add_subparsers(dest="jobs_command")
    # `--remote` on read/steer commands routes to genesis/portal expeditions;
    # without it they operate on the LOCAL job store (~/.aither/jobs.db).
    jobs_list_p = jobs_sub.add_parser("list", help="List jobs (local by default)")
    jobs_list_p.add_argument("--remote", action="store_true", help="List cloud expeditions instead")
    jobs_status_p = jobs_sub.add_parser("status", help="Show status of a job")
    jobs_status_p.add_argument("id", help="Job ID (local) or Expedition ID (--remote)")
    jobs_status_p.add_argument("--remote", action="store_true", help="Query the cloud expedition")
    jobs_steer_p = jobs_sub.add_parser("steer", help="Send a follow-up message to a job")
    jobs_steer_p.add_argument("id", help="Job/Expedition ID")
    jobs_steer_p.add_argument("message", help="Follow-up message")
    jobs_steer_p.add_argument("--remote", action="store_true", help="Steer the cloud expedition")
    jobs_hint_p = jobs_sub.add_parser("hint", help="Send an invisible hint to a job")
    jobs_hint_p.add_argument("id", help="Job/Expedition ID")
    jobs_hint_p.add_argument("message", help="Hint message")
    jobs_hint_p.add_argument("--remote", action="store_true", help="Hint the cloud expedition")
    jobs_watch_p = jobs_sub.add_parser("watch", help="Watch a cloud job's progress in real-time")
    jobs_watch_p.add_argument("id", help="Expedition ID")
    # ── LOCAL job engine (runs on THIS machine; no server required) ──
    jobs_run_p = jobs_sub.add_parser("run", help="Run a job locally in the FOREGROUND")
    jobs_run_p.add_argument("query", nargs="+", help="The task to run")
    jobs_run_p.add_argument("--agent", default="aither", help="Local agent persona")
    jobs_start_p = jobs_sub.add_parser("start", help="Start a local job in the BACKGROUND (detached)")
    jobs_start_p.add_argument("query", nargs="+", help="The task to run")
    jobs_start_p.add_argument("--agent", default="aither", help="Local agent persona")
    jobs_cancel_p = jobs_sub.add_parser("cancel", help="Cancel a running local job")
    jobs_cancel_p.add_argument("id", help="Local job ID")
    jobs_sync_p = jobs_sub.add_parser("sync", help="Sync a local job to/from the portal (opt-in)")
    jobs_sync_p.add_argument("direction", choices=["push", "pull"], help="push=mirror up, pull=refresh")
    jobs_sync_p.add_argument("id", help="Local job ID")
    # Hidden executor used by `start` to run the detached job body.
    jobs_exec_p = jobs_sub.add_parser("_exec", help=argparse.SUPPRESS)
    jobs_exec_p.add_argument("id", help="Local job ID to execute")

    # adk forge — dispatch agent tasks to Genesis /forge/dispatch
    forge_p = sub.add_parser("forge", help="Dispatch tasks to agent forge (Genesis)")
    forge_p.add_argument("task", help="Task description (quoted string)")
    forge_p.add_argument("--agent", default="demiurge",
                         help="Agent to dispatch to (default: demiurge)")
    forge_p.add_argument("--effort", type=int, default=5,
                         help="Effort level 1-10 (default: 5)")
    forge_p.add_argument("--watch", action="store_true", dest="watch",
                         help="Stream progress (default: true)")
    forge_p.add_argument("--no-watch", action="store_false", dest="watch",
                         help="Don't stream progress")
    forge_p.set_defaults(watch=True)

    # adk briefs — the executive-brief delivery plane (host store)
    briefs_p = sub.add_parser("briefs", help="List and read executive briefs")
    briefs_sub = briefs_p.add_subparsers(dest="briefs_command")
    briefs_sub.add_parser("list", help="List recorded briefs")
    briefs_show_p = briefs_sub.add_parser("show", help="Print one brief in full")
    briefs_show_p.add_argument("brief_id", help="The session id of the brief")

    # adk notebook — plan, run, and inspect Agent Notebooks (.anb) on Genesis
    nb_p = sub.add_parser(
        "notebook", help="Plan, run, and inspect Agent Notebooks (.anb) on Genesis")
    nb_sub = nb_p.add_subparsers(dest="nb_command")

    nb_plan_p = nb_sub.add_parser("plan", help="Create a notebook from a natural-language task")
    nb_plan_p.add_argument("prompt", nargs="+", help="The task to plan a notebook for")
    nb_plan_p.add_argument("--agent", default="atlas", help="Planning agent (default: atlas)")
    nb_plan_p.add_argument("--effort", type=int, default=5, help="Planner effort 1-10 (default: 5)")
    nb_plan_p.add_argument("--context", default="", help="Extra context to ground the plan")

    nb_list_p = nb_sub.add_parser("list", help="List Agent Notebooks")
    nb_list_p.add_argument("--workspace", default="", help="Filter by workspace")
    nb_list_p.add_argument("--status", default="", help="Filter by status (draft/ready/running/completed)")
    nb_list_p.add_argument("--limit", type=int, default=50, help="Max notebooks to return (default: 50)")

    nb_get_p = nb_sub.add_parser("get", help="Show a notebook definition (cells, spec, variables)")
    nb_get_p.add_argument("notebook_id", help="Notebook id")

    nb_run_p = nb_sub.add_parser("run", help="Execute a notebook; returns a run handle")
    nb_run_p.add_argument("notebook_id", help="Notebook id")
    nb_run_p.add_argument("--var", action="append", default=[], metavar="KEY=VALUE",
                          help="Set a run variable (repeatable)")
    nb_run_p.add_argument("--mode", default="sequential",
                          choices=["sequential", "parallel"], help="Execution mode")

    nb_status_p = nb_sub.add_parser("status", help="Show a run's status, cell traces, and cost")
    nb_status_p.add_argument("run_id", help="Run id (from `notebook run`)")

    nb_export_p = nb_sub.add_parser("export", help="Export a notebook to a Jupyter .ipynb file")
    nb_export_p.add_argument("notebook_id", help="Notebook id")
    nb_export_p.add_argument("-o", "--output", default="", help="Output path (default: ./<id>.ipynb)")

    # adk wm — world model management (status, inspect, train, reset)
    wm_p = sub.add_parser("wm", help="World model management (status, inspect, train, reset)")
    wm_sub = wm_p.add_subparsers(dest="wm_command")

    wm_sub.add_parser("status", help="List all agents with checkpoints")

    wm_inspect_p = wm_sub.add_parser("inspect", help="Show learned effects for an agent")
    wm_inspect_p.add_argument("agent", help="Agent ID (e.g., agent.aither)")

    wm_train_p = wm_sub.add_parser("train", help="Force a bootstrap/refit now")
    wm_train_p.add_argument("agent", help="Agent ID (e.g., agent.aither)")

    wm_reset_p = wm_sub.add_parser("reset", help="Delete checkpoint + transitions")
    wm_reset_p.add_argument("agent", help="Agent ID (e.g., agent.aither)")
    wm_reset_p.add_argument("--yes", action="store_true", help="Skip confirmation prompt")

    # adk graph — provenance graph CLI: status, drain, claim, ground, context, leaves, runs, show, purge
    graph_p = sub.add_parser("graph", help="Provenance graph management (status, drain, claim, ground, context, leaves, lineage, runs, show, purge)")
    graph_sub = graph_p.add_subparsers(dest="graph_command")

    gs_status_p = graph_sub.add_parser("status", help="Show spool stats + platform health")
    gs_status_p.add_argument("--json", action="store_true", help="JSON output for scripting")

    gs_drain_p = graph_sub.add_parser("drain", help="Force drain pending spool entries")
    gs_drain_p.add_argument("--limit", type=int, default=100, help="Max entries to drain (default: 100)")
    gs_drain_p.add_argument("--json", action="store_true", help="JSON output")

    gs_claim_p = graph_sub.add_parser("claim", help="Record a claim from the shell")
    gs_claim_p.add_argument("statement", help="Claim statement text")
    gs_claim_p.add_argument("--source", action="append", dest="source", help="Source URI (repeatable)")
    gs_claim_p.add_argument("--inference", action="store_true", help="Mark as inferred (requires --derived-from)")
    gs_claim_p.add_argument("--derived-from", action="append", dest="derived_from", help="Source node id (repeatable, required if --inference)")
    gs_claim_p.add_argument("--json", action="store_true", help="JSON output")

    gs_ground_p = graph_sub.add_parser("ground", help="Check platform grounding of a statement")
    gs_ground_p.add_argument("statement", help="Statement to ground")
    gs_ground_p.add_argument("--json", action="store_true", help="JSON output")

    gs_context_p = graph_sub.add_parser("context", help="Fetch bounded context subgraph for a task")
    gs_context_p.add_argument("task", help="Task description")
    gs_context_p.add_argument("--hops", type=int, default=2, help="Graph traversal depth (default: 2)")
    gs_context_p.add_argument("--budget", type=int, default=4000, help="Max tokens in response (default: 4000)")
    gs_context_p.add_argument("--json", action="store_true", help="JSON output")

    gs_leaves_p = graph_sub.add_parser("leaves", help="Fetch leaf/frontier nodes (unexplored)")
    gs_leaves_p.add_argument("--limit", type=int, default=50, help="Max leaves (default: 50)")
    gs_leaves_p.add_argument("--json", action="store_true", help="JSON output")

    gs_lineage_p = graph_sub.add_parser("lineage", help="Show ancestry path for a node")
    gs_lineage_p.add_argument("node_id", help="Node ID")
    gs_lineage_p.add_argument("--json", action="store_true", help="JSON output")

    gs_runs_p = graph_sub.add_parser("runs", help="List recent runs from local spool")
    gs_runs_p.add_argument("--limit", type=int, default=50, help="Max runs (default: 50)")
    gs_runs_p.add_argument("--json", action="store_true", help="JSON output")

    gs_show_p = graph_sub.add_parser("show", help="Show one node with its edges")
    gs_show_p.add_argument("node_id", help="Node ID")
    gs_show_p.add_argument("--json", action="store_true", help="JSON output")

    gs_purge_p = graph_sub.add_parser("purge", help="Purge old sent entries from spool")
    gs_purge_p.add_argument("--older-than", type=int, default=7, help="Days old (default: 7)")
    gs_purge_p.add_argument("--json", action="store_true", help="JSON output")


# ── Forge subcommand handler ─────────────────────────────────────────────


def cmd_forge(args) -> int:
    """Dispatch a task to Genesis /forge/dispatch with optional streaming."""
    import httpx

    task = getattr(args, "task", "")
    agent = getattr(args, "agent", "demiurge")
    effort = getattr(args, "effort", 5)
    watch = getattr(args, "watch", True)

    if not task:
        print("Error: task is required", file=sys.stderr)
        return 1

    genesis_url = _get_genesis_url()

    # Build request payload
    payload = {
        "task": task,
        "agent": agent,
        "effort": effort,
    }

    try:
        # Dispatch to Genesis
        with httpx.Client(verify=tls_verify()) as client:
            try:
                response = client.post(
                    f"{genesis_url}/forge/dispatch",
                    json=payload,
                    timeout=30.0,
                )
            except Exception as e:
                print(
                    f"Error: Genesis offline or unreachable ({genesis_url})",
                    file=sys.stderr,
                )
                print(f"  {type(e).__name__}: {e}", file=sys.stderr)
                return 1

            if response.status_code != 200:
                try:
                    err_data = response.json()
                    print(
                        f"Error: Genesis returned {response.status_code}",
                        file=sys.stderr,
                    )
                    print(f"  {err_data.get('detail', 'Unknown error')}",
                          file=sys.stderr)
                except Exception:
                    print(
                        f"Error: Genesis returned {response.status_code}: "
                        f"{response.text[:200]}",
                        file=sys.stderr,
                    )
                return 1

            dispatch_result = response.json()
            session_id = dispatch_result.get("session_id", "unknown")
            print(f"Session ID: {session_id}")

        # Stream if requested
        if watch:
            return _stream_forge_session(genesis_url, session_id)

        return 0

    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def cmd_notebook(args) -> int:
    """Plan, run, and inspect Agent Notebooks via the ADK notebook tools."""
    from adk import notebook_tools as nt

    sub = getattr(args, "nb_command", None)
    if not sub:
        print("Usage: adk notebook <plan|list|get|run|status|export> ...", file=sys.stderr)
        return 1

    if sub == "plan":
        prompt = " ".join(getattr(args, "prompt", []) or [])
        out = nt.notebook_plan(prompt, agent=args.agent, effort=args.effort,
                               context=args.context)
    elif sub == "list":
        out = nt.notebook_list(workspace=args.workspace, status=args.status,
                               limit=args.limit)
    elif sub == "get":
        out = nt.notebook_get(args.notebook_id)
    elif sub == "run":
        variables: dict = {}
        for pair in getattr(args, "var", []) or []:
            if "=" in pair:
                k, v = pair.split("=", 1)
                variables[k.strip()] = v
            else:
                print(f"Warning: ignoring malformed --var '{pair}' (expected KEY=VALUE)",
                      file=sys.stderr)
        out = nt.notebook_execute(args.notebook_id, variables=variables or None,
                                  mode=args.mode)
    elif sub == "status":
        out = nt.notebook_run_status(args.run_id)
    elif sub == "export":
        out = nt.notebook_export(args.notebook_id, path=args.output)
    else:
        print(f"Unknown notebook subcommand: {sub}", file=sys.stderr)
        return 1

    # Tools return JSON strings; pretty-print and set exit code on error.
    try:
        parsed = json.loads(out)
    except Exception:
        print(out)
        return 0
    print(json.dumps(parsed, indent=2, default=str))
    return 1 if isinstance(parsed, dict) and parsed.get("error") else 0


def _stream_forge_session(genesis_url: str, session_id: str) -> int:
    """Stream forge session progress via GET /forge/sessions/{id}/stream (SSE)."""
    import httpx

    try:
        with httpx.Client(verify=tls_verify()) as client:
            stream_url = f"{genesis_url}/forge/sessions/{session_id}/stream"
            try:
                with client.stream(
                    "GET",
                    stream_url,
                    timeout=300.0,  # 5 minutes for long-running tasks
                ) as response:
                    if response.status_code != 200:
                        print(
                            f"Error: Stream returned {response.status_code}",
                            file=sys.stderr,
                        )
                        return 1

                    # Parse SSE lines
                    for line in response.iter_lines():
                        if not line:
                            continue
                        line = line.strip()
                        if line.startswith("data: "):
                            data_str = line[6:]  # Remove "data: " prefix
                            try:
                                data = json.loads(data_str)
                                phase = data.get("phase", "")
                                message = data.get("message", "")

                                if phase:
                                    print(f"[{phase}] {message}")
                                else:
                                    print(message)

                                # Check for final result or error
                                if data.get("status") == "completed":
                                    result = data.get("result", "")
                                    if result:
                                        print(f"\nResult:\n{result}")
                                    pr_url = data.get("pr_url", "")
                                    if pr_url:
                                        print(f"PR: {pr_url}")
                                    return 0
                                elif data.get("status") == "failed":
                                    error = data.get("error", "Unknown error")
                                    print(
                                        f"Error: {error}",
                                        file=sys.stderr,
                                    )
                                    return 1

                            except json.JSONDecodeError:
                                # Not JSON, print as-is
                                print(line)

                    # Stream ended without explicit completion
                    print("Stream ended", file=sys.stderr)
                    return 0

            except httpx.ReadTimeout:
                print(
                    "Error: Stream timeout (task taking longer than 5 min)",
                    file=sys.stderr,
                )
                return 1
            except Exception as e:
                print(f"Error streaming: {type(e).__name__}: {e}",
                      file=sys.stderr)
                return 1

    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


# ── MCP subcommand handlers ───────────────────────────────────────────────


def _cmd_acp_serve(args) -> int:
    """Serve an AitherOS agent over ACP stdio — the editor-facing entrypoint.

    An ACP client (JetBrains, Zed, VS Code, neovim, the ACP registry) runs
    ``adk acp serve`` as a subprocess and drives it over stdio JSON-RPC. The
    served agent is a real :class:`AitherAgent` on the configured LLM backend;
    its approval gate maps onto ACP ``session/request_permission``.
    """
    import asyncio

    from adk.acp_server import serve_stdio
    from adk.agent import AitherAgent
    from adk.llm import LLMRouter

    async def _run() -> None:
        llm = LLMRouter(model=args.model) if args.model else LLMRouter()
        # Backend resolution is DEFERRED to the first prompt, on purpose.
        #
        # This used to `await llm.get_provider()` here so a missing backend
        # failed loudly at startup. That is the wrong place: `initialize` and
        # `authenticate` need no model, and an ACP client — including the ACP
        # registry's CI verifier, which handshakes on a machine with no fleet,
        # no Ollama and no API key — sees the process exit before it can read
        # the `authMethods` it is there to check. The failure surfaces as
        # "timeout waiting for initialize", i.e. as a protocol bug.
        #
        # Loudness is preserved where it belongs: the first `session/prompt`
        # resolves the provider and its failure becomes a real turn error.
        agent = AitherAgent(
            name=args.name,
            llm=llm,
            system_prompt=(
                "You are an AitherOS agent exposed over the Agent Client Protocol. "
                "Complete the user's request directly, using your tools when helpful."
            ),
        )
        await serve_stdio(agent, name=args.name, version=args.version)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_acp_login(args) -> int:
    """ACP **Terminal Auth** entrypoint — interactive AitherIdentity sign-in.

    An ACP client that picks the `aither-terminal` auth method re-launches this
    process with the method's `args` (`["acp", "login"]`) in a real terminal, so
    everything here must be visible on a TTY and must exit non-zero on failure —
    a login that prints nothing and exits 0 tells the editor the user is signed
    in when they are not.
    """
    import asyncio

    from adk.auth import AuthError, begin_device_login, finish_device_login

    async def _run() -> int:
        try:
            challenge = await begin_device_login()
        except AuthError as e:
            print(f"Could not start sign-in: {e}")
            return 1
        except Exception as e:  # noqa: BLE001 — network/DNS/proxy land here
            # `{e}` alone printed "Could not reach AitherIdentity:" with NOTHING
            # after it, because several httpx errors carry an empty str(). A
            # failure message that names no cause is barely better than silence.
            print(f"Could not reach AitherIdentity: {type(e).__name__}: {e}".rstrip(": "))
            return 1

        print()
        print("  Sign in to Aitherium")
        print(f"    1. open  {challenge.verification_uri}")
        print(f"    2. enter {challenge.user_code}")
        print()
        try:
            import webbrowser

            webbrowser.open(challenge.verification_uri_complete)
        except Exception as e:  # noqa: BLE001 — headless box has no browser
            # Not fatal: the URL and code are printed above and are enough. Say
            # so rather than swallowing it, or a user on a box where the browser
            # never opens has no way to tell that was expected.
            print(f"  (could not open a browser here: {e} — use the link above)")

        print("  Waiting for approval...")
        try:
            creds = await finish_device_login(challenge)
        except AuthError as e:
            print(f"  Sign-in failed: {e}")
            return 1
        who = (creds.user or {}).get("email") or (creds.user or {}).get("username") or ""
        print(f"  Signed in{f' as {who}' if who else ''}.")
        return 0

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n  Sign-in cancelled.")
        return 1


def _cmd_acp_connect(args) -> int:
    """Connect to an external ACP agent and report its identity and capabilities."""
    import asyncio
    import os

    from adk.acp import ACPClient

    async def _run() -> int:
        client = ACPClient(command=args.agent_command, args=args.arg)
        await client.connect()
        try:
            caps = await client.initialize()
            print(f"Connected to ACP agent: {caps.agent_name} v{caps.agent_version}")
            print(f"  protocol: {caps.protocol_version}")
            sid = await client.create_session(cwd=os.getcwd())
            print(f"  session:  {sid}")
        finally:
            await client.disconnect()
        return 0

    return asyncio.run(_run())


def _cmd_acp_prompt(args) -> int:
    """Prompt an external ACP agent once and print its reply."""
    import asyncio
    import os

    from adk.acp import ACPClient

    async def _run() -> int:
        client = ACPClient(command=args.agent_command, args=args.arg)
        await client.connect()
        try:
            await client.initialize()
            sid = await client.create_session(cwd=os.getcwd())
            result = await client.prompt(sid, args.message, drain_timeout=args.timeout)
            print(result.text)
        finally:
            await client.disconnect()
        return 0

    return asyncio.run(_run())


def _cmd_acp_list_sessions(args) -> int:
    """List the sessions an external ACP agent currently holds."""
    import asyncio

    from adk.acp import ACPClient

    async def _run() -> int:
        client = ACPClient(command=args.agent_command, args=args.arg)
        await client.connect()
        try:
            await client.initialize()
            sessions = await client.list_sessions()
            for s in sessions:
                print(s)
        finally:
            await client.disconnect()
        return 0

    return asyncio.run(_run())


def _cmd_acp_config(args) -> int:
    """Emit editor config that runs ``adk acp serve`` as an ACP agent.

    Every ACP editor needs (a) a way to launch the agent on stdio and (b) the
    agent's advertised identity. The zed/registry format is the canonical
    ``agent.json``; the others embed the same launch command in their own
    shape. ``--command`` overrides the embedded serve command.
    """
    import json

    if args.agent_command:
        serve_argv = args.agent_command.split()
    else:
        serve_argv = [sys.executable, "-m", "adk.cli", "acp", "serve"]
    agent_json = {
        "name": "awdk",
        "description": "AitherOS agent exposed over the Agent Client Protocol",
        "distribution": {"type": "pip", "package": "awdk"},
        "runtime": {"type": "stdio", "command": serve_argv},
        "capabilities": {
            "session": {"list": {}, "resume": {}, "close": {}, "delete": {}},
            "prompt": {"text": {}},
        },
    }

    if args.ide == "zed":
        # Zed's ACP integration reads .zed/agents/<name>/agent.json (the ACP
        # registry agent schema).
        print(json.dumps(agent_json, indent=2))
        print(
            "\n# Install: save as .zed/agents/awdk/agent.json and restart Zed."
        )
    elif args.ide == "jetbrains":
        # JetBrains' ACP plugin reads the same agent.json via its registry path.
        print(json.dumps(agent_json, indent=2))
        print("\n# Install: register this agent.json with the JetBrains ACP plugin.")
    elif args.ide == "vscode":
        print(json.dumps(agent_json, indent=2))
        print(
            "\n# Install: point the VS Code ACP extension at this agent.json "
            "(or its folder) as a custom agent."
        )
    else:  # neovim
        print(json.dumps(agent_json, indent=2))
        print(
            "\n# Install: configure your neovim ACP client to spawn the command "
            "in this agent.json's runtime.command."
        )
    return 0


def _cmd_mcp_setup(args) -> int:
    """Generate IDE MCP config with OAuth-first auth."""
    from adk.mcp_setup import resolve_auth, resolve_gateway_url, generate_config, write_config, probe_gateway

    mode = getattr(args, "mode", "local")
    ide = getattr(args, "ide", "claude-code")
    project_dir = getattr(args, "project_dir", ".")
    bake_token = getattr(args, "bake_token", False)

    url = resolve_gateway_url(mode)
    token, source = resolve_auth()

    if bake_token:
        if token:
            print(f"  Auth: baking token from {source}")
        else:
            print("  Auth: no token found for --bake-token")
            print("  Run 'adk login' first, then re-run this command.")
            return 1
        config = generate_config(ide, url, token=token)
    else:
        print("  Auth: OAuth (IDE will handle via /authenticate)")
        config = generate_config(ide, url, token=None)

    out_path = write_config(config, ide, project_dir)
    print(f"  Config: {out_path}")
    print(f"  Gateway: {url}")

    if mode == "local":
        status = probe_gateway(url, token)
        if status["connected"]:
            print(f"  Status: connected ({status['status']})")
        else:
            print("  Status: not reachable")
            print("  Tip: Run 'adk mcp node' for a lightweight local server.")

        from adk.mcp_setup import ensure_local_ca_trust
        ca_result = ensure_local_ca_trust()
        if ca_result == "set":
            print("  TLS: AitherNet CA trusted")
        elif ca_result == "already":
            print("  TLS: AitherNet CA already trusted")
        elif ca_result:
            print(f"  TLS: {ca_result}")

    print()
    if not bake_token:
        print("  Restart your IDE, then use /authenticate to connect.")
    else:
        print("  Restart your IDE to apply.")
    return 0


def _cmd_mcp_node(args) -> int:
    """Start lightweight local MCP server."""
    from adk.node.server import run_node
    mode = getattr(args, "mode", "proxy")
    port = getattr(args, "port", 8182)
    run_node(mode=mode, port=port)
    return 0


def _cmd_mcp_status(args) -> int:
    """Check MCP gateway connectivity and tier."""
    from adk.mcp_setup import resolve_auth, probe_gateway, _GATEWAY_URLS

    token, source = resolve_auth()
    print(f"  Auth source: {source}")

    for mode_name, url in _GATEWAY_URLS.items():
        result = probe_gateway(url, token)
        icon = "[OK]" if result["connected"] else "[--]"
        print(f"  {icon} {mode_name:8s} {url}")
        if result["connected"]:
            if result.get("tier"):
                print(f"           tier={result['tier']}  tools={result['tool_count']}  "
                      f"balance={result['balance']}")
            if result.get("user"):
                print(f"           user={result['user']}")
        elif result.get("error"):
            print(f"           {result['error']}")
    return 0


def main():
    # GENERATED doctor intercept (gen_aw_doctor.py) -- do not edit
    _dv = locals().get("argv")
    if (_dv if _dv is not None else __import__("sys").argv[1:])[:1] == ["doctor"]:
        from ._doctor import report
        return report()
    # GENERATED repo-state intercept (gen_aw_doctor.py) -- do not edit
    try:
        from awgit import state as _aw_state
    except Exception:
        _aw_state = None
    if _aw_state is not None:
        _sv = locals().get("argv")
        if _aw_state.cli_banner(_sv if _sv is not None else __import__("sys").argv[1:]):
            return 0
    global _cached_parser
    # Windows consoles default to a legacy code page (cp1252) that cannot encode
    # the Unicode glyphs (arrows, box-drawing, emoji) the CLI prints — which
    # otherwise crashes commands like `adk login` with UnicodeEncodeError AFTER
    # they have already done their real work. Make all stdout/stderr writes
    # encoding-safe up front (errors="replace" degrades an unencodable glyph to
    # "?" instead of aborting). No-op where the stream can't be reconfigured.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass

    # `adk decide` owns its own parser, so hand it the raw argv before the big
    # one runs. argparse.REMAINDER cannot do this job: it only begins capturing
    # after a positional, so a LEADING flag (`adk decide --self-test`) is parsed
    # as an unknown option of this parser and exits 2 before the subcommand ever
    # sees it. That failed silently the first time — the flag simply did nothing.
    if len(sys.argv) > 1 and sys.argv[1] == "decide":
        from adk.decisions.cli import main as decide_main
        sys.exit(decide_main(sys.argv[2:]))
    # Same shape for `adk storage`: leading flags (`adk storage --self-test`) must
    # reach awstorage's own parser, not die as unknown options of this one.
    if len(sys.argv) > 1 and sys.argv[1] == "storage":
        from adk.storage_cmd import main as storage_main
        sys.exit(storage_main(sys.argv[2:]))

    parser = argparse.ArgumentParser(
        prog="adk",
        description="AitherADK — Build AI agent fleets with any LLM backend",
    )
    sub = parser.add_subparsers(dest="command")
    _register_commands(sub)
    _cached_parser = parser

    args = parser.parse_args()

    # Non-blocking update check (once per day)
    _check_for_updates()

    if args.command == "start":
        sys.exit(cmd_start(args))
    elif args.command == "init":
        sys.exit(cmd_init(args))
    elif args.command == "new":
        sys.exit(cmd_new(args))
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "image":
        cmd_image(args)
    elif args.command == "up":
        sys.exit(cmd_up(args))
    elif args.command == "sandbox":
        sys.exit(cmd_sandbox(args))
    elif args.command == "publish-preflight":
        from .toolpacks.publish_preflight.tools import (
            publish_diagnose_failure, publish_preflight)
        if args.diagnose:
            print(publish_diagnose_failure(args.diagnose))
        else:
            out = publish_preflight(args.path, args.import_name)
            print(out)
            # Exit non-zero on a refusal so a caller can gate on it. A
            # preflight that reports failure and exits 0 is advice, not a check.
            import json as _json
            if _json.loads(out).get("status") != "success":
                return 1
    elif args.command == "bonsai-local":
        sys.exit(cmd_bonsai_local(args))
    elif args.command == "down":
        sys.exit(cmd_down(args))
    elif args.command == "reregister":
        sys.exit(cmd_reregister(args))
    elif args.command == "stack":
        sys.exit(cmd_stack(args))
    elif args.command == "ssh":
        sys.exit(cmd_ssh(args))
    elif args.command == "ssh-cert":
        sys.exit(cmd_ssh_cert(args))
    elif args.command == "wizard":
        if getattr(args, "gui", False):
            from adk.shell.gui_wizard import main as _gui_main
            gui_argv = ["--auto"] if getattr(args, "yes", False) else []
            sys.exit(_gui_main(gui_argv))
        from adk.setup_cli import cmd_wizard
        sys.exit(cmd_wizard(args))
    elif args.command == "register":
        sys.exit(cmd_register(args))
    elif args.command == "login":
        sys.exit(cmd_login(args))
    elif args.command == "pair":
        from adk.node_pairing import cmd_pair
        sys.exit(cmd_pair(args))
    elif args.command == "whoami":
        sys.exit(cmd_whoami(args))
    elif args.command == "logout":
        sys.exit(cmd_logout(args))
    elif args.command == "balance":
        sys.exit(cmd_balance(args))
    elif args.command == "ambient":
        sys.exit(cmd_ambient(args))
    elif args.command == "x-session":
        sys.exit(cmd_x_session(args))
    elif args.command == "decide":
        from adk.decisions.cli import main as decide_main
        sys.exit(decide_main(args.decide_args))
    elif args.command == "harness":
        from adk.harnesses.cli import cmd_shell
        sys.exit(cmd_shell(args))
    elif args.command == "claude-model":
        from adk.claude_model import cmd_claude_model
        sys.exit(cmd_claude_model(args))
    elif args.command == "claude-account":
        from adk.claude_accounts import cmd_claude_account
        sys.exit(cmd_claude_account(args))
    elif args.command == "claude":
        from adk.claude_runner import cmd_claude
        sys.exit(cmd_claude(args))
    elif args.command == "agent-prompt":
        from adk.agent_prompt import cmd_agent_prompt
        sys.exit(cmd_agent_prompt(args))
    elif args.command == "connect":
        sys.exit(cmd_connect(args))
    elif args.command == "setup":
        from adk.setup_cli import cmd_setup
        sys.exit(cmd_setup(args))
    elif args.command == "setup-all":
        from adk.bootstrap_cli import cmd_setup_all
        sys.exit(cmd_setup_all(args))
    elif args.command == "ui":
        sys.exit(cmd_ui(args))
    elif args.command == "agents":
        sys.exit(cmd_agents(args))
    elif args.command == "agent":
        sys.exit(cmd_agent(args))
    elif args.command == "chat":
        sys.exit(cmd_chat(args))
    elif args.command == "invoke":
        sys.exit(cmd_invoke(args))
    elif args.command == "aeon":
        sys.exit(cmd_aeon(args))
    elif args.command == "create-app":
        sys.exit(cmd_create_app(args))
    elif args.command == "deploy":
        component = getattr(args, "component", None)
        if component == "agent":
            # --tenant or --from triggers download+run mode (deploy TO this machine)
            if getattr(args, "tenant", None) or getattr(args, "from_url", None):
                from adk.deploy import cmd_deploy_tenant_agent
                sys.exit(cmd_deploy_tenant_agent(args))
            else:
                sys.exit(cmd_deploy(args))
        else:
            from adk.deploy import cmd_deploy_component
            sys.exit(cmd_deploy_component(args))
    elif args.command == "workspace":
        sys.exit(cmd_workspace(args))
    elif args.command == "onboard":
        sys.exit(cmd_onboard(args))
    elif args.command == "enroll":
        sys.exit(cmd_enroll(args))
    elif args.command == "host":
        sys.exit(cmd_host(args))
    elif args.command == "integrate":
        sys.exit(cmd_integrate(args))
    elif args.command == "publish":
        sys.exit(cmd_publish(args))
    elif args.command == "index":
        sys.exit(cmd_index(args))
    elif args.command == "test":
        sys.exit(cmd_test(args))
    elif args.command == "status":
        sys.exit(cmd_status(args))
    elif args.command == "admin":
        sys.exit(cmd_admin(args))
    elif args.command == "relay":
        sys.exit(cmd_relay(args))
    elif args.command == "backend":
        sys.exit(cmd_backend(args))
    elif args.command == "keys":
        sys.exit(cmd_keys(args))
    elif args.command == "secret":
        sys.exit(cmd_secret(args))
    elif args.command == "vault":
        from adk import vault_lockbox
        sys.exit(vault_lockbox.dispatch(args))
    elif args.command == "voice":
        sys.exit(cmd_voice(args))
    elif args.command == "routing":
        sys.exit(cmd_routing(args))
    elif args.command == "join":
        from adk.commands.join import cmd_join
        sys.exit(cmd_join(args))
    elif args.command == "approvals":
        from adk.commands.approvals import cmd_approvals
        sys.exit(cmd_approvals(args))
    elif args.command == "mesh":
        sys.exit(cmd_mesh(args))
    elif args.command == "costs":
        sys.exit(cmd_costs(args))
    elif args.command == "tools":
        sys.exit(cmd_tools(args))
    elif args.command == "quickstart":
        sys.exit(cmd_quickstart(args))
    elif args.command == "quickstart-local":
        sys.exit(cmd_quickstart_local(args))
    elif args.command == "backup":
        sys.exit(cmd_backup(args))
    elif args.command == "ingest":
        sys.exit(cmd_ingest(args))
    elif args.command == "disconnect":
        sys.exit(cmd_disconnect(args))
    elif args.command == "jobs":
        sys.exit(cmd_jobs(args))
    elif args.command == "doctor":
        from adk.doctor import cmd_doctor
        sys.exit(cmd_doctor(args))
    elif args.command == "gobbonet":
        from adk.packs.gobbonet.launch import cmd_gobbonet
        sys.exit(cmd_gobbonet(args))
    elif args.command == "gateway":
        from adk.gateway_process import cmd_gateway
        sys.exit(cmd_gateway(args))
    elif args.command == "cron":
        sys.exit(_cmd_cron(args))
    elif args.command in ("addon", "component", "components"):
        sys.exit(_cmd_addon(args))
    elif args.command == "install":
        sys.exit(cmd_install(args))
    elif args.command == "packs":
        sys.exit(cmd_packs(args))
    elif args.command == "contribute":
        sys.exit(cmd_contribute(args))
    elif args.command == "pack":
        sys.exit(_cmd_pack(args))
    elif args.command == "fleet":
        sys.exit(_cmd_fleet(args))
    elif args.command == "instance":
        sys.exit(_cmd_instance(args))
    elif args.command == "skills":
        sys.exit(_cmd_skills(args))
    elif args.command == "soul":
        sys.exit(_cmd_soul(args))
    elif args.command == "doc":
        from adk.doc import cmd_doc
        sys.exit(cmd_doc(args))
    elif args.command == "mcp":
        mcp_cmd = getattr(args, "mcp_command", None)
        if mcp_cmd == "serve":
            from adk.mcp_stdio import cmd_mcp_serve
            sys.exit(cmd_mcp_serve(args))
        elif mcp_cmd == "config":
            from adk.mcp_stdio import cmd_mcp_config
            sys.exit(cmd_mcp_config(args))
        elif mcp_cmd == "setup":
            sys.exit(_cmd_mcp_setup(args))
        elif mcp_cmd == "node":
            sys.exit(_cmd_mcp_node(args))
        elif mcp_cmd == "status":
            sys.exit(_cmd_mcp_status(args))
        else:
            print("Usage: adk mcp [serve|config|setup|node|status]")
            print()
            print("  serve    Start stdio MCP server (pipe into Claude Code)")
            print("  config   Print MCP client configuration JSON")
            print("  setup    Generate IDE config for MCP gateway (OAuth-first)")
            print("  node     Start lightweight local MCP server")
            print("  status   Check MCP gateway connectivity and tier")
            sys.exit(1)
    elif args.command == "eval":
        import asyncio
        eval_cmd = getattr(args, "eval_command", None)
        if eval_cmd == "tools":
            from adk.evalharness.cli import cmd_eval_tools
            sys.exit(asyncio.run(cmd_eval_tools(args)))
        elif eval_cmd == "pack":
            from adk.evalharness.cli import cmd_eval_pack
            sys.exit(asyncio.run(cmd_eval_pack(args)))
        elif eval_cmd == "self-test":
            from adk.evalharness.cli import cmd_eval_self_test
            sys.exit(asyncio.run(cmd_eval_self_test(args)))
        else:
            print("Usage: adk eval [tools|pack|self-test]")
            print()
            print("  tools      Evaluate all available tools on a gateway")
            print("  pack       Evaluate a specific pack's declared tools")
            print("  self-test  Run offline self-test (proves it can fail)")
            sys.exit(1)
    elif args.command == "acp":
        acp_cmd = getattr(args, "acp_command", None)
        if acp_cmd == "serve":
            sys.exit(_cmd_acp_serve(args))
        elif acp_cmd == "login":
            sys.exit(_cmd_acp_login(args))
        elif acp_cmd == "connect":
            sys.exit(_cmd_acp_connect(args))
        elif acp_cmd == "prompt":
            sys.exit(_cmd_acp_prompt(args))
        elif acp_cmd == "list-sessions":
            sys.exit(_cmd_acp_list_sessions(args))
        elif acp_cmd == "config":
            sys.exit(_cmd_acp_config(args))
        else:
            print("Usage: adk acp [serve|login|connect|prompt|list-sessions|config]")
            print()
            print("  serve          Serve an AitherOS agent to ACP editors (JetBrains/Zed/...)")
            print("  login          Interactive AitherIdentity sign-in (ACP Terminal Auth)")
            print("  connect        Connect to an external ACP agent and report its identity")
            print("  prompt         Prompt an external ACP agent once and print its reply")
            print("  list-sessions  List an external ACP agent's sessions")
            print("  config <ide>   Emit editor config that runs `adk acp serve`")
            sys.exit(1)
    elif args.command == "shell":
        from adk.shell_launcher import cmd_shell
        sys.exit(cmd_shell(args))
    elif args.command == "platform":
        # Delegate to the internal platform CLI (relocated to aither-platform package)
        try:
            from aither_platform.cli import main as platform_main
            # Replace sys.argv so the platform CLI parses its own args
            sys.argv = ["adk-platform"] + (args.platform_args or [])
            platform_main()
        except ImportError:
            print("Platform toolkit not available (internal AitherOS builds only).")
            sys.exit(1)
    elif args.command == "listen":
        sys.exit(_cmd_listen(args))
    elif args.command == "sync":
        sys.exit(cmd_sync(args))
    elif args.command == "grid":
        sys.exit(cmd_grid(args))
    elif args.command == "explore":
        sys.exit(cmd_explore(args))
    elif args.command == "upgrade":
        sys.exit(cmd_upgrade(args))
    elif args.command == "support":
        print("\n  AitherADK Support")
        print("  " + "=" * 40)
        print("  Docs:      https://github.com/Aitherium/awdk")
        print("  Discord:   https://discord.gg/aitherium")
        print("  Issues:    https://github.com/Aitherium/awdk/issues")
        print("  Portal:    https://portal.aitherium.com")
        print("  Email:     support@aitherium.com")
        print()
        sys.exit(0)
    elif args.command == "forge":
        sys.exit(cmd_forge(args))
    elif args.command == "notebook":
        sys.exit(cmd_notebook(args))
    elif args.command == "briefs":
        from adk.commands.briefs import cmd_briefs_list, cmd_briefs_show

        brief_cmd = getattr(args, "briefs_command", None)
        if brief_cmd == "show":
            sys.exit(cmd_briefs_show(args))
        sys.exit(cmd_briefs_list(args))
    elif args.command == "wm":
        wm_cmd = getattr(args, "wm_command", None)
        if wm_cmd == "status":
            from adk.commands.wm import cmd_wm_status
            sys.exit(cmd_wm_status(args))
        elif wm_cmd == "inspect":
            from adk.commands.wm import cmd_wm_inspect
            sys.exit(cmd_wm_inspect(args))
        elif wm_cmd == "train":
            from adk.commands.wm import cmd_wm_train
            sys.exit(cmd_wm_train(args))
        elif wm_cmd == "reset":
            from adk.commands.wm import cmd_wm_reset
            sys.exit(cmd_wm_reset(args))
        else:
            print("Usage: adk wm [status|inspect|train|reset]")
            print()
            print("  status    List all agents with checkpoints")
            print("  inspect   Show learned effects for an agent")
            print("  train     Force a bootstrap/refit now")
            print("  reset     Delete checkpoint + transitions")
            sys.exit(1)
    elif args.command == "train":
        sys.exit(_cmd_train(args))
    elif args.command == "graph":
        from adk.selfgraph.cli import cmd_graph
        sys.exit(cmd_graph(args))
    elif args.command is None:
        # No command — default to start
        args.path = "."
        sys.exit(cmd_start(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
