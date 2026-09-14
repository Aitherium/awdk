"""``adk shell`` — drive the shell-of-shells from a terminal.

Everything here is a CLIENT of the daemon, never an in-process shortcut. That
is deliberate: if the CLI created sessions in its own process they would vanish
when it exited and would be invisible to the browser. Going through the daemon
is what makes ``adk harness list`` on the desktop show the session you started
from aitherium.com on your phone.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from adk.harnesses.daemon import DEFAULT_HOST, DEFAULT_PORT, resolve_token


def _base_url(args: Any) -> str:
    explicit = getattr(args, "url", "") or os.environ.get("AITHER_HARNESS_URL", "")
    if explicit:
        return explicit.rstrip("/")
    return f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"


def _request(
    args: Any, path: str, method: str = "GET", body: Optional[dict[str, Any]] = None,
    timeout: float = 60.0,
) -> tuple[int, Any]:
    url = _base_url(args) + path
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {resolve_token(getattr(args, 'token', ''))}")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return 0, f"cannot reach harness daemon at {url}: {exc.reason}"


def _die_if_down(status: int, payload: Any) -> None:
    """A dead daemon must say so, not print an empty list that reads as 'none'."""
    if status == 0:
        print(payload, file=sys.stderr)
        print("Start it with:  adk harness serve", file=sys.stderr)
        raise SystemExit(2)
    if status >= 400:
        print(f"HTTP {status}: {payload}", file=sys.stderr)
        raise SystemExit(1)


def cmd_shell(args: Any) -> int:
    command = getattr(args, "shell_command", None)
    if command in (None, "help"):
        _print_help()
        return 0
    handler = {
        "serve": _cmd_serve,
        "harnesses": _cmd_harnesses,
        "agents": _cmd_agents,
        "profiles": _cmd_profiles,
        "list": _cmd_list,
        "new": _cmd_new,
        "send": _cmd_send,
        "attach": _cmd_attach,
        "kill": _cmd_kill,
        "wrap": _cmd_wrap,
    }.get(command)
    if handler is None:
        print(f"Unknown subcommand: {command}", file=sys.stderr)
        _print_help()
        return 2
    return handler(args)


def _cmd_serve(args: Any) -> int:
    from adk.harnesses.daemon import serve

    return serve(
        host=getattr(args, "host", "") or "",
        port=int(getattr(args, "port", 0) or 0),
        token=getattr(args, "token", "") or "",
    )


def _cmd_harnesses(args: Any) -> int:
    versions = "true" if bool(getattr(args, "versions", False)) else "false"
    status, payload = _request(args, f"/harnesses?versions={versions}")
    _die_if_down(status, payload)
    print(f"{'ID':12} {'INSTALLED':10} {'TRANSPORT':17} DESCRIPTION")
    for h in payload["harnesses"]:
        mark = "yes" if h["installed"] else "NO"
        print(f"{h['id']:12} {mark:10} {h['transport']:17} {h['description']}")
        if not h["installed"] and h.get("install_hint"):
            print(f"{'':12} {'':10} {'':17} -> {h['install_hint']}")
    return 0


def _cmd_agents(args: Any) -> int:
    status, payload = _request(args, "/agents")
    _die_if_down(status, payload)
    for a in payload["agents"]:
        print(f"{a['id']:12} {a['label']:14} {a['role']}")
    return 0


def _cmd_profiles(args: Any) -> int:
    status, payload = _request(args, "/profiles")
    _die_if_down(status, payload)
    for p in payload["profiles"]:
        window = f"{p['context_window']:,}" if p.get("context_window") else "-"
        print(f"{p['id']:28} {p.get('transport',''):8} {window:>10}  {p.get('description','')}")
    return 0


def _cmd_list(args: Any) -> int:
    status, payload = _request(args, "/sessions")
    _die_if_down(status, payload)
    sessions = payload["sessions"]
    if not sessions:
        print("No sessions. Start one:  adk harness new --harness claude")
        return 0
    print(f"{'ID':18} {'HARNESS':10} {'STATE':9} {'TURNS':>5}  TITLE")
    for s in sessions:
        print(f"{s['id']:18} {s['harness']:10} {s['state']:9} {s['turn']:>5}  {s['title']}")
    return 0


def _cmd_new(args: Any) -> int:
    body = {
        "harness": getattr(args, "harness", "claude"),
        "cwd": os.path.abspath(getattr(args, "cwd", "") or os.getcwd()),
        "model_profile": getattr(args, "model_profile", "") or "",
        "model": getattr(args, "model", "") or "",
        "permission_mode": getattr(args, "permission_mode", "") or "",
        "title": getattr(args, "title", "") or "",
        "agent": getattr(args, "agent", "") or "",
        "target": getattr(args, "target", "") or "",
        "participants": [p for p in (getattr(args, "participants", "") or "").split(",") if p],
    }
    status, payload = _request(args, "/sessions", "POST", body)
    _die_if_down(status, payload)
    print(payload["id"])
    if getattr(args, "attach", False):
        args.session_id = payload["id"]
        return _cmd_attach(args)
    return 0


def _cmd_send(args: Any) -> int:
    status, payload = _request(
        args, f"/sessions/{args.session_id}/input", "POST", {"text": args.text}
    )
    _die_if_down(status, payload)
    return 0


def _cmd_kill(args: Any) -> int:
    status, payload = _request(args, f"/sessions/{args.session_id}", "DELETE")
    _die_if_down(status, payload)
    print(f"stopped {args.session_id} (exit {payload.get('exit_code')})")
    return 0


def _cmd_wrap(args: Any) -> int:
    """Bridge this terminal's stdin/stdout to a daemon session.

    The parser has advertised ``wrap`` since the subcommand was added, but it was
    never registered in the dispatch table below — so it answered "Unknown
    subcommand: wrap" and exited 2 while `--help` listed it as available. The
    bridge itself (:class:`DaemonPtyBridge`) was complete the whole time.
    """
    from urllib.parse import urlsplit

    from adk.harnesses.wrap import DaemonPtyBridge

    parts = urlsplit(_base_url(args))
    bridge = DaemonPtyBridge(
        host=parts.hostname or DEFAULT_HOST,
        port=parts.port or DEFAULT_PORT,
        token=getattr(args, "token", "") or "",
    )
    return bridge.run(
        harness=getattr(args, "harness", "") or "claude",
        cwd=getattr(args, "cwd", "") or "",
        model=getattr(args, "model", "") or "",
        resume_session_id=getattr(args, "resume", "") or "",
        title=getattr(args, "title", "") or "",
    )


def _cmd_attach(args: Any) -> int:
    """Follow a session's event stream until it completes a turn or exits."""
    cursor = int(getattr(args, "since", 0) or 0)
    idle_since = time.time()
    follow = bool(getattr(args, "follow", False))
    while True:
        status, payload = _request(args, f"/sessions/{args.session_id}/events?since={cursor}")
        _die_if_down(status, payload)
        for event in payload["events"]:
            cursor = event["seq"]
            idle_since = time.time()
            _render(event)
            if event["kind"] == "session.exited":
                return 0
            if event["kind"] == "turn.completed" and not follow:
                return 0
        if payload["state"] in ("exited", "failed"):
            return 0
        if not follow and time.time() - idle_since > 900:
            print("(no events for 15 minutes; detaching)", file=sys.stderr)
            return 0
        time.sleep(0.3)


