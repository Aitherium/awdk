"""Inbound MCP — the agent as a CLIENT of servers the USER configures.

WHY THIS EXISTS
===============
Every MCP path in this package pointed outward. `MCPBridge` is documented as a
"Client for AitherOS MCP gateway at mcp.aitherium.com" and `connect_mcp()`
defaults to that host; every `mcpServers` reference in `cli.py` WRITES us into
somebody else's editor config (Claude Code, Cursor, opencode); and
`mcp_stdio.py` runs the agent AS an MCP server for those editors to call.

So the answer to "can I extend this with my own MCP server?" was no. Not
"undocumented" — absent. A self-hoster could add a Python tool pack
(`.toolpack.yaml`, `AITHER_TOOLPACK_DIRS`) and nothing else, which means the
agent's capabilities were bounded by what we shipped.

This module is the missing direction: read the user's own server list, connect
to each, and register their tools alongside the built-ins.

CONFIG FORMAT
=============
Deliberately the `mcpServers` shape Claude Code and Cursor already use, because
a self-hoster almost certainly has one of these files already and inventing a
fourth format would be a tax with no payer:

    {
      "mcpServers": {
        "sqlite":  {"command": "uvx", "args": ["mcp-server-sqlite", "--db", "x.db"]},
        "weather": {"url": "https://example.com/mcp", "headers": {"X-Key": "..."}},
        "off":     {"command": "...", "disabled": true}
      }
    }

Discovered in this order, first hit wins — the same "nearest config wins"
discipline the tool-pack loader uses:

    1. $AITHER_MCP_CONFIG            (explicit; a missing file here is an ERROR,
                                      never a silent fall-through to the next)
    2. ./.mcp.json, ./mcp.json       (project-local)
    3. ~/.aither/mcp.json            (user-global)

NAMING
======
Tools are exposed as ``mcp__<server>__<tool>``. Two servers routinely ship a
tool called `search`, and a bare merge would let whichever connected last
silently shadow the other — the caller would see one tool, get the other's
answer, and have nothing to look at. The prefix is also what Claude Code shows,
so the names are already familiar.

FAILURE DISCIPLINE
==================
A user's MCP server is somebody else's process on somebody else's machine, so
it WILL be down sometimes. Two rules, both learned the hard way in this repo:

* a server that cannot be reached must not break the agent — the other servers
  and every built-in tool keep working;
* and it must not fail QUIETLY. `connect_all()` returns the failures, the
  registrar logs them at WARNING with the reason, and a tool belonging to a
  dead server returns a string that NAMES the server rather than an empty
  result. An empty result is indistinguishable from "nothing matched", which is
  how a dead integration passes for a working one.

SECURITY
========
A stdio server is an arbitrary command from a config file, exactly as in Claude
Code — so this is opt-in by the config existing at all, never by discovery of
something that happens to be lying around. Nothing here reads a server list out
of a prompt, a tool result or any other model-influenceable input.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("adk.mcp_client")

#: Protocol version we advertise in `initialize`. Servers negotiate down.
PROTOCOL_VERSION = "2024-11-05"

#: How long to wait for one JSON-RPC round trip on a stdio server.
DEFAULT_TIMEOUT = 30.0

#: How long to wait for the initial handshake. Separate from DEFAULT_TIMEOUT and
#: longer on purpose: `uvx`/`npx` servers routinely DOWNLOAD themselves on first
#: run, and a 30s cap turns "installing" into "your server is broken".
HANDSHAKE_TIMEOUT = 120.0

CONFIG_ENV = "AITHER_MCP_CONFIG"


class MCPClientError(RuntimeError):
    """A user MCP server could not be reached or answered badly."""


@dataclass
class ServerSpec:
    """One entry from the user's `mcpServers` block."""

    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    disabled: bool = False
    cwd: str = ""

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"


