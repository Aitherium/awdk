"""
Hearth Plugin for AitherShell
=============================

Talk to your running ``adk home serve`` from the shell -- a window onto the one
Hearth process over its loopback ``local`` channel, never a second agent.

Usage:
    /hearth <text>             send <text>; prints the replies and any approval card
    /hearth yes <nonce>        answer an approval card (or: no <nonce>)
    /hearth say <text>         send <text> verbatim (e.g. a message that starts
                               with "receipts" or "help")
    /hearth receipts [n]       the last n receipts (default 10) + the verify verdict
    /hearth cloud <verb>       your PLATFORM Hearth on the tool gateway (hearth_* tools):
                               status | reminders | due | receipts [n] | yes|no <code>
                               | remind <when> -- <text>
    /hearth teach <verb>       Aither Classroom, as the teacher, on the tool gateway
                               (the same classroom_* tools the platform Hearth uses):
                               classes | roster <class> | hard <class> [days]
                               | insight <class> [member] | queue <class>
                               | assign <class> [lesson] [--skills a,b] [--to member]
                                 [--due YYYY-MM-DD] [--yes] [-- <title>]
                               | announce <class> [--yes] -- <text>
                               | draft <class> [--minutes N] [--grade G] [--yes] -- <topic>
                               assign, announce and draft send NOTHING until you repeat
                               the command with --yes; first they show what would go out.

The token and the port are read from ``<home>/local.token``, which the running
serve writes once it listens. Start the server with ``adk home serve``. When nothing
is serving, the platform verbs (status, reminders, due, receipts, yes, no, remind)
are answered by your platform Hearth instead (see ``adk.home.gateway_hearth``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from adk.shell.plugins import SlashCommand


def _say(text: str) -> str:
    from adk.home.local_client import LocalClient, render_replies

    return render_replies(LocalClient().say(text))


def _receipts(n: int) -> str:
    from adk.home.local_client import LocalClient, render_receipts

    return render_receipts(LocalClient().receipts(n))


def _cloud(args: List[str]) -> str:
    from adk.home.gateway_hearth import run_verb

    return run_verb(args)


TEACH_USAGE = """\
/hearth teach -- Aither Classroom as the teacher (gateway classroom_* tools)
    classes                                   the classes you teach (with their ids)
    roster <class>                            one class's students, by alias
    hard <class> [days]                       what the class finds hard right now
    insight <class> [member]                  an observation for the class or one student
    queue <class>                             submitted work waiting for your review
    assign <class> [lesson] [--skills a,b] [--to member] [--due YYYY-MM-DD] [--yes] [-- title]
    announce <class> [--yes] -- <text>
    draft <class> [--minutes N] [--grade G] [--yes] -- <topic>
