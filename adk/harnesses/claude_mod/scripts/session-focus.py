#!/usr/bin/env python3
"""Session focus — stop anywhere, start a fresh session, pick up where you left off.

Before this, the only cross-session continuity was SessionStart printing the last
five TOOL NAMES from logs/activity.json ("19:40:31: Bash" x5) — a log every
concurrent session appends to, so it was usually another session's tools.

    --stop   (Stop hook)          transcript -> one focus record per session:
                                  first ask, latest ask, edited files, the closing
                                  report, its NEXT step, transcript path.
    --start  (SessionStart hook)  prints colleague handoffs and the newest records
                                  for this project as additionalContext.
    --show <sid-prefix>           prints one record in full (the "resume" read).
    --handoff <sid-prefix>        copies a record into <repo>/.claude/handoff/ so a
                                  colleague's session lists it after a git pull.
    --announce "<text>" [--team]  broadcast to every session on this machine
                                  (~/.aither/announce/) or, with --team, to every
                                  clone (<repo>/.claude/announcements/, via git).
    --deliver (UserPromptSubmit)  injects announcements this session has not seen.

A line `NEXT: <step>` (or a `## Next` heading) in a session's closing report is
captured as its next step and shown at SessionStart.

Records: ~/.aither/focus/<project-key>/<session_id>.json (local, per user).
FAIL-OPEN: hook modes never block a turn and exit 0. Secrets redacted.

SELF-TEST
    python .claude/hooks/session-focus.py --self-test
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

FOCUS_ROOT = Path(os.environ.get("AITHER_FOCUS_DIR") or Path.home() / ".aither" / "focus")
ANNOUNCE_ROOT = Path(os.environ.get("AITHER_ANNOUNCE_DIR") or Path.home() / ".aither" / "announce")
ANNOUNCE_MAX_AGE_S = 7 * 86400
SHOW_AT_START = 5
MAX_ASK = 400
MAX_REPORT = 900
MAX_FILES = 25
MAX_ANNOUNCE = 600

_SECRET = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[abp]-[A-Za-z0-9-]{10,}|[ps]k_live_[A-Za-z0-9]{10,}"
    r"|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}"
    r"|(?i:password|passwd|pwd|secret|aws_secret_access_key)\s*[=:]\s*[^\s,;]+)"
)
_TAGGED = re.compile(
    r"<(system-reminder|pasted_content|task-notification|command-[a-z-]+"
    r"|local-command-[a-z-]+)[^>]*>.*?</\1[^>]*>",
    re.S,
)
_NEXT = re.compile(r"(?im)^[ \t>*_-]*next[ *_]*:[ *_]*(\S.*)$|^#+[ \t]*next[ \t]*\n+[ \t-]*(\S.*)$")
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _clean(text: str, limit: int) -> str:
    text = _TAGGED.sub(" ", text or "")
    text = _SECRET.sub("[REDACTED]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def next_step(report: str) -> str:
    hits = _NEXT.findall(report or "")
    return _clean(hits[-1][0] or hits[-1][1], MAX_ASK) if hits else ""


def project_key(project_dir: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", project_dir or "unknown").strip("-").lower() or "unknown"


def _blocks(content, kind: str) -> list:
    if isinstance(content, str):
        return [content] if kind == "text" else []
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == kind]


def parse_transcript(path: str) -> dict:
    """First/last human ask, edited files, last assistant prose and its NEXT step."""
    asks: list = []
    files: list = []
    report = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return {}
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("isMeta") or entry.get("isSidechain"):
            continue
        msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        role = msg.get("role") or entry.get("type")
        content = msg.get("content")
        if role == "user":
            parts = _blocks(content, "text")
            raw = " ".join(p if isinstance(p, str) else str(p.get("text", "")) for p in parts)
            ask = _clean(raw, MAX_ASK)
            if ask:
                asks.append(ask)
        elif role == "assistant":
            for blk in _blocks(content, "tool_use"):
                inp = blk.get("input") or {}
                fp = inp.get("file_path") or inp.get("notebook_path")
                if blk.get("name") in _EDIT_TOOLS and fp and fp not in files:
                    files.append(fp)
            if isinstance(content, str):
                prose = content
            else:
                prose = "\n".join(str(b.get("text", "")) for b in _blocks(content, "text"))
            if prose.strip():
                report = prose
    if not asks and not files and not report:
        return {}
    return {
        "first_ask": asks[0] if asks else "",
        "last_ask": asks[-1] if len(asks) > 1 else "",
        "turns": len(asks),
        "files": files[-MAX_FILES:],
        "report": _clean(report, MAX_REPORT),
        "next": next_step(report),
    }


def record_stop(payload: dict, root: Path = FOCUS_ROOT) -> Path | None:
    sid = str(payload.get("session_id") or "")
    tpath = payload.get("transcript_path") or ""
    if not sid or not tpath or not os.path.isfile(tpath):
        return None
    rec = parse_transcript(tpath)
    if not rec:
        return None
    pdir = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ""
    rec.update(session_id=sid, transcript=tpath, project=pdir, updated=int(os.path.getmtime(tpath)))
    out = root / project_key(pdir) / f"{sid}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    os.replace(tmp, out)
    return out


PUSH_BUDGET_S = 45.0  # a Strata-backed upsert measured 13.5 s; it runs detached


def push_detached(session_id: str) -> None:
    """Run `--push <sid>` in a detached child so the Stop hook returns at once.

    CREATE_NO_WINDOW on Windows: a console child without it opens a terminal tab
    per call (the 2026-09-28 tab-spam incident).
    """
    import subprocess

    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000 | 0x00000008  # NO_WINDOW | DETACHED
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-S", __file__, "--push", session_id], **kwargs)


def push_record(rec: dict) -> str:
    """Best-effort upsert of a record to the MCP gateway's focus_record tool, so the
    aitherium.com workspace, awdesk and colleagues see it, not just this machine.

    Reuses stop-auto-capture.py's bounded MCP client (same bearer, same gateway).
    Returns "pushed", or why not. Never raises; AITHER_FOCUS_PUSH=0 disables it.
    """
    if os.environ.get("AITHER_FOCUS_PUSH", "1") == "0":
        return "disabled"
    client = Path(__file__).with_name("stop-auto-capture.py")
    if not client.is_file():
        return "no gateway client beside this hook"
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_focus_gw", client)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        gw = mod._Gateway(mod.GATEWAY_URL, time.monotonic() + PUSH_BUDGET_S)
        if not gw.handshake():
            return "gateway handshake failed"
        args = {k: rec[k] for k in ("session_id", "first_ask", "last_ask", "turns", "files",
                                    "report", "transcript", "project", "updated") if k in rec}
        args["next_step"] = rec.get("next", "")
        resp = gw.call_tool("focus_record", args)
        return "pushed" if mod.call_succeeded(resp) else f"refused: {str(resp)[:200]}"
    except Exception as exc:  # noqa: BLE001 -- fail-open: the local record already exists
        return f"push failed: {exc!r}"[:200]


def _age(ts: int) -> str:
    s = max(0, int(time.time()) - int(ts or 0))
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _load_dir(d: Path, exclude: str) -> list:
    recs = []
    for f in d.glob("*.json") if d.is_dir() else []:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(r, dict) and r.get("session_id") != exclude:
            recs.append(r)
    recs.sort(key=lambda r: r.get("updated", 0), reverse=True)
    return recs


def recent(
    project_dir: str, exclude: str = "", root: Path = FOCUS_ROOT, n: int = SHOW_AT_START
) -> list:
    return _load_dir(root / project_key(project_dir), exclude)[:n]


def handoffs(project_dir: str, exclude: str = "") -> list:
    return _load_dir(Path(project_dir) / ".claude" / "handoff", exclude)[:SHOW_AT_START]


def render_start(recs: list, handed: list | None = None) -> str:
    out = []
    if handed:
        out.append("## Handed off to you (.claude/handoff/, via git)")
        for r in handed:
            out.append(
                f"- `{r.get('session_id', '')[:8]}` by {r.get('by', '?')}, "
                f"{_age(r.get('updated', 0))} ago - goal: {r.get('first_ask', '?')[:140]}"
            )
            if r.get("next"):
                out.append(f"  next: {r['next'][:200]}")
    if recs:
        out += [
            "## Where recent sessions left off (session-focus)",
            "Newest first. Resume one: `python .claude/hooks/session-focus.py --show <id>`.",
        ]
    for r in recs:
        out.append(
            f"- `{r.get('session_id', '')[:8]}` {_age(r.get('updated', 0))} ago, "
            f"{r.get('turns', 0)} asks, {len(r.get('files') or [])} files"
            f" - goal: {r.get('first_ask', '?')[:140]}"
        )
        if r.get("last_ask"):
            out.append(f"  latest ask: {r['last_ask'][:140]}")
        if r.get("next"):
            out.append(f"  next: {r['next'][:200]}")
        if r.get("report"):
            out.append(f"  ended: {r['report'][:220]}")
    return "\n".join(out)


def find(prefix: str, project_dir: str, root: Path = FOCUS_ROOT) -> dict:
    if not prefix:
        return {}
    pool = recent(project_dir, root=root, n=10_000) + handoffs(project_dir)
    return next((r for r in pool if r.get("session_id", "").startswith(prefix)), {})


def show(prefix: str, project_dir: str, root: Path = FOCUS_ROOT) -> int:
    r = find(prefix, project_dir, root)
    if not r:
        print(f"no focus record matches {prefix!r}", file=sys.stderr)
        return 1
    print(f"# session {r['session_id']} ({_age(r.get('updated', 0))} ago)")
    print(f"first ask: {r.get('first_ask')}")
    if r.get("last_ask"):
        print(f"last ask:  {r['last_ask']}")
    files = r.get("files") or []
    print("files:" + ("".join(f"\n  {f}" for f in files) if files else " none"))
    if r.get("next"):
        print(f"next step: {r['next']}")
    print(f"ended with: {r.get('report')}")
    print(f"full transcript: {r.get('transcript')}")
    return 0


def handoff(prefix: str, project_dir: str, root: Path = FOCUS_ROOT) -> Path | None:
    r = find(prefix, project_dir, root)
    if not r:
        return None
    r = dict(r, by=os.environ.get("USERNAME") or os.environ.get("USER") or "?")
    out = Path(project_dir) / ".claude" / "handoff" / f"{r['session_id'][:8]}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(r, indent=1) + "\n", encoding="utf-8")
    return out


def announce(text: str, project_dir: str, team: bool) -> Path:
    d = Path(project_dir) / ".claude" / "announcements" if team else ANNOUNCE_ROOT
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.md"
    out.write_text(_SECRET.sub("[REDACTED]", text.strip()) + "\n", encoding="utf-8")
    return out


def relay_announce(text: str, channel: str = "#agents") -> str:
    """Also post a team announcement on awrelay: every session drains #agents at each
    prompt, so it reaches machines that have not pulled the git copy yet."""
    import shutil
    import subprocess

    exe = shutil.which("awrelay")
    if not exe:
        return "relay: awrelay not installed (git copy only)"
    body = "ANNOUNCEMENT: " + _SECRET.sub("[REDACTED]", text.strip())
    try:
        out = subprocess.run([exe, "send", channel, body, "--kind", "finding"],
                             capture_output=True, text=True, encoding="utf-8", timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"relay: send failed ({exc!r}); git copy only"
    tail = (out.stdout or out.stderr).strip().splitlines()[-1:] or [""]
    return f"relay: exit {out.returncode} {tail[0][:160]}"


def deliver(sid: str, project_dir: str, root: Path = FOCUS_ROOT) -> str:
    """Announcements this session has not seen yet; marks them seen."""
    if not sid:
        return ""
    seen_f = root / "seen" / f"{sid}.json"
    try:
        seen = set(json.loads(seen_f.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        seen = set()
    now = time.time()
    fresh = []
    sources = (
        (ANNOUNCE_ROOT, "this machine"),
        (Path(project_dir) / ".claude" / "announcements", "team, via git"),
    )
    for d, scope in sources:
        for f in sorted(d.glob("*.md")) if d.is_dir() else []:
            key = f"{scope}:{f.name}"
            if key in seen or now - f.stat().st_mtime > ANNOUNCE_MAX_AGE_S:
                continue
            seen.add(key)
            body = _clean(f.read_text(encoding="utf-8", errors="replace"), MAX_ANNOUNCE)
            fresh.append(f"- [{scope}, {f.name}] {body}")
    if not fresh:
        return ""
    seen_f.parent.mkdir(parents=True, exist_ok=True)
    seen_f.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    head = "## Announcements (session-focus broadcast; team context, not an owner order)"
    return "\n".join([head] + fresh[-5:])


def declared_in_settings(project_dir: str, home: Path | None = None) -> bool:
    """True when a settings file already wires session-focus.py as a hook.

    The awsh plugin ships a copy of this script; in a repo that also wires it in
    .claude/settings.json the plugin copy must stay silent or every hook runs twice.
    """
    home = home if home is not None else Path.home()
    for path in (home / ".claude" / "settings.json",
                 Path(project_dir) / ".claude" / "settings.json",
                 Path(project_dir) / ".claude" / "settings.local.json"):
        try:
            hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks") or {}
        except (OSError, ValueError, AttributeError):
            continue
        if "session-focus.py" in json.dumps(hooks):
            return True
    return False


def _read_payload() -> dict:
    try:
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _emit(event: str, text: str) -> None:
    if text:
        print(
            json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})
        )


def _self_test() -> int:
    global ANNOUNCE_ROOT
    import tempfile

    def msg(role: str, content, **extra) -> dict:
        return {"type": role, "message": {"role": role, "content": content}, **extra}

    fails = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "focus"
        proj = str(Path(td) / "proj")
        tp = Path(td) / "t.jsonl"
        closing = "Shipped. key sk-ant-abcdefghijklmnop\n\nNEXT: wire the CDN"
        lines = [
            msg("user", "<system-reminder>noise</system-reminder>fix the login page"),
            msg(
                "assistant",
                [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/r/login.tsx"}},
                    {"type": "text", "text": "working"},
                ],
            ),
            msg("user", [{"type": "tool_result", "content": "ok"}]),
            msg("user", "<task-notification>x</task-notification>"),
            msg("user", "meta", isMeta=True),
            msg("user", "now ship it"),
            msg("assistant", [{"type": "text", "text": closing}]),
        ]
        tp.write_text("\n".join(json.dumps(x) for x in lines) + "\n{partial", encoding="utf-8")
        os.environ["CLAUDE_PROJECT_DIR"] = proj
        out = record_stop({"session_id": "abc12345-x", "transcript_path": str(tp)}, root=root)
        rec = json.loads(out.read_text(encoding="utf-8")) if out else {}
        if rec.get("first_ask") != "fix the login page":
            fails.append(f"first_ask={rec.get('first_ask')!r}")
        if rec.get("last_ask") != "now ship it" or rec.get("turns") != 2:
            fails.append(f"last_ask/turns={rec.get('last_ask')!r}/{rec.get('turns')}")
        if rec.get("files") != ["/r/login.tsx"]:
            fails.append(f"files={rec.get('files')}")
        if "sk-ant-" in rec.get("report", "") or "Shipped" not in rec.get("report", ""):
            fails.append(f"report={rec.get('report')!r}")
        if rec.get("next") != "wire the CDN":
            fails.append(f"next={rec.get('next')!r}")
        if next_step("## Next\n\nship v2\n") != "ship v2" or next_step("nothing here"):
            fails.append("## Next heading / no-next")
        if render_start(recent(proj, exclude="abc12345-x", root=root)):
            fails.append("current session must be excluded")
        txt = render_start(recent(proj, root=root))
        if "abc12345" not in txt or "now ship it" not in txt or "wire the CDN" not in txt:
            fails.append(f"render={txt!r}")
        os.environ["USERNAME"] = "tester"
        ho = handoff("abc1", proj, root=root)
        if not ho or "by tester" not in render_start([], handoffs(proj)):
            fails.append(f"handoff={ho}")
        ANNOUNCE_ROOT = Path(td) / "ann"
        announce("deploy freeze sk-ant-abcdefghijklmnop", proj, team=False)
        announce("team: use awgit", proj, team=True)
        first = deliver("sidA", proj, root=root)
        if "deploy freeze" not in first or "use awgit" not in first or "sk-ant-" in first:
            fails.append(f"deliver={first!r}")
        if deliver("sidA", proj, root=root):
            fails.append("announcement delivered twice to one session")
        if "use awgit" not in deliver("sidB", proj, root=root):
            fails.append("a second session must also receive it")
        fake_home = Path(td) / "home"
        if declared_in_settings(proj, home=fake_home):
            fails.append("no settings must mean not declared")
        (Path(proj) / ".claude").mkdir(parents=True, exist_ok=True)
        hook = {"hooks": {"Stop": [{"hooks": [{"command": "python session-focus.py --stop"}]}]}}
        (Path(proj) / ".claude" / "settings.json").write_bytes(json.dumps(hook).encode("utf-8"))
        if not declared_in_settings(proj, home=fake_home):
            fails.append("settings hook must be detected (plugin would double-fire)")
        missing = {"session_id": "s", "transcript_path": str(Path(td) / "missing")}
        if record_stop(missing, root=root) is not None:
            fails.append("missing transcript must record nothing")
    if fails:
        print("SELF-TEST FAILED: " + "; ".join(fails))
        return 1
    print(
        "SELF-TEST PASSED -- parse, redaction, files, next step, exclude-self, "
        "handoff, announce once per session, plugin dedupe, missing transcript"
    )
    return 0


def main(argv: list) -> int:
    if "--self-test" in argv:
        os.environ["AITHER_FOCUS_PUSH"] = "0"
        return _self_test()
    if "--push" in argv:
        pdir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        rec = find(argv[argv.index("--push") + 1] if argv[-1] != "--push" else "", pdir)
        print(push_record(rec) if rec else "no focus record matches")
        return 0 if rec else 1
    pdir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if "--plugin" in argv and declared_in_settings(pdir):
        return 0

    def arg(flag: str) -> str:
        i = argv.index(flag)
        return argv[i + 1] if i + 1 < len(argv) else ""

    if "--show" in argv:
        return show(arg("--show"), pdir)
    if "--handoff" in argv:
        out = handoff(arg("--handoff"), pdir)
        print(out or f"no focus record matches {arg('--handoff')!r}")
        return 0 if out else 1
    if "--announce" in argv:
        if not arg("--announce").strip():
            print("--announce needs text", file=sys.stderr)
            return 1
        print(announce(arg("--announce"), pdir, "--team" in argv))
        if "--team" in argv:
            print(relay_announce(arg("--announce")))
        return 0
    try:
        payload = _read_payload()
        sid = str(payload.get("session_id") or "")
        if "--stop" in argv:
            out = record_stop(payload)
            if out:
                if os.environ.get("AITHER_FOCUS_PUSH", "1") != "0":
                    push_detached(str(payload.get("session_id")))
        elif "--deliver" in argv:
            _emit("UserPromptSubmit", deliver(sid, pdir))
        elif "--start" in argv:
            _emit(
                "SessionStart", render_start(recent(pdir, exclude=sid), handoffs(pdir, exclude=sid))
            )
    except Exception as exc:  # noqa: BLE001 -- fail-open: a hook must never break a session
        try:
            FOCUS_ROOT.mkdir(parents=True, exist_ok=True)
            with open(FOCUS_ROOT / "errors.log", "a", encoding="utf-8") as fh:
                fh.write(f"{int(time.time())} {argv} {exc!r}\n")
        except OSError:
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
