"""``GET /sessions/unified/stream``: one SSE stream over the whole session directory.

Before it existed a cockpit had to re-poll ``/sessions/unified`` every 2 s and diff
the lists itself. These arms run the real ``create_app`` under ``TestClient`` with a
scripted directory, so the frames asserted are the ones a client would parse.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

TOKEN = "root-bearer-for-tests-only"


def _row(sid: str, status: str = "idle"):
    from adk.harnesses.session_directory import UnifiedSession

    return UnifiedSession(
        id=sid, title=sid, cwd="/w", harness="claude", harness_label="Claude Code",
        origin="discovered", status=status, last_activity_at=1.0,
        last_activity_summary="", transcript_path="", steer_capability="turn-boundary",
    )


class _ScriptedDirectory:
    """Answers each ``list_sessions_sync`` with the next scripted view."""

    def __init__(self, views):
        self.views = list(views)

    def list_sessions_sync(self, _daemon_sessions):
        return self.views.pop(0) if len(self.views) > 1 else self.views[0]


@pytest.fixture()
def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("AITHER_STEER_DISPATCH_STATUS", str(tmp_path / "dispatch.json"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    registry_path = tmp_path / "harness_tokens.json"
    monkeypatch.setenv("AITHER_HARNESS_PRINCIPALS", str(registry_path))

    import adk.harnesses.daemon as daemon
    from adk.harnesses import rooms as rooms_mod
    from adk.harnesses import session_directory as directory_mod
    from adk.harnesses.manager import SessionManager

    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", registry_path)
    monkeypatch.setattr(rooms_mod, "_registry", None)

    def make(views):
        monkeypatch.setattr(directory_mod, "_directory", _ScriptedDirectory(views))
        app = daemon.create_app(manager=SessionManager(root=tmp_path / "sessions"),
                                token=TOKEN)
        client = TestClient(app)
        client.headers = {"Authorization": f"Bearer {TOKEN}"}
        return client

    return make


def _frames(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln and not ln.startswith(":")]
        if not lines:
            continue
        kind = next(ln[len("event: "):] for ln in lines if ln.startswith("event: "))
        data = next(ln[len("data: "):] for ln in lines if ln.startswith("data: "))
        out.append((kind, json.loads(data)))
    return out


def test_unified_diff_reports_new_changed_and_gone():
    from adk.harnesses.daemon import unified_diff

    prev = {"a": {"id": "a", "status": "idle"}, "b": {"id": "b", "status": "idle"}}
    cur = {"a": {"id": "a", "status": "working"}, "c": {"id": "c", "status": "idle"}}
    diff = unified_diff(prev, cur)
    assert sorted(r["id"] for r in diff["upserted"]) == ["a", "c"]
    assert diff["removed"] == ["b"]
    assert unified_diff(cur, dict(cur)) == {"upserted": [], "removed": []}


def test_stream_sends_snapshot_then_only_the_diff(make_client):
    client = make_client([
        [_row("a"), _row("b")],
        [_row("a"), _row("b")],             # unchanged tick: no frame
        [_row("a", "working"), _row("c")],  # a changed, b gone, c new
    ])
    resp = client.get("/sessions/unified/stream?interval=0.25&max_frames=2")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _frames(resp.text)
    assert [k for k, _ in frames] == ["snapshot", "diff"]
    assert [r["id"] for r in frames[0][1]["sessions"]] == ["a", "b"]
    diff = frames[1][1]
    assert {r["id"]: r["status"] for r in diff["upserted"]} == {"a": "working", "c": "idle"}
    assert diff["removed"] == ["b"]


def test_stream_snapshot_rows_match_the_list_endpoint(make_client):
    client = make_client([[_row("a")]])
    listed = client.get("/sessions/unified").json()["sessions"]
    frames = _frames(client.get("/sessions/unified/stream?max_frames=1").text)
    assert frames == [("snapshot", {"sessions": listed})]


def test_stream_refuses_an_anonymous_caller(make_client):
    client = make_client([[_row("a")]])
    client.headers = {}
    assert client.get("/sessions/unified/stream?max_frames=1").status_code == 401
