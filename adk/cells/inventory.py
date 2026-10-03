"""Measure this machine as an estate Node: CPU, RAM, GPUs, labels.

The planner must place cells on what a node really has, not on a hand-written guess.
``local_node()`` probes the host; ``node_doc()`` renders it in the ``nodes:`` shape
``estate.load`` reads, so a swarm inventory is the concatenation of each node's report.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import socket
import subprocess
import sys
from typing import Any, Callable

from .estate import Node

Runner = Callable[[list[str]], str]


class ProbeError(RuntimeError):
    """A probe could not measure what it claims to report."""


def _run(cmd: list[str]) -> str:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=15, check=False, **kwargs,
    )
    if proc.returncode != 0:
        raise ProbeError(f"{cmd[0]} exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def gpus_vram_gb(run: Runner = _run, which: Callable[[str], Any] = shutil.which) -> list[float]:
    """VRAM per NVIDIA GPU in GB. No ``nvidia-smi`` on PATH means no NVIDIA GPU: ``[]``.
    An ``nvidia-smi`` that is present but fails raises, so a broken driver is never
    reported as a GPU-less node the planner would silently route around."""
    if which("nvidia-smi") is None:
        return []
    out = run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
    vram = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            vram.append(round(float(line) / 1024, 1))
        except ValueError:
            raise ProbeError(f"nvidia-smi printed an unparseable line: {line!r}") from None
    return vram


def memory_gb() -> float:
    """Physical RAM in GB."""
    if sys.platform == "win32":
        class _Status(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _Status()
        status.dwLength = ctypes.sizeof(_Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise ProbeError("GlobalMemoryStatusEx failed")
        return round(status.ullTotalPhys / 1024**3, 1)
    if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3, 1)
    raise ProbeError(f"no memory probe for platform {sys.platform}")


def local_node(
    name: str | None = None,
    labels: dict[str, Any] | None = None,
    tags: set[str] | None = None,
    *,
    run: Runner = _run,
    which: Callable[[str], Any] = shutil.which,
) -> Node:
    """This machine as a Node. Measured labels (``os``, ``arch``) are added; caller
    labels (zone, trust, role) win, because only the operator knows those."""
    measured = {"os": platform.system().lower(), "arch": platform.machine().lower()}
    return Node(
        name=name or socket.gethostname().lower(),
        cpu=float(os.cpu_count() or 0),
        mem_gb=memory_gb(),
        gpus=gpus_vram_gb(run, which),
        labels={**measured, **(labels or {})},
        tags=set(tags or ()),
    )


def node_doc(node: Node) -> dict[str, Any]:
    """The ``nodes:`` document ``estate.load`` reads, for one node."""
    entry: dict[str, Any] = {"cpu": node.cpu, "mem": f"{node.mem_gb}G"}
    if node.gpus:
        entry["gpus"] = [f"{v}G" for v in node.gpus]
    if node.labels:
        entry["labels"] = dict(node.labels)
    if node.tags:
        entry["tags"] = sorted(node.tags)
    return {"nodes": {node.name: entry}}
