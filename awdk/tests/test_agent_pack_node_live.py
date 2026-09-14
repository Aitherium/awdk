"""The Supervisor's `node` runtime, proven against a REAL node process.

The docker runtime turned out to be structurally broken when finally exercised
(`docker run` without `-i` discards stdin), so the `node` runtime — argv-tested
only — deserved the same treatment rather than an assumption that it worked.

Uses `tests/fixtures/node_echo_agent.js`, a dependency-free stdio echo agent, so
no npm install is required. Skipped (never failed) when node is absent.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from adk.agent_pack import AgentPackManifest, RuntimeConfig, Supervisor

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "node_echo_agent.js"


def _node_ok() -> bool:
    if not shutil.which("node") or not FIXTURE.exists():
        return False
    try:
        return subprocess.run(
            ["node", "--version"], capture_output=True, timeout=20
        ).returncode == 0
    except Exception:
        return False


node_required = pytest.mark.skipif(not _node_ok(), reason="node or fixture unavailable")


def _manifest(cmd: str) -> AgentPackManifest:
    return AgentPackManifest(
        id="node-echo",
        name="Node Echo",
        version="1.0.0",
        framework="custom",
        protocol="acp",
        entrypoint="echo",
        runtime=RuntimeConfig(type="node", cmd=cmd),
    )


# ── argv construction (no node needed) ─────────────────────────────────────


def test_node_argv_is_split_not_one_blob():
    sup = Supervisor()
    cmd = sup._build_command(_manifest("server.js --port 9"))
    assert cmd == ["node", "server.js", "--port", "9"], cmd


def test_node_argv_preserves_a_quoted_path_with_spaces():
    """shlex must keep a quoted path together — the bug the python path had."""
    sup = Supervisor()
    cmd = sup._build_command(_manifest('"my agent/server.js" --x'))
    assert cmd == ["node", "my agent/server.js", "--x"], cmd


def test_node_requires_a_cmd():
    with pytest.raises(ValueError, match="cmd required"):
        RuntimeConfig(type="node")


# ── the real process ───────────────────────────────────────────────────────


@node_required
def test_supervisor_spawns_real_node_and_stdin_round_trips():
    """The whole point: a request must actually reach the node agent."""

    async def go():
        sup = Supervisor()
        manifest = _manifest(f'"{FIXTURE}"')
        handle = await sup.spawn(manifest)
        try:
            proc = handle.process
            assert proc is not None and proc.stdin is not None, "stdin must be piped"
            banner = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
            proc.stdin.write(b"HELLO-FROM-HOST\n")
            await proc.stdin.drain()
            reply = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
            return banner, reply
        finally:
            await handle.terminate()

    banner, reply = asyncio.run(go())
    assert banner.startswith(b"ready:"), banner
    assert reply.strip() == b"echo:HELLO-FROM-HOST", reply


@node_required
def test_runtime_args_reach_the_node_process():
    """`runtime.args` must actually arrive as argv, not be dropped."""

    async def go():
        sup = Supervisor()
        manifest = AgentPackManifest(
            id="node-echo-args",
            name="Node Echo Args",
            version="1.0.0",
            framework="custom",
            protocol="acp",
            entrypoint="echo",
            runtime=RuntimeConfig(type="node", cmd=f'"{FIXTURE}"', args=["--alpha", "--beta"]),
        )
        handle = await sup.spawn(manifest)
        try:
            return await asyncio.wait_for(handle.process.stdout.readline(), timeout=60)
        finally:
            await handle.terminate()

    assert asyncio.run(go()).strip() == b"ready:--alpha,--beta"


@node_required
def test_terminate_reaps_the_node_process():
    async def go():
        sup = Supervisor()
        handle = await sup.spawn(_manifest(f'"{FIXTURE}"'))
        assert handle.is_running()
        await handle.terminate()
        return handle.is_running()

    assert asyncio.run(go()) is False


@node_required
def test_a_missing_script_fails_loudly_not_silently():
    """A bad cmd must surface, not leave a zombie handle that looks healthy."""

    async def go():
        sup = Supervisor()
        handle = await sup.spawn(_manifest('"does-not-exist-anywhere.js"'))
        try:
            # node exits non-zero; the handle must report it rather than pretend.
            await asyncio.wait_for(handle.process.wait(), timeout=60)
            return handle.is_running(), handle.process.returncode
        finally:
            await handle.terminate()

    running, code = asyncio.run(go())
    assert running is False
    assert code not in (0, None), f"a missing script must exit non-zero, got {code!r}"
