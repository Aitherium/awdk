"""Session-focus records for hookless harnesses (codex, gemini, aider, opencode).

Claude Code writes ~/.aither/focus/<project-key>/<sid>.json from its Stop hook and
reads it back at SessionStart. Daemon-run non-Claude harnesses have no hooks, so the
session writes the same record from its own events and the daemon prefixes the
previous session's next step at spawn.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from adk.harnesses import focus, registry
from adk.harnesses.events import EventKind, HarnessEvent, text_delta, tool_call
from adk.harnesses.session import HarnessSession, SessionConfig
from fastapi.testclient import TestClient

TOKEN = "root-bearer-for-tests-only"


@pytest.fixture()
def focus_dir(tmp_path, monkeypatch):
    root = tmp_path / "focus"
    monkeypatch.setenv("AITHER_FOCUS_DIR", str(root))
    monkeypatch.delenv("AITHER_FOCUS_INJECT", raising=False)
    return root


def _run_turn(session: HarnessSession, prompt: str, reply: str, *, edit: str = "") -> None:
    session._emit(HarnessEvent(kind=EventKind.TURN_STARTED, text=prompt))
    if edit:
        session._emit(tool_call("apply_patch", "t1", {"changes": [{"path": edit}]}))
    session._emit(text_delta(reply[: len(reply) // 2]))
    session._emit(text_delta(reply[len(reply) // 2:]))
    session._emit(HarnessEvent(kind=EventKind.TURN_COMPLETED, data={"exit_code": 0}))


def _records(root):
    return [json.loads(p.read_text(encoding="utf-8")) for p in root.rglob("*.json")]


def test_codex_turn_completed_writes_the_focus_record(focus_dir, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    s = HarnessSession(registry.get("codex"), SessionConfig(harness="codex", cwd=str(proj)),
                       root=tmp_path / "sessions")
    _run_turn(s, "<session-context>\nDOCTRINE\n</session-context>\n\nfix the login page",
              "working on it", edit="/r/login.tsx")
    _run_turn(s, "now ship it", "Shipped. key sk-ant-abcdefghijklmnop\n\nNEXT: wire the CDN")

    out = focus_dir / focus.project_key(os.path.abspath(str(proj))) / f"{s.id}.json"
    assert out.is_file()
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["first_ask"] == "fix the login page"
    assert rec["last_ask"] == "now ship it"
    assert rec["turns"] == 2
    assert rec["files"] == ["/r/login.tsx"]
    assert rec["next"] == "wire the CDN"
    assert "sk-ant-" not in rec["report"] and "Shipped" in rec["report"]
    assert rec["session_id"] == s.id and rec["transcript"].endswith("events.jsonl")
    assert rec["project"] == os.path.abspath(str(proj))
    # the key matches the Claude Code hook's key for the same directory
    assert out.parent.name == focus.project_key(rec["project"])


def test_claude_session_writes_no_record(focus_dir, tmp_path):
    s = HarnessSession(registry.get("claude"), SessionConfig(harness="claude", cwd=str(tmp_path)),
                       root=tmp_path / "sessions")
    _run_turn(s, "hello", "hi\nNEXT: nothing")
    assert not s.writes_focus_record
    assert _records(focus_dir) == []


def test_resume_line_respects_age_and_env(focus_dir, tmp_path, monkeypatch):
    proj = os.path.abspath(str(tmp_path / "p"))
    focus.write_record(session_id="old1", project=proj, asks=["a"], files=[],
                       report="NEXT: stale step")
    assert focus.resume_line(proj) == "Previous session in this project stopped at: stale step"
    monkeypatch.setenv("AITHER_FOCUS_INJECT", "0")
    assert focus.resume_line(proj) == ""
    monkeypatch.delenv("AITHER_FOCUS_INJECT")
    rec_path = focus_dir / focus.project_key(proj) / "old1.json"
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    rec["updated"] = int(time.time()) - focus.RECENT_SECONDS - 60
    rec_path.write_text(json.dumps(rec), encoding="utf-8")
    assert focus.resume_line(proj) == ""


@pytest.fixture()
def client(tmp_path, monkeypatch, focus_dir):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("AITHER_STEER_DISPATCH_STATUS", str(tmp_path / "dispatch.json"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    monkeypatch.setenv("AITHER_HARNESS_PRINCIPALS", str(tmp_path / "harness_tokens.json"))
    monkeypatch.setenv("AITHER_HOME_OVERRIDE", str(tmp_path / "nohome"))

    import adk.harnesses.daemon as daemon
    from adk.harnesses import rooms as rooms_mod
    from adk.harnesses import session as session_mod
    from adk.harnesses import session_directory as directory_mod
    from adk.harnesses.manager import SessionManager

    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", tmp_path / "harness_tokens.json")
    monkeypatch.setattr(daemon, "allowed_roots", lambda: [])
    monkeypatch.setattr(rooms_mod, "_registry", None)
    monkeypatch.setattr(
        directory_mod, "_directory", directory_mod.SessionDirectory(discover_fn=lambda: []),
    )
    monkeypatch.setattr(session_mod.HarnessSession, "start", lambda self: None)
    monkeypatch.setattr(daemon, "recall_awm_context", lambda cwd: "")

    mgr = SessionManager(root=tmp_path / "sessions")
    c = TestClient(daemon.create_app(manager=mgr, token=TOKEN))
    c.headers = {"Authorization": f"Bearer {TOKEN}"}
    return c, mgr


def _spawn_ctx(c, mgr, harness, cwd, system_prompt=""):
    r = c.post("/sessions", json={"harness": harness, "cwd": cwd,
                                  "system_prompt_append": system_prompt})
    assert r.status_code == 200, r.text
    return mgr._sessions[r.json()["id"]].config.system_prompt_append


def test_daemon_injects_previous_next_step_for_codex_only(client, tmp_path, monkeypatch):
    c, mgr = client
    proj = tmp_path / "proj"
    proj.mkdir()
    marker = "Previous session in this project stopped at: wire the CDN"
    # no record yet -> nothing injected
    assert marker not in _spawn_ctx(c, mgr, "codex", str(proj))
    focus.write_record(session_id="prev", project=os.path.abspath(str(proj)),
                       asks=["ship"], files=[], report="done\nNEXT: wire the CDN")
    ctx = _spawn_ctx(c, mgr, "codex", str(proj))
    assert ctx.startswith(marker)
    # caller supplied its own system prompt -> left alone
    assert marker not in _spawn_ctx(c, mgr, "codex", str(proj), system_prompt="mine")
    # claude has its own SessionStart hook
    assert marker not in _spawn_ctx(c, mgr, "claude", str(proj))
    # opt-out
    monkeypatch.setenv("AITHER_FOCUS_INJECT", "0")
    assert marker not in _spawn_ctx(c, mgr, "codex", str(proj))
