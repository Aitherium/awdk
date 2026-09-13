"""adk claude-model — switch Claude Code between local, DeepSeek, Kimi and Anthropic.

Wraps AitherOS/dev/tools/claude_model_profile.py as a first-class adk command.
Adds bridge lifecycle management and a one-shot ``auto`` command that starts the
bridge AND switches the profile atomically — the thing the user always wants.

    adk claude-model list                   # show profiles
    adk claude-model use deepseek-pro       # switch + restart hint
    adk claude-model use aither-best        # local qwen3.6-27b
    adk claude-model use anthropic          # restore stock Claude
    adk claude-model status                 # what's active
    adk claude-model check                  # prove the round-trip works
    adk claude-model bridge start|status    # bridge lifecycle
    adk claude-model auto deepseek-pro      # start bridge + switch (one shot)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT_CANDIDATES = [
    Path(__file__).resolve().parents[1],               # awdk/ is one level up
    Path(__file__).resolve().parents[2],               # repo root if adk/ is nested
    Path(os.environ.get("AITHEROS_ROOT", "")).resolve() if os.environ.get("AITHEROS_ROOT") else None,
    Path.cwd(),                                         # fallback to cwd
]
_REPO_ROOT = next(
    (p for p in _REPO_ROOT_CANDIDATES
     if p and (p / "AitherOS" / "dev" / "tools" / "claude_model_profile.py").exists()),
    Path(__file__).resolve().parents[1],
)
_PROFILE_TOOL = _REPO_ROOT / "AitherOS" / "dev" / "tools" / "claude_model_profile.py"
_BRIDGE_URL = os.environ.get("AITHER_BRIDGE_URL", "http://127.0.0.1:8151")


def _tool_exists() -> bool:
    return _PROFILE_TOOL.exists()


def _run_tool(args: list[str], timeout: float = 300) -> int:
    if not _tool_exists():
        print(f"Profile tool not found: {_PROFILE_TOOL}", file=sys.stderr)
        print("Run from the AitherOS repo root, or set AITHEROS_ROOT.", file=sys.stderr)
        return 2
    cmd = [sys.executable, str(_PROFILE_TOOL)] + args
    try:
        result = subprocess.run(cmd, cwd=str(_REPO_ROOT), timeout=timeout)
        return result.returncode
    except subprocess.TimeoutExpired:
        print("Command timed out.", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"Python not found: {sys.executable}", file=sys.stderr)
        return 2


def _bridge_healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{_BRIDGE_URL}/health", timeout=5) as resp:
            data = json.load(resp)
            return data.get("status") == "healthy"
    except Exception:
        return False


def _start_bridge_from_env() -> int:
    """Start the bridge with fleet env vars sourced from compose .env."""
    compose_env = _REPO_ROOT / ".DEPLOYMENT" / "compose" / ".env"
    env = dict(os.environ)

    if compose_env.exists():
        for line in compose_env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip().strip("\"'")

    token_path = Path.home() / ".aither" / "claude_bridge_token"
    if token_path.exists():
        env["AITHER_BRIDGE_TOKEN"] = token_path.read_text(encoding="utf-8").strip()
    else:
        print("No bridge token. Provisioning...")
        rc = _run_tool(["provision-token"])
        if rc != 0:
            return rc
        if token_path.exists():
            env["AITHER_BRIDGE_TOKEN"] = token_path.read_text(encoding="utf-8").strip()

    env["AITHER_MICROSCHEDULER_URL"] = env.get(
        "AITHER_MICROSCHEDULER_URL", "https://127.0.0.1:8150"
    )
    env["AITHER_BRIDGE_HOST"] = "127.0.0.1"
    env["AITHER_BRIDGE_PORT"] = "8151"
    env.pop("AITHER_DOCKER_MODE", None)

    import tempfile
    log = Path(tempfile.gettempdir()) / "aither-claude-bridge.log"
    with open(log, "ab") as handle:
        subprocess.Popen(
            [sys.executable, "-m", "services.inference.AitherClaudeBridge"],
            cwd=str(_REPO_ROOT / "AitherOS"),
            env=env,
            stdout=handle,
            stderr=handle,
        )
    print(f"Bridge starting on {_BRIDGE_URL}  (log: {log})")
    print("Waiting for health...")

    import time
    for _ in range(40):
        time.sleep(1)
        if _bridge_healthy():
            print("Bridge is HEALTHY.")
            return 0
    print("Bridge did not become healthy in 40s. Check the log.", file=sys.stderr)
    return 1


# ── CLI command handlers ──────────────────────────────────────────────────


def cmd_claude_model(args: Any) -> int:
    sub = getattr(args, "claude_model_command", None)
    try:
        if sub == "list":
            return _run_tool(["list"])
        elif sub == "use":
            return cmd_use(args)
        elif sub == "status":
            return _run_tool(["status"])
        elif sub == "check":
            timeout = getattr(args, "timeout", 120)
            return _run_tool(["check", "--timeout", str(timeout)])
        elif sub == "bridge":
            return cmd_bridge(args)
        elif sub == "auto":
            return cmd_auto(args)
        elif sub == "failover":
            return cmd_failover(args)
        elif sub == "watch":
            return cmd_watch(args)
        elif sub in _WORKFLOW_ALIASES:
            return cmd_workflow(sub, args)
        else:
            _print_help()
            return 1
    except KeyboardInterrupt:
        print()
        return 130


def cmd_use(args: Any) -> int:
    profile = getattr(args, "profile", "")
    if not profile:
        print("Usage: adk claude-model use <profile>", file=sys.stderr)
        return 2

    # For bridge-backed profiles, ensure the bridge is running
    needs_bridge = profile not in ("anthropic",) and not profile.startswith("kimi")
    if needs_bridge and not _bridge_healthy():
        print("Bridge not running. Starting it first...")
        rc = _start_bridge_from_env()
        if rc != 0:
            print("Could not start the bridge. Switch anyway? (y/n) ", end="")
            if input().strip().lower() != "y":
                return rc

    extra = []
    if getattr(args, "project", False):
        extra.append("--project")
    rc = _run_tool(["use", profile, "--persist"] + extra)  # explicit operator action -> allowed to persist
    if rc == 0:
        print()
        print("  Next: restart Claude Code for the switch to take effect.")
        print("        In the CLI: exit and re-run `claude`")
        print("        In VS Code: Ctrl+Shift+P → 'Reload Window'")
    return rc


def cmd_bridge(args: Any) -> int:
    action = getattr(args, "bridge_action", "status")
    if action == "start":
        if _bridge_healthy():
            print(f"Bridge already healthy at {_BRIDGE_URL}")
            return 0
        return _start_bridge_from_env()
    elif action == "status":
        if _bridge_healthy():
            return _run_tool(["bridge", "status"])
        else:
            print(f"Bridge not reachable at {_BRIDGE_URL}")
            return 1
    elif action == "stop":
        import signal
        # Find and kill the bridge process
        try:
            result = subprocess.run(
                [sys.executable, "-c",
                 "import psutil\n"
                 "for p in psutil.process_iter(['pid','cmdline']):\n"
                 "  cl = ' '.join(p.info.get('cmdline') or [])\n"
                 "  if 'AitherClaudeBridge' in cl:\n"
                 "    print(p.pid)\n"
                 "    p.terminate()\n"],
                capture_output=True, text=True, timeout=10,
            )
            if result.stdout.strip():
                for pid in result.stdout.strip().split("\n"):
                    print(f"Stopped bridge (PID {pid.strip()})")
                return 0
        except Exception:
            pass
        # Fallback: platform-specific kill
        if sys.platform == "win32":
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-Process python -ErrorAction SilentlyContinue | "
                 "Where-Object { $_.CommandLine -like '*AitherClaudeBridge*' } | "
                 "Stop-Process -Force"],
                capture_output=True,
            )
            print("Bridge stopped (if it was running).")
            return 0
        else:
            subprocess.run(["pkill", "-f", "AitherClaudeBridge"], capture_output=True)
            print("Bridge stopped (if it was running).")
            return 0
    else:
        print(f"Unknown bridge action: {action}", file=sys.stderr)
        return 2


def cmd_auto(args: Any) -> int:
    """One-shot: ensure bridge is running, switch profile, verify."""
    profile = getattr(args, "profile", "")
    if not profile:
        print("Usage: adk claude-model auto <profile>", file=sys.stderr)
        return 2

    is_native = profile in ("anthropic",) or profile.startswith("kimi")

    if not is_native:
        print(f"[1/3] Ensuring bridge is running...")
        if not _bridge_healthy():
            rc = _start_bridge_from_env()
            if rc != 0:
                return rc
        else:
            print(f"      Bridge already healthy at {_BRIDGE_URL}")

    step = 2 if not is_native else 1
    total = 3 if not is_native else 2
    print(f"[{step}/{total}] Switching to '{profile}'...")
    rc = _run_tool(["use", profile, "--persist"])  # explicit operator action -> allowed to persist
    if rc != 0:
        return rc

    if not is_native:
        print(f"[3/3] Verifying round-trip...")
        rc = _run_tool(["check", "--timeout", "120"])
        if rc != 0:
            print("  Verification failed — profile is set but may not work.")
            return rc
    else:
        print(f"[{step + 1}/{total}] Native provider — no bridge check needed.")

    print()
    print(f"  DONE. '{profile}' is live and verified.")
    print("  Restart Claude Code to use it.")
    return 0


def _print_help():
    print("Usage: adk claude-model <command>")
    print()
    print("Workflow shortcuts (no restart needed between these):")
    print("  plan                  → Anthropic Opus 5 (architecture, design, review)")
    print("  code                  → DeepSeek Flash (fast ultracode, 1M context)")
    print("  reason                → DeepSeek Pro (deep reasoning, 1M context)")
    print("  kimi                  → Kimi K3 (1M context, thinking always on)")
    print("  local                 → qwen3.6-27b on DGX")
    print("  fast                  → gemma4-12b for trivial tasks")
    print()
    print("Commands:")
    print("  list                  Show available model profiles")
    print("  use <profile>         Switch Claude Code to a profile")
    print("  status                Show the active profile")
    print("  check                 Prove the active profile answers a real turn")
    print("  bridge start|status|stop   Manage the translation bridge")
    print("  auto <profile>        One-shot: start bridge + switch + verify")
    print("  failover              Try current profile; if broken, switch to next working one")
    print()
    print("Profiles: anthropic, deepseek-pro, deepseek-flash, aither-best,")
    print("          aither-fast, aither-orchestrator, kimi-k3, kimi-k2.7-code")


# ── Failover ──────────────────────────────────────────────────────────────

# Priority order: try each, switch to first that answers
_FAILOVER_CHAIN = [
    "deepseek-flash",
    "deepseek-pro",
    "kimi-k2.6",
    "kimi-k3",
    "aither-best",
    "anthropic",
]


def cmd_failover(args: Any) -> int:
    """Test the current profile; if broken, walk the chain until one works.

    DISABLED BY DEFAULT (2026-09-06). Each candidate in _FAILOVER_CHAIN is applied
    by shelling out to claude_model_profile.py, which persists the switch into
    settings.json. Run from the `watch --daemon` logon task, that meant the
    machine could silently repoint itself at kimi-k3 between sessions; the owner
    then launched the session-scoped switcher (cds), settings.json outranked it,
    and Claude Code came up as `deepseek-v4-flash[1m]@api.moonshot.ai` -- a model
    name that does not exist at that endpoint. Every turn failed, and the only
    way out was a full Claude Code reset.

    Automatic backend switching is not worth that trade: the failure it prevents
    (one provider being down) is loud and obvious, while the failure it CAUSES is
    silent and looks like Claude Code itself is broken. The daemon has been
    removed from the logon task; this is the second lock, so re-enabling that task
    cannot bring the behaviour back on its own.
    """
    if not (getattr(args, "yes", False) or os.environ.get("AITHER_ALLOW_AUTO_FAILOVER")):
        print(
            "REFUSING: automatic backend failover is disabled.\n"
            "\n"
            "It switches by writing a PERSISTENT override into settings.json, which\n"
            "outranks the session-scoped switcher and survives closing the terminal.\n"
            "Done in the background it silently repoints your backend, and the next\n"
            "`cds` comes up as a MIXED backend that fails every turn.\n"
            "\n"
            "Switch explicitly instead (session-scoped, dies with the terminal):\n"
            "    cds    DeepSeek      cks    Kimi K3      cas    Anthropic\n"
            "\n"
            "Override for a one-off, deliberate run:\n"
            "    adk claude-model failover --yes\n"
            "    (or AITHER_ALLOW_AUTO_FAILOVER=1)",
            file=sys.stderr,
        )
        return 3
    print("Failover: testing current profile...")
    rc = _run_tool(["check", "--timeout", "30"])
    if rc == 0:
        print("Current profile works. No failover needed.")
        return 0

    print("Current profile FAILED. Walking failover chain...")
    for profile in _FAILOVER_CHAIN:
        print(f"  Trying '{profile}'...", end=" ", flush=True)
        # Switch
        switch_rc = _run_tool(["use", profile, "--persist"])  # explicit operator action -> allowed to persist
        if switch_rc != 0:
            print("skip (can't switch)")
            continue
        # Verify
        check_rc = _run_tool(["check", "--timeout", "30"])
        if check_rc == 0:
            print(f"OK!")
            print(f"\n  Failover complete → '{profile}'")
            print("  Restart Claude Code to use it.")
            return 0
        else:
            print("FAIL")
    print("\n  All profiles failed. Check your API keys and network.")
    return 1


# ── Workflow aliases (plan/code/hybrid) ───────────────────────────────────
# These are the shortcuts that let you swap between Opus planning and
# DeepSeek ultracode without remembering profile names.

_WORKFLOW_ALIASES = {
    "plan": "anthropic",          # Real Opus 5 — architecture, design, review
    "code": "deepseek-flash",     # DeepSeek V4 Flash — fast 1M ultracode
    "reason": "deepseek-pro",     # DeepSeek V4 Pro — deep reasoning, 1M ctx
    "kimi": "kimi-k3",            # Kimi K3 — 1M context, thinking always on
    "local": "aither-best",       # Local qwen3.6-27b on DGX
    "fast": "aither-fast",        # Local gemma4-12b — trivial edits
}


def cmd_workflow(alias: str, args: Any) -> int:
    """Switch to a workflow-named profile instantly.

    Usage:
        adk claude-model plan      → stock Anthropic Opus (architecture/review)
        adk claude-model code      → DeepSeek Flash (fast ultracode, 1M ctx)
        adk claude-model reason    → DeepSeek Pro (deep reasoning)
        adk claude-model kimi      → Kimi K3 (1M, thinking on)
        adk claude-model local     → qwen3.6-27b on DGX
        adk claude-model fast      → gemma4-12b for trivial tasks
    """
    profile = _WORKFLOW_ALIASES.get(alias)
    if not profile:
        print(f"Unknown workflow alias: {alias}", file=sys.stderr)
        print(f"Available: {', '.join(_WORKFLOW_ALIASES.keys())}", file=sys.stderr)
        return 2

    print(f"  [{alias}] → switching to '{profile}'")

    # Native profiles don't need the bridge
    is_native = profile in ("anthropic",) or profile.startswith(("kimi", "deepseek"))
    if not is_native and not _bridge_healthy():
        print("  Starting bridge...")
        rc = _start_bridge_from_env()
        if rc != 0:
            return rc

    rc = _run_tool(["use", profile, "--persist"])  # explicit operator action -> allowed to persist
    if rc == 0:
        print()
        print(f"  SWITCHED: {alias} → {profile}")
        print("  Restart Claude Code (Ctrl+Shift+P → Reload Window)")
    return rc