def _as_str_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def parse_config(data: dict, *, source: str = "<memory>") -> dict[str, ServerSpec]:
    """`mcpServers` block -> specs. Malformed entries are skipped LOUDLY.

    A skipped entry is logged with its name and the reason, never dropped in
    silence: a typo'd key that removes a server the user believes they
    configured is indistinguishable from a server that is merely down.
    """
    servers = data.get("mcpServers")
    if servers is None:
        # Accept a bare mapping too — some people write the servers at top level.
        servers = data if all(isinstance(v, dict) for v in data.values()) else None
    if not isinstance(servers, dict):
        logger.warning("%s: no usable 'mcpServers' object", source)
        return {}

    out: dict[str, ServerSpec] = {}
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            logger.warning("%s: server %r is not an object — skipped", source, name)
            continue
        url = str(raw.get("url") or raw.get("serverUrl") or "")
        command = str(raw.get("command") or "")
        if not url and not command:
            logger.warning(
                "%s: server %r declares neither 'command' nor 'url' — skipped",
                source, name)
            continue
        if url and command:
            # Ambiguous rather than clever: guessing would make one of the two
            # settings silently inert, and the user cannot see which.
            logger.warning(
                "%s: server %r declares BOTH 'command' and 'url'; using url and "
                "ignoring command", source, name)
        out[str(name)] = ServerSpec(
            name=str(name),
            command=command,
            args=[str(a) for a in (raw.get("args") or [])],
            env=_as_str_map(raw.get("env")),
            url=url,
            headers=_as_str_map(raw.get("headers")),
            disabled=bool(raw.get("disabled", False)),
            cwd=str(raw.get("cwd") or ""),
        )
    return out


def config_candidates(cwd: Path | None = None) -> list[Path]:
    """Where a config may live, in precedence order."""
    here = Path(cwd) if cwd else Path.cwd()
    return [
        here / ".mcp.json",
        here / "mcp.json",
        Path.home() / ".aither" / "mcp.json",
    ]


def load_config(
    explicit: str | Path | None = None,
    *,
    cwd: Path | None = None,
) -> tuple[dict[str, ServerSpec], str]:
    """Find and parse the user's server list. Returns (specs, source).

    An EXPLICIT path (argument or $AITHER_MCP_CONFIG) that does not exist raises
    rather than falling through to the next candidate. Someone who names a file
    is telling you which file they mean; quietly using a different one — or
    none — is how "my servers aren't loading" becomes unanswerable.
    """
    named = explicit or os.environ.get(CONFIG_ENV) or ""
    if named:
        p = Path(named).expanduser()
        if not p.is_file():
            raise MCPClientError(
                f"{CONFIG_ENV}={named!r} does not exist. Point it at a file with an "
                "'mcpServers' object, or unset it to use ./.mcp.json or "
                "~/.aither/mcp.json.")
        return _read(p), str(p)

    for p in config_candidates(cwd):
        if p.is_file():
            return _read(p), str(p)
    return {}, ""


