"""The `claude-tty` harness: the REAL interactive Claude Code behind a daemon-owned pty.

Pins the four facts that make a tab MANAGED rather than discovered (2026-09-19):
argv is the TUI (no -p), the daemon mints ONE --session-id, the child env never
inherits the daemon's model wiring, and the session directory folds the discovered
copy of that same session into the daemon row. Nothing here spawns a process.
"""

import os

import pytest

from adk.harnesses import pty_session as pty_mod
from adk.harnesses.discovery import DiscoveredSession
from adk.harnesses.models import MANAGED_VARS
from adk.harnesses.pty_session import PtyHarnessSession
from adk.harnesses.registry import SPECS, LaunchSpec, Transport, exe_from_cmd_shim
from adk.harnesses.session import SessionConfig
from adk.harnesses.session_directory import SessionDirectory

UUID = "0b1afff7-4a0d-4eab-8036-41ff82c7f9c1"


def test_claude_tty_is_a_pty_program_harness_without_print_mode():
    spec = SPECS["claude-tty"]
    assert spec.transport is Transport.PTY_STREAM
    assert spec.supports_resume and not spec.supports_model_binding
    argv = spec.argv(LaunchSpec(session_id=UUID, title="awsh main"))
    assert argv[0] == "claude"
    assert argv[argv.index("--session-id") + 1] == UUID
    assert argv[argv.index("--name") + 1] == "awsh main"
    # The whole point: this is the screen, not the SDK.
    assert "-p" not in argv
    assert "--output-format" not in argv
    assert "--input-format" not in argv


def test_claude_tty_resume_uses_resume_and_never_a_fresh_id():
    argv = SPECS["claude-tty"].argv(LaunchSpec(resume_session_id="r-1", session_id=UUID))
    assert argv[argv.index("--resume") + 1] == "r-1"
    assert "--session-id" not in argv


def test_claude_tty_forwards_permission_mode_and_extra_args():
    argv = SPECS["claude-tty"].argv(
        LaunchSpec(session_id=UUID, permission_mode="acceptEdits", extra_args=["--verbose"])
    )
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[-1] == "--verbose"


def test_exe_from_cmd_shim_follows_the_npm_shim_to_the_exe():
    shim = r'"%dp0%\node_modules\@anthropic-ai\claude-code\bin\claude.exe"   %*'
    got = exe_from_cmd_shim(os.path.join("C:\\", "npm", "claude.cmd"), text=shim)
    assert got is not None
    assert got.replace("/", "\\").endswith(
        "npm\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"
    )
    assert exe_from_cmd_shim("x.cmd", text="@echo off\r\nnode %*\r\n") is None


def _session(tmp_path, **cfg):
    config = SessionConfig(harness="claude-tty", cwd=str(tmp_path), **cfg)
    return PtyHarnessSession(SPECS["claude-tty"], config, root=tmp_path)


def test_resolve_argv_mints_one_id_and_resolves_the_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(pty_mod, "resolve_binary", lambda spec: "/opt/claude/claude")
    s = _session(tmp_path, title="tab one")
    argv = s._resolve_argv()
    assert argv is not None
    assert argv[0] == "/opt/claude/claude"
    assert s.harness_session_id, "the daemon must mint the id before the program starts"
    assert argv[argv.index("--session-id") + 1] == s.harness_session_id
    assert argv[argv.index("--name") + 1] == "tab one"
    assert "-p" not in argv


def test_resolve_argv_on_resume_adopts_the_resumed_id(tmp_path, monkeypatch):
    monkeypatch.setattr(pty_mod, "resolve_binary", lambda spec: "/opt/claude/claude")
    s = _session(tmp_path, resume_session_id=UUID)
    argv = s._resolve_argv()
    assert argv[argv.index("--resume") + 1] == UUID
    assert s.harness_session_id == UUID
    assert "--session-id" not in argv


def test_resolve_argv_fails_loudly_when_claude_is_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(pty_mod, "resolve_binary", lambda spec: None)
    s = _session(tmp_path)
    assert s._resolve_argv() is None
    assert s.state == "failed"


