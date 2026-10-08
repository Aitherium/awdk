"""Stop the harness daemon's console children from opening terminal tabs.

The daemon runs DETACHED (scheduled task / pythonw), so it has no console. On
Windows 11 with Windows Terminal as the default terminal, any console program
it starts without ``CREATE_NO_WINDOW`` (git, nvidia-smi, where, ...) gets a NEW
console, which means a new terminal tab flashing on the owner's desktop. That
happened with nvidia-smi on 2026-10-03 (#11436) and again with
``git status --porcelain`` from the well on 2026-10-07: about three tabs a
minute. Fixing each call site only moves the problem to the next one.

``install()`` wraps ``subprocess.Popen`` once, process-wide, so a child that
did not ask for a console of its own (``CREATE_NEW_CONSOLE``) or for none at all
(``DETACHED_PROCESS``) gets ``CREATE_NO_WINDOW``. Pipes and return codes do not
change. It does nothing off Windows, and nothing when the daemon already has a
console, because its children then share that console and open no window.
"""

from __future__ import annotations

import subprocess
import sys

_installed = False


def _has_console() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetConsoleWindow())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - no ctypes/kernel32 = treat as console-less
        return False


def patched_flags(flags: int) -> int:
    """The creationflags a child actually gets. Exposed for the test."""
    explicit = (getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
                | getattr(subprocess, "DETACHED_PROCESS", 0x8))
    if flags & explicit:
        return flags
    return flags | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def install(force: bool = False) -> bool:
    """Patch ``subprocess.Popen`` once. True when the patch is active."""
    global _installed
    if _installed:
        return True
    if sys.platform != "win32":
        return False
    if _has_console() and not force:
        return False

    original_init = subprocess.Popen.__init__

    def _init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["creationflags"] = patched_flags(int(kwargs.get("creationflags") or 0))
        original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _init  # type: ignore[method-assign]
    _installed = True
    return True
