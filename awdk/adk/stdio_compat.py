"""Cross-platform async stdio for stdio-protocol servers (ACP, MCP).

``loop.connect_read_pipe`` **cannot attach to stdin under Windows' Proactor event
loop** — it raises inside ``_loop_reading``, so every stdio server built on it
crashes on Windows the moment it tries to read a request. That was found by
actually running adk's ACP server on Windows; the same unguarded call was then
found in two more places (``adk/mcp_stdio.py``, ``adk/shell/mcp_bridge.py``),
which is why the fix lives here once instead of three times.

Reads are delegated to the default executor (a thread), which behaves
identically on POSIX and Windows. Both classes expose the minimal shape a
JSON-RPC-over-stdio loop needs: ``await reader.readline() -> bytes`` and
``writer.write(bytes)`` / ``await writer.drain()``.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

__all__ = ["ThreadStdinReader", "ThreadStdoutWriter"]


class ThreadStdinReader:
    """Async line reader over a blocking binary stream (Windows-safe)."""

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream if stream is not None else sys.stdin.buffer

    async def readline(self) -> bytes:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._stream.readline
        )


class ThreadStdoutWriter:
    """Blocking binary writer with the ``write``/``drain`` shape servers expect."""

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream if stream is not None else sys.stdout.buffer

    def write(self, data: bytes) -> None:
        self._stream.write(data)

    async def drain(self) -> None:
        # Deliberately a direct flush, matching the implementation already proven
        # live against Zed's reference ACP client. Only the READ side needs the
        # executor (that is what Windows' Proactor loop cannot do).
        self._stream.flush()