def _read(path: Path) -> dict[str, ServerSpec]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MCPClientError(f"could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MCPClientError(f"{path}: top level must be an object")
    return parse_config(data, source=str(path))


# ─────────────────────────────────────────────────────────────────────────────
# stdio transport — the one that did not exist
# ─────────────────────────────────────────────────────────────────────────────

class StdioMCPClient:
    """Newline-delimited JSON-RPC 2.0 over a subprocess's stdin/stdout.

    `mcp_stdio.py` implements the SERVER half of this protocol (adk speaking to
    Claude Code). This is the client half, which nothing here had — and it is
    the transport most community MCP servers use, so without it "bring your own
    MCP server" would have meant "bring your own HTTP MCP server", which is a
    small minority of them.
    """

    def __init__(self, spec: ServerSpec, timeout: float = DEFAULT_TIMEOUT):
        self.spec = spec
        self._timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        exe = shutil.which(self.spec.command) or self.spec.command
        env = {**os.environ, **self.spec.env}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                exe, *self.spec.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # Kept SEPARATE, never merged into stdout: many servers log
                # chatter to stderr, and folding it into the protocol stream
                # corrupts every response with something that is not JSON.
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.spec.cwd or None,
            )
        except (OSError, ValueError) as exc:
            raise MCPClientError(
                f"{self.spec.name}: could not start {self.spec.command!r}: {exc}"
            ) from exc

    async def _rpc(self, method: str, params: dict | None = None,
                   *, timeout: float | None = None,
                   notify: bool = False) -> dict:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise MCPClientError(f"{self.spec.name}: not started")

        # One request in flight at a time. This transport correlates by reading
        # the next line, so two concurrent callers would read each other's
        # replies — and JSON-RPC ids would make that look like a server bug.
        async with self._lock:
            msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            if not notify:
                self._next_id += 1
                msg["id"] = self._next_id

            line = json.dumps(msg) + "\n"
            try:
                self._proc.stdin.write(line.encode("utf-8"))
                await self._proc.stdin.drain()
            except (OSError, BrokenPipeError) as exc:
                raise MCPClientError(
                    f"{self.spec.name}: server closed its input ({exc}); "
                    f"{await self._stderr_hint()}") from exc

            if notify:
                return {}

            deadline = timeout if timeout is not None else self._timeout
            while True:
                try:
                    raw = await asyncio.wait_for(
                        self._proc.stdout.readline(), timeout=deadline)
                except asyncio.TimeoutError as exc:
                    raise MCPClientError(
                        f"{self.spec.name}: no reply to {method} within "
                        f"{deadline:.0f}s; {await self._stderr_hint()}") from exc
                if not raw:
                    raise MCPClientError(
                        f"{self.spec.name}: server exited during {method}; "
                        f"{await self._stderr_hint()}")
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except ValueError:
                    # Not protocol traffic. Servers do print banners to stdout
                    # despite the spec; skipping beats failing the whole session
                    # on somebody's startup message.
                    logger.debug("%s: non-JSON stdout line: %.200s",
                                 self.spec.name, text)
                    continue
                # Ignore anything that is not OUR reply: notifications and
                # server->client requests share this stream.
                if "id" not in payload or payload.get("id") != msg.get("id"):
                    continue
                if "error" in payload:
                    err = payload["error"] or {}
                    raise MCPClientError(
                        f"{self.spec.name}: {method} failed: "
                        f"{err.get('message', err)}")
                return payload.get("result") or {}

    async def _stderr_hint(self) -> str:
        """The server's own last words, which is usually the real reason."""
        if self._proc is None or self._proc.stderr is None:
            return "no stderr captured"
        try:
            data = await asyncio.wait_for(self._proc.stderr.read(4096), timeout=0.5)
        except (asyncio.TimeoutError, OSError):
            return "no stderr captured"
        text = data.decode("utf-8", errors="replace").strip()
        return f"stderr: {text[-400:]}" if text else "stderr was empty"

    async def initialize(self) -> dict:
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "awdk", "version": "1"},
            },
            timeout=HANDSHAKE_TIMEOUT,
        )
        # REQUIRED by the spec. Servers that enforce it reject every subsequent
        # call with a message about initialization, which reads as our bug.
        await self._rpc("notifications/initialized", {}, notify=True)
        return result

    async def list_tools(self) -> list[dict]:
        result = await self._rpc("tools/list")
        tools = result.get("tools")
        return [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []

    async def call_tool(self, name: str, arguments: dict) -> str:
        result = await self._rpc("tools/call",
                                 {"name": name, "arguments": arguments or {}})
        return render_tool_result(result)

    async def close(self) -> None:
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        try:
            if proc.stdin:
                proc.stdin.close()
        except (OSError, RuntimeError) as exc:
            # Debug, not warning: a server that already exited closes this
            # pipe for us, and that is the ordinary case. Logged anyway so
            # a shutdown that fails for a REAL reason is not invisible.
            logger.debug('%s: closing stdin: %s', self.spec.name, exc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            # A server that will not exit must not wedge agent shutdown.
            try:
                proc.kill()
            except ProcessLookupError:
                logger.debug('%s: already gone', self.spec.name)
            else:
                # REAP IT. kill() only signals; without a second wait the
                # transport is finalised later by the garbage collector, which
                # on Windows touches a loop that may already be closed and
                # raises `Event loop is closed` from __del__ -- an error with no
                # stack into our code, arriving long after the cause.
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (asyncio.TimeoutError, ProcessLookupError):
                    # A LEAK, and the only place it can be reported. The
                    # process survived SIGKILL or vanished mid-wait; either
                    # way something is still holding resources and silence
                    # here is how that becomes unexplainable later.
                    logger.warning(
                        '%s: did not reap after kill — the process may '
                        'still be running', self.spec.name)


def render_tool_result(result: dict) -> str:
    """MCP `tools/call` result -> the string an agent tool returns.

    The spec's shape is `{"content": [{"type": "text", "text": ...}], "isError": bool}`.
    An error is returned as TEXT rather than raised: the agent recovers from a
    tool that says why it failed, and a raised exception here would abort the
    whole turn over one bad call.
    """
    if not isinstance(result, dict):
        return str(result)
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        elif "text" in item:
            parts.append(str(item["text"]))
        else:
            # Images/resources: name the kind rather than dumping base64 into
            # the transcript, which would blow the context window on one call.
            parts.append(f"[{item.get('type', 'content')}]")
    text = "\n".join(p for p in parts if p)
    if not text:
        # Never an empty string: "the tool returned nothing" and "the tool did
        # not run" must not look the same to the model.
        text = json.dumps(result)[:2000] if result else "(the tool returned no content)"
    if result.get("isError"):
        return f"Error from tool: {text}"
    return text


# ─────────────────────────────────────────────────────────────────────────────
# HTTP transport — thin, because MCPBridge already does this correctly
# ─────────────────────────────────────────────────────────────────────────────

class HttpMCPClient:
    """A user-configured HTTP MCP server, over the existing MCPBridge.

    Not reimplemented: MCPBridge already handles the initialize handshake, the
    `Mcp-Session-Id` header a spec-strict server demands, and the fact that a
    StreamableHTTP reply may arrive as either JSON or an SSE event stream. A
    second implementation of that would drift from it.
    """

    def __init__(self, spec: ServerSpec, timeout: float = DEFAULT_TIMEOUT):
        self.spec = spec
        self._timeout = timeout
        self._bridge = None

    async def start(self) -> None:
        from adk.mcp import MCPBridge  # local: keeps httpx off the import path

        key = ""
        for k, v in self.spec.headers.items():
            if k.lower() == "authorization":
                key = v.split(" ", 1)[-1] if " " in v else v
        self._bridge = MCPBridge(mcp_url=self.spec.url, api_key=key,
                                 timeout=self._timeout)

    async def initialize(self) -> dict:
        return {}

    async def list_tools(self) -> list[dict]:
        if self._bridge is None:
            raise MCPClientError(f"{self.spec.name}: not started")
        tools = await self._bridge.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.parameters or {"type": "object", "properties": {}},
            }
            for t in tools
        ]

    async def call_tool(self, name: str, arguments: dict) -> str:
        if self._bridge is None:
            raise MCPClientError(f"{self.spec.name}: not started")
        return str(await self._bridge.call_tool(name, arguments or {}))

    async def close(self) -> None:
        self._bridge = None


