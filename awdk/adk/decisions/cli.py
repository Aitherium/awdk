"""``adk decide`` — raise, list, and answer decision cards.

Usable three ways, and all three matter:

* an **agent** raises a card (``ask``) and keeps working, or blocks on it (``--wait``);
* the **owner** answers from any terminal (``list`` / ``show`` / ``answer``);
* a **hook or daemon** drives it as a library, or shells out with ``--json``.

Exit codes are part of the contract: ``0`` success, ``1`` a real failure, ``2`` the
command could not run at all (bad arguments, unreadable store). A command that
cannot reach a verdict never exits 0 — silence is not a pass.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, Sequence

from adk.decisions.notify import notify
from adk.decisions.render import (
    colour_enabled,
    print_card,
    render_card,
    render_markdown,
    render_row,
    render_summary,
)
from adk.decisions.store import (
    CLOSED_STATUSES,
    STATUS_OPEN,
    DecisionCard,
    DecisionError,
    DecisionOption,
    DecisionSource,
    DecisionStore,
)

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 60}


def parse_duration(text: str) -> float:
    """``90s`` / ``30m`` / ``2h`` / ``1d`` → seconds. A bare number means minutes."""
    match = _DURATION_RE.match(text or "")
    if not match:
        raise ValueError(f"not a duration: {text!r} (use 90s, 30m, 2h, 1d)")
    return float(match.group(1)) * _UNITS[match.group(2).lower()]


def parse_option(spec: str) -> DecisionOption:
    """``key|Label|consequence`` → an option. ``:`` is still accepted.

    The pipe is the preferred separator because the colon form silently mangles
    any option whose text contains a Windows path: ``repoint|...to C:\\Aither...``
    split on ``:`` puts ``C`` at the end of the label and the drive's tail at the
    start of the consequence. It produced a card that rendered as nonsense while
    reporting success, which is the exact class of failure these cards exist to
    surface. Pipe wins when present; otherwise the colon form is used unchanged so
    existing callers keep working.
    """
    raw = spec or ""
    separator = "|" if "|" in raw else ":"
    parts = raw.split(separator, 2)
    key = parts[0].strip()
    if not key:
        raise ValueError(f"option needs a key: {spec!r} (use key|Label|consequence)")
    return DecisionOption(
        key=key,
        label=(parts[1].strip() if len(parts) > 1 else key),
        consequence=(parts[2].strip() if len(parts) > 2 else ""),
    )


#: DETACHED_PROCESS | CREATE_NO_WINDOW.
_CREATE_NO_WINDOW = 0x08000000


def _git_branch(cwd: str) -> str:
    # CREATE_NO_WINDOW is not cosmetic here. A card raised by the Stop hook runs
    # in a DETACHED process, which has NO console — so when it spawns a console
    # program without this flag, Windows allocates a NEW console for the child
    # and a window flashes on the owner's desktop. Every single card raise did
    # that, on a machine where the whole point of the flag is quality gate 1t
    # (STW001: a console on the interactive desktop TAKES FOCUS and eats
    # keystrokes). Found while auditing a report of terminal-window spam.
    extra: dict = {"creationflags": _CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd or None, capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace", **extra,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _detect_session_id(explicit: str = "") -> str:
    """Best guess at the raising session, so the answer can be routed back.

    Claude Code exports ``CLAUDE_SESSION_ID`` to hooks; adk sets
    ``AITHER_SESSION_ID``. With neither, the card is still raised — it just has no
    return path, which the card itself makes visible rather than pretending.
    """
    candidates = (
        explicit,
        os.getenv("AITHER_SESSION_ID", ""),
        os.getenv("CLAUDE_SESSION_ID", ""),
        os.getenv("CLAUDE_CODE_SESSION_ID", ""),
    )
    for candidate in candidates:
        value = (candidate or "").strip()
        if value:
            return value
    return ""


def _detect_transcript(explicit: str = "", session_id: str = "") -> str:
    """Find the raising session's transcript when the caller did not pass one.

    The Stop hook passes ``--transcript`` because it receives the path in its
    payload — but every OTHER raiser (the skill's ``adk decide ask``, agents,
    the daemon) leaves it empty, and the card's "what this session was doing"
    panel then reads "no transcript anchored". Claude Code names its transcripts
    ``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl``; given a session id
    and cwd we can locate the newest one. Returns "" when nothing plausibly
    matches — the panel then reports the absence honestly rather than guessing.
    """
    if explicit.strip():
        return explicit.strip()
    sid = (session_id or _detect_session_id()).strip()
    if not sid:
        return ""
    root = Path(os.getenv("CLAUDE_CONFIG_DIR", "")) if os.getenv("CLAUDE_CONFIG_DIR") else Path.home() / ".claude"
    projects = root / "projects"
    if not projects.is_dir():
        return ""
    # The transcript lives under the ENCODED project dir; we do not need the
    # exact encoding to find it — the session id is unique across the tree.
    candidates = sorted(
        projects.glob(f"*/{sid}.jsonl"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
        reverse=True,
    )
    return str(candidates[0]) if candidates else ""


def _resolve_session_pid(explicit: int = 0) -> int:
    """The long-lived process whose terminal this card is about.

    The card's terminal controls act on this pid, and it must be one that is
    still alive when the owner finally clicks — which is why it is resolved HERE,
    at raise time, and why a caller that knows better (a hook, whose own
    interpreter exits in milliseconds) passes it explicitly. Walking up from a
    dead pid finds nothing, so a lazily-resolved pid would leave every
    hook-raised card with a terminal it could never locate.
    """
    if explicit:
        return int(explicit)
    try:
        from adk.decisions import winproc

        return winproc.resolve_owner_pid(os.getpid())
    except (ImportError, OSError):
        # Losing the pid costs the terminal buttons, never the card.
        return os.getpid()


# ── commands ────────────────────────────────────────────────────────────────────


def cmd_ask(args: argparse.Namespace, store: DecisionStore) -> int:
    options = [parse_option(spec) for spec in (args.option or [])]
    if args.recommend:
        wanted = args.recommend.strip().lower()
        matched = False
        for opt in options:
            if opt.key.lower() == wanted:
                opt.recommended = True
                matched = True
        if not matched:
            print(f"--recommend {args.recommend!r} is not one of the options", file=sys.stderr)
            return 2

    default_key = args.default or ""
    if not default_key and options:
        # An explicit recommendation is a usable default; falling back to it beats
        # refusing the card, but a silent guess would not — so it is stated.
        recommended = next((o.key for o in options if o.recommended), "")
        default_key = recommended or options[0].key
        if not args.quiet:
            print(f"note: no --default given; using {default_key!r}", file=sys.stderr)

    deadline: Optional[float] = None
    if args.deadline:
        try:
            deadline = time.time() + parse_duration(args.deadline)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    kind = args.kind
    if args.credential:
        if not (args.secret_name or "").strip():
            print("--credential requires --secret-name VAULT_KEY", file=sys.stderr)
            return 2
        if not (args.credential_description or "").strip():
            print("--credential requires --credential-description (shown to the owner)",
                  file=sys.stderr)
            return 2
        kind = "credential"
    elif args.secret_name:
        print("--secret-name requires --credential", file=sys.stderr)
        return 2

    cwd = args.cwd or os.getcwd()
    session_pid = _resolve_session_pid(args.session_pid)
    session_id = _detect_session_id(args.session)
    card = DecisionCard(
        id="",
        title=args.title.strip(),
        summary=(args.summary or "").strip(),
        detail=(args.detail or "").strip(),
        kind=kind,
        urgency=args.urgency,
        options=options,
        default_key=default_key,
        facts=[f for f in (args.fact or []) if f.strip()],
        source=DecisionSource(
            session_id=session_id,
            agent=args.agent or os.getenv("AITHER_AGENT_NAME", "") or "claude-code",
            cwd=cwd,
            branch=args.branch or _git_branch(cwd),
            session_pid=session_pid,
            transcript=_detect_transcript(args.transcript or "", session_id),
        ),
        deadline=deadline,
        secret_name=(args.secret_name or "").strip() if args.credential else None,
        credential_format=args.credential_format if args.credential else None,
        credential_scope=args.credential_scope if args.credential else None,
        credential_description=((args.credential_description or "").strip()
                                if args.credential else None),
    )

    try:
        card = store.create(card)
    except DecisionError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    result = notify(card, store)

    if args.json:
        print(json.dumps({
            "id": card.id,
            "status": card.status,
            "notify": result.describe(),
            "card": card.to_dict(),
        }, indent=2))
    else:
        print_card(card)
        if not args.quiet:
            print(f"  {result.describe()}", file=sys.stderr)

    if args.wait:
        return _wait_for_answer(card.id, store, timeout=args.wait, as_json=args.json)
    return 0


def _wait_for_answer(
    card_id: str, store: DecisionStore, *, timeout: str, as_json: bool
) -> int:
    """Block until the card closes. Returns 0 answered, 1 timed out."""
    try:
        limit = parse_duration(timeout)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    deadline = time.time() + limit
    while time.time() < deadline:
        card = store.get(card_id)
        if card is None:
            print(f"card {card_id} disappeared while waiting", file=sys.stderr)
            return 1
        if card.status in CLOSED_STATUSES:
            if as_json:
                print(json.dumps({"id": card.id, "status": card.status,
                                  "answer": card.answer, "note": card.answer_note}, indent=2))
            else:
                print(f"\n{card.id} {card.status}: {card.answer or '(none)'}")
                if card.answer_note:
                    print(f"  note: {card.answer_note}")
            return 0
        time.sleep(1.0)
    print(f"still unanswered after {timeout}", file=sys.stderr)
    return 1


def cmd_list(args: argparse.Namespace, store: DecisionStore) -> int:
    status = None if args.all else STATUS_OPEN
    cards = store.list(status=status, session_id=args.session or "")
    if args.json:
        print(json.dumps([c.to_dict() for c in cards], indent=2))
        return 0
    if not cards:
        print("no decisions waiting" if not args.all else "no decision cards")
        return 0
    colour = colour_enabled(sys.stdout)
    for card in cards:
        print(render_row(card, colour=colour))
    if not args.all:
        print()
        print(f"  {render_summary(cards)}")
        print("  answer with:  adk decide answer <id> <option>")
    return 0


def cmd_show(args: argparse.Namespace, store: DecisionStore) -> int:
    try:
        card = store.get(args.id)
    except DecisionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if card is None:
        print(f"no such card: {args.id}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(card.to_dict(), indent=2))
    elif args.markdown:
        print(render_markdown(card))
    else:
        print_card(card)
    return 0


def cmd_answer(args: argparse.Namespace, store: DecisionStore) -> int:
    pre = store.get(args.id)
    if pre is not None and (pre.kind or "").strip().lower() == "credential":
        if args.choice:
            print("credential cards take the value via the masked prompt only "
                  "— no positional choice", file=sys.stderr)
            return 2
        from adk.decisions.secure_prompt import capture_credential
        try:
            return capture_credential(args.id, store)
        except DecisionError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    try:
        # deliver=False — delivered explicitly below so the path can be shown.
        card = store.answer(args.id, args.choice, note=args.note or "", via=args.via,
                            deliver=False)
    except DecisionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    chosen = card.option(card.answer or "")
    label = f" ({chosen.label})" if chosen else ""
    print(f"{card.id} answered: {card.answer}{label}")
    box = store.deliver_answer(card)
    if box:
        print(f"delivered to session {card.source.session_id} → {box}")
    else:
        # Saying so is the point: an answer that reached nobody must not look
        # like one that did.
        print("no session on this card — nothing to steer; the answer is recorded only",
              file=sys.stderr)
    return 0


def cmd_steer(args: argparse.Namespace, store: DecisionStore) -> int:
    """Send the owner's own words to the raising session; the card stays open."""
    text = " ".join(args.text).strip()
    try:
        card = store.steer(args.id, text, via=args.via)
    except DecisionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    live = bool(card.notes and card.notes[-1].delivered_live)
    # Two different facts. Printing one for the other is the "silent no-op"
    # pattern: "sent" for something merely queued reads as delivered.
    print(f"{card.id} steered — " + (
        "the session has it now" if live
        else "queued; the session picks it up at its next prompt"
    ))
    return 0


