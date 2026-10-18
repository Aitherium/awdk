"""``/posture`` on the harness daemon: the host posture, for the MCP gateway and the phone.

The host posture is ONE word the owner lives by -- ``shop`` (Shop open), ``after`` (After
hours), ``game`` (Game on) or ``auto`` (follow the schedule). Its engine is the AitherOS
repo's fleet verbs tool (``fleet_verbs.py posture``); its state is ``~/.aither/posture.json``.
Both live on the fleet HOST, so a caller in a container (the MCP gateway, which is how the
owner's phone reaches the house) cannot run them. These routes run in the daemon on that
host and shell exactly one thing:

    python fleet_verbs.py posture <word> [--execute] [--until <t>] --json

Nothing else: no shell, no path, no free text. ``word`` is from :data:`WORDS`, ``until``
must match :data:`UNTIL_RE`, and the argv is a list (never a shell string). awdk never
imports AitherOS code (the wheel boundary), so the tool is found the way ``adk inference``
finds it (:func:`adk.commands.fleet_host.find_tool`).

Who may do what:

* ``GET /posture`` -- any principal the daemon authenticates and scopes here.
* ``POST /posture/set`` -- the OWNER only: the daemon's own owner principal (the root
  bearer, a local caller such as awdesk), or a request carrying the gateway's Ed25519
  owner assertion (:mod:`adk.harnesses.owner_steer`, target ``posture``) over the exact
  action text. A scoped ``agent`` token alone is a transport, never authority. Scope comes
  from the authenticated principal, never from a payload field.

A flip that is still applying after :data:`SET_WAIT_S` is NOT killed: the route answers
``pending`` and the process keeps going (stopping half the fleet mid-verb is the one
outcome worse than a slow answer). The phone reads the result back from ``GET /posture``.
"""

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

#: The posture words a caller may set. ``status`` is the read verb, never a "set".
WORDS = ("shop", "after", "game", "auto")
#: The assertion's ``to``: a posture assertion can never steer a session or a KV relay.
TARGET = "posture"
TOOL_REL = Path("AitherOS") / "dev" / "tools" / "fleet_verbs.py"
#: ``until``: a clock time (21:30), a local or zoned ISO-8601 date-time, or a span (2h, 90m).
UNTIL_RE = re.compile(
    r"^(?:\d{1,2}:\d{2}"
    r"|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|\d{1,4}[mh])$"
)
STATUS_TIMEOUT_S = 60.0
#: How long a set waits for the CLI before answering ``pending``. Under the 30 s the phone
#: allows for posture.json to change, so the answer never arrives after the fact.
SET_WAIT_S = 20.0

_set_lock = threading.Lock()


def build_argv(word, tool, until="", python=None):
    """The exact argv for ``word`` (``status`` or one of :data:`WORDS`).

    Raises ValueError on anything outside the closed language -- the route turns that into
    a 400 before a process is ever started.
    """
    word = (word or "").strip().lower()
    if word != "status" and word not in WORDS:
        raise ValueError(f"unknown posture {word!r}; one of {', '.join(WORDS)} or status")
    until = (until or "").strip()
    if until and word == "status":
        raise ValueError("until applies to a set, not to status")
    if until and not UNTIL_RE.match(until):
        raise ValueError(f"until {until!r}: a clock time (21:30), an ISO date-time or a "
                         f"span (2h, 90m)")
    argv = [python or sys.executable, str(tool), "posture", word]
    if word != "status":
        argv.append("--execute")        # every mutating fleet_verbs verb is a dry run without it
    if until:
        argv += ["--until", until]
    argv.append("--json")
    return argv


def _parse_json(stdout):
    """The CLI's JSON document: the whole stdout, else its last JSON object line."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        lines = text.splitlines()       # notes before the document: take its last line
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def _explain(rc, stderr):
    tail = (stderr or "").strip()[-400:]
    if rc == 2 and "invalid choice" in tail and "posture" in tail:
        return "this checkout's fleet_verbs.py has no `posture` verb yet (update the repo)"
    return tail


def _result(rc, stdout, stderr):
    doc = _parse_json(stdout)
    out = {"ok": rc == 0, "rc": rc, "posture": doc if isinstance(doc, dict) else None}
    if rc != 0 or out["posture"] is None:
        out["error"] = _explain(rc, stderr) or "fleet_verbs.py posture printed no JSON"
    return out


#: A detached daemon's console child opens a terminal tab that steals focus.
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def find_tool():
    from adk.commands.fleet_host import find_tool as _find

    return _find(rel=TOOL_REL)


def run_posture(word, until="", tool=None, wait_s=None, popen=subprocess.Popen):
    """Run the CLI. ``status`` waits for its answer; a set waits ``wait_s`` then reports
    ``pending`` and lets the process finish on its own."""
    argv_tool = tool or find_tool()
    if argv_tool is None:
        return {"ok": False, "rc": 2, "posture": None,
                "error": "no AitherOS checkout with the fleet verbs tool on this host "
                         "(set AITHEROS_ROOT)"}
    argv_tool = Path(argv_tool)
    argv = build_argv(word, argv_tool, until)
    proc = popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                 encoding="utf-8", errors="replace", cwd=str(argv_tool.parents[3]),
                 creationflags=_NOWIN)
    limit = STATUS_TIMEOUT_S if word == "status" else (SET_WAIT_S if wait_s is None else wait_s)
    try:
        stdout, stderr = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        if word == "status":
            proc.kill()
            proc.communicate()
            return {"ok": False, "rc": 2, "posture": None,
                    "error": f"fleet_verbs.py posture status took over {limit:.0f} s"}
        # The flip keeps applying; a reaper thread collects it so it never zombies.
        threading.Thread(target=proc.communicate, name="posture-reap", daemon=True).start()
        return {"ok": True, "rc": None, "pending": True, "word": word, "posture": None,
                "note": "still applying; read GET /posture for the result"}
    return _result(proc.returncode, stdout, stderr)


def mount(app, auth, verifier) -> None:
    """Add the routes to the daemon's FastAPI ``app`` (``auth`` = its bearer dependency)."""
    from fastapi import Body, Depends, HTTPException

    from adk.harnesses.kvholder_routes import parse_action

    @app.get("/posture")
    def posture_status(principal=Depends(auth)):
        return run_posture("status")

    @app.post("/posture/set")
    def posture_set(body: dict = Body(...), principal=Depends(auth)):
        args, error = parse_action(body, "set", getattr(principal, "plan", ""), verifier,
                                   target=TARGET)
        if error:
            raise HTTPException(status_code=403, detail=error)
        word = str(args.get("word") or "")
        until = str(args.get("until") or "")
        try:
            build_argv(word, TOOL_REL, until)      # validate before anything runs
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not _set_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="a posture change is already running")
        try:
            return run_posture(word.strip().lower(), until)
        except OSError as exc:
            return {"ok": False, "rc": 2, "posture": None, "error": str(exc)}
        finally:
            _set_lock.release()
