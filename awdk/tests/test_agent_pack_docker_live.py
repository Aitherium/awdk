"""The Supervisor's `docker` runtime, proven against a REAL container.

A real customer pack is a container, and this runtime had never been exercised
against one — only its argv construction was unit-tested. Doing so found a
structural bug: `docker run` WITHOUT `-i` does not attach the container's stdin,
so a stdio-protocol agent (acp, mcp) in a container silently never receives a
request. Measured against `python:3.12-slim`:

    docker run --rm     <img> ...  ->  stdout=b''          (stdin discarded)
    docker run --rm -i  <img> ...  ->  b'echo:HELLO...'    (round-trips)

Same defect class as forgetting `stdin=PIPE` on the child process.

The argv tests always run. The container tests are skipped (never failed) when
Docker is unavailable, so CI without a daemon stays green.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from adk.agent_pack import AgentPackManifest, RuntimeConfig, Supervisor

IMAGE = "python:3.12-slim"

# A tiny in-container stdio echo agent: reads lines, echoes them back.
# Deliberately ONE line with no newlines: runtime.cmd goes through shlex.split,
# and a repr containing "\n" would reach `python -c` as a literal backslash-n
# (a SyntaxError in the container) rather than a newline.
ECHO = "import sys; [print('echo:' + l.strip(), flush=True) for l in sys.stdin]"


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        if subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, timeout=20,
        ).returncode != 0:
            return False
        # The image must already be local — a test must not pull hundreds of MB.
        out = subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=20
        )
        return out.returncode == 0
    except Exception:
        return False


docker_required = pytest.mark.skipif(
    not _docker_ok(), reason=f"docker daemon or local {IMAGE} unavailable"
)


def _manifest(**runtime_kw) -> AgentPackManifest:
    return AgentPackManifest(
        id="docker-echo",
        name="Docker Echo",
        version="1.0.0",
        framework="custom",
        protocol="acp",
        entrypoint="echo",
        runtime=RuntimeConfig(**runtime_kw),
    )


# ── argv construction (no daemon needed) ───────────────────────────────────


def test_docker_argv_includes_dash_i():
    """Regression guard: dropping -i makes every containerized stdio agent mute."""
    sup = Supervisor()
    cmd = sup._build_command(_manifest(type="docker", image=IMAGE))
    assert cmd[:4] == ["docker", "run", "--rm", "-i"], cmd


def test_docker_argv_splits_the_command():
    """`cmd` appended whole becomes one argv element docker tries to exec."""
    sup = Supervisor()
    cmd = sup._build_command(_manifest(type="docker", image=IMAGE, cmd="python -u app.py"))
    assert cmd[-3:] == ["python", "-u", "app.py"], cmd
    assert "python -u app.py" not in cmd, "cmd must be shlex-split, not appended whole"


def test_docker_argv_does_not_leak_env_into_argv():
    """Credentials must never ride in argv (visible to ps/docker inspect)."""
    sup = Supervisor()
    cmd = sup._build_command(_manifest(type="docker", image=IMAGE))
    assert "-e" not in cmd


def test_docker_requires_an_image():
    sup = Supervisor()
    with pytest.raises(ValueError, match="image required"):
        sup._build_command(_manifest(type="docker", cmd="whatever"))


def test_node_argv_splits_the_command():
    sup = Supervisor()
    cmd = sup._build_command(_manifest(type="node", cmd="server.js --port 9"))
    assert cmd == ["node", "server.js", "--port", "9"], cmd


# ── the real container ─────────────────────────────────────────────────────


@docker_required
def test_supervisor_spawns_a_real_container_and_stdin_round_trips():
    """Spawn through the Supervisor and prove a request reaches the container."""

    async def go():
        sup = Supervisor()
        manifest = _manifest(type="docker", image=IMAGE, cmd=f"python -u -c {ECHO!r}")
        handle = await sup.spawn(manifest)
        try:
            proc = handle.process
            assert proc is not None and proc.stdin is not None
            proc.stdin.write(b"HELLO-FROM-HOST\n")
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=90)
            return line
        finally:
            await handle.terminate()

    assert asyncio.run(go()).strip() == b"echo:HELLO-FROM-HOST", (
        "stdin did not reach the container — is `-i` still on the docker run?"
    )


@docker_required
def test_dropping_dash_i_really_does_break_it():
    """Prove the fix is necessary, not cargo-culted: same container, no -i."""

    async def go(with_i: bool):
        cmd = ["docker", "run", "--rm"] + (["-i"] if with_i else []) + [
            IMAGE, "python", "-u", "-c", ECHO
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            proc.stdin.write(b"PING\n")
            await proc.stdin.drain()
            return await asyncio.wait_for(proc.stdout.readline(), timeout=45)
        except asyncio.TimeoutError:
            return b""
        finally:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass

    assert asyncio.run(go(with_i=True)).strip() == b"echo:PING"
    assert asyncio.run(go(with_i=False)) == b"", (
        "docker now forwards stdin without -i; the workaround can be revisited"
    )


@docker_required
def test_terminate_stops_the_spawned_container_process():
    """`--rm` plus terminate must not leave the docker client running."""

    async def go():
        sup = Supervisor()
        manifest = _manifest(type="docker", image=IMAGE, cmd=f"python -u -c {ECHO!r}")
        handle = await sup.spawn(manifest)
        assert handle.is_running()
        await handle.terminate()
        return handle.is_running()

    assert asyncio.run(go()) is False, "terminate must reap the docker run process"
