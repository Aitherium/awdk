"""GobboNet from inside AitherShell — layer 6 of the product surface.

    /gobbonet                 start it (clones the UI if you have no checkout)
    /gobbonet status          is it actually answering?
    /gobbonet stop            stop it, and prove the port let go
    /gobbonet url             print the address again
    /gobbonet setup           guided onboarding (detect system, grant capabilities)
    /gobbonet capabilities    list and revoke capability grants

`adk gobbonet` blocks in `serve_forever()`, which is right for a terminal you
gave to it and wrong for a shell you are still using. `serve()` hands back the
server without blocking, so the shell keeps it on a daemon thread and stays
usable.

TWO THINGS THIS FILE IS DELIBERATELY CAREFUL ABOUT.

**Nothing heavy is imported at module scope.** `PluginRegistry._load_python_plugin`
wraps the whole load in `except Exception` and logs at DEBUG, so a plugin that
raises on import does not error — the command simply does not exist, with no
message anywhere. Importing the pack (and through it the agent runtime) at the
top would make `/gobbonet` vanish on any machine where that import is unhappy,
and the user would have no way to find out why. Every such import is inside
`run()`, where the failure can be returned as text.

**`status` asks the SERVER, not this module.** Holding "is it running?" in a
variable is how a dead server keeps reporting healthy: the thread can die, the
socket can be taken over, the object stays. It probes `/search/health` — the
path the UI itself calls — and believes the answer.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

# The running server, if this shell started one. Only ever a CACHE of the
# handle needed to stop it — never the source of truth for whether it is up.
#
# 🚨 This dict is NOT reachable from outside, by any import. `_load_python_plugin`
# does `module_from_spec` + `exec_module` and never inserts the result into
# `sys.modules`, so:
#   * `import adk.shell.plugins.builtins.gobbonet` executes this source a SECOND
#     time and yields a different module with a different `_SERVER`;
#   * `sys.modules["aithershell_plugin_gobbonet"]` raises KeyError.
# The only handle is the loaded class's own globals —
# `type(cmd).run.__globals__["_SERVER"]`. Found the honest way: a test seeded the
# imported copy, the command never saw it, and the second guess was wrong too.
_SERVER: Dict[str, Any] = {}

DEFAULT_PORT = 11434


def _probe_sync(port: int, timeout: float) -> bool:
    """Is something answering GobboNet's health check on this port?

    Uses `/search/health` rather than `/health` on purpose: that is the path the
    UI actually calls (`SEARCH_PROXY_URL + '/health'`), and it 404'd for months
    while `/health` answered 200 — so a probe of the root would have reported a
    healthy server whose search was dead. Probe what the app uses.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/search/health", timeout=timeout
        ) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


async def _probe(port: int, timeout: float = 2.0) -> bool:
    """`_probe_sync` off the event loop.

    `urlopen` blocks for DNS, connect and read. On the shell's loop that is not
    "a slow command" — it is the whole REPL frozen, for up to `timeout`, on a
    start that waits 5s. Every keystroke and every other coroutine stops.

    A bare `asyncio.to_thread` is correct HERE, where the house rule is normally
    `EventLoopMonitor.offload`. Two reasons, both specific: this package ships to
    PyPI and may not import `lib.*` at all (it would be a `ModuleNotFoundError`
    on a stranger's machine), and the hazard `offload` exists for does not apply
    — that wrapper carries the caller's loop into the worker so sync code can
    still schedule background work, and the subtree here schedules nothing. It
    opens a socket, reads a status, returns a bool.
    """
    import asyncio

    return await asyncio.to_thread(_probe_sync, port, timeout)


def _setup_note(url: str) -> str:
    """The one manual step, stated where the user is about to hit it.

    There is no `SEARCH_URL` to configure — the UI derives its endpoint from the
    page origin. What it does need is a NON-EMPTY key field, because
    `webSearch()` returns null on an empty one before sending anything, and the
    result is an empty search rather than an error.
    """
    return (
        f"  {url}\n"
        "\n"
        "  Search is wired automatically (the page derives it from its origin).\n"
        "  ONE manual step: Settings -> 'Search API Key (web search)' must not be\n"
        "  empty. Type anything; this pack never reads it. On an empty key GobboNet\n"
        "  returns no results without telling you why.\n"
        "  'Test Connection' beside that field confirms it."
    )