def cmd_window(args: argparse.Namespace, store: DecisionStore) -> int:
    """Open the card window (what a raise does automatically)."""
    from adk.decisions.popup import show_queue

    return show_queue(store, start_id=args.id or "")


def cmd_cancel(args: argparse.Namespace, store: DecisionStore) -> int:
    try:
        card = store.cancel(args.id, note=args.note or "")
    except DecisionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{card.id} withdrawn")
    return 0


def cmd_promise(args: argparse.Namespace, store: DecisionStore) -> int:
    """Record work owed — an agent commitment with a deadline (promise plane).

    A promise BREACHES when overdue; it never expires. The host promise sweep
    (AitherOS-Promise-Sweep, every 15 min) breaches overdue promises and raises
    an owner escalation card, so a promise with no due time is refused here.
    """
    import time as _time

    due = (args.due or "").strip()
    deadline = None
    if due:
        try:
            deadline = _time.time() + parse_duration(due)
        except ValueError:
            try:
                deadline = _time.mktime(_time.strptime(due, "%Y-%m-%dT%H:%M"))
            except ValueError:
                try:
                    deadline = _time.mktime(_time.strptime(due, "%Y-%m-%d"))
                except ValueError:
                    deadline = None
    if deadline is None:
        print(f"cannot parse --due {args.due!r} (use 30m/2h/1d or ISO)", file=sys.stderr)
        return 2
    try:
        card = store.create(DecisionCard(
            id="",
            title=args.title,
            kind="promise",
            deadline=deadline,
            owed_by=args.owed_by or "",
            owed_to=args.owed_to or "",
            preconditions=list(args.precondition or []),
            summary=args.summary or "",
            source=DecisionSource(agent=args.agent or "cli",
                                  cwd=args.cwd or str(Path.cwd())),
        ))
    except DecisionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{card.id} promise recorded (due {_time.strftime('%Y-%m-%d %H:%M', _time.localtime(deadline))})")
    return 0