def make_client(spec: ServerSpec, timeout: float = DEFAULT_TIMEOUT):
    return HttpMCPClient(spec, timeout) if spec.transport == "http" \
        else StdioMCPClient(spec, timeout)


# ─────────────────────────────────────────────────────────────────────────────
# The manager
# ─────────────────────────────────────────────────────────────────────────────

def qualified_name(server: str, tool: str) -> str:
    """`mcp__<server>__<tool>` — the spelling Claude Code shows users.

    Non-identifier characters are flattened so the result is a legal tool name
    whatever the server called itself.
    """
    def clean(s: str) -> str:
        return "".join(c if (c.isalnum() or c == "_") else "_" for c in s)
    return f"mcp__{clean(server)}__{clean(tool)}"


class _MCPLoop:
    """One long-lived event loop, on a daemon thread, owning every client.

    An asyncio subprocess transport belongs to the loop that created it. Connect
    on a throwaway loop -- which is what `asyncio.run()` gives you -- and every
    client is attached to a closed loop the moment the call returns: the tools
    register, the count is right, the log says success, and the first real call
    dies with `'NoneType' object has no attribute 'send'`, which names neither
    loops nor MCP.

    So there is exactly one loop and it outlives registration. Daemon thread on
    purpose: a user's MCP server must never be the reason a process refuses to
    exit."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name='adk-mcp-loop', daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def submit(self, coro):
        """Schedule on the owned loop; returns a concurrent Future."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run(self, coro, timeout: float | None = None):
        """Block until the coroutine finishes on the owned loop."""
        return self.submit(coro).result(timeout)

    def stop(self) -> None:
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if not self._thread.is_alive():
            self._loop.close()


