"""
``aither brief`` — the executive briefing, in your terminal.
=============================================================

Reads the archived briefs Atlas already produces daily
(``GET :8778/routines/executive-briefing/history``) and renders the latest
as formatted markdown. ``--run`` triggers a fresh briefing first
(``POST /routines/executive-briefing`` — that also emails/inboxes it, so it
is opt-in, not the default).
"""

from __future__ import annotations

import asyncio

import click

from adk.shell.command_center.fleet_client import FleetClient

ATLAS_BASE = "https://localhost:8778"


def _render_markdown(md: str) -> None:
    import sys
    # Briefs are emoji-heavy; on a legacy-codepage console let characters
    # degrade to '?' rather than let the whole render throw.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass
    try:
        from rich.console import Console
        from rich.markdown import Markdown
        Console().print(Markdown(md))
    except Exception:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        click.echo(md.encode(enc, errors="replace").decode(enc))


async def _brief(run: bool, history: int) -> None:
    async with FleetClient() as fc:
        if run:
            click.echo("  running a fresh executive briefing on Atlas "
                       "(this also delivers to inbox/email; can take a few minutes)...")
            res = await fc.post_json(ATLAS_BASE + "/routines/executive-briefing",
                                     timeout=600.0)
            if not res.ok:
                click.echo(click.style(f"  briefing run failed: {res.error}", fg="red"))
        res = await fc.get_json(ATLAS_BASE + "/routines/executive-briefing/history",
                                params={"limit": max(1, history)})
        if not res.ok:
            click.echo(click.style(
                f"  Atlas briefing history unreachable: {res.error}", fg="red"))
            return
        briefs = (res.data or {}).get("briefings") or []
        if not briefs:
            click.echo("  no archived briefs yet — run `aither brief --run`.")
            return
        if history <= 1:
            latest = briefs[0]
            click.echo(click.style(
                f"\n  Executive brief — {latest.get('date', '?')}\n", fg="cyan", bold=True))
            _render_markdown(latest.get("markdown", "(empty)"))
        else:
            for b in briefs:
                click.echo(f"  {b.get('date', '?')}  {b.get('path', '')}")


def run_brief(run: bool = False, history: int = 1) -> None:
    try:
        asyncio.run(_brief(run, history))
    except KeyboardInterrupt:
        pass
