"""``adk home calendar | todo | connect`` -- the owner's side of the home planner.

    adk home calendar                         this week (built-in + subscribed)
    adk home calendar today|tomorrow|"next week"|2026-10-05
    adk home calendar add "tomorrow 9:00" "Dentist" [--minutes 45] [--location ..]
    adk home calendar move <id> "friday 14:00"
    adk home calendar delete <id>
    adk home calendar subscriptions | refresh | unsubscribe <id-or-name>
    adk home todo [list] | add "Buy milk" [--due friday] | done <id> | delete <id>
    adk home connect                          what is connected
    adk home connect calendar --ics <url> [--name NAME]
    adk home connect calendar --caldav <url> --user <login>    (app password)
    adk home connect mail --user you@example.com [--imap-host H --smtp-host H]
    adk home connect mail --remove

These are the OWNER's commands, typed at their own terminal, so they act at once;
the same change asked of the agent pauses for the owner's yes (the tools in
:mod:`adk.home.home_tools` are in ``life_tools.ALWAYS_ASK``).

A password is read from ``$HEARTH_MAIL_PASSWORD`` / ``$HEARTH_CALDAV_PASSWORD`` or
typed at a no-echo prompt -- never an argument, so it cannot land in shell history.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Any, Callable, List, Optional

from . import planner as pl
from .config import HomeError

EXIT_OK, EXIT_FAIL, EXIT_SETUP = 0, 1, 2

ICS_HELP = """\
Where the link is:
  Google    calendar.google.com > Settings > your calendar > "Secret address in iCal format"
  Outlook   outlook.com > Settings > Calendar > Shared calendars > Publish a calendar > ICS
  iCloud    Calendar app > share the calendar > Public Calendar > copy the webcal:// link