@dataclass
class ConnectedTool:
    server: str
    remote_name: str
    name: str
    description: str
    parameters: dict


class UserMCPManager:
    """Connect to every configured server and merge their tools."""

    def __init__(self, specs: dict[str, ServerSpec], timeout: float = DEFAULT_TIMEOUT,
                 own_loop: bool = True):
        self.specs = specs
        self._timeout = timeout
        # Created eagerly so connect and call cannot end up on different loops.
        # `own_loop=False` is for tests that drive the manager inside their own
        # asyncio.run() and close it themselves.
        self._owned: _MCPLoop | None = _MCPLoop() if own_loop else None
        self.clients: dict[str, Any] = {}
        self.tools: list[ConnectedTool] = []
        #: server name -> why it is not usable. Read by the registrar so a
        #: failure is REPORTED rather than merely absent.
        self.failures: dict[str, str] = {}

    async def connect_all(self) -> list[ConnectedTool]:
        # A SNAPSHOT. Each iteration awaits, and a concurrent caller adding
        # or removing a server at that yield point raises 'changed size
        # during iteration' -- which the caller swallows, silently skipping
        # every server after this one. The agent then has fewer tools than
        # the config asked for, with nothing to look at.
        for name, spec in list(self.specs.items()):
            if spec.disabled:
                logger.info("mcp: %s is disabled in config — skipped", name)
                continue
            try:
                client = make_client(spec, self._timeout)
                await client.start()
                await client.initialize()
                raw = await client.list_tools()
            except Exception as exc:  # noqa: BLE001 - one bad server, not all
                # BROAD ON PURPOSE. This is somebody else's process: it can
                # fail in ways no enumerated list predicts, and one server's
                # creativity must not take the agent down with it.
                self.failures[name] = str(exc)
                logger.warning("mcp: %s unavailable — %s", name, exc)
                continue

            self.clients[name] = client
            added = 0
            for t in raw:
                tool_name = str(t.get("name") or "")
                if not tool_name:
                    continue
                self.tools.append(ConnectedTool(
                    server=name,
                    remote_name=tool_name,
                    name=qualified_name(name, tool_name),
                    description=str(t.get("description") or f"{name} tool {tool_name}"),
                    parameters=t.get("inputSchema") or t.get("input_schema")
                    or {"type": "object", "properties": {}},
                ))
                added += 1
            logger.info("mcp: %s connected (%s) — %d tool(s)",
                        name, spec.transport, added)
        return self.tools

    async def call(self, server: str, remote_name: str, arguments: dict) -> str:
        client = self.clients.get(server)
        if client is None:
            why = self.failures.get(server, "it was never connected")
            # NAMES THE SERVER. An empty string here is indistinguishable from
            # "nothing matched", which is how a dead integration passes for a
            # working one.
            return (f"MCP server {server!r} is unavailable: {why}. "
                    f"The tool was not run.")
        try:
            return await client.call_tool(remote_name, arguments)
        except Exception as exc:  # noqa: BLE001
            return f"MCP server {server!r} failed running {remote_name!r}: {exc}"

    # ---- sync entry points, for callers that are not async -------------

    def connect_all_sync(self) -> list[ConnectedTool]:
        if self._owned is None:
            raise MCPClientError('manager was built without an owned loop')
        return self._owned.run(self.connect_all())

    async def call_from_any_loop(self, server: str, remote_name: str,
                                 arguments: dict) -> str:
        """Await a call that RUNS on the owned loop, from any other loop.

        `wrap_future` rather than `.result()`: blocking the agent\'s loop on a
        remote tool would stall every other concurrent turn for its full
        duration."""
        if self._owned is None:
            return await self.call(server, remote_name, arguments)
        fut = self._owned.submit(self.call(server, remote_name, arguments))
        return await asyncio.wrap_future(fut)

    def close_sync(self) -> None:
        if self._owned is None:
            return
        try:
            self._owned.run(self.close(), timeout=10)
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning('mcp: shutdown did not complete cleanly: %s', exc)
        self._owned.stop()
        self._owned = None

    async def close(self) -> None:
        # Snapshot, same reason as connect_all -- and close() clears the
        # dict at the end, so iterating it live is a mutation during its own
        # loop.
        for name, client in list(self.clients.items()):
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001
                # One server refusing to shut down must not strand the
                # others, but it is a leaked process and must be named.
                logger.warning('mcp: %s did not shut down: %s', name, exc)
        self.clients.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────