assign, announce and draft send nothing without --yes: they show what would go out."""

#: The write verbs: each needs ``--yes`` before anything leaves this machine.
_TEACH_WRITES = {"assign": "classroom_assign", "announce": "classroom_announce",
                 "draft": "classroom_draft_lesson"}
_TEACH_FLAGS = {"--skills", "--to", "--due", "--minutes", "--grade"}


def _teach_call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    # Looked up at call time so one gateway seam (gateway_hearth.call_gateway) serves
    # /hearth cloud and /hearth teach alike.
    from adk.home import gateway_hearth

    return gateway_hearth.call_gateway(name, arguments)


def _split_teach(rest: List[str]) -> Tuple[List[str], Dict[str, str], bool, str]:
    """``rest`` -> (positionals, --flag values, --yes given, text after ``--``)."""
    text = ""
    if "--" in rest:
        cut = rest.index("--")
        rest, text = rest[:cut], " ".join(rest[cut + 1:]).strip()
    pos: List[str] = []
    flags: Dict[str, str] = {}
    yes = False
    i = 0
    while i < len(rest):
        word = rest[i]
        if word == "--yes":
            yes = True
        elif word in _TEACH_FLAGS:
            if i + 1 >= len(rest):
                raise ValueError(f"{word} needs a value")
            flags[word[2:]] = rest[i + 1]
            i += 1
        elif word.startswith("--"):
            raise ValueError(f"unknown option {word}")
        else:
            pos.append(word)
        i += 1
    return pos, flags, yes, text


def _render_classes(d: Dict[str, Any]) -> str:
    rows = d.get("classes") or []
    if not rows:
        return "(you teach no classes yet)"
    return "\n".join(f"{c.get('class_id')}  {c.get('name')}  grade {c.get('grade_level') or '?'}"
                     f"  {c.get('student_count', 0)} student(s)"
                     f"{'' if (c.get('consent') or {}).get('consent_recorded') else '  [no consent on file]'}"
                     for c in rows if isinstance(c, dict))


def _render_roster(d: Dict[str, Any]) -> str:
    rows = d.get("students") or []
    head = f"class {d.get('class_id')}: {len(rows)} student(s)"
    return "\n".join([head] + [f"  {s.get('member_id')}  {s.get('alias')}"
                               for s in rows if isinstance(s, dict)])


def _render_queue(d: Dict[str, Any]) -> str:
    rows = d.get("responses") or []
    if not rows:
        return "(nothing waiting for review)"
    return "\n".join([f"{d.get('count', len(rows))} waiting for review "
                      "(answers stay in the Classroom review page)"]
                     + [f"  {r.get('alias')}: {r.get('challenge_title')} [{r.get('flag')}]"
                        for r in rows if isinstance(r, dict)])


def _render_json(d: Dict[str, Any]) -> str:
    return json.dumps(d, indent=2, default=str)[:4000]


def _teach_plan(verb: str, rest: List[str]
                ) -> Tuple[str, Dict[str, Any], Callable[[Dict[str, Any]], str], bool]:
    """Parse one teach verb -> (tool, arguments, renderer, confirmed). Raises ValueError."""
    pos, flags, yes, text = _split_teach(rest)
    if verb == "classes" and not pos and not flags and not text:
        return "classroom_list_classes", {}, _render_classes, False
    if not pos:
        raise ValueError(f"{verb} needs a class id (see /hearth teach classes)")
    cid, more = pos[0], pos[1:]
    if verb in ("roster", "queue") and not more and not flags and not text:
        tool = "classroom_roster" if verb == "roster" else "classroom_review_queue"
        return tool, {"class_id": cid}, (_render_roster if verb == "roster"
                                         else _render_queue), False
    if verb == "hard" and len(more) <= 1 and not flags and not text:
        raw = more[0] if more else "14"
        if not raw.isdigit():
            raise ValueError("usage: /hearth teach hard <class> [days]")
        return "classroom_hard_now", {"class_id": cid, "days": int(raw)}, _render_json, False
    if verb == "insight" and len(more) <= 1 and not flags and not text:
        args: Dict[str, Any] = {"class_id": cid}
        if more:
            args["student_member_id"] = more[0]
        return "classroom_insight", args, _render_json, False
    if verb == "assign" and len(more) <= 1 and set(flags) <= {"skills", "to", "due"}:
        args = {"class_id": cid}
        if more:
            args["lesson_id"] = more[0]
        skills = [s for s in flags.get("skills", "").split(",") if s.strip()]
        if skills:
            args["skill_ids"] = [s.strip() for s in skills]
        if "lesson_id" not in args and not skills:
            raise ValueError("assign needs a lesson id or --skills a,b")
        if flags.get("to"):
            args["student_member_id"] = flags["to"]
        if flags.get("due"):
            args["due_at"] = flags["due"]
        if text:
            args["title"] = text
        return "classroom_assign", args, _render_json, yes
    if verb == "announce" and not more and not flags:
        if not text:
            raise ValueError("usage: /hearth teach announce <class> [--yes] -- <text>")
        return "classroom_announce", {"class_id": cid, "text": text}, _render_json, yes
    if verb == "draft" and not more and set(flags) <= {"minutes", "grade"}:
        if not text:
            raise ValueError("usage: /hearth teach draft <class> [--minutes N] [--grade G] "
                             "[--yes] -- <topic>")
        args = {"class_id": cid, "topic": text}
        if "minutes" in flags:
            if not flags["minutes"].isdigit():
                raise ValueError("--minutes takes a number")
            args["minutes"] = int(flags["minutes"])
        if flags.get("grade"):
            args["grade_level"] = flags["grade"]
        return "classroom_draft_lesson", args, _render_json, yes
    raise ValueError(f"unknown or malformed teach verb {' '.join([verb] + rest)!r}")


def teach_command(args: List[str],
                  call: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None
                  ) -> str:
    """``/hearth teach <verb> ...``: one classroom_* tool call, writes only with ``--yes``."""
    if not args or args[0] in ("help", "-h", "--help"):
        return TEACH_USAGE
    verb, rest = args[0], args[1:]
    try:
        tool, arguments, render, confirmed = _teach_plan(verb, rest)
    except ValueError as exc:
        return f"hearth teach: {exc}\n\n{TEACH_USAGE}"
    if tool in _TEACH_WRITES.values() and not confirmed:
        # Nothing leaves this machine: the teacher sees the exact write and the
        # command that sends it.
        again = " ".join(["/hearth", "teach", *_with_yes(args)])
        return (f"NOT sent -- {verb} needs your OK. It would call {tool} with:\n"
                f"{json.dumps(arguments, indent=2)}\n"
                f"to send it: {again}")
    if tool in _TEACH_WRITES.values():
        arguments = {**arguments, "confirm": True}
    data = (call or _teach_call)(tool, arguments)
    if not isinstance(data, dict):
        return f"hearth teach: {tool} returned no result"
    if data.get("error"):
        return f"hearth teach: {data['error']}"
    if data.get("status") == "confirm_required":    # the tool itself refused to write
        return f"hearth teach: {tool} wrote nothing: {data.get('hint', '')}".rstrip()
    return render(data)


def _with_yes(args: List[str]) -> List[str]:
    """``args`` with ``--yes`` placed before any ``--`` text."""
    if "--" in args:
        cut = args.index("--")
        return [*args[:cut], "--yes", *args[cut:]]
    return [*args, "--yes"]


def hearth_command(args: List[str]) -> str:
    """Run one /hearth invocation synchronously and return what to print."""
    from adk.home.config import HomeError
    from adk.home.gateway_hearth import VERBS

    if not args or args[0] in ("help", "-h", "--help"):
        return __doc__ or "hearth"
    if args[0] == "cloud":
        return _cloud(args[1:])
    if args[0] == "teach":
        return teach_command(args[1:])
    try:
        if args[0] == "receipts" and len(args) <= 2:
            raw = args[1] if len(args) == 2 else "10"
            if not raw.isdigit():
                return "hearth: usage: /hearth receipts [n]"
            return _receipts(int(raw))
        words = args[1:] if args[0] == "say" else args
        text = " ".join(words).strip()
        if not text:
            return "hearth: nothing to say"
        return _say(text)
    except HomeError as exc:            # LocalClientError included: unreachable, 401, 413
        if args[0] in VERBS:
            # No local Hearth answered: the same verb on the platform Hearth, labelled so
            # nobody mistakes which home replied.
            return f"(local Hearth unavailable: {exc})\nplatform Hearth:\n{_cloud(list(args))}"
        return f"hearth: {exc}\n  your platform Hearth: /hearth cloud status"


class HearthPlugin(SlashCommand):
    name: str = "hearth"
    aliases: List[str] = []
    description: str = "Your Hearth: the running adk home serve, else the platform Hearth"

    def __init__(self) -> None:
        super().__init__(name="hearth", aliases=[],
                         description="Your Hearth: the running adk home serve, else the "
                                     "platform Hearth")

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        # The client is synchronous and one turn can take minutes: a worker thread
        # keeps the shell's loop responsive.
        return await asyncio.to_thread(hearth_command, list(args))
