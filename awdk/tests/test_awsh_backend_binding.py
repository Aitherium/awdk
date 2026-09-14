"""A session spawned through awsh must be able to choose its BACKEND.

Which model answers a coding session is decided once, at launch, from the
child's environment. A spawn tool that cannot express that choice can only ever
produce sessions on the global default, however carefully the caller asked for
something else.

The daemon accepted `model_profile` on POST /sessions the whole time and
resolved it into a per-session ModelBinding. The MCP tool never sent it. Nothing
failed: sessions came up healthy, answered turns, and were quietly on the wrong
model. These tests pin the wire contract so that gap cannot reopen.
"""

from __future__ import annotations

import pytest

from adk.harnesses import mcp_stdio


@pytest.fixture
def captured(monkeypatch):
    """Capture the request instead of reaching the daemon."""
    calls: list[dict] = []

    def fake_req(method, path, body=None, timeout=None):
        calls.append({"method": method, "path": path, "body": body})
        return {"id": "sid-test"}

    monkeypatch.setattr(mcp_stdio, "_req", fake_req)
    return calls


def test_model_profile_reaches_the_daemon(captured):
    mcp_stdio._spawn({"harness": "claude", "cwd": "/repo", "model_profile": "example-profile"})
    body = captured[-1]["body"]
    assert body["model_profile"] == "example-profile"
    assert captured[-1]["path"] == "/sessions"


def test_a_single_model_id_also_reaches_the_daemon(captured):
    """Placeholder ids on purpose: a real serving name in a shipped test file
    advertises the shape of the platform (ADK005) and proves nothing extra --
    these assertions are about PASSTHROUGH, so the value only has to be
    distinctive."""
    mcp_stdio._spawn({"cwd": "/repo", "model": "example-model-id"})
    assert captured[-1]["body"]["model"] == "example-model-id"


def test_omitting_the_backend_sends_nothing(captured):
    """An absent choice must not become an explicit empty one.

    Sending `model_profile: ""` would ask the daemon to bind a profile named
    "", which is a different request from "use whatever the default is".
    """
    mcp_stdio._spawn({"cwd": "/repo"})
    body = captured[-1]["body"]
    assert "model_profile" not in body
    assert "model" not in body


def test_the_tool_schema_advertises_the_backend(captured):
    """A parameter the schema hides is a parameter no caller passes."""
    spawn = [t for t in mcp_stdio.TOOLS if t["name"] == "awsh_spawn"]
    assert spawn, "awsh_spawn is no longer registered"
    props = spawn[0]["schema"]["properties"]
    assert "model_profile" in props
    assert "backend" in props["model_profile"]["description"].lower()


def test_a_discovery_tool_exists_so_names_are_not_guessed(monkeypatch):
    """model_profile is free text; without a list, callers leave it unset.

    monkeypatch, not a bare attribute assignment: `mcp_stdio._req = ...` is
    process-global and does NOT unwind when the test ends, so it decides the
    verdict of whatever runs next and the failure blames the wrong suite.
    """
    names = [t["name"] for t in mcp_stdio.TOOLS]
    assert "awsh_backends" in names

    def fake_req(method, path, body=None, timeout=None):
        assert path == "/profiles"
        return {"profiles": [{"id": "example-profile", "model": "example-model-id"}]}

    monkeypatch.setattr(mcp_stdio, "_req", fake_req)
    out = mcp_stdio._backends()
    assert out["count"] == 1
    assert out["backends"][0]["id"] == "example-profile"


def test_discovery_surfaces_a_daemon_error_rather_than_an_empty_list(monkeypatch):
    """An unreachable daemon must not look like "this box has no backends"."""
    monkeypatch.setattr(
        mcp_stdio, "_req", lambda *a, **k: {"error": "harness daemon unreachable"}
    )
    out = mcp_stdio._backends()
    assert "error" in out
    assert "backends" not in out


# ── resume: the lifecycle half, for every harness that supports it ──────────
#
# The daemon could resume all along: every harness spec carries supports_resume
# with its own flag built into argv (`--resume <id>`, `-r <id>`), and
# SessionConfig.resume_session_id plumbed to the command line. Nothing exposed
# it, so the only way to reopen a session was a separate script that knows about
# one harness. These pin the tool that closes that.

HARNESSES = {
    "harnesses": [
        {"id": "claude", "supports_resume": True},
        {"id": "gemini", "supports_resume": True},
        {"id": "terminal", "supports_resume": False},
    ]
}


@pytest.fixture
def daemon(monkeypatch):
    """Answer /harnesses truthfully, capture everything else."""
    calls: list[dict] = []

    def fake_req(method, path, body=None, timeout=None):
        if path == "/harnesses":
            return HARNESSES
        calls.append({"method": method, "path": path, "body": body})
        return {"id": "new-sid"}

    monkeypatch.setattr(mcp_stdio, "_req", fake_req)
    return calls


