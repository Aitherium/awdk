"""
``aither inbox`` — one queue for everything that wants your attention.
=======================================================================

Merges three sources into a single numbered list, newest first:

- **mail** — CommCore ``/mail/inbox`` unread
- **relay** — mentions/notifications for your nick (``/v1/notifications``)
- **alerts** — Pulse ``/alerts`` (severity-filtered)

Interactive loop: a number opens the item (and marks mail/relay read),
``d N <text>`` replies to a relay item as a DM, ``all`` marks everything
read, Enter/q leaves. Sources that are down are listed as such and skipped —
a wedged CommCore never hides your Pulse alerts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import click

from adk.shell.command_center.fleet_client import FleetClient


@dataclass
class InboxItem:
    source: str          # mail | relay | alert
    id: str
    title: str
    body: str
    sender: str = ""
    when: Optional[datetime] = None
    severity: float = 0.0
    raw: dict = None


def _parse_when(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _sort_key(item: InboxItem):
    return item.when or datetime.min.replace(tzinfo=timezone.utc)


async def gather_inbox(fc: FleetClient, min_severity: float = 0.3,
                       limit: int = 40) -> tuple[list[InboxItem], list[str]]:
    """(items newest-first, list of 'source: error' strings for dead sources)."""
    nick = fc.default_nick()
    mail_t = asyncio.create_task(fc.mail_inbox(unread_only=True, limit=limit))
    alerts_t = asyncio.create_task(fc.alerts(min_severity=min_severity))
    relay_t = asyncio.create_task(fc.relay_notifications(nick)) if nick else None

    items: list[InboxItem] = []
    down: list[str] = []

    mail = await mail_t
    if mail.ok:
        for m in (mail.data or {}).get("messages", []):
            items.append(InboxItem(
                source="mail", id=str(m.get("id", "")),
                title=str(m.get("subject") or m.get("title") or "(no subject)"),
                body=str(m.get("body") or m.get("preview") or ""),
                sender=str(m.get("sender") or m.get("from") or ""),
                when=_parse_when(m.get("created_at") or m.get("timestamp")),
                raw=m,
            ))
    else:
        down.append(f"mail: {mail.error}")

    if relay_t:
        relay = await relay_t
        if relay.ok:
            for n in (relay.data or {}).get("notifications", []):
                items.append(InboxItem(
                    source="relay", id=str(n.get("id", "")),
                    title=str(n.get("title") or n.get("type") or "mention"),
                    body=str(n.get("message") or n.get("content") or ""),
                    sender=str(n.get("from_nick") or n.get("from") or ""),
                    when=_parse_when(n.get("created_at") or n.get("timestamp")),
                    raw=n,
                ))
        else:
            down.append(f"relay: {relay.error}")
    else:
        down.append("relay: no nick (run `aither login`, or set AITHER_RELAY_NICK)")

    alerts = await alerts_t
    if alerts.ok:
        for a in (alerts.data or {}).get("alerts", []):
            items.append(InboxItem(
                source="alert", id=str(a.get("id", "")),
                title=str(a.get("title") or a.get("alert_key") or "alert"),
                body=str(a.get("message") or ""),
                sender=str(a.get("source") or "pulse"),
                when=_parse_when(a.get("last_occurred_at") or a.get("created_at")),
                severity=float(a.get("severity", 0) or 0),
                raw=a,
            ))
    else:
        down.append(f"alerts: {alerts.error}")

    items.sort(key=_sort_key, reverse=True)
    return items, down


def _age(when: Optional[datetime]) -> str:
    if not when:
        return "?"
    secs = max((datetime.now(timezone.utc) - when).total_seconds(), 0)
    if secs < 60:
        return "now"
    if secs < 3600:
        return f"{secs / 60:.0f}m"
    if secs < 86400:
        return f"{secs / 3600:.0f}h"
    return f"{secs / 86400:.0f}d"


def _secho(text=""):
    import sys
    try:
        click.echo(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        click.echo(str(text).encode(enc, errors="replace").decode(enc))


_SRC_STYLE = {"mail": "cyan", "relay": "magenta", "alert": "yellow"}


def _print_items(items: list[InboxItem], down: list[str]) -> None:
    if down:
        for d in down:
            _secho(click.style(f"  (source down — {d})", fg="bright_black"))
    if not items:
        _secho(click.style("\n  inbox zero. nothing wants your attention.\n",
                           fg="green", bold=True))
        return
    _secho(click.style(f"\n  {len(items)} item(s)", fg="cyan", bold=True))
    _secho(click.style("  " + "-" * 72, fg="bright_black"))
    for i, it in enumerate(items, 1):
        src = click.style(f"{it.source:<5}", fg=_SRC_STYLE.get(it.source, "white"))
        sev = (click.style(f" [{it.severity:.1f}]", fg="red")
               if it.source == "alert" and it.severity >= 0.7 else "")
        sender = f" <{it.sender}>" if it.sender else ""
        title = " ".join(it.title.split())[:56]
        _secho(f"  {click.style(f'{i:>2}', fg='yellow', bold=True)}  "
               f"{click.style(f'{_age(it.when):<4}', fg='bright_black')} {src}{sev} "
               f"{click.style(title, bold=True)}"
               f"{click.style(sender, fg='bright_black')}")
    _secho(click.style("  " + "-" * 72, fg="bright_black"))


async def _open_item(fc: FleetClient, item: InboxItem) -> None:
    _secho("")
    _secho(click.style(f"  [{item.source}] ", fg=_SRC_STYLE.get(item.source, "white"))
           + click.style(item.title, bold=True)
           + (click.style(f"  <{item.sender}>", fg="bright_black") if item.sender else ""))
    body = item.body.strip() or "(no body)"
    for line in body.splitlines()[:40]:
        _secho("  " + line)
    if item.source == "mail" and item.id:
        res = await fc.mail_mark_read(item.id)
        _secho(click.style("  (marked read)" if res.ok else f"  (mark-read failed: {res.error})",
                           fg="bright_black"))
    elif item.source == "relay" and item.id:
        res = await fc.relay_notifications_read(fc.default_nick(), [item.id])
        _secho(click.style("  (marked read)" if res.ok else f"  (mark-read failed: {res.error})",
                           fg="bright_black"))
    _secho("")


async def _inbox_loop(min_severity: float) -> None:
    async with FleetClient() as fc:
        while True:
            items, down = await gather_inbox(fc, min_severity=min_severity)
            _print_items(items, down)
            if not items:
                return
            answer = click.prompt(
                "  open # / 'd # reply text' / all=read-all / Enter=quit",
                default="", show_default=False).strip()
            if not answer or answer.lower() in ("q", "quit"):
                return
            if answer.lower() == "all":
                nick = fc.default_nick()
                for it in items:
                    if it.source == "mail" and it.id:
                        await fc.mail_mark_read(it.id)
                    elif it.source == "relay" and it.id and nick:
                        await fc.relay_notifications_read(nick, [it.id])
                _secho(click.style("  marked all mail/relay items read "
                                   "(alerts clear from Pulse on their own).", fg="green"))
                continue
            if answer.lower().startswith("d "):
                parts = answer.split(None, 2)
                if len(parts) < 3 or not parts[1].isdigit():
                    _secho("  usage: d <number> <reply text>")
                    continue
                idx, text = int(parts[1]), parts[2]
                if not 1 <= idx <= len(items):
                    continue
                item = items[idx - 1]
                if item.source != "relay" or not item.sender:
                    _secho("  can only DM-reply to relay items with a sender.")
                    continue
                res = await fc.relay_send_dm(item.sender, text)
                _secho(click.style(f"  -> DM sent to {item.sender}" if res.ok
                                   else f"  DM failed: {res.error}",
                                   fg="green" if res.ok else "red"))
                continue
            if answer.isdigit() and 1 <= int(answer) <= len(items):
                await _open_item(fc, items[int(answer) - 1])


def run_inbox(min_severity: float = 0.3) -> None:
    try:
        asyncio.run(_inbox_loop(min_severity))
    except KeyboardInterrupt:
        pass
