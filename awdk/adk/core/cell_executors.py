"""Pluggable execution backends for CodeAct cells — including real containment.

``CodeActLoop`` executes cells IN PROCESS by default so the model can operate on
live objects. That is the point of CodeAct, but it is emphatically not a sandbox:
a cell can ``import os``, spawn a ``subprocess``, and write real files (verified
by live probe). So untrusted input needs an actual boundary.

This module supplies that boundary as a swappable executor:

``InProcessExecutor``
    Today's behaviour, unchanged. Live namespace, no containment.

``DockerCellExecutor``
    Runs each cell inside a container with **no network, a read-only root
    filesystem, dropped capabilities, a memory cap, and a killable timeout**.
    Genuine containment: the escapes that succeed in-process (writing a file on
    the host, reading host env, reaching the network) fail here.

The trade-off is real and stated rather than hidden: a container is a fresh
process, so a cell cannot hold references to the host's live objects. Containment
and a live namespace are only *both* available with fork-from-warm snapshotting
(what ``adk/forkd_client.py`` targets via Firecracker) — that needs Linux ≥ 5.7 +
KVM and a reachable forkd daemon, and it is a fan-out adapter today, not a cell
executor. So: pick ``DockerCellExecutor`` for untrusted code, keep the in-process
default for trusted code operating on live objects.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Protocol

__all__ = [
    "CellOutcome",
    "CellExecutor",
    "DockerCellExecutor",
    "docker_available",
    "RESULT_SENTINEL",
    "isolated_prelude",
    "extract_result",
]

DEFAULT_IMAGE = "python:3.12-slim"

#: Marks a ``return_result()`` value on an isolated cell's stdout. A container is
#: a separate process, so the in-process ``_ReturnResultSignal`` exception cannot
#: cross the boundary — the value comes back over stdout instead.
RESULT_SENTINEL = "__ADK_CELL_RESULT__"


def isolated_prelude() -> str:
    """Source prepended to every isolated cell so ``return_result`` still works.

    Uses ``os._exit`` so the value cannot be swallowed by a bare ``except:`` in
    the model's own code (a plain ``SystemExit`` would be).
    """
    return (
        "import os as _os, sys as _sys\n"
        "def return_result(v):\n"
        f"    _sys.stdout.write({RESULT_SENTINEL!r} + repr(v) + '\\n')\n"
        "    _sys.stdout.flush()\n"
        "    _os._exit(0)\n"
    )


def extract_result(stdout: str) -> tuple[str | None, str]:
    """Split ``(result_repr, remaining_stdout)`` out of an isolated cell's stdout."""
    result = None
    kept: list[str] = []
    for line in stdout.splitlines():
        if line.startswith(RESULT_SENTINEL):
            result = line[len(RESULT_SENTINEL) :]
        else:
            kept.append(line)
    return result, "\n".join(kept)


@dataclass
class CellOutcome:
    """The result of running one cell in an isolated backend."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    blocked_reason: str = ""

    @property
    def ok(self) -> bool:
        return (
            not self.timed_out and not self.blocked_reason and self.exit_code == 0
        )


class CellExecutor(Protocol):
    """Anything that can run a cell's source and report what happened."""

    async def run(self, code: str, *, timeout: float) -> CellOutcome: ...


def docker_available(image: str = DEFAULT_IMAGE, *, timeout: float = 20.0) -> bool:
    """True when a docker daemon answers AND *image* is already local.

    Deliberately requires the image to be present: an executor must not silently
    pull hundreds of megabytes on first use.
    """
    if not shutil.which("docker"):
        return False
    try:
        if subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=timeout,
        ).returncode != 0:
            return False
        return subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, timeout=timeout
        ).returncode == 0
    except Exception:
        return False


@dataclass
class DockerCellExecutor:
    """Run a cell inside a locked-down container.

    Every flag below is load-bearing, not decoration:

    ``--network none``
        No egress. A cell cannot exfiltrate, call an LLM directly (bypassing
        MicroScheduler), or reach an internal service.
    ``--read-only`` + ``--tmpfs /tmp``
        The image filesystem cannot be modified. Scratch space is a tmpfs that
        dies with the container, so a cell cannot persist anything.
    ``--cap-drop ALL`` + ``--security-opt no-new-privileges``
        No capabilities, and no way to regain them via setuid.
    ``--memory`` / ``--pids-limit``
        A fork bomb or runaway allocation is bounded by the kernel, not by hope.
    ``--user``
        Not root inside the container.

    The container is killed on timeout, which is the part in-process execution
    fundamentally cannot do (a non-yielding cell can only be *interrupted*, and
    only if it is pure Python).
    """

    image: str = DEFAULT_IMAGE
    memory: str = "256m"
    pids_limit: int = 128
    user: str = "65534:65534"  # nobody:nogroup
    extra_args: list[str] = field(default_factory=list)

    def _argv(self, code: str) -> list[str]:
        return [
            "docker", "run", "--rm", "-i",
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--memory", self.memory,
            "--pids-limit", str(self.pids_limit),
            "--user", self.user,
            *self.extra_args,
            self.image,
            "python", "-I", "-c", code,
        ]

    async def run(self, code: str, *, timeout: float) -> CellOutcome:
        if not isinstance(code, str) or not code.strip():
            return CellOutcome(blocked_reason="empty cell")

        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv(code),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return CellOutcome(blocked_reason="docker not available")
        except Exception as exc:  # noqa: BLE001 — surface, never execute unsandboxed
            return CellOutcome(blocked_reason=f"{type(exc).__name__}: {exc}")

        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            # A container CAN be killed — this is the guarantee in-process
            # execution cannot make.
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            return CellOutcome(
                stderr=f"cell exceeded {timeout}s and the container was killed",
                timed_out=True,
            )

        return CellOutcome(
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
            exit_code=proc.returncode,
        )