Then:  adk home connect calendar --ics "<that link>"
The link is read-only and stays on this machine (connections.json, owner-only)."""

CALENDAR_ACTIONS = ("list", "add", "move", "delete", "subscribe", "subscriptions",
                    "unsubscribe", "refresh")


def build_parsers(hs: Any) -> None:
    """Add ``calendar``, ``todo`` and ``connect`` to the ``adk home`` subparsers."""
    cal = hs.add_parser("calendar", help="Your built-in calendar + subscribed calendars: "
                                         "view, add, move, delete")
    cal.add_argument("action", nargs="?", default="this week",
                     help="today | tomorrow | 'this week' | 'next week' | YYYY-MM-DD, or "
                     + " | ".join(CALENDAR_ACTIONS))
    cal.add_argument("args", nargs="*", help="add: WHEN TITLE; move: ID WHEN; delete: ID")
    cal.add_argument("--minutes", type=int, default=30, help="add: length (default 30)")
    cal.add_argument("--location", default="", help="add: where")
    cal.add_argument("--name", default="", help="subscribe: what to call the calendar")
    cal.add_argument("--json", action="store_true", help="Machine-readable output")

    td = hs.add_parser("todo", help="Your built-in to-do list: list, add, done, delete")
    td.add_argument("action", nargs="?", default="list",
                    choices=["list", "add", "done", "delete"])
    td.add_argument("args", nargs="*", help="add: TEXT; done / delete: ID")
    td.add_argument("--due", default="", help="add: a day ('friday', YYYY-MM-DD)")
    td.add_argument("--all", action="store_true", help="list: include finished items")
    td.add_argument("--json", action="store_true", help="Machine-readable output")

    co = hs.add_parser("connect", help="Connect a calendar by link or a mailbox by app "
                                       "password (no OAuth app needed)")
    co.add_argument("what", nargs="?", default="status",
                    choices=["status", "calendar", "mail"])
    co.add_argument("--ics", default="", help="calendar: the ICS / webcal link (Google "
                    "secret address, Outlook published calendar, iCloud public calendar)")
    co.add_argument("--caldav", default="", help="calendar: a CalDAV collection URL "
                    "(with --user and an app password)")
    co.add_argument("--name", default="", help="calendar: what to call it")
    co.add_argument("--user", default="", help="mail: your address; CalDAV: your login")
    co.add_argument("--imap-host", default="", help="mail: IMAP server (known for Gmail, "
                    "iCloud, Yahoo, Fastmail)")
    co.add_argument("--imap-port", type=int, default=993)
    co.add_argument("--smtp-host", default="", help="mail: SMTP server (default: IMAP host)")
    co.add_argument("--smtp-port", type=int, default=0, help="mail: 587 STARTTLS or 465 TLS")
    co.add_argument("--remove", action="store_true",
                    help="mail: forget the account; calendar: with --name, unsubscribe")
    co.add_argument("--json", action="store_true", help="Machine-readable output")


def _out(data: Any, as_json: bool, text: str) -> None:
    print(json.dumps(data, indent=2, default=str) if as_json else text)


def _fail(exc: Exception) -> int:
    print(str(exc), file=sys.stderr)
    return EXIT_FAIL


def _password(env_name: str, prompt: str,
              ask: Optional[Callable[[str], str]] = None) -> str:
    value = (os.environ.get(env_name) or "").strip()
    if value:
        return value
    if ask is None and not sys.stdin.isatty():
        raise pl.PlannerError(f"no password: set {env_name} in the environment, or run "
                              "this in a terminal to type it at a hidden prompt")
    return (ask or getpass.getpass)(prompt).strip()


# ── calendar ──────────────────────────────────────────────────────────────────

def cmd_calendar(args: argparse.Namespace, plan: Optional[pl.Planner] = None) -> int:
    plan = plan or pl.Planner()
    action = str(args.action or "this week").strip()
    rest: List[str] = list(getattr(args, "args", []) or [])
    as_json = bool(getattr(args, "json", False))
    try:
        if action == "add":
            if len(rest) < 2:
                print('usage: adk home calendar add "<when>" "<title>" [--minutes N]',
                      file=sys.stderr)
                return EXIT_SETUP
            start, all_day = pl.parse_start(rest[0])
            row = plan.add_event(start, " ".join(rest[1:]), args.minutes, all_day=all_day,
                                 location=getattr(args, "location", ""))
            shown = row.get("date") or str(row["start"])[:16].replace("T", " ")
            _out(row, as_json, f"added {row['id']}: {row['title']} -- {shown}"
                 + (" (all day)" if all_day else f", {row['minutes']} min"))
            return EXIT_OK
        if action == "move":
            if len(rest) < 2:
                print('usage: adk home calendar move <id> "<when>"', file=sys.stderr)
                return EXIT_SETUP
            start, all_day = pl.parse_start(" ".join(rest[1:]))
            row = plan.move_event(rest[0], start, all_day=all_day)
            shown = row.get("date") or str(row["start"])[:16].replace("T", " ")
            _out(row, as_json, f"moved {row['id']}: {row['title']} -- {shown}")
            return EXIT_OK
        if action == "delete":
            if not rest:
                print("usage: adk home calendar delete <id>", file=sys.stderr)
                return EXIT_SETUP
            row = plan.delete_event(rest[0])
            _out(row, as_json, f"deleted {row['id']}: {row.get('title', '')}")
            return EXIT_OK
        if action == "subscribe":
            if not rest:
                print(ICS_HELP, file=sys.stderr)
                return EXIT_SETUP
            return _subscribe(plan, rest[0], getattr(args, "name", ""), as_json)
        if action == "subscriptions":
            subs = [plan.public_subscription(s) for s in plan.subscriptions()]
            text = "\n".join(f"{s['id']}  {s['name']}  ({s['events']} events, refreshed "
                             f"{s['last_refreshed'] or 'never'})"
                             + (f"  ! {s['last_error']}" if s["last_error"] else "")
                             for s in subs)
            _out(subs, as_json, text or "No subscribed calendars. " + ICS_HELP)
            return EXIT_OK
        if action == "unsubscribe":
            if not rest:
                print("usage: adk home calendar unsubscribe <id-or-name>", file=sys.stderr)
                return EXIT_SETUP
            sub = plan.unsubscribe(" ".join(rest))
            _out(sub, as_json, f"unsubscribed {sub['name']}")
            return EXIT_OK
        if action == "refresh":
            done = plan.refresh(force=True)
            bad = [s for s in done if s["last_error"]]
            text = "\n".join(f"{s['name']}: " + (f"FAILED -- {s['last_error']}"
                                                 if s["last_error"]
                                                 else f"{s['events']} events")
                             for s in done)
            _out(done, as_json, text or "No subscribed calendars to refresh.")
            return EXIT_FAIL if bad else EXIT_OK
        span = " ".join(rest) if action == "list" else " ".join([action, *rest])
        first, after_last, label = pl.parse_range(span or "this week")
        rows, warnings = plan.agenda(first, after_last)
    except (pl.PlannerError, ValueError, OverflowError, OSError) as exc:
        return _fail(exc)
    text = pl.format_agenda(rows, label)
    if warnings:
        text += "\n" + "\n".join(f"note: {w}" for w in warnings)
    _out({"range": label, "events": rows, "warnings": warnings}, as_json, text)
    return EXIT_OK


def _subscribe(plan: pl.Planner, url: str, name: str, as_json: bool, kind: str = "ics",
               username: str = "", password: str = "") -> int:
    try:
        sub = plan.subscribe(url, name=name, kind=kind, username=username,
                             password=password)
    except pl.PlannerError as exc:
        return _fail(exc)
    _out(sub, as_json, f"subscribed to {sub['name']} ({sub['events']} events, read-only). "
         "It is refreshed every 15 minutes while it is in use.\n"
         'See it: adk home calendar   Ask: adk home chat "what\'s on my calendar this week?"')
    return EXIT_OK


# ── to-do ─────────────────────────────────────────────────────────────────────

def cmd_todo(args: argparse.Namespace, plan: Optional[pl.Planner] = None) -> int:
    plan = plan or pl.Planner()
    rest: List[str] = list(getattr(args, "args", []) or [])
    as_json = bool(getattr(args, "json", False))
    try:
        if args.action == "add":
            if not rest:
                print('usage: adk home todo add "<text>" [--due friday]', file=sys.stderr)
                return EXIT_SETUP
            row = plan.add_todo(" ".join(rest), due=getattr(args, "due", ""))
            _out(row, as_json, f"added {row['id']}: {row['text']}"
                 + (f" (due {row['due']})" if row["due"] else ""))
            return EXIT_OK
        if args.action in ("done", "delete"):
            if not rest:
                print(f"usage: adk home todo {args.action} <id>", file=sys.stderr)
                return EXIT_SETUP
            fn = plan.done_todo if args.action == "done" else plan.delete_todo
            row = fn(" ".join(rest))
            _out(row, as_json, f"{'done' if args.action == 'done' else 'deleted'} "
                 f"{row['id']}: {row['text']}")
            return EXIT_OK
        rows = plan.todos(include_done=bool(getattr(args, "all", False)))
    except pl.PlannerError as exc:
        return _fail(exc)
    text = "\n".join(f"[{'x' if r.get('done') else ' '}] {r['text']}"
                     + (f"  (due {r['due']})" if r.get("due") else "") + f"  ({r['id']})"
                     for r in rows)
    _out(rows, as_json, text or "Nothing on the to-do list.")
    return EXIT_OK


# ── connect ───────────────────────────────────────────────────────────────────

def connection_status(plan: pl.Planner) -> dict:
    acct = plan.mail_account()
    return {"calendar": {"built_in": True, "events": len(plan.events()),
                         "subscribed": [plan.public_subscription(s)
                                        for s in plan.subscriptions()]},
            "todo": {"built_in": True, "open": len(plan.todos())},
            "mail": ({"user": acct["user"], "imap": acct["imap_host"],
                      "smtp": acct["smtp_host"]} if acct else None)}


def _status_text(status: dict) -> str:
    cal = status["calendar"]
    lines = [f"calendar  built-in: ready ({cal['events']} events)"]
    for s in cal["subscribed"]:
        lines.append(f"          {s['name']}: {s['events']} events, read-only, refreshed "
                     f"{s['last_refreshed'] or 'never'}"
                     + (f"  ! {s['last_error']}" if s["last_error"] else ""))
    if not cal["subscribed"]:
        lines.append('          add Google / Outlook / iCloud: adk home connect calendar '
                     '--ics "<link>"')
    lines.append(f"to-do     built-in: ready ({status['todo']['open']} open)")
    mail = status["mail"]
    lines.append(f"mail      {mail['user']} via {mail['imap']}" if mail else
                 "mail      not connected: adk home connect mail --user you@example.com")
    return "\n".join(lines)


def cmd_connect(args: argparse.Namespace, plan: Optional[pl.Planner] = None,
                ask: Optional[Callable[[str], str]] = None) -> int:
    plan = plan or pl.Planner()
    as_json = bool(getattr(args, "json", False))
    what = getattr(args, "what", "status") or "status"
    try:
        if what == "calendar":
            if args.remove:
                if not args.name:
                    print("usage: adk home connect calendar --remove --name <id-or-name>",
                          file=sys.stderr)
                    return EXIT_SETUP
                sub = plan.unsubscribe(args.name)
                _out(sub, as_json, f"unsubscribed {sub['name']}")
                return EXIT_OK
            if args.ics:
                return _subscribe(plan, args.ics, args.name, as_json)
            if args.caldav:
                if not args.user:
                    print("CalDAV needs --user <login> (and an app password, asked next)",
                          file=sys.stderr)
                    return EXIT_SETUP
                password = _password(pl.CALDAV_PASSWORD_ENV,
                                     f"App password for {args.user}: ", ask)
                return _subscribe(plan, args.caldav, args.name, as_json, kind="caldav",
                                  username=args.user, password=password)
            print("The built-in calendar is ready -- nothing to connect:\n"
                  '  adk home calendar add "tomorrow 9:00" "Dentist"\n\n'
                  "To also see a calendar you already have (read-only):\n" + ICS_HELP)
            return EXIT_OK
        if what == "mail":
            if args.remove:
                had = plan.disconnect_mail()
                _out({"removed": had}, as_json,
                     "mail account removed" if had else "no mail account was connected")
                return EXIT_OK
            if not args.user:
                print("usage: adk home connect mail --user you@example.com\n"
                      "You are asked for an APP PASSWORD (not your normal password) at a "
                      f"hidden prompt, or set {pl.MAIL_PASSWORD_ENV}.\n"
                      "Gmail: https://myaccount.google.com/apppasswords   iCloud: "
                      "https://account.apple.com", file=sys.stderr)
                return EXIT_SETUP
            password = _password(pl.MAIL_PASSWORD_ENV,
                                 f"App password for {args.user}: ", ask)
            acct = plan.connect_mail(args.user, password, imap_host=args.imap_host,
                                     smtp_host=args.smtp_host, imap_port=args.imap_port,
                                     smtp_port=args.smtp_port)
            _out(acct, as_json, f"mail connected: {acct['user']} ({acct['imap_host']}); "
                 f"password kept in the {acct['password_in']}.\n"
                 'Ask: adk home chat "any unread mail?"  (sending always asks you first)')
            return EXIT_OK
    except (pl.PlannerError, HomeError) as exc:
        return _fail(exc)
    status = connection_status(plan)
    _out(status, as_json, _status_text(status))
    return EXIT_OK


# ── first-run offer (adk home init) ───────────────────────────────────────────

def offer_calendar(ics_url: str = "", interactive: Optional[bool] = None,
                   ask: Callable[[str], str] = input,
                   plan: Optional[pl.Planner] = None) -> int:
    """``adk home init``: the built-in calendar by default, or one by link.

    ``ics_url`` (``--calendar-ics``) subscribes without asking. On a terminal the
    owner is offered the choice; anywhere else the built-in calendar is used and
    the one-liner for later is printed.
    """
    plan = plan or pl.Planner()
    if ics_url:
        return _subscribe(plan, ics_url, "", False)
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        print("Calendar and to-do list: built in and ready -- ask \"what's on my calendar "
              "this week?\"\n"
              '  add an event:            adk home calendar add "tomorrow 9:00" "Dentist"\n'
              "  Google/Outlook/iCloud:   adk home connect calendar --ics \"<link>\"\n"
              "  mail (app password):     adk home connect mail --user you@example.com")
        return EXIT_OK
    print("Calendar:\n"
          "  1. Use the built-in calendar (ready now, nothing to connect)\n"
          "  2. Also connect Google, Outlook or iCloud by link (read-only)")
    try:
        choice = ask("Choose [1]: ").strip()
    except EOFError:
        choice = ""
    if choice != "2":
        print("Using the built-in calendar. Connect one later: adk home connect calendar "
              '--ics "<link>"')
        return EXIT_OK
    print(ICS_HELP)
    try:
        url = ask("Paste the link (empty to skip): ").strip()
    except EOFError:
        url = ""
    if not url:
        print("Skipped. The built-in calendar is ready.")
        return EXIT_OK
    return _subscribe(plan, url, "", False)


# ── chat: the Hearth agent, with the owner's yes asked at the terminal ────────

def describe_call(pending: dict) -> str:
    """A pending call as the owner's prompt shows it: the same rule as a Hearth card
    (``hearth._card_value``) -- a recipient is NEVER cut, so a second address padded
    past the edge cannot hide; other values are one line, cut with a visible count."""
    from .hearth import _card_value

    args = pending.get("args") or {}
    shown = ", ".join(f"{k}={_card_value(k, v)!r}" for k, v in args.items())
    return f"{pending.get('tool')}({shown})"


def chat_with_approvals(agent: Any, message: str, run: Callable[[Any], Any],
                        interactive: Optional[bool] = None,
                        ask: Callable[[str], str] = input, rounds: int = 4) -> Any:
    """One chat turn; a gated tool pauses it and the owner answers y/N here.

    Off a terminal nothing is approved: the reply names what waited, and the direct
    command that does it (the owner's own command needs no second yes).
    """
    resp = run(agent.chat(message))
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    for _ in range(rounds):
        pending = list(getattr(resp, "pending", None) or [])
        if not getattr(resp, "requires_action", False) or not pending:
            return resp
        if not interactive:
            calls = "; ".join(describe_call(p) for p in pending)
            resp.content = (f"Not done -- this needs your yes: {calls}. Run `adk home chat` "
                            "in a terminal to answer, or do it yourself with `adk home "
                            "calendar add` / `adk home todo add`.")
            return resp
        decisions = []
        for p in pending:
            try:
                answer = ask(f"Allow {describe_call(p)}? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            decisions.append({"tool_use_id": p.get("tool_use_id"), "tool": p.get("tool"),
                              "result": "allow" if answer in ("y", "yes") else "deny"})
        resp = run(agent.resume(getattr(resp, "session_id", ""), decisions))
    return resp


__all__ = ["build_parsers", "cmd_calendar", "cmd_todo", "cmd_connect", "offer_calendar",
           "chat_with_approvals", "connection_status"]
