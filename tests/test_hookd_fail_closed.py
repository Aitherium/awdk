"""hookd fail-closed mode + dead-daemon bridge (`hookd call --fail-closed`).

Closes the fail-open HTTP hook gap:
1. Setting precedence: `?fail=closed|open` > `AITHER_HOOKD_FAIL_CLOSED` >
   `~/.aither/hookd/config.json` > default False.
2. `merge(..., fail_closed=True)` turns PreToolUse crashes/timeouts (code 1 / 124)
   into `permissionDecision: "deny"` while respecting `SOFT` (`|| true`) hooks.
3. Server `/pdp` and `/run` both honor `is_fail_closed()`.
4. `call_hookd()` tries HTTP fast path, heals once if requested, falls back to direct
   in-process execution when hookd is dead, and denies fail-closed on PreToolUse
   if even direct fallback cannot run.
5. `convert_hooks(..., fail_closed=True)` wraps PreToolUse guards in exec-form
   `hookd call --fail-closed` so a dead daemon can never bypass a PreToolUse guard.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from adk.harnesses import hookd


def _unused_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _r(code: int, out: str = "", err: str = "", **extra) -> dict:
    return {"code": code, "out": out, "err": err, "ms": 0, **extra}


def test_is_fail_closed_precedence(tmp_path, monkeypatch):
    cfg = tmp_path / "hookd-config.json"
    monkeypatch.setenv("AITHER_HOOKD_CONFIG", str(cfg))
    monkeypatch.delenv("AITHER_HOOKD_FAIL_CLOSED", raising=False)

    assert hookd.is_fail_closed() is False

    hookd.save_config({"fail_closed": True})
    assert hookd.is_fail_closed() is True
    assert hookd.is_fail_closed({"fail": ["open"]}) is False

    monkeypatch.setenv("AITHER_HOOKD_FAIL_CLOSED", "0")
    assert hookd.is_fail_closed() is False
    assert hookd.is_fail_closed({"fail": ["closed"]}) is True

    monkeypatch.setenv("AITHER_HOOKD_FAIL_CLOSED", "1")
    assert hookd.is_fail_closed() is True
    assert hookd.is_fail_closed({"fail": ["open"]}) is False


def test_config_cli_round_trip(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "cfg.json"
    monkeypatch.setenv("AITHER_HOOKD_CONFIG", str(cfg))
    monkeypatch.delenv("AITHER_HOOKD_FAIL_CLOSED", raising=False)

    assert hookd.main(["config", "--fail-closed", "on"]) == 0
    on_out = json.loads(capsys.readouterr().out)
    assert on_out["fail_closed"] is True and on_out["effective_fail_closed"] is True

    assert hookd.main(["config", "--fail-closed", "off"]) == 0
    off_out = json.loads(capsys.readouterr().out)
    assert off_out["fail_closed"] is False and off_out["effective_fail_closed"] is False


def test_merge_fail_closed_denies_pretooluse_crash_and_timeout():
    # Legacy default keeps non-zero crash non-blocking.
    assert hookd.merge("PreToolUse", [_r(1, err="boom")], fail_closed=False) == {}
    assert hookd.merge("PreToolUse", [_r(124, err="timed out")], fail_closed=False) == {}

    # Fail-closed turns PreToolUse crashes & timeouts into explicit denies.
    m_crash = hookd.merge("PreToolUse", [_r(1, err="guard crashed")], fail_closed=True)
    assert m_crash["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "exit 1" in m_crash["hookSpecificOutput"]["permissionDecisionReason"]

    m_timeout = hookd.merge("PreToolUse", [_r(124, err="no free worker")], fail_closed=True)
    assert m_timeout["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "exit 124" in m_timeout["hookSpecificOutput"]["permissionDecisionReason"]

    # Non-PreToolUse events (PostToolUse/Stop) stay non-blocking on non-zero crashes.
    assert hookd.merge("PostToolUse", [_r(1, err="boom")], fail_closed=True) == {}

    # SOFT (`|| true`) hooks softened to code=0 stay non-blocking.
    soft_res = hookd.soften(_r(1, err="hub down"), True)
    assert hookd.merge("PreToolUse", [soft_res], fail_closed=True) == {}


def test_ephemeral_server_pdp_and_run_honor_env_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(hookd, "STATE_DIR", tmp_path / "hookd")
    monkeypatch.setenv("AITHER_HOOKD_CONFIG", str(tmp_path / "hookd" / "config.json"))
    monkeypatch.setenv("AITHER_HOOKD_FAIL_CLOSED", "1")
    monkeypatch.setitem(hookd._PDP, "loaded", True)
    monkeypatch.setitem(hookd._PDP, "api", None)

    hooks_dir = tmp_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    crash_hook = hooks_dir / "crash.py"
    crash_hook.write_text("import sys; sys.stderr.write('kaboom'); sys.exit(1)\n", encoding="utf-8")

    srv, _ = hookd.ephemeral_server(1)
    assert srv is not None
    port = srv.server_address[1]
    try:
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "cwd": str(tmp_path),
        }).encode("utf-8")

        # 1. /pdp?mode=enforce fails closed via AITHER_HOOKD_FAIL_CLOSED=1 even without ?fail=closed.
        req_pdp = urllib.request.Request(
            f"http://127.0.0.1:{port}/pdp?mode=enforce",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "X-Hookd": "1"},
        )
        with urllib.request.urlopen(req_pdp, timeout=10) as r:
            body = json.loads(r.read())
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"

        # 2. /run with a crashing PreToolUse hook fails closed via AITHER_HOOKD_FAIL_CLOSED=1.
        q = urllib.parse.urlencode({"h": ".claude/hooks/crash.py"})
        req_run = urllib.request.Request(
            f"http://127.0.0.1:{port}/run?{q}",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Hookd": "1",
                "X-Project-Dir": str(tmp_path),
            },
        )
        with urllib.request.urlopen(req_run, timeout=10) as r:
            run_body = json.loads(r.read())
        assert run_body["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "kaboom" in run_body["hookSpecificOutput"]["permissionDecisionReason"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_call_hookd_dead_daemon_runs_direct_fallback_and_fails_closed(tmp_path, monkeypatch):
    hooks_dir = tmp_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    guard = hooks_dir / "guard.py"
    guard.write_text("import sys; sys.stderr.write('blocked dangerous git'); sys.exit(2)\n", encoding="utf-8")

    dead_port = _unused_port()
    payload = json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git reset --hard"},
        "cwd": str(tmp_path),
    }).encode("utf-8")

    # Case A: hookd is dead, valid allowlisted guard -> direct fallback runs and DENIES.
    q = urllib.parse.urlencode({"h": ".claude/hooks/guard.py"})
    rc, out, _ = hookd.call_hookd(
        dead_port, q, payload, project_dir=str(tmp_path), fail_closed=True, auto_ensure=False
    )
    assert rc == 0
    resp = json.loads(out)
    assert resp["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "blocked dangerous git" in resp["hookSpecificOutput"]["permissionDecisionReason"]

    # Case B: hookd is dead AND spec cannot run (outside allowed roots) -> fail-closed DENY.
    bad_q = urllib.parse.urlencode({"h": "/definitely/outside/allowed/evil.py"})
    rc2, out2, _ = hookd.call_hookd(
        dead_port, bad_q, payload, project_dir=str(tmp_path), fail_closed=True, auto_ensure=False
    )
    assert rc2 == 0
    resp2 = json.loads(out2)
    assert resp2["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "failing closed" in resp2["hookSpecificOutput"]["permissionDecisionReason"]


def test_convert_hooks_defaults_pretooluse_to_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HOOKD_CONFIG", str(tmp_path / "missing-cfg.json"))
    monkeypatch.delenv("AITHER_HOOKD_FAIL_CLOSED", raising=False)
    hooks_dir = tmp_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "g.py").write_text("", encoding="utf-8")

    hooks = {
        "PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "python", "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/g.py"]}
        ]}],
        "PostToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "python", "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/g.py"]}
        ]}],
    }
    # Default (fail_closed=None): PreToolUse wraps in fail-closed `hookd call --fail-closed`.
    out, n = hookd.convert_hooks(hooks, str(tmp_path), port=8369)
    assert n == 2
    pre_entry = out["PreToolUse"][0]["hooks"][0]
    assert pre_entry["type"] == "command"
    assert "call" in pre_entry["args"] and "--fail-closed" in pre_entry["args"]
    post_entry = out["PostToolUse"][0]["hooks"][0]
    assert post_entry["type"] == "http"

    # Explicit opt-out via fail_closed=False or AITHER_HOOKD_FAIL_CLOSED=0 emits legacy http.
    out_open, _ = hookd.convert_hooks(hooks, str(tmp_path), port=8369, fail_closed=False)
    assert out_open["PreToolUse"][0]["hooks"][0]["type"] == "http"

    monkeypatch.setenv("AITHER_HOOKD_FAIL_CLOSED", "0")
    out_env_open, _ = hookd.convert_hooks(hooks, str(tmp_path), port=8369)
    assert out_env_open["PreToolUse"][0]["hooks"][0]["type"] == "http"


def test_claude_mod_register_ts_defaults_hookd_ensure_fail_closed():
    reg_path = Path(__file__).resolve().parents[1] / "adk" / "harnesses" / "claude_mod" / "hooks" / "register.ts"
    text = reg_path.read_text(encoding="utf-8")
    assert "export const HOOKD_PRETOOLUSE_FAIL_CLOSED = true" in text
    assert "'--fail-closed'" in text
    assert '"call"' in text and "--port" in text