def _render(event: dict[str, Any]) -> None:
    kind = event["kind"]
    who = event.get("data", {}).get("participant") or ""
    prefix = f"[{who}] " if who else ""
    if kind == "text.delta":
        sys.stdout.write(prefix + event["text"])
        sys.stdout.flush()
    elif kind == "thinking.delta":
        sys.stdout.write(f"\033[2m{event['text']}\033[0m")
        sys.stdout.flush()
    elif kind == "tool.call":
        print(f"\n\033[36m→ {event['tool']}\033[0m")
    elif kind == "tool.result":
        state = "error" if event["data"].get("is_error") else "ok"
        print(f"\033[36m← {event.get('tool') or 'tool'} ({state})\033[0m")
    elif kind == "error":
        print(f"\n\033[31m! {event['text']}\033[0m", file=sys.stderr)
    elif kind == "turn.completed":
        print()
    elif kind == "session.exited":
        print(f"\n\033[2m-- session exited ({event['data'].get('exit_code')}) --\033[0m")


def _print_help() -> None:
    print(
        """adk shell — one shell that drives every coding shell

  adk harness serve [--host H] [--port P]     run the harness daemon
  adk harness harnesses [--versions]          what this box can drive
  adk harness agents                          sovereign agent roster
  adk harness profiles                        model profiles (per-session)
  adk harness list                            live sessions
  adk harness new --harness claude [--model-profile deepseek-flash]
                [--cwd .] [--title T] [--attach]
  adk harness new --harness terminal          a REAL pty terminal
  adk harness new --harness sandbox --target <container>
  adk harness new --harness group --participants aither,atlas
  adk harness send <id> "<text>"
  adk harness attach <id> [--follow]
  adk harness kill <id>

Sessions live in the daemon, so one started here is the same session the
browser at aitherium.com attaches to."""
    )
