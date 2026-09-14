"""ACP tool pack — drive external Agent Client Protocol agents.

Exposes the external-agent (driver) direction as ``acp_*`` agent tools. Each
external ACP agent (claude-agent-acp, codex-acp, gemini-cli, ...) is a stdio
subprocess speaking ACP JSON-RPC; the client is ``adk.acp.ACPClient`` (v2).

Sessions survive across tool calls: connected clients are cached by
``(command, args)`` for the agent's lifetime, so ``acp_prompt`` can reuse a
``session_id`` to continue the same conversation instead of re-spawning every
turn. ``acp_close`` ends the session and frees the subprocess.

The server direction (an AitherOS agent served to ACP editors) lives in the
``adk acp serve`` CLI; the model-backend direction in ``adk backend add acp``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
from pathlib import Path
from typing import Any

logger = logging.getLogger("acp_pack")

PACK_ID = "acp"

# Long-lived ACP clients keyed by (command, tuple(args)) so external-agent
# sessions survive across tool calls within the agent's lifetime.
_CLIENTS: dict[tuple, Any] = {}

_AGENTS_DIR = Path(__file__).parent / "agents"

# How long to keep draining trailing session/update notifications after the
# prompt response settles (tool completions that land late are not dropped).
_PROMPT_DRAIN_SECS = 30.0


def _split_args(args: Any) -> list[str]:
    """Accept an args string ("-p x --flag") or a list; normalize to a list.

    ``posix=False`` is deliberate: the default ``shlex.split`` treats ``\\`` as
    an escape, so a Windows path like ``C:\\Users\\...\\agent.py`` is silently
    mangled to ``C:Users...agent.py`` and the subprocess never starts — a hang
    with no error. ``posix=False`` splits on whitespace and keeps backslashes.
    """
    if args is None:
        return []
    if isinstance(args, str):
        return shlex.split(args, posix=False) if args.strip() else []
    if isinstance(args, (list, tuple)):
        return [str(a) for a in args]
    return [str(args)]


def _agents_manifest() -> list[dict]:
    """Load the bundled external ACP agent manifests (agents/*.yaml)."""
    out: list[dict] = []
    if not _AGENTS_DIR.is_dir():
        return out
    for f in sorted(_AGENTS_DIR.glob("*.yaml")):
        try:
            import yaml

            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            out.append({"id": f.stem, **data})
        except Exception as exc:  # noqa: BLE001 — a bad manifest must not hide the rest
            out.append({"id": f.stem, "error": str(exc)})
    return out


async def _get_client(command: str, args: list[str]) -> tuple:
    """Return (cache_key, connected ACPClient), connecting on first use.

    The cache is LOOP-AWARE: a client's read task lives on the event loop that
    spawned it, so reusing it from a different loop (a tool invoked from a
    fresh ``asyncio.run``) would never resolve its responses and HANG. On a
    loop change the stale client is dropped and a fresh one is connected —
    sessions do not survive the switch, and we say so rather than silently
    returning a dead handle.
    """
    from adk.acp import ACPClient

    key = (command, tuple(args))
    loop = asyncio.get_running_loop()
    entry = _CLIENTS.get(key)
    if entry is not None:
        stored_loop, client = entry
        if stored_loop is loop:
            return key, client
        logger.warning(
            "acp pack: dropping agent %r from a previous event loop; "
            "its sessions do not survive a loop change — reconnecting fresh",
            command,
        )
        # The stale client's transport lives on a CLOSED loop: awaiting its
        # graceful disconnect can hang (old-loop futures never resolve here),
        # so hard-close instead — kill the subprocess, close the writer, drop.
        try:
            proc = getattr(client, "subprocess", None)
            if proc is not None and proc.returncode is None:
                proc.kill()
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            logger.debug("acp pack: stale subprocess kill failed: %s", exc)
        try:
            writer = getattr(client, "_writer", None)
            if writer is not None:
                writer.close()
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            logger.debug("acp pack: stale writer close failed: %s", exc)
        _CLIENTS.pop(key, None)
    client = ACPClient(command=command, args=args)
    try:
        await client.connect()
        await client.initialize()
    except Exception:
        try:
            await client.disconnect()
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            logger.debug("acp pack: failed client disconnect: %s", exc)
        raise
    _CLIENTS[key] = (loop, client)
    return key, client


def acp_list_agents() -> dict:
    """List the bundled external ACP agent manifests (claude-agent-acp,
    codex-acp, gemini-cli, ...). Read-only; no subprocess is spawned."""
    return {"agents": _agents_manifest()}


async def acp_connect(command: str, args: Any = None) -> dict:
    """Spawn an external ACP agent (e.g. ``command="claude"`` for
    claude-agent-acp) and open a fresh session. Returns a ``session_id`` to
    pass to acp_prompt so the conversation can continue."""
    if not command or not str(command).strip():
        return {"error": "command is required (e.g. 'claude' for claude-agent-acp)"}
    key, client = await _get_client(str(command).strip(), _split_args(args))
    sid = await client.create_session(cwd=os.getcwd())
    name = client._capabilities.agent_name if client._capabilities else command
    return {"ok": True, "agent": name, "session_id": sid}


async def acp_prompt(
    command: str, args: Any = None, message: str = "", session_id: str = ""
) -> dict:
    """Send one turn to an external ACP agent. Reuse ``session_id`` (from
    acp_connect or a prior acp_prompt) to continue the same conversation;
    omit it to open a fresh session. Returns the agent's reply plus the
    session_id for the next turn."""
    if not message or not str(message).strip():
        return {"error": "message is required"}
    if not command or not str(command).strip():
        return {"error": "command is required (e.g. 'claude' for claude-agent-acp)"}
    key, client = await _get_client(str(command).strip(), _split_args(args))
    try:
        sid = session_id or await client.create_session(cwd=os.getcwd())
        result = await client.prompt(sid, str(message), drain_timeout=_PROMPT_DRAIN_SECS)
        return {"session_id": sid, "reply": result.text}
    except Exception as exc:  # noqa: BLE001 — surface the failure loud, never silently
        return {"error": f"{type(exc).__name__}: {exc}"}


async def acp_close(command: str, args: Any = None, session_id: str = "") -> dict:
    """End a session with an external ACP agent and free its subprocess. Safe to
    call when no agent is running — returns ok with a note."""
    key = (str(command).strip(), tuple(_split_args(args)))
    entry = _CLIENTS.get(key)
    if entry is None:
        return {"ok": True, "note": "no such agent running"}
    client = entry[1]
    try:
        if session_id:
            await client.close_session(session_id)
    finally:
        try:
            await client.disconnect()
        finally:
            _CLIENTS.pop(key, None)
    return {"ok": True}


_TOOLS = [
    (acp_list_agents, "acp_list_agents",
     "List the bundled external ACP agent manifests."),
    (acp_connect, "acp_connect",
     "Spawn an external ACP agent and open a session; returns its session_id."),
    (acp_prompt, "acp_prompt",
     "Send one turn to an external ACP agent; reuse session_id to continue."),
    (acp_close, "acp_close",
     "End a session with an external ACP agent and free its subprocess."),
]


def register(registry) -> int:
    """Register the acp_* tools on the agent's tool registry."""
    n = 0
    for fn, name, desc in _TOOLS:
        try:
            registry.register(fn, name=name, description=desc)
            n += 1
        except TypeError:
            # Older registries take only the fn (name from __name__, desc from docstring).
            try:
                registry.register(fn)
                n += 1
            except Exception as exc:  # noqa: BLE001 — one bad tool must not block the pack
                logger.debug("acp pack: skip %s (%s)", name, exc)
        except Exception as exc:  # noqa: BLE001
            logger.debug("acp pack: skip %s (%s)", name, exc)
    logger.info("ACP pack registered %d acp_* tools", n)
    return n
