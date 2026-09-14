"""Situational context — what an agent should simply KNOW, every turn, without a tool.

WHY THIS EXISTS. Asked "what time is it" through a shell, an ADK agent took ~18 s
and illustrated its answer with "Tuesday, June 4, 2025" — a date it invented,
because nothing in its prompt carried a clock. Its only alternatives were to
guess or to spend a tool round-trip (``time_now``) on a fact the host already
had in a register. Both are wrong for an assistant: the clock, the host, the OS
and the working directory are free, and a turn should arrive with them attached
the way a shell prompt carries them for a human.

So every system prompt the agent builds ends with a short ``[AGENT HOST]`` block
from :func:`situation_block`, plus whatever the CALLER attached as
``system_additions`` (AitherShell sends a ``[USER'S SHELL]`` block with the
human's own clock, cwd and shell dialect). Two blocks, two labels — a remote
daemon's clock is not the user's clock, and the model must not confuse "where I
run" with "where you are".

Design rules (each one was paid for):

* **Cheap.** No subprocess, no network, no filesystem walk. Everything here is
  process state already in memory; the block is built in well under a
  millisecond and rides on every turn.
* **Small.** ~6 lines. It must not crowd the context window.
* **Last, not first.** Appended at the END of the system prompt, after identity,
  protocol and tool schema. A timestamp at the TOP would invalidate the
  provider's cached prefix on every single turn — the agent's own ``chat()``
  documents that prefix as "a near-pure win"; this block must not tax it.
* **Minute precision on the host clock.** Seconds would change the cached
  suffix every call inside one ReAct loop; the user's shell block carries
  seconds when seconds matter.
* **Honest.** A value that cannot be determined is omitted, never guessed.

Kill switch: ``ADK_SITUATION=0``.
"""

from __future__ import annotations

import os
import platform as _platform
import socket
import time
from datetime import datetime, timezone

HOST_HEADER = "[AGENT HOST — live state where this agent is running]"
USER_SHELL_HEADER_PREFIX = "[USER'S SHELL"

_MAX_ADDITION_CHARS = 4000
_MAX_ADDITIONS = 8


def situation_enabled(env: dict[str, str] | None = None) -> bool:
    e = os.environ if env is None else env
    return e.get("ADK_SITUATION", "1") != "0"


def _local_tz_name() -> str:
    try:
        name = time.strftime("%Z")
        return name or ""
    except Exception:  # noqa: BLE001 - a tz name is decoration; never fail the turn
        return ""


def render_host_block(now: datetime | None = None, *, hostname: str | None = None,
                      system: str | None = None, release: str | None = None,
                      cwd: str | None = None, user: str | None = None) -> str:
    """Render the ``[AGENT HOST]`` block. Pure given its inputs (tests pin it)."""
    now = now or datetime.now().astimezone()
    tz = now.tzname() or _local_tz_name()
    off = now.strftime("%z")
    off_fmt = f"UTC{off[:3]}:{off[3:]}" if len(off) == 5 else "UTC"
    weekday = now.strftime("%A")
    tz_part = f", {tz}" if tz else ""
    lines = [
        HOST_HEADER,
        f"local time: {weekday} {now.strftime('%Y-%m-%d %H:%M')} ({off_fmt}{tz_part})",
        f"utc: {now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}",
    ]
    host = hostname if hostname is not None else _safe(socket.gethostname)
    if host:
        lines.append(f"host: {host}")
    sysname = system if system is not None else _safe(_platform.system)
    rel = release if release is not None else _safe(_platform.release)
    if sysname:
        lines.append(f"os: {sysname} {rel}".rstrip())
    u = user if user is not None else (os.environ.get("USER") or os.environ.get("USERNAME") or "")
    if u:
        lines.append(f"user: {u}")
    c = cwd if cwd is not None else _safe(os.getcwd)
    if c:
        lines.append(f"cwd: {c}")
    lines.append(
        "Treat this as ground truth for the current date/time and host; if a [USER'S SHELL] "
        "block is also present, the USER's clock and cwd are the ones the human means. "
        "Answer time/date/where questions from these blocks — do not call a tool for them "
        "and never invent a date."
    )
    return "\n".join(lines)