def test_program_env_never_inherits_the_daemons_model_wiring(tmp_path, monkeypatch):
    for var in MANAGED_VARS:
        monkeypatch.setenv(var, "poisoned")
    monkeypatch.setenv("PATH_KEEPS", "1")
    s = _session(tmp_path)
    env = s._scrub_program_env(s._child_env())
    assert not any(var in env for var in MANAGED_VARS)
    assert env["PATH_KEEPS"] == "1"
    # A plain shell keeps the owner's environment -- the scrub is for PROGRAM harnesses.
    shell = PtyHarnessSession(SPECS["terminal"], SessionConfig(harness="terminal"), root=tmp_path)
    assert "ANTHROPIC_BASE_URL" in shell._scrub_program_env(shell._child_env())


def test_directory_folds_the_discovered_copy_into_the_daemon_row():
    discovered = [
        DiscoveredSession(
            id=UUID, cwd="C:/repo", name="AitherOS-Fresh", pid=4242, entrypoint="cli",
            kind="interactive", status="idle", transcript_path="",
        )
    ]
    directory = SessionDirectory(discover_fn=lambda: discovered)
    daemon_rows = [{
        "id": "d1", "harness": "claude-tty", "harness_label": "Claude Code (terminal)",
        "harness_session_id": UUID, "title": "tab one", "cwd": "C:/repo", "transcript": "",
    }]
    rows = directory.list_sessions_sync(daemon_rows)
    assert [r.id for r in rows] == ["d1"]
    assert rows[0].origin == "daemon"
    assert rows[0].steer_capability == "full"


def test_directory_still_lists_a_genuinely_separate_discovered_tab():
    discovered = [
        DiscoveredSession(
            id="other-tab", cwd="C:/repo", name="AitherOS-Fresh", pid=1, entrypoint="cli",
            kind="interactive", status="idle", transcript_path="",
        )
    ]
    directory = SessionDirectory(discover_fn=lambda: discovered)
    rows = directory.list_sessions_sync([{"id": "d1", "harness_session_id": UUID}])
    assert sorted(r.id for r in rows) == ["d1", "other-tab"]


@pytest.mark.parametrize("harness", ["claude", "claude-tty"])
def test_both_claude_harnesses_share_the_binary_but_not_the_transport(harness):
    spec = SPECS[harness]
    assert spec.binary == "claude"
    assert (spec.transport is Transport.PTY_STREAM) == (harness == "claude-tty")


def test_child_env_never_says_it_is_inside_a_claude_session(tmp_path, monkeypatch):
    # A daemon started from a Claude Code shell carries these; measured 2026-09-19 a
    # claude-tty spawned under them rendered nothing at all.
    for var in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_TOKEN",
                "CLAUDE_PID", "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.setenv(var, "nested")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "C:/keep/me")
    for harness in ("claude-tty", "terminal", "claude"):
        s = PtyHarnessSession(SPECS[harness], SessionConfig(harness=harness), root=tmp_path)
        env = s._child_env()
        assert not [k for k in env if k.startswith("CLAUDE_CODE_")], harness
        assert "CLAUDECODE" not in env and "CLAUDE_PID" not in env, harness
        assert env["CLAUDE_CONFIG_DIR"] == "C:/keep/me", harness
        assert env["AITHER_HARNESS_SESSION"] == s.id


def test_a_dead_daemon_session_is_not_idle_and_not_steerable():
    # Measured 2026-09-19: a killed claude-tty (state=exited, exit 2) listed as idle/full.
    directory = SessionDirectory(discover_fn=lambda: [])
    rows = directory.list_sessions_sync([
        {"id": "dead1", "state": "exited", "exit_code": 2, "title": "gone", "transcript": ""},
        {"id": "bad1", "state": "failed", "title": "never started", "transcript": ""},
        {"id": "live1", "state": "ready", "title": "here", "transcript": ""},
    ])
    by_id = {r.id: r for r in rows}
    assert by_id["dead1"].status == "exited" and by_id["dead1"].steer_capability == "none"
    assert by_id["bad1"].status == "exited" and by_id["bad1"].steer_capability == "none"
    assert by_id["live1"].status != "exited" and by_id["live1"].steer_capability == "full"
