"""
Gaming Mode Plugin for AitherShell
====================================

Toggle between AitherOS and gaming mode directly from AitherShell.

Usage:
    /gaming              -- Enter gaming mode (free the GPU + stop the routine runners)
    /gaming resume       -- Resume AitherOS (bring everything back, one unit at a time)
    /gaming status       -- VRAM, HOLD state, what is stopped

Aliases: /game, /gpu

2026-09-07: this used to run scripts/Switch-GamingMode.ps1 (717 lines, 91 docker calls,
0 systemctl) -- dead on the podman-quadlet fleet. It now calls the same backend as
/quiesce (.DEPLOYMENT/scripts/llm-quiesce.ps1) with the `gaming` action, which is
`quiesce --deep`: systemd-level stops that HOLD (a sentinel gpu-boot.py honours), plus
the two routine runners so the CPU goes quiet too.
"""

import subprocess
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand
from adk.shell.plugins.builtins.quiesce import _find_pwsh, _find_quiesce_script


class GamingModePlugin(SlashCommand):
    name = "gaming"
    description = "Toggle gaming mode — free GPU or resume AitherOS"
    aliases = ["game", "gpu"]

    def __init__(self):
        super().__init__(
            name="gaming",
            description="Toggle gaming mode — free GPU or resume AitherOS",
            aliases=["game", "gpu"],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return await self._enter_gaming_mode()

        subcmd = args[0].lower()
        rest = args[1:]

        if subcmd in ("resume", "back", "start", "up", "on"):
            return await self._resume()
        elif subcmd in ("stop", "off", "enter", "free"):
            return await self._enter_gaming_mode()
        elif subcmd == "status":
            return self._status()
        elif subcmd == "help":
            return self._help()
        else:
            return (
                f"Unknown sub-command: {subcmd}\n\n"
                + self._help()
            )

    def _invoke(self, action: str, timeout: int) -> str:
        script = _find_quiesce_script()
        if not script:
            return "[x] llm-quiesce.ps1 not found. Set AITHEROS_ROOT or run from the repo root."
        try:
            result = subprocess.run(
                [_find_pwsh(), "-NoProfile", "-File", script, action],
                capture_output=False,  # stream to the terminal
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"[!] {action} timed out after {timeout // 60} minutes."
        except FileNotFoundError:
            return "[x] PowerShell (pwsh) not found. Install: https://aka.ms/powershell"
        except Exception as e:  # noqa: BLE001 - surface, never hide, in an owner-facing verb
            return f"[x] Error: {e}"
        return "" if result.returncode == 0 else f"[Exit code: {result.returncode}]"

    async def _enter_gaming_mode(self) -> str:
        """Enter gaming mode: quiesce --deep (GPU units + routine runners), HOLD set."""
        return self._invoke("gaming", 600)

    async def _resume(self) -> str:
        """Resume: HOLD cleared, units back one at a time via gpu-boot, runners restarted."""
        return self._invoke("resume", 3600)

    def _status(self) -> str:
        return self._invoke("status", 120)

    def _help(self) -> str:
        return (
            "Gaming Mode Commands:\n"
            "  /gaming                Enter gaming mode (free the GPU, stop routine runners, HOLD)\n"
            "  /gaming resume         Resume everything the last /gaming stopped\n"
            "  /gaming status         VRAM, HOLD state, what is stopped\n"
            "\n"
            "Aliases: /game, /gpu"
        )
