"""Session context reaches every harness, not only Claude Code (2026-09-29).

Measured before this test: the daemon appended the reasoning doctrine to
``system_prompt_append`` for every foreign harness, but only the Claude argv
builders read that field. gemini, codex, aider and opencode build argv from the
turn prompt alone, so the doctrine, any rendered skill and any memory were
dropped with no error. Those specs now say ``prompt_carries_context`` and the
session frames the context into the prompt; the daemon also recalls awm facts
for them, since they have no SessionStart hook of their own.
"""

from __future__ import annotations

import pytest
from adk.harnesses import registry
from adk.harnesses.session import HarnessSession, SessionConfig
from fastapi.testclient import TestClient

TOKEN = "root-bearer-for-tests-only"


def test_only_flagless_harnesses_carry_context_in_the_prompt():
    for hid in ("gemini", "codex", "aider", "opencode"):
        assert registry.get(hid).prompt_carries_context, hid
    assert not registry.get("claude").prompt_carries_context


def _session(harness_id, ctx, resumed=""):
    s = HarnessSession.__new__(HarnessSession)
    s.spec = registry.get(harness_id)
    s.config = SessionConfig(harness=harness_id, system_prompt_append=ctx)
    s.harness_session_id = resumed
    return s


def test_codex_prompt_carries_the_context_every_turn():
    out = _session("codex", "DOCTRINE + MEMORY")._prompt_with_context("fix the bug")
    assert out.startswith("<session-context>\nDOCTRINE + MEMORY\n</session-context>")
    assert out.endswith("fix the bug")
    # the argv the process actually gets carries it
    argv = registry.get("codex").argv(registry.LaunchSpec(prompt=out))
    assert "DOCTRINE + MEMORY" in argv[-1]


def test_resumed_gemini_is_not_told_twice_and_empty_context_is_a_noop():
    assert _session("gemini", "CTX", resumed="g-1")._prompt_with_context("hi") == "hi"
    assert "CTX" in _session("gemini", "CTX")._prompt_with_context("hi")
    assert _session("codex", "")._prompt_with_context("hi") == "hi"
    assert _session("claude", "CTX")._prompt_with_context("hi") == "hi"


def test_recall_awm_context_extracts_text_and_survives_failure(monkeypatch):
    pytest.importorskip("awm")
    import adk.harnesses.daemon as daemon
    import awm.claude_hook as hook

    monkeypatch.setattr(hook, "build", lambda db, cwd=None: {
        "hookSpecificOutput": {"additionalContext": "awm memory for x:\n- k: v"}})
    assert daemon.recall_awm_context(".") == "awm memory for x:\n- k: v"
    monkeypatch.setattr(hook, "build", lambda db, cwd=None: None)
    assert daemon.recall_awm_context(".") == ""

    def boom(db, cwd=None):
        raise OSError("locked")

    monkeypatch.setattr(hook, "build", boom)
    assert daemon.recall_awm_context(".") == ""


@pytest.fixture()
def client(tmp_path, monkeypatch):
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
    monkeypatch.setattr(rooms_mod, "_registry", None)
    monkeypatch.setattr(
        directory_mod, "_directory", directory_mod.SessionDirectory(discover_fn=lambda: []),
    )
    monkeypatch.setattr(session_mod.HarnessSession, "start", lambda self: None)
    monkeypatch.setattr(daemon, "recall_awm_context", lambda cwd: "AWM-FACTS")

    mgr = SessionManager(root=tmp_path / "sessions")
    c = TestClient(daemon.create_app(manager=mgr, token=TOKEN))
    c.headers = {"Authorization": f"Bearer {TOKEN}"}
    return c, mgr


def test_daemon_gives_codex_memory_and_claude_keeps_its_own_hook(client):
    c, mgr = client
    codex = c.post("/sessions", json={"harness": "codex", "cwd": ""})
    assert codex.status_code == 200, codex.text
    assert "AWM-FACTS" in mgr._sessions[codex.json()["id"]].config.system_prompt_append
    claude = c.post("/sessions", json={"harness": "claude", "cwd": ""})
    assert claude.status_code == 200, claude.text
    assert "AWM-FACTS" not in mgr._sessions[claude.json()["id"]].config.system_prompt_append