class GobboNetPlugin(SlashCommand):
    name = "gobbonet"
    description = "Run GobboNet locally — keyless search, no account"
    aliases = ["gobbo", "gn"]

    def __init__(self):
        super().__init__(
            name="gobbonet",
            description="Run GobboNet locally — keyless search, no account",
            aliases=["gobbo", "gn"],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        sub = (args[0] if args else "start").lower()
        rest = args[1:] if args else []

        if sub in ("status", "st"):
            return await self._status()
        if sub == "stop":
            return await self._stop()
        if sub == "url":
            if not _SERVER:
                return "Not started from this shell. `/gobbonet` to start it."
            return _setup_note(_SERVER["url"])
        if sub in ("start", "run"):
            return await self._start(rest)
        if sub == "setup":
            return await self._setup(rest)
        if sub == "capabilities":
            return await self._capabilities(rest)
        if sub in ("help", "-h", "--help"):
            return __doc__.split("\n\n")[1]
        # An unknown subcommand is a typo, not a request to start something.
        return f"Unknown: {sub!r}. Try: start | status | stop | url | setup | capabilities"

    # ── subcommands ─────────────────────────────────────────────────────────

    async def _status(self) -> str:
        port = _SERVER.get("port", DEFAULT_PORT)
        alive = await _probe(port)
        if alive and _SERVER:
            return f"running — {_SERVER['url']}\n  ui: {_SERVER['ui']}"
        if alive:
            # Someone else's server, or one from a previous shell. Say so rather
            # than claiming it as ours or reporting nothing there.
            return (
                f"something is answering on :{port}, but this shell did not start it.\n"
                "  `/gobbonet stop` cannot stop it; stop it where it was started."
            )
        if _SERVER:
            return (
                f"NOT answering on :{port}, though this shell started one.\n"
                "  It died. `/gobbonet stop` to clear the handle, then start again."
            )
        return "not running. `/gobbonet` to start it."

    async def _stop(self) -> str:
        if not _SERVER:
            return "nothing to stop (this shell did not start one)."
        port = _SERVER["port"]
        httpd = _SERVER.get("httpd")
        try:
            if httpd is not None:
                httpd.shutdown()
                httpd.server_close()
        except Exception as exc:  # noqa: BLE001 - report, never pretend it stopped
            return f"shutdown raised {type(exc).__name__}: {exc}"
        _SERVER.clear()
        # VERIFY. "I called shutdown" is not "the port is free" — and a half-shut
        # server is exactly what makes the next start fail with a bind error that
        # names nothing.
        if await _probe(port, timeout=1.0):
            return f"called shutdown, but :{port} is STILL answering."
        return f"stopped, and :{port} is free."

    async def _start(self, args: List[str]) -> str:
        port = DEFAULT_PORT
        ui_arg = None
        for i, a in enumerate(args):
            if a in ("--port", "-p") and i + 1 < len(args):
                try:
                    port = int(args[i + 1])
                except ValueError:
                    return f"--port wants a number, got {args[i + 1]!r}"
            elif a in ("--ui", "-u") and i + 1 < len(args):
                ui_arg = args[i + 1]

        if _SERVER:
            return f"already running — {_SERVER['url']}\n  `/gobbonet stop` first."
        if await _probe(port, timeout=1.0):
            return (
                f"port {port} is already answering, and it is not ours.\n"
                f"  Use `/gobbonet --port <other>`, or stop whatever holds :{port}."
            )

        # Imported HERE, not at module scope — see the note at the top of the file.
        try:
            from adk.packs.gobbonet.launch import _clone, _find_existing
            from adk.packs.gobbonet.server import AgenticEngine, serve
        except Exception as exc:  # noqa: BLE001
            return (
                f"could not load the GobboNet pack: {type(exc).__name__}: {exc}\n"
                "  (`pip install -U awdk` if this is an old install)"
            )

        from pathlib import Path

        ui = _find_existing(ui_arg)
        if ui is None:
            if ui_arg:
                return f"no chat.html under {ui_arg} — point --ui at a checkout."
            try:
                ui = _clone(Path.cwd() / "GobboNet")
            except SystemExit as exc:
                return str(exc)

        try:
            httpd = serve(ui, AgenticEngine(exclude_port=port),
                          host="127.0.0.1", port=port)
        except OSError as exc:
            return f"could not bind :{port} — {exc}"

        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        bound = httpd.server_address[1]
        url = f"http://127.0.0.1:{bound}/chat.html"
        _SERVER.update({"httpd": httpd, "port": bound, "url": url, "ui": str(ui)})

        # The requested port is not necessarily the bound one, so read it back
        # rather than echoing what was asked for.
        if not await _probe(bound, timeout=5.0):
            return (
                f"started on :{bound}, but it is not answering its own health check.\n"
                f"  {url}\n"
                "  `/gobbonet status` to re-check."
            )
        return "GobboNet is up.\n" + _setup_note(url)

    async def _setup(self, args: List[str]) -> str:
        """Guided onboarding: detect system, summarize state, offer capability grants.

        The flow reports what is present and missing, then lets the user opt-in to
        capabilities. NETWORK_EGRESS defaults to NO (pack does not declare it).
        """
        try:
            from adk.capabilities import (
                CAPABILITY_FILE_IO,
                CAPABILITY_LLM_INFERENCE,
                CAPABILITY_NETWORK_EGRESS,
                get_capability_store,
            )
        except Exception as exc:
            return f"could not load capability system: {type(exc).__name__}: {exc}"

        store = get_capability_store()

        # 1. Report current state
        output = ["GobboNet Onboarding Setup", "=" * 50]

        # Check if we're in a checkout
        try:
            from adk.packs.gobbonet.launch import _find_existing
            ui = _find_existing(None)
        except Exception:
            ui = None

        if ui:
            output.append(f"UI checkout: {ui}")
        else:
            output.append("UI checkout: none (will clone on first /gobbonet start)")

        # 2. Summarize current capability grants
        output.append("\nCurrent Capability Grants:")
        output.append("-" * 50)

        for cap, name, desc in [
            (CAPABILITY_LLM_INFERENCE, "LLM Inference", "Run language models"),
            (CAPABILITY_FILE_IO, "File I/O", "Read/write user files"),
            (CAPABILITY_NETWORK_EGRESS, "Network Egress", "Make external network requests"),
        ]:
            is_granted = store.is_granted(cap)
            status = "✓ GRANTED" if is_granted else "✗ NOT GRANTED"
            output.append(f"  {name:20} {status:15} ({desc})")

        output.append("\nGrant/Revoke a capability:")
        output.append("  /gobbonet capabilities grant network  (allow network access)")
        output.append("  /gobbonet capabilities revoke network (deny network access)")

        output.append("\nNote: LLM_INFERENCE and FILE_IO are declared in the pack.")
        output.append("NETWORK_EGRESS is explicitly NOT declared and defaults to DENIED.")

        return "\n".join(output)

    async def _capabilities(self, args: List[str]) -> str:
        """List and revoke capability grants.

        Usage:
          /gobbonet capabilities               (list all)
          /gobbonet capabilities grant <cap>   (allow a capability)
          /gobbonet capabilities revoke <cap>  (deny a capability)
        """
        try:
            from adk.capabilities import (
                CAPABILITY_FILE_IO,
                CAPABILITY_LLM_INFERENCE,
                CAPABILITY_NETWORK_EGRESS,
                get_capability_store,
            )
        except Exception as exc:
            return f"could not load capability system: {type(exc).__name__}: {exc}"

        if not args:
            # List all capabilities
            store = get_capability_store()
            output = ["Capability Grants", "=" * 50]

            for cap, name in [
                (CAPABILITY_LLM_INFERENCE, "LLM Inference"),
                (CAPABILITY_FILE_IO, "File I/O"),
                (CAPABILITY_NETWORK_EGRESS, "Network Egress"),
            ]:
                is_granted = store.is_granted(cap)
                status = "GRANTED" if is_granted else "NOT GRANTED"
                output.append(f"  {name:20} {status:15}")

            return "\n".join(output)

        sub = args[0].lower()
        cap_arg = args[1].lower() if len(args) > 1 else None

        if sub == "grant" and cap_arg:
            store = get_capability_store()

            # Map aliases to full names
            cap_map = {
                "network": CAPABILITY_NETWORK_EGRESS,
                "file": CAPABILITY_FILE_IO,
                "llm": CAPABILITY_LLM_INFERENCE,
            }

            cap_name = cap_map.get(cap_arg)
            if not cap_name:
                return (
                    f"Unknown capability: {cap_arg!r}\n"
                    "  Use: network, file, or llm"
                )

            store._grant(cap_name, reason="user granted via /gobbonet capabilities")
            return f"Granted: {cap_name}"

        if sub == "revoke" and cap_arg:
            store = get_capability_store()

            cap_map = {
                "network": CAPABILITY_NETWORK_EGRESS,
                "file": CAPABILITY_FILE_IO,
                "llm": CAPABILITY_LLM_INFERENCE,
            }

            cap_name = cap_map.get(cap_arg)
            if not cap_name:
                return (
                    f"Unknown capability: {cap_arg!r}\n"
                    "  Use: network, file, or llm"
                )

            store._revoke(cap_name, reason="user revoked via /gobbonet capabilities")
            return f"Revoked: {cap_name}"

        return (
            "Usage: /gobbonet capabilities [grant|revoke] [network|file|llm]\n"
            "  /gobbonet capabilities           (list all)\n"
            "  /gobbonet capabilities grant network\n"
            "  /gobbonet capabilities revoke network"
        )