def test_resume_carries_the_prior_id_to_the_daemon(daemon):
    out = mcp_stdio._resume({"session_id": "prior-1", "harness": "claude"})
    body = daemon[-1]["body"]
    assert body["resume_session_id"] == "prior-1"
    assert daemon[-1]["path"] == "/sessions"
    assert out["resumed_from"] == "prior-1"


def test_resume_works_for_a_non_claude_harness(daemon):
    """The point of putting this in awdk: one place that knows every harness."""
    mcp_stdio._resume({"session_id": "prior-2", "harness": "gemini"})
    assert daemon[-1]["body"]["harness"] == "gemini"


def test_a_harness_that_cannot_resume_is_REFUSED_by_name(daemon):
    """Silently spawning a fresh session would look like it worked and lose the
    conversation -- the worst of the three outcomes."""
    out = mcp_stdio._resume({"session_id": "prior-3", "harness": "terminal"})
    assert "cannot resume" in out["error"]
    assert "terminal" in out["error"]
    assert out["resumable_harnesses"] == ["claude", "gemini"]
    assert not daemon, "a refused resume must not POST anything"


def test_resume_takes_a_backend_because_launch_is_when_it_can_be_applied(daemon):
    mcp_stdio._resume({"session_id": "prior-4", "harness": "claude",
                       "model_profile": "example-profile"})
    assert daemon[-1]["body"]["model_profile"] == "example-profile"


def test_resume_without_a_backend_sends_none(daemon):
    mcp_stdio._resume({"session_id": "prior-5", "harness": "claude"})
    assert "model_profile" not in daemon[-1]["body"]


def test_resume_needs_a_session_id(daemon):
    out = mcp_stdio._resume({"harness": "claude"})
    assert "session_id" in out["error"]
    assert not daemon


def test_both_lifecycle_tools_are_registered(daemon):
    names = [t["name"] for t in mcp_stdio.TOOLS]
    assert "awsh_resume" in names
    assert "awsh_harnesses" in names


def test_harnesses_reports_which_can_resume(daemon):
    out = mcp_stdio._harnesses()
    assert out["count"] == 3
    assert out["resumable"] == ["claude", "gemini"]


# ── resumable: the ids resume needs, which nothing produced ─────────────────

def test_resumable_lists_dead_sessions_newest_first(tmp_path, monkeypatch):
    """awsh_sessions lists what is RUNNING. Resume needs the opposite, and the
    daemon's directory cannot see it: it merges daemon sessions with DISCOVERED
    LIVE tabs, so an ended session is invisible to every listing."""
    proj = tmp_path / "projects" / "C--repo"
    proj.mkdir(parents=True)
    for name in ("old", "new"):
        (proj / f"{name}.jsonl").write_text("{}", encoding="utf-8")
    import os
    os.utime(proj / "old.jsonl", (1_000_000, 1_000_000))
    os.utime(proj / "new.jsonl", (2_000_000, 2_000_000))

    monkeypatch.setitem(mcp_stdio._RESUMABLE_STORES, "claude", (str(tmp_path / "projects"), "*.jsonl"))
    monkeypatch.setattr(mcp_stdio, "_unsearched_resumable", lambda s: [])

    out = mcp_stdio._resumable({"harness": "claude"})
    assert [s["session_id"] for s in out["sessions"]] == ["new", "old"]
    assert out["total_found"] == 2


def test_resumable_reports_a_harness_it_cannot_enumerate(monkeypatch):
    """Returning an empty list would read as 'you have no sessions' rather than
    'this tool cannot see them', which sends people to the wrong fix."""
    monkeypatch.setattr(mcp_stdio, "_unsearched_resumable", lambda s: [])
    out = mcp_stdio._resumable({"harness": "not-a-harness"})
    assert "no transcript store" in out["error"]
    assert out["unsearched"] == ["not-a-harness"]


def test_resumable_names_the_coverage_gap(tmp_path, monkeypatch):
    """gemini and aither CAN resume; their sessions are not enumerable here. An
    empty `unsearched` would claim coverage this tool does not have."""
    monkeypatch.setitem(mcp_stdio._RESUMABLE_STORES, "claude", (str(tmp_path), "*.jsonl"))
    monkeypatch.setattr(mcp_stdio, "_req",
                        lambda *a, **k: {"harnesses": [
                            {"id": "claude", "supports_resume": True},
                            {"id": "gemini", "supports_resume": True},
                            {"id": "terminal", "supports_resume": False}]})
    out = mcp_stdio._resumable({"harness": "claude"})
    assert out["unsearched"] == ["gemini"]


def test_a_dead_daemon_does_not_break_the_listing(tmp_path, monkeypatch):
    monkeypatch.setitem(mcp_stdio._RESUMABLE_STORES, "claude", (str(tmp_path), "*.jsonl"))
    monkeypatch.setattr(mcp_stdio, "_req", lambda *a, **k: {"error": "unreachable"})
    out = mcp_stdio._resumable({"harness": "claude"})
    assert out["unsearched"] == []
    assert "error" not in out


def test_resumable_is_registered():
    assert "awsh_resumable" in [t["name"] for t in mcp_stdio.TOOLS]
