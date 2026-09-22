"""The front door carries the agent and the skill a caller named (2026-09-21).

Measured before this test existed: ``adk shell new --harness aither --agent atlas``
sent ``agent`` in the body, ``SessionConfig`` accepted it, and the daemon's
``CreateSession`` model did not declare it -- so ``model_dump()`` dropped it and
every named-agent session ran as aither, with no error anywhere. The same class
would have swallowed ``participants`` for a group room. These arms run the real
``create_app`` under an in-process ``TestClient``; only the harness PROCESS is
stubbed, and the relay session never reaches a network (no turn is sent).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

TOKEN = "root-bearer-for-tests-only"


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("AITHER_STEER_DISPATCH_STATUS", str(tmp_path / "dispatch.json"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    monkeypatch.setenv("AITHER_HARNESS_PRINCIPALS", str(tmp_path / "harness_tokens.json"))
    # Keep ~/.claude out of the picture: the skills root is the test's cwd only.
    monkeypatch.setenv("AITHER_HOME_OVERRIDE", str(tmp_path / "nohome"))

    import adk.harnesses.daemon as daemon
    from adk.harnesses import rooms as rooms_mod
    from adk.harnesses import session as session_mod
    from adk.harnesses import session_directory as directory_mod
    from adk.harnesses.manager import SessionManager

    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", tmp_path / "harness_tokens.json")
    monkeypatch.setattr(rooms_mod, "_registry", None)
    monkeypatch.setattr(
        directory_mod, "_directory",
        directory_mod.SessionDirectory(discover_fn=lambda: []),
    )
    monkeypatch.setattr(session_mod.HarnessSession, "start", lambda self: None)

    mgr = SessionManager(root=tmp_path / "sessions")
    app = daemon.create_app(manager=mgr, token=TOKEN)
    client = TestClient(app)
    client.headers = {"Authorization": f"Bearer {TOKEN}"}
    return {"client": client, "mgr": mgr, "tmp": tmp_path}


def _skills_root(tmp_path):
    root = tmp_path / "proj"
    (root / ".claude" / "skills" / "demo").mkdir(parents=True)
    (root / ".claude" / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: A demo procedure\nallowed-tools: Bash\n---\n"
        "# Demo\n\nDo the thing with $ARGUMENTS.\n",
        encoding="utf-8",
    )
    (root / ".claude" / "commands").mkdir()
    (root / ".claude" / "commands" / "report.md").write_text(
        "Write the report for $ARGUMENTS.\n", encoding="utf-8",
    )
    return root


def _config(harness, session_id):
    return harness["mgr"]._sessions[session_id].config


# ── the agent field ──────────────────────────────────────────────────────────


def test_named_agent_reaches_the_session_config(harness):
    client = harness["client"]
    resp = client.post("/sessions", json={"harness": "aither", "cwd": "", "agent": "atlas"})
    assert resp.status_code == 200, resp.text
    cfg = _config(harness, resp.json()["id"])
    assert cfg.agent == "atlas"


def test_unknown_agent_is_refused_with_the_roster(harness):
    client = harness["client"]
    resp = client.post("/sessions", json={"harness": "aither", "cwd": "", "agent": "bogus"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "bogus" in detail and "atlas" in detail and "aither" in detail


def test_group_participants_reach_the_config_and_are_validated(harness):
    client = harness["client"]
    ok = client.post("/sessions", json={
        "harness": "group", "cwd": "", "participants": ["atlas", "lyra"],
    })
    assert ok.status_code == 200, ok.text
    assert _config(harness, ok.json()["id"]).participants == ["atlas", "lyra"]
    bad = client.post("/sessions", json={
        "harness": "group", "cwd": "", "participants": ["atlas", "nobody"],
    })
    assert bad.status_code == 400 and "nobody" in bad.json()["detail"]


def test_non_relay_harness_ignores_the_roster(harness):
    """A claude session names no agent; the roster gate must not touch it."""
    client = harness["client"]
    resp = client.post("/sessions", json={"harness": "claude", "cwd": "", "agent": ""})
    assert resp.status_code == 200, resp.text


def test_awdk_agent_persona_is_prepended_to_the_system_prompt(harness):
    """adk serve has no identity store, so the daemon carries the persona line."""
    client = harness["client"]
    resp = client.post("/sessions", json={"harness": "awdk", "cwd": "", "agent": "atlas"})
    assert resp.status_code == 200, resp.text
    cfg = _config(harness, resp.json()["id"])
    assert cfg.system_prompt_append.startswith("You are Atlas (")
    # Genesis loads the identity itself: the aither harness gets no persona line.
    resp = client.post("/sessions", json={"harness": "aither", "cwd": "", "agent": "atlas"})
    assert resp.status_code == 200, resp.text
    assert not _config(harness, resp.json()["id"]).system_prompt_append


def test_roster_carries_saga_vera_iris(harness):
    """The three identities the hand-typed roster never had (2026-09-21)."""
    ids = {a["id"] for a in harness["client"].get("/agents").json()["agents"]}
    assert {"aither", "atlas", "saga", "vera", "iris"} <= ids
    resp = harness["client"].post(
        "/sessions", json={"harness": "awdk", "cwd": "", "agent": "saga"},
    )
    assert resp.status_code == 200, resp.text


# ── the skill field ──────────────────────────────────────────────────────────


def test_skill_renders_into_system_prompt_append(harness):
    client = harness["client"]
    root = _skills_root(harness["tmp"])
    resp = client.post("/sessions", json={
        "harness": "aither", "cwd": str(root), "agent": "atlas",
        "skill": "demo", "skill_arguments": "the widget",
        "system_prompt_append": "Be terse.",
    })
    assert resp.status_code == 200, resp.text
    cfg = _config(harness, resp.json()["id"])
    assert cfg.system_prompt_append.startswith("Be terse.")
    assert "# Skill: demo" in cfg.system_prompt_append
    assert "Do the thing with the widget." in cfg.system_prompt_append
    assert "allowed-tools" not in cfg.system_prompt_append  # frontmatter stripped
    assert not hasattr(cfg, "skill")  # resolved at the door, never travels


def test_unknown_skill_is_refused(harness):
    client = harness["client"]
    root = _skills_root(harness["tmp"])
    resp = client.post("/sessions", json={
        "harness": "aither", "cwd": str(root), "agent": "atlas", "skill": "nope",
    })
    assert resp.status_code == 400
    assert "nope" in resp.json()["detail"] and "2 known" in resp.json()["detail"]


def test_skills_endpoints_list_and_render(harness):
    client = harness["client"]
    root = _skills_root(harness["tmp"])
    listing = client.get("/skills", params={"cwd": str(root)}).json()
    names = {s["name"]: s for s in listing["skills"]}
    assert set(names) == {"demo", "report"}
    assert names["demo"]["kind"] == "skill"
    assert names["demo"]["description"] == "A demo procedure"
    assert names["report"]["kind"] == "command"
    assert listing["collisions"] == {}

    one = client.get("/skills/report", params={"cwd": str(root), "arguments": "Q3"}).json()
    assert one["text"].startswith("# Command: report")
    assert "Write the report for Q3." in one["text"]

    missing = client.get("/skills/absent", params={"cwd": str(root)})
    assert missing.status_code == 404


# ── the relay forwards it ────────────────────────────────────────────────────


def test_relay_session_forwards_the_addition_as_system_additions(tmp_path, monkeypatch):
    """The body ``_run_turn`` POSTs to Genesis carries the rendered skill."""
    import sys
    import types

    import adk.harnesses.agents as agents_mod
    from adk.harnesses.registry import get as get_harness
    from adk.harnesses.session import SessionConfig

    captured: dict = {}

    class _Resp:
        status_code = 200
        text = ""

        def read(self):
            return None

        def iter_lines(self):
            return iter(())

    class _Stream:
        def __init__(self, body):
            captured["body"] = body

        def __enter__(self):
            return _Resp()

        def __exit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url, json=None):
            return _Stream(json)

    fake = types.ModuleType("httpx")
    fake.Client = _Client
    fake.Timeout = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "httpx", fake)

    cfg = SessionConfig(harness="aither", agent="lyra", system_prompt_append="# Skill: x")
    sess = agents_mod.AgentRelaySession(get_harness("aither"), cfg, root=tmp_path, agent="lyra")
    monkeypatch.setattr(sess, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(sess, "_observe", lambda *a, **k: None)
    sess._run_turn("hello", 1)

    assert captured["body"]["agent"] == "lyra"
    assert captured["body"]["system_additions"] == ["# Skill: x"]

    plain = SessionConfig(harness="aither", agent="lyra")
    sess2 = agents_mod.AgentRelaySession(get_harness("aither"), plain, root=tmp_path, agent="lyra")
    monkeypatch.setattr(sess2, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(sess2, "_observe", lambda *a, **k: None)
    sess2._run_turn("hello", 1)
    assert "system_additions" not in captured["body"]
