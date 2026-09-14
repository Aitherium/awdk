"""
``aither agents`` — talk to the fleet's agents from one prompt.
================================================================

- Roster (Atlas ``/agents/roster``, Genesis fallback) with descriptions.
- ``ask <agent> <question>`` — blocking ask through Genesis ``/agent/sync``,
  with an effort tier (``ask!8 atlas why is ...`` = effort 8). Effort maps to
  model per the dispatch ladder: 1-2 triage, 3-6 procedural, 7-10 reasoning.
- ``forge`` / ``forge <id>`` — list or inspect Forge dispatches.
- ``routines`` — worker routine health (failing/overdue at a glance).

Asks are patient by design: reasoning turns can take minutes, and hanging up
the client cancels the run — don't Ctrl+C an ask you still want the answer to.
"""

from __future__ import annotations

import asyncio
import re

import click

from adk.shell.command_center.fleet_client import FleetClient


def _secho(text=""):
    import sys
    try:
        click.echo(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        click.echo(str(text).encode(enc, errors="replace").decode(enc))


async def _print_roster(fc: FleetClient) -> list[str]:
    res = await fc.agents_roster()
    names: list[str] = []
    if not res.ok:
        _secho(click.style(f"  roster unavailable: {res.error} "
                           "(you can still `ask <name> ...`)", fg="yellow"))
        return names
    data = res.data or {}
    roster = data.get("roster") or data.get("agents") or []
    if not roster:
        _secho(click.style("  roster is empty right now — agents come online "
                           "as their services start.", fg="bright_black"))
        return names
    _secho(click.style(f"\n  {len(roster)} agent(s)", fg="cyan", bold=True))
    _secho(click.style("  " + "-" * 68, fg="bright_black"))
    for a in roster:
        name = str(a.get("id") or a.get("name") or "?")
        names.append(name)
        persona = str(a.get("persona") or a.get("role") or "")
        desc = " ".join(str(a.get("description") or "").split())[:46]
        status = str(a.get("status") or "")
        status_s = (click.style(" online", fg="green") if status == "online"
                    else (click.style(f" {status}", fg="yellow") if status else ""))
        _secho(f"  {click.style(f'{name:<14}', bold=True)}"
               f"{click.style(f'{persona:<18}', fg='cyan')}"
               f"{click.style(desc, fg='bright_black')}{status_s}")
    _secho(click.style("  " + "-" * 68, fg="bright_black"))
    return names


async def _ask_via_chat_stream(agent: str, question: str, effort: int) -> None:
    """Fallback ask over the /chat/stream SSE path (same as the REPL) — used
    when /agent/sync 403s (the execute gate resolves docker-published-port
    callers as non-local, and portal-token role resolution may be degraded)."""
    import sys
    from adk.shell.genesis_client import GenesisClient
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass
    async with GenesisClient() as gc:
        printed = False
        async for chunk in gc.chat_stream(question, persona=agent, effort=effort):
            printed = True
            print(chunk, end="", flush=True)
        print()
        if not printed:
            _secho(click.style("  (no response)", fg="yellow"))


async def _do_ask(fc: FleetClient, agent: str, question: str, effort: int) -> None:
    _secho(click.style(
        f"\n  asking {agent} (effort {effort}) — reasoning can take a while; "
        "Ctrl+C cancels the run...", fg="bright_black"))
    res = await fc.ask_agent(agent, question, effort=effort)
    if not res.ok:
        if "403" in res.error:
            _secho(click.style("  (/agent/sync gated — streaming via /chat instead)",
                               fg="bright_black"))
            try:
                await _ask_via_chat_stream(agent, question, effort)
            except Exception as exc:
                _secho(click.style(f"  ask failed: {type(exc).__name__}: {exc}", fg="red"))
            return
        _secho(click.style(f"  ask failed: {res.error}", fg="red"))
        return
    data = res.data or {}
    answer = data.get("response") or data.get("answer") or data.get("message") or ""
    if isinstance(answer, dict):
        import json as _json
        answer = _json.dumps(answer, indent=2)
    _secho("")
    for line in str(answer).splitlines():
        _secho("  " + line)
    model = data.get("model_used") or data.get("model")
    if model:
        _secho(click.style(f"\n  ({agent} via {model})", fg="bright_black"))


async def _show_forge(fc: FleetClient, dispatch_id: str = "") -> None:
    if dispatch_id:
        res = await fc.forge_dispatch(dispatch_id)
        if not res.ok:
            _secho(click.style(f"  forge: {res.error}", fg="red"))
            return
        d = res.data or {}
        _secho(f"\n  {d.get('id')}  {d.get('mode')}  "
               + click.style(str(d.get('status')), bold=True))
        _secho(f"  {d.get('title', '')}")
        for key in ("github_issue", "github_pr", "workflow_run_url"):
            if d.get(key):
                _secho(click.style(f"  {key}: {d[key]}", fg="bright_black"))
        for log in (d.get("logs") or [])[-10:]:
            _secho(click.style(f"    {log}", fg="bright_black"))
        return
    res = await fc.forge_dispatches()
    if not res.ok:
        _secho(click.style(f"  forge: {res.error}", fg="red"))
        return
    dispatches = (res.data or {}).get("dispatches") or []
    if not dispatches:
        _secho("  no forge dispatches.")
        return
    for d in dispatches[:15]:
        status = str(d.get("status", "?"))
        color = {"success": "green", "completed": "green",
                 "failed": "red"}.get(status, "yellow")
        _secho(f"  {str(d.get('id', ''))[:12]:<13} "
               f"{click.style(f'{status:<12}', fg=color)}"
               f"{str(d.get('title', ''))[:48]}")


async def _show_routines(fc: FleetClient) -> None:
    res = await fc.routines_stats()
    if not res.ok:
        _secho(click.style(f"  routines: {res.error}", fg="red"))
        return
    d = res.data or {}
    _secho(f"  routines: {d.get('enabled', '?')}/{d.get('total', '?')} enabled, "
           f"queue={d.get('queue_size', '?')}")
    for r in d.get("failing") or []:
        _secho(click.style(f"    FAILING: {r}", fg="red"))
    for r in d.get("overdue") or []:
        _secho(click.style(f"    overdue: {r}", fg="yellow"))


_ASK_RE = re.compile(r"^ask(?:!(\d+))?\s+(\S+)\s+(.+)$", re.IGNORECASE | re.DOTALL)


async def _console_loop() -> None:
    async with FleetClient() as fc:
        await _print_roster(fc)
        _secho(click.style(
            "  ask <agent> <question>   ask!8 <agent> <q> (effort 8)   "
            "forge [id]   routines   q", fg="bright_black"))
        while True:
            try:
                line = click.prompt("  agents>", default="", show_default=False,
                                    prompt_suffix=" ").strip()
            except (click.Abort, EOFError):
                return
            if not line or line.lower() in ("q", "quit", "exit"):
                return
            if line.lower() in ("r", "roster"):
                await _print_roster(fc)
                continue
            if line.lower().startswith("forge"):
                await _show_forge(fc, line[5:].strip())
                continue
            if line.lower() == "routines":
                await _show_routines(fc)
                continue
            m = _ASK_RE.match(line)
            if m:
                effort = int(m.group(1)) if m.group(1) else 5
                await _do_ask(fc, m.group(2), m.group(3), max(1, min(effort, 10)))
                continue
            _secho("  ? try: ask hydra review the relay auth change   |   forge   |   routines")


def run_agents() -> None:
    try:
        asyncio.run(_console_loop())
    except KeyboardInterrupt:
        pass


def ask_once(agent: str, question: str, effort: int = 5) -> None:
    """One-shot `aither agents ask <agent> <question>`."""
    async def go():
        async with FleetClient() as fc:
            await _do_ask(fc, agent, question, effort)
    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass
