"""Run the harness daemon -- and so every terminal and git it spawns -- at NORMAL priority.

WHY (measured 2026-10-10 on the owner's desktop): the daemon is started by a Task
Scheduler task, and a task's default priority (7) starts its process BELOW_NORMAL
with LOW I/O priority and low memory priority. Children inherit all three. The OS
IDE's ``git status`` on the C: monorepo then took 15 s+ (0.8 s from a normal shell)
and a terminal's PowerShell profile never finished loading. The daemon is an
interactive surface for the person at the keyboard; it is not background work.

Best effort and Windows-only: a refusal is logged by the caller, never fatal.
"""

from __future__ import annotations

import sys
from typing import Any

NORMAL_PRIORITY_CLASS = 0x00000020
#: NtSetInformationProcess ProcessIoPriority; 2 = IoPriorityNormal.
_PROCESS_IO_PRIORITY = 33
_IO_PRIORITY_NORMAL = 2
#: SetProcessInformation ProcessMemoryPriority; 5 = MEMORY_PRIORITY_NORMAL.
_PROCESS_MEMORY_PRIORITY = 0
_MEMORY_PRIORITY_NORMAL = 5


def normalize_process_priority(api: Any = None) -> dict[str, bool]:
    """Raise THIS process to normal CPU, I/O and memory priority. Returns what took."""
    if sys.platform != "win32" and api is None:
        return {}
    import ctypes

    if api is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        # HANDLE-typed, or the pseudo-handle (-1) is truncated to 32 bits on x64 and
        # every call fails as an invalid handle.
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel32.SetProcessInformation.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        ntdll.NtSetInformationProcess.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        ntdll.NtSetInformationProcess.restype = ctypes.c_long
        api = kernel32, ntdll
    kernel32, ntdll = api
    handle = kernel32.GetCurrentProcess()
    done = {"cpu": bool(kernel32.SetPriorityClass(handle, NORMAL_PRIORITY_CLASS))}
    io = ctypes.c_ulong(_IO_PRIORITY_NORMAL)
    status = ntdll.NtSetInformationProcess(
        handle, _PROCESS_IO_PRIORITY, ctypes.byref(io), ctypes.sizeof(io))
    done["io"] = status == 0
    mem = ctypes.c_ulong(_MEMORY_PRIORITY_NORMAL)
    setter = getattr(kernel32, "SetProcessInformation", None)
    done["memory"] = bool(setter and setter(
        handle, _PROCESS_MEMORY_PRIORITY, ctypes.byref(mem), ctypes.sizeof(mem)))
    return done
