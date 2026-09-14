"""
LLM Quiesce Plugin for AitherShell
===================================

Stop (and later resume) every GPU-reserving LLM/model container on THIS host
while leaving the rest of the fleet running. The lighter sibling of /gaming:
/gaming stops the whole stack; /quiesce frees the GPU only.

The actual work is done by .DEPLOYMENT/scripts/llm-quiesce.ps1, which discovers
GPU containers from docker ground truth (DeviceRequests), stops them with plain
`docker stop` (holds against the watchdog; EXITED is never auto-revived), and
records exactly what it stopped so resume restarts only that set.

Usage:
    /quiesce             — stop all LLM/GPU containers, free the 5090
    /quiesce resume      — start exactly the set the last quiesce stopped
    /quiesce status      — GPU VRAM + per-container state

Aliases: /llmq, /quiesce-llm
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

_SCRIPT_REL = Path(".DEPLOYMENT") / "scripts" / "llm-quiesce.ps1"


def _find_quiesce_script() -> Optional[str]:
    """Locate llm-quiesce.ps1 — same discovery ladder as the gaming plugin."""
    candidates = []

    root = os.environ.get("AITHEROS_ROOT")
    if root:
        candidates.append(Path(root) / ".." / _SCRIPT_REL)
        candidates.append(Path(root) / _SCRIPT_REL)

    cwd = Path.cwd()
    candidates.extend([cwd / _SCRIPT_REL, cwd / ".." / _SCRIPT_REL])

    # Drive-root checkouts, DISCOVERED rather than hardcoded (never ship a
    # developer's drive layout in a public package).
    from adk.shell._repo_roots import candidate_repo_roots

    for repo_root in candidate_repo_roots(include_cwd=False):
        candidates.append(repo_root / _SCRIPT_REL)

    for c in candidates:
        resolved = c.resolve()
        if resolved.is_file():
            return str(resolved)
    return None


def _find_pwsh() -> str:
    """Find PowerShell 7 binary."""
    return shutil.which("pwsh") or shutil.which("powershell") or "pwsh"


class LLMQuiescePlugin(SlashCommand):
    name = "quiesce"
    description = "Quiesce LLM/GPU containers on this host (free the GPU) or resume them"
    aliases = ["llmq", "quiesce-llm"]

    def __init__(self):
        super().__init__(
            name="quiesce",
            description="Quiesce LLM/GPU containers on this host (free the GPU) or resume them",
            aliases=["llmq", "quiesce-llm"],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        subcmd = args[0].lower() if args else "quiesce"

        if subcmd in ("quiesce", "stop", "off", "free"):
            return self._invoke("quiesce")
        if subcmd in ("resume", "start", "on", "back", "up"):
            return self._invoke("resume")
        if subcmd == "status":
            return self._invoke("status")
        if subcmd == "help":
            return self._help()
        return f"Unknown sub-command: {subcmd}\n\n" + self._help()

    def _invoke(self, action: str) -> str:
        script = _find_quiesce_script()
        if not script:
            return (
                "❌ llm-quiesce.ps1 not found.\n"
                "Set AITHEROS_ROOT or run from an AitherOS checkout "
                f"(expected {_SCRIPT_REL})."
            )
        pwsh = _find_pwsh()
        try:
            # Interactive CLI REPL: the shell awaits this single command and
            # streams its output; there are no concurrent turns to stall.
            result = subprocess.run(  # blocking-ok: single-user REPL, no concurrent turns
                [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, action],
                capture_output=False,  # stream straight to the terminal
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            if result.returncode == 0:
                return ""  # output already printed
            if result.returncode == 2:
                return "❌ Could not judge — docker unreachable? (exit 2)"
            return f"⚠️ Partial failure — some containers refused (exit {result.returncode})."
        except subprocess.TimeoutExpired:
            return "⚠️ Quiesce script timed out after 5 minutes."
        except FileNotFoundError:
            return "❌ PowerShell 7 (pwsh) not found. Install: https://aka.ms/powershell"
        except Exception as e:
            return f"❌ Error: {e}"

    def _help(self) -> str:
        return (
            "LLM Quiesce Commands:\n"
            "  /quiesce               Stop all LLM/GPU containers (fleet stays up)\n"
            "  /quiesce resume        Restart exactly what the last quiesce stopped\n"
            "  /quiesce status        GPU VRAM + per-container state\n"
            "\n"
            "Heavier option: /gaming stops the entire stack, not just LLMs.\n"
            "Aliases: /llmq, /quiesce-llm"
        )
