"""``GET /sessions/{id}/transcript``: a discovered session's transcript, bounded.

``/events`` and ``/stream`` resolve through the daemon's own session manager, so
a Claude Code tab the daemon DISCOVERED had no readable body: a cockpit listed it
and showed an empty pane. These arms run the real ``create_app`` under
``TestClient`` against a scripted directory and a transcript file on disk.

What they pin:
  - the tail comes back as harness events a client already renders
  - ``since`` resumes at the returned byte cursor and returns only what is new
  - the answer is bounded however large the file or one line is
  - no token is 401; a scoped non-owner token that CAN list sessions is 403; the
    link principal is 403 until the tunnel vouches for the owner
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

TOKEN = "root-bearer-for-tests-only"
PEER_TOKEN = "peer-bearer-for-tests-only"
LINK_TOKEN = "link-bearer-for-tests-only"
SID = "0f6d2c1e-aaaa-bbbb-cccc-1234567890ab"


def _row(sid: str, transcript_path: str):
    from adk.harnesses.session_directory import UnifiedSession

    return UnifiedSession(
        id=sid, title=sid, cwd="/w", harness="claude", harness_label="Claude Code",
        origin="discovered", status="idle", last_activity_at=1.0,
        last_activity_summary="", transcript_path=transcript_path,
        steer_capability="turn-boundary",
    )


class _Directory:
    def __init__(self, rows):
        self.rows = rows

    def list_sessions_sync(self, _daemon_sessions):
        return self.rows


def _user(text: str) -> str:
    return json.dumps({"type": "user", "timestamp": "2026-10-02T12:00:00.000Z",
                       "message": {"role": "user", "content": text}}) + "\n"


def _assistant(*blocks) -> str:
    return json.dumps({"type": "assistant", "timestamp": "2026-10-02T12:00:01.000Z",
                       "message": {"role": "assistant", "content": list(blocks)}}) + "\n"


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
    registry_path.write_text(json.dumps({
        hashlib.sha256(PEER_TOKEN.encode()).hexdigest(): {
            "principal": "peer-agent", "plan": "agent", "paths": ["/sessions"],
        },
        hashlib.sha256(LINK_TOKEN.encode()).hexdigest(): {
            "principal": "node-link", "plan": "link",
            "paths": list(daemon.SCOPED_LINK_PATHS),
        },
    }), encoding="utf-8")

    def make(rows):
        monkeypatch.setattr(directory_mod, "_directory", _Directory(rows))
        app = daemon.create_app(manager=SessionManager(root=tmp_path / "sessions"),
                                token=TOKEN)
        client = TestClient(app)
        client.headers = {"Authorization": f"Bearer {TOKEN}"}
        return client

    return make


@pytest.fixture()
def transcript(tmp_path):
    path = tmp_path / "projects" / f"{SID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes((
        _user("make the catalog read the live products")
        + _assistant(
            {"type": "thinking", "thinking": "the catalog is static"},
            {"type": "text", "text": "Reading the catalog."},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "shop-catalog.tsx"}},
        )
        # A tool RESULT is a user entry with a content list: not a human prompt.
        + json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "export const CATALOG = []"}]}}) + "\n"
    ).encode("utf-8"))
    return path


def test_tail_of_a_discovered_session_comes_back_as_harness_events(make_client, transcript):
    client = make_client([_row(SID, str(transcript))])
    resp = client.get(f"/sessions/{SID}/transcript")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == SID
    assert [(e["kind"], e["text"], e["tool"]) for e in body["events"]] == [
        ("turn.started", "make the catalog read the live products", ""),
        ("thinking.delta", "the catalog is static", ""),
        ("text.delta", "Reading the catalog.", ""),
        ("tool.call", "", "Read"),
    ]
    assert body["events"][3]["data"] == {"input": {"file_path": "shop-catalog.tsx"}}
    seqs = [e["seq"] for e in body["events"]]
    assert seqs == sorted(set(seqs)), "seq is unique and increasing"
    assert body["events"][0]["ts"] > 0
    assert body["next"] == body["size"] == transcript.stat().st_size
    assert body["more"] is False and body["truncated"] is False


def test_since_resumes_at_the_cursor_and_returns_only_what_is_new(make_client, transcript):
    client = make_client([_row(SID, str(transcript))])
    cursor = client.get(f"/sessions/{SID}/transcript").json()["next"]

    again = client.get(f"/sessions/{SID}/transcript?since={cursor}").json()
    assert again["events"] == [] and again["next"] == cursor

    with transcript.open("ab") as handle:
        handle.write(_assistant({"type": "text", "text": "Done."}).encode("utf-8"))
        handle.write(b'{"type": "assistant", "message": {"content": [{"type": "te')  # in flight
    fresh = client.get(f"/sessions/{SID}/transcript?since={cursor}").json()
    assert [e["text"] for e in fresh["events"]] == ["Done."]
    # The half-written line is neither parsed nor skipped: the cursor stops before it.
    assert cursor < fresh["next"] < transcript.stat().st_size
    assert fresh["more"] is True

    # A cursor past the end means the file was rotated: answer with the tail.
    rotated = client.get(f"/sessions/{SID}/transcript?since=99999999").json()
    assert [e["kind"] for e in rotated["events"]][0] == "turn.started"


def test_the_answer_is_bounded_on_a_large_transcript(make_client, tmp_path):
    path = tmp_path / "big.jsonl"
    line = _assistant({"type": "text", "text": "x" * 20_000})
    with path.open("wb") as handle:
        for _ in range(400):  # ~8 MB
            handle.write(line.encode("utf-8"))
    client = make_client([_row(SID, str(path))])

    tail = client.get(f"/sessions/{SID}/transcript?limit=65536")
    assert tail.status_code == 200
    body = tail.json()
    assert body["truncated"] is True
    assert 1 <= len(body["events"]) <= 4
    assert all(len(e["text"]) <= 4000 for e in body["events"]), "each field is clipped"
    assert len(tail.content) < 65536
    assert body["next"] == path.stat().st_size

    # Paging from the start consumes about `limit` bytes per read, not the file.
    page = client.get(f"/sessions/{SID}/transcript?since=0&limit=65536").json()
    assert page["more"] is True and 0 < page["next"] <= 65536 + len(line)

    assert client.get(f"/sessions/{SID}/transcript?limit=99999999").status_code == 422


def test_a_line_too_large_to_parse_is_skipped_not_loaded(make_client, tmp_path, monkeypatch):
    from adk.harnesses import transcript_tail

    monkeypatch.setattr(transcript_tail, "MAX_LINE_BYTES", 2048)
    path = tmp_path / "wide.jsonl"
    path.write_bytes(
        _user("before").encode() + _assistant({"type": "text", "text": "y" * 9000}).encode()
        + _user("after").encode()
    )
    client = make_client([_row(SID, str(path))])
    body = client.get(f"/sessions/{SID}/transcript?since=0").json()
    assert [e["text"] for e in body["events"]] == ["before", "after"]
    assert body["skipped_lines"] == 1 and body["next"] == path.stat().st_size


def test_the_route_refuses_an_anonymous_caller(make_client, transcript):
    client = make_client([_row(SID, str(transcript))])
    client.headers = {}
    assert client.get(f"/sessions/{SID}/transcript").status_code == 401
    client.headers = {"Authorization": "Bearer not-the-token"}
    assert client.get(f"/sessions/{SID}/transcript").status_code == 403


def test_a_scoped_peer_that_can_list_sessions_cannot_read_a_transcript(make_client, transcript):
    client = make_client([_row(SID, str(transcript))])
    client.headers = {"Authorization": f"Bearer {PEER_TOKEN}"}
    assert client.get("/sessions/unified").status_code == 200  # in its path scope
    resp = client.get(f"/sessions/{SID}/transcript")
    assert resp.status_code == 403
    assert "make the catalog" not in resp.text


def test_the_link_reads_it_only_when_the_tunnel_vouches_for_the_owner(make_client, transcript):
    import adk.harnesses.daemon as daemon

    client = make_client([_row(SID, str(transcript))])
    client.headers = {"Authorization": f"Bearer {LINK_TOKEN}"}
    assert client.get(f"/sessions/{SID}/transcript").status_code == 403
    client.headers = {"Authorization": f"Bearer {LINK_TOKEN}",
                      daemon.LINK_ACTOR_HEADER: daemon.LINK_ACTOR_OWNER}
    resp = client.get(f"/sessions/{SID}/transcript")
    assert resp.status_code == 200, resp.text
    assert resp.json()["events"][0]["text"] == "make the catalog read the live products"


def test_unknown_session_and_a_row_without_a_transcript_are_404(make_client, tmp_path):
    not_jsonl = tmp_path / "secrets.txt"
    not_jsonl.write_text("x", encoding="utf-8")
    client = make_client([_row("no-file", ""), _row("wrong-kind", str(not_jsonl)),
                          _row("gone", str(tmp_path / "gone.jsonl"))])
    for sid in ("nope", "no-file", "wrong-kind", "gone"):
        assert client.get(f"/sessions/{sid}/transcript").status_code == 404, sid