def cmd_resolve(args: argparse.Namespace, store: DecisionStore) -> int:
    """Close an open promise as KEPT."""
    try:
        card = store.resolve(args.id, note=args.note or "")
    except DecisionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{card.id} promise resolved")
    return 0


def cmd_watch(args: argparse.Namespace, store: DecisionStore) -> int:
    """Render cards as they appear. This is the poor-man's cockpit."""
    seen: set[str] = {c.id for c in store.list(status=None)} if args.only_new else set()
    interval = max(0.5, float(args.interval))
    print(f"watching {store.path} — ctrl-c to stop", file=sys.stderr)
    try:
        while True:
            for card in store.list(status=STATUS_OPEN):
                if card.id in seen:
                    continue
                seen.add(card.id)
                print_card(card)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def cmd_sweep(args: argparse.Namespace, store: DecisionStore) -> int:
    try:
        keep = parse_duration(args.keep)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    removed = store.sweep(keep_closed_seconds=keep)
    print(f"swept {removed} closed card(s) older than {args.keep}")
    return 0


# ── self-test ───────────────────────────────────────────────────────────────────


def _self_test() -> int:
    """Prove the store can still fail, and that the round-trip really round-trips.

    Every assertion here has a matching failure mode that would otherwise be
    silent: a card that stores but cannot be found, an answer that reports success
    without reaching the session, a corrupt file that empties the list.
    """
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  ok   {name}")
        else:
            print(f"  FAIL {name} {detail}")
            failures.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AITHER_DECISIONS_DIR"] = str(Path(tmp) / "cards")
        os.environ["AITHER_STEER_DIR"] = str(Path(tmp) / "steer")
        store = DecisionStore(Path(tmp) / "cards")

        # 1. a decision card with no options is refused
        try:
            store.create(DecisionCard(id="", title="bare", kind="decision"))
            check("refuses an optionless decision card", False, "it was accepted")
        except DecisionError:
            check("refuses an optionless decision card", True)

        # 2. a decision card with no default is refused
        try:
            store.create(DecisionCard(
                id="", title="no default", kind="decision",
                options=[DecisionOption(key="a", label="A")],
            ))
            check("refuses a card with no default", False, "it was accepted")
        except DecisionError:
            check("refuses a card with no default", True)

        # 3. a default that is not an option is refused
        try:
            store.create(DecisionCard(
                id="", title="bad default", kind="decision", default_key="zzz",
                options=[DecisionOption(key="a", label="A")],
            ))
            check("refuses a default that is not an option", False, "it was accepted")
        except DecisionError:
            check("refuses a default that is not an option", True)

        # 4. a real card stores and comes back
        card = store.create(DecisionCard(
            id="", title="Bridge the harness proxy", kind="decision", default_key="host",
            summary="The Veil proxy points at container-local loopback.",
            facts=["nothing listening on 127.0.0.1:8362", "AITHER_HARNESS_TOKEN unset"],
            options=[
                DecisionOption(key="host", label="host.docker.internal", recommended=True),
                DecisionOption(key="tunnel", label="AitherTunnel"),
            ],
            source=DecisionSource(session_id="selftest-session"),
        ))
        check("stores a valid card", bool(card.id))
        check("reads it back", store.get(card.id) is not None)
        check("lists it as open", any(c.id == card.id for c in store.list()))
        check("renders without raising", len(render_card(card)) > 0)
        check("renders markdown", "Bridge the harness proxy" in render_markdown(card))
        check("renders a row", card.id in render_row(card))

        # A Windows path inside an option must survive parsing. The colon form
        # cannot carry one, which is why the pipe form exists.
        windows = parse_option(r"repoint|Repoint to C:\Projects\Example|Drops the D: dep")
        check("an option can carry a Windows path",
              windows.key == "repoint"
              and windows.label == r"Repoint to C:\Projects\Example"
              and windows.consequence == "Drops the D: dep",
              f"got key={windows.key!r} label={windows.label!r} cons={windows.consequence!r}")
        legacy = parse_option("a:Plain label:and a consequence")
        check("the colon form still parses",
              legacy.key == "a" and legacy.label == "Plain label"
              and legacy.consequence == "and a consequence")

        # The box must be a box. A one-column drift in the header arithmetic is
        # invisible to every other assertion here and is exactly what shipped in
        # the first version, so it gets its own check at several widths.
        ragged: list[str] = []
        for width in (56, 72, 100):
            drawn = render_card(card, width=width).splitlines()
            widths = {len(line) for line in drawn}
            if len(widths) != 1:
                ragged.append(f"w={width} produced {sorted(widths)}")
        check("every rendered row is the same width", not ragged, "; ".join(ragged))

        # Long unbroken content must not blow the frame out either.
        wide = DecisionCard.from_dict({
            **card.to_dict(),
            "title": "x" * 300,
            "facts": ["y" * 200],
        })
        drawn = render_card(wide, width=72).splitlines()
        check("an over-long field still frames cleanly",
              len({len(line) for line in drawn}) == 1)

        # 5. path traversal in a card id is refused
        try:
            store.get("../../../etc/passwd")
            check("refuses a traversal id", False, "it was accepted")
        except DecisionError:
            check("refuses a traversal id", True)

        # 6. a corrupt file is skipped, not fatal
        (Path(tmp) / "cards" / "d-bad1.json").write_text("{not json", encoding="utf-8")
        listed = store.list()
        check("a corrupt card does not empty the list", any(c.id == card.id for c in listed))

        # 6b. free-text steering: reaches the session, does NOT close the card.
        # This is the half the first version lacked, and its absence is what
        # forced the owner back to the terminal to say anything but a button.
        steered = store.steer(card.id, "do the OTHER one first", via="selftest")
        check("records a free-text steer", bool(steered.notes)
              and steered.notes[-1].text == "do the OTHER one first")
        check("steering does NOT answer the card", steered.is_open,
              f"status became {steered.status}")
        steer_box = Path(tmp) / "steer" / "selftest-session"
        steer_files = list(steer_box.glob("*-steer.md")) if steer_box.exists() else []
        check("a steer reaches the session mailbox", len(steer_files) == 1,
              f"found {len(steer_files)}")
        if steer_files:
            check("the mailbox carries the owner's words",
                  "do the OTHER one first" in steer_files[0].read_text(encoding="utf-8"))
        try:
            store.steer(card.id, "   ")
            check("refuses an empty steer", False, "whitespace was accepted")
        except DecisionError:
            check("refuses an empty steer", True)
        # Two steers inside the same second must not overwrite each other.
        store.steer(card.id, "and check staging", via="selftest")
        steer_files = list(steer_box.glob("*-steer*.md"))
        check("a second steer does not clobber the first", len(steer_files) == 2,
              f"found {len(steer_files)}")

        # 7. answering delivers to the steering mailbox
        answered = store.answer(card.id, "tunnel", note="phone matters", via="selftest")
        check("records the answer", answered.answer == "tunnel")
        box = Path(tmp) / "steer" / "selftest-session"
        # `*-answer*` on purpose: a double delivery lands as `-answer-1.md`, and
        # a narrower glob would report the duplicate as a clean single write.
        delivered = list(box.glob("*-answer*.md")) if box.exists() else []
        check("delivers to the steering mailbox exactly once", len(delivered) == 1,
              f"found {len(delivered)} file(s)")
        if delivered:
            body = delivered[0].read_text(encoding="utf-8")
            check("mailbox names the answer", "tunnel" in body)
            check("mailbox carries the owner note", "phone matters" in body)

        # 8. answering twice loses
        try:
            store.answer(card.id, "host")
            check("refuses a second answer", False, "the race had two winners")
        except DecisionError:
            check("refuses a second answer", True)

        # 9. an expired card applies its default
        soon = store.create(DecisionCard(
            id="", title="expires immediately", kind="decision", default_key="a",
            options=[DecisionOption(key="a", label="A")],
            deadline=time.time() - 1,
        ))
        refreshed = store.list(status=None)
        expired = next((c for c in refreshed if c.id == soon.id), None)
        check("expires an overdue card", expired is not None and expired.status == "expired")
        check("applies the default on expiry", expired is not None and expired.answer == "a")

        # 10. sweep keeps open cards regardless of age
        keeper = store.create(DecisionCard(
            id="", title="ancient but open", kind="info",
        ))
        store._write(  # noqa: SLF001 - deliberately ageing a card for the test
            DecisionCard.from_dict({**keeper.to_dict(), "created_at": 0.0})
        )
        store.sweep(keep_closed_seconds=0)
        check("never sweeps an open card", store.get(keeper.id) is not None)
        check("sweeps a closed card", store.get(card.id) is None)

    print()
    if failures:
        print(f"SELF-TEST FAILED — {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("self-test passed — the store can still fail, and the round-trip round-trips")
    return 0


# ── argument parsing ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adk decide",
        description="Raise and answer decision cards — structured asks instead of "
                    "a paragraph nobody reads.",
    )
    parser.add_argument("--self-test", action="store_true",
                        help="prove the store and the answer round-trip still work")
    sub = parser.add_subparsers(dest="command")

    ask = sub.add_parser("ask", help="raise a card")
    ask.add_argument("title")
    ask.add_argument("--summary", default="", help="one or two lines: what you need to know")
    ask.add_argument("--detail", default="", help="longer context, shown under the summary")
    ask.add_argument("--option", action="append", metavar="key:Label:consequence",
                     help="repeatable; the consequence is what a phone reader needs")
    ask.add_argument("--recommend", default="", metavar="KEY", help="mark one option starred")
    ask.add_argument("--default", default="", metavar="KEY",
                     help="what happens if nobody answers (required for kind=decision)")
    ask.add_argument("--deadline", default="", metavar="30m",
                     help="apply the default after this long")
    ask.add_argument("--fact", action="append", help="repeatable; a measurement you took")
    ask.add_argument("--kind", default="decision", choices=("decision", "blocked", "info"))
    ask.add_argument("--credential", action="store_true",
                     help="ask for a secret: the value goes to the vault via a masked "
                          "prompt (secure_prompt.py), never the card")
    ask.add_argument("--secret-name", default="", metavar="VAULT_KEY",
                     help="vault key the value is written to (required with --credential)")
    ask.add_argument("--credential-format", default="api_key",
                     choices=("password", "api_key", "totp_seed", "custom"))
    ask.add_argument("--credential-scope", default="platform",
                     choices=("platform", "workspace", "user"))
    ask.add_argument("--credential-description", default="",
                     help="why we need it — shown to the owner (required with --credential)")
    ask.add_argument("--urgency", default="normal",
                     choices=("low", "normal", "high", "critical"))
    ask.add_argument("--session", default="", help="session id the answer routes back to")
    ask.add_argument("--transcript", default="", metavar="PATH",
                     help="the session transcript, so the card's context panel can show "
                          "what this session was DOING (an anchor, never a snapshot)")
    ask.add_argument("--session-pid", type=int, default=0, metavar="PID",
                     help="the LONG-LIVED process whose terminal this is about "
                          "(auto-resolved from the caller's ancestry when omitted; "
                          "a hook must pass it, because the hook itself exits at once)")
    ask.add_argument("--agent", default="", help="who is asking")
    ask.add_argument("--cwd", default="", help="working directory the ask is about")
    ask.add_argument("--branch", default="", help="git branch (auto-detected when omitted)")
    ask.add_argument("--wait", default="", metavar="30m",
                     help="block until answered, then print the answer")
    ask.add_argument("--json", action="store_true")
    ask.add_argument("--quiet", action="store_true", help="suppress the notify report")

    listing = sub.add_parser("list", help="what is waiting")
    listing.add_argument("--all", action="store_true", help="include closed cards")
    listing.add_argument("--session", default="")
    listing.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="one card in full")
    show.add_argument("id")
    show.add_argument("--json", action="store_true")
    show.add_argument("--markdown", action="store_true")

    answer = sub.add_parser("answer", help="answer a card and steer the session")
    answer.add_argument("id")
    # Optional: a credential card must NOT take a positional choice — its value
    # is entered via the masked prompt (secure_prompt.py).
    answer.add_argument("choice", nargs="?", default="")
    answer.add_argument("--note", default="", help="anything the agent should also know")
    answer.add_argument("--via", default="cli")

    steer = sub.add_parser(
        "steer", help="send free text to the raising session WITHOUT answering the card")
    steer.add_argument("id")
    steer.add_argument("text", nargs="+", help="what the session should do")
    steer.add_argument("--via", default="cli")

    window = sub.add_parser("window", help="open the card window on what is waiting")
    window.add_argument("id", nargs="?", default="", help="start on this card")

    cancel = sub.add_parser("cancel", help="withdraw a card you no longer need answered")
    cancel.add_argument("id")
    cancel.add_argument("--note", default="")

    promise = sub.add_parser("promise", help="record work owed (an agent commitment with a due time)")
    promise.add_argument("title")
    promise.add_argument("--due", required=True, metavar="30m|ISO",
                         help="when the promise must be kept; overdue promises BREACH")
    promise.add_argument("--owed-by", default="", help="agent identity making the promise")
    promise.add_argument("--owed-to", default="", help="who it is owed to")
    promise.add_argument("--precondition", action="append",
                         help="repeatable; a fact that must hold for the promise to be kept")
    promise.add_argument("--summary", default="")
    promise.add_argument("--agent", default="")
    promise.add_argument("--cwd", default="")

    resolve = sub.add_parser("resolve", help="close an open promise")
    resolve.add_argument("id")
    resolve.add_argument("--note", default="")

    watch = sub.add_parser("watch", help="render cards as they appear")
    watch.add_argument("--interval", default="2.0")
    watch.add_argument("--only-new", action="store_true",
                       help="ignore cards that already existed at startup")

    sweep = sub.add_parser("sweep", help="delete old CLOSED cards (open cards are kept)")
    sweep.add_argument("--keep", default="7d")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Windows consoles here are cp1252, and this command prints card text the
    # agent wrote — an em dash or an arrow in it would raise UnicodeEncodeError
    # and turn a successful answer into a traceback AFTER the answer was already
    # recorded, which reads as a failure that did not happen.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            # A stream that cannot be reconfigured (a pipe wrapper, a captured
            # buffer) is fine — it just does not get the guarantee.
            continue

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.command:
        parser.print_help()
        return 0

    try:
        store = DecisionStore()
    except OSError as exc:
        # Cannot reach the store at all. Exit 2, never 0 — "I could not look" is
        # not "nothing is waiting".
        print(f"cannot open the decision store: {exc}", file=sys.stderr)
        return 2

    handler = {
        "ask": cmd_ask,
        "list": cmd_list,
        "show": cmd_show,
        "answer": cmd_answer,
        "steer": cmd_steer,
        "window": cmd_window,
        "cancel": cmd_cancel,
        "promise": cmd_promise,
        "resolve": cmd_resolve,
        "watch": cmd_watch,
        "sweep": cmd_sweep,
    }[args.command]
    return handler(args, store)


if __name__ == "__main__":
    raise SystemExit(main())
