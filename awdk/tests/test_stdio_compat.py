"""stdio-protocol servers must read stdin on EVERY platform.

`loop.connect_read_pipe` cannot attach to stdin under Windows' Proactor event
loop — it raises inside `_loop_reading`. That crashed adk's ACP server on
Windows (found by actually running it), and the same unguarded call was then
found in `adk/mcp_stdio.py` and `adk/shell/mcp_bridge.py`.

These tests pin the fix: the shared thread-backed reader works, it works
specifically on the Proactor loop, and no stdio server has regressed to
`connect_read_pipe` on stdin.
"""
from __future__ import annotations

import asyncio
import io
import re
import sys
from pathlib import Path

import pytest

from adk.stdio_compat import ThreadStdinReader, ThreadStdoutWriter

STDIO_SERVERS = [
    "adk/acp_server.py",
    "adk/mcp_stdio.py",
    "adk/shell/mcp_bridge.py",
]


def _repo_file(rel: str) -> Path:
    return Path(__file__).resolve().parent.parent / rel


# ── the adapters themselves ────────────────────────────────────────────────


def test_reader_yields_lines_in_order():
    async def go():
        r = ThreadStdinReader(io.BytesIO(b'{"a":1}\n{"b":2}\n'))
        return [await r.readline(), await r.readline(), await r.readline()]

    assert asyncio.run(go()) == [b'{"a":1}\n', b'{"b":2}\n', b""]


def test_reader_returns_empty_bytes_at_eof():
    async def go():
        r = ThreadStdinReader(io.BytesIO(b""))
        return await r.readline()

    assert asyncio.run(go()) == b""


def test_writer_writes_and_drains():
    buf = io.BytesIO()

    async def go():
        w = ThreadStdoutWriter(buf)
        w.write(b'{"jsonrpc":"2.0"}\n')
        await w.drain()

    asyncio.run(go())
    assert buf.getvalue() == b'{"jsonrpc":"2.0"}\n'


# ── the actual regression: the Windows Proactor loop ───────────────────────


@pytest.mark.skipif(sys.platform != "win32", reason="Proactor loop is Windows-only")
def test_reader_works_on_the_proactor_loop():
    """The exact loop `connect_read_pipe` fails on must still read fine."""
    loop = asyncio.ProactorEventLoop()  # type: ignore[attr-defined]
    try:
        r = ThreadStdinReader(io.BytesIO(b"line-1\nline-2\n"))
        assert loop.run_until_complete(r.readline()) == b"line-1\n"
        assert loop.run_until_complete(r.readline()) == b"line-2\n"
    finally:
        loop.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Proactor loop is Windows-only")
def test_connect_read_pipe_on_stdin_really_does_fail_here():
    """Prove the bug is real on this platform, so the fix is not cargo-culted.

    Verified mechanism (CPython 3.12, Windows): `connect_read_pipe` itself
    *succeeds*, then `_ProactorReadPipeTransport._loop_reading` raises
    ``OSError: [WinError 6] The handle is invalid`` when registering stdin with
    the IOCP — and its own error path then dies on a missing ``_empty_waiter``
    attribute. So the read future is NEVER resolved: the observable symptom is a
    silent HANG, not a clean exception. That is exactly why the bug survived —
    an exception would have been obvious.

    If a future Python fixes this, the read completes and this test fails loudly,
    rather than the codebase carrying a workaround nobody rechecks.
    """
    loop = asyncio.ProactorEventLoop()  # type: ignore[attr-defined]

    async def attempt():
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
        )
        return await asyncio.wait_for(reader.readline(), timeout=2.0)

    try:
        # A hang is the real symptom; wait_for turns it into TimeoutError.
        with pytest.raises(asyncio.TimeoutError):
            loop.run_until_complete(attempt())
    finally:
        loop.close()


# ── no stdio server may regress to the broken call ────────────────────────


@pytest.mark.parametrize("rel", STDIO_SERVERS)
def test_no_stdio_server_uses_connect_read_pipe_on_stdin(rel):
    src = _repo_file(rel).read_text(encoding="utf-8")
    offenders = [
        ln.strip()
        for ln in src.splitlines()
        if "connect_read_pipe" in ln and not ln.lstrip().startswith("#")
    ]
    assert not offenders, (
        f"{rel} calls connect_read_pipe outside a comment — it crashes on "
        f"Windows' Proactor loop. Use adk.stdio_compat.ThreadStdinReader.\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("rel", STDIO_SERVERS)
def test_every_stdio_server_uses_the_shared_reader(rel):
    src = _repo_file(rel).read_text(encoding="utf-8")
    assert re.search(r"ThreadStdinReader", src), (
        f"{rel} must read stdin via adk.stdio_compat.ThreadStdinReader"
    )


def test_acp_server_aliases_resolve_to_the_shared_implementation():
    """The ACP server's private names are kept as aliases, not copies."""
    import adk.acp_server as srv

    assert srv._ThreadStdinReader is ThreadStdinReader
    assert srv._ThreadStdoutWriter is ThreadStdoutWriter