def _make_handler(manager: UserMCPManager, tool: ConnectedTool):
    async def _call(**kwargs) -> str:
        # Onto the OWNED loop, always. The agent may run this from any loop --
        # or from a thread with none -- and the client only works on the loop
        # that created its subprocess.
        return await manager.call_from_any_loop(
            tool.server, tool.remote_name, kwargs)
    _call.__name__ = tool.name
    _call.__doc__ = tool.description
    return _call


def register_user_mcp_tools(agent, *, config: str | Path | None = None,
                            timeout: float = DEFAULT_TIMEOUT) -> int:
    """Register the user's own MCP tools on an agent. Returns how many.

    Returns 0 when no config exists, which is the ordinary case and not an
    error: most agents have no user MCP servers, and treating their absence as
    a problem would make every clean run noisy.
    """
    try:
        specs, source = load_config(config)
    except MCPClientError as exc:
        # A BROKEN config is loud. It means the user tried and it did not work,
        # which is the opposite of having no config at all.
        logger.warning("mcp: %s", exc)
        return 0
    if not specs:
        return 0

    manager = UserMCPManager(specs, timeout=timeout)
    try:
        tools = manager.connect_all_sync()
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp: could not connect user servers from %s — %s",
                       source, exc)
        return 0

    registry = getattr(agent, "_tools", None)
    if registry is None:
        logger.warning("mcp: agent has no tool registry — %d tool(s) dropped",
                       len(tools))
        return 0

    count = 0
    for tool in tools:
        existing = getattr(registry, "_tools", {})
        if tool.name in existing:
            # Cannot happen between two MCP servers (the prefix makes names
            # unique) but CAN against a built-in. Refusing beats shadowing: a
            # built-in silently replaced by a stranger's tool of the same name
            # is a capability swap nobody can see.
            logger.warning("mcp: %s already registered — %s/%s not added",
                           tool.name, tool.server, tool.remote_name)
            continue
        try:
            registry.register(
                _make_handler(manager, tool),
                name=tool.name,
                description=tool.description,
            )
            # The JSON Schema the SERVER published, not one inferred from a
            # Python signature: the handler takes **kwargs, so inference would
            # describe every tool as taking nothing.
            registered = getattr(registry, "_tools", {}).get(tool.name)
            if registered is not None:
                registered.parameters = tool.parameters
            count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("mcp: could not register %s — %s", tool.name, exc)

    if manager.failures:
        logger.warning("mcp: %d of %d server(s) unavailable: %s",
                       len(manager.failures), len(specs),
                       ", ".join(sorted(manager.failures)))
    logger.info("mcp: %d user tool(s) from %s", count, source or "(no config)")
    # Held so the connections outlive this call; without it the stdio
    # subprocesses would be garbage-collected and every tool would fail on
    # first use, long after this function reported success.
    setattr(agent, "_user_mcp", manager)
    return count