def _safe(fn) -> str:
    try:
        return str(fn() or "")
    except Exception:  # noqa: BLE001 - omission beats a crash in a context block
        return ""


def sanitize_additions(additions) -> list[str]:
    """Normalise caller-supplied ``system_additions`` (a list of strings).

    Bounded on purpose: a client can send anything here, and an unbounded list
    of unbounded strings is a context-window DoS dressed as a feature. Non-strings
    are dropped, each is capped, the count is capped, empties are removed.
    """
    if not additions:
        return []
    if isinstance(additions, str):
        additions = [additions]
    out: list[str] = []
    for a in list(additions)[:_MAX_ADDITIONS]:
        if not isinstance(a, str):
            continue
        a = a.strip()
        if not a:
            continue
        out.append(a[:_MAX_ADDITION_CHARS])
    return out


def situation_block(system_additions=None, *, env: dict[str, str] | None = None) -> str:
    """The text to append to a turn's system prompt: host block + caller additions.

    Returns ``""`` when disabled AND nothing was supplied, so callers can do
    ``prompt + situation_block(...)`` unconditionally.
    """
    parts: list[str] = []
    if situation_enabled(env):
        parts.append(render_host_block())
    parts.extend(sanitize_additions(system_additions))
    if not parts:
        return ""
    return "\n\n" + "\n\n".join(parts)


def self_test() -> list[str]:
    """Every property a refactor could drop silently. Returns failures (empty = ok)."""
    failures: list[str] = []
    from datetime import timedelta
    from datetime import timezone as _tz
    fixed = datetime(2026, 8, 23, 6, 11, 5, tzinfo=_tz(timedelta(hours=-7), "PDT"))
    blk = render_host_block(fixed, hostname="BOX", system="Windows", release="11",
                            cwd="C:\\x", user="wzns")
    if not blk.startswith(HOST_HEADER):
        failures.append("host block lost its header")
    if "Sunday 2026-08-23 06:11 (UTC-07:00, PDT)" not in blk:
        failures.append(f"local time not rendered from the supplied clock: {blk!r}")
    if "utc: 2026-08-23T13:11Z" not in blk:
        failures.append("utc line wrong")
    if ":05" in blk.split("\n")[1]:
        failures.append("host clock carries seconds — that busts the cached suffix per call")
    for want in ("host: BOX", "os: Windows 11", "user: wzns", "cwd: C:\\x"):
        if want not in blk:
            failures.append(f"missing {want!r}")
    if "do not call a tool" not in blk:
        failures.append("lost the no-tool instruction — the whole point")
    sparse = render_host_block(fixed, hostname="", system="", release="", cwd="", user="")
    for bad in ("host:", "os:", "user:", "cwd:"):
        if any(line.startswith(bad) for line in sparse.splitlines()):
            failures.append(f"empty {bad} rendered as a fact")
    if len(blk) > 700:
        failures.append(f"host block is {len(blk)} chars; budget 700")
    # Additions: bounded and ordered after the host block.
    adds = sanitize_additions(["  a  ", 5, "", "b" * 10_000] + ["c"] * 20)
    if adds[0] != "a" or len(adds[1]) != _MAX_ADDITION_CHARS or len(adds) > _MAX_ADDITIONS:
        failures.append(f"sanitize_additions bounds wrong: {[len(x) for x in adds]}")
    full = situation_block(["[USER'S SHELL] t=1"], env={})
    if full.find(HOST_HEADER) == -1 or full.find(HOST_HEADER) > full.find("[USER'S SHELL]"):
        failures.append("caller additions must follow the host block")
    if situation_block(None, env={"ADK_SITUATION": "0"}) != "":
        failures.append("ADK_SITUATION=0 did not disable")
    if not situation_block(["x"], env={"ADK_SITUATION": "0"}).endswith("x"):
        failures.append("kill switch must not drop CALLER additions")
    return failures


if __name__ == "__main__":  # pragma: no cover
    import sys
    fails = self_test()
    print("\n".join(fails) if fails else "adk.situation self-test: ok")
    sys.exit(1 if fails else 0)
