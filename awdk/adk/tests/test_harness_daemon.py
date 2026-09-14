"""HTTP contract tests for the AitherShell harness daemon.

Written because the daemon surface was verified only BY HAND. Two of the bugs
found during its construction were HTTP-shaped and invisible to the
pure-function tests that existed at the time:

1. ``from __future__ import annotations`` plus Pydantic models defined INSIDE
   ``create_app`` made FastAPI resolve the body annotation against module
   globals, fail, and silently demote the request body to a QUERY parameter.
   Every ``POST /sessions`` returned 422 "field required: body" — which reads
   as a client bug, not a wiring bug.
2. A stale ``Transport.RAW_STREAM`` reference raised AttributeError inside
   ``send``, surfacing as an opaque HTTP 500.

Both are guarded here. No test in this module spawns a coding agent or a
terminal: the ``aither`` relay harness starts no process, so the HTTP contract
is exercised without burning a model call or leaving a pty behind.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="daemon tests need fastapi")
from adk.harnesses.daemon import allowed_origins, create_app, validate_cwd  # noqa: E402
from adk.harnesses.manager import ManagerError, SessionManager  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "test-token-do-not-reuse"


@pytest.fixture()
def client(tmp_path):
    manager = SessionManager(root=tmp_path)
    app = create_app(manager=manager, token=TOKEN)
    with TestClient(app) as test_client:
        yield test_client
    manager.stop_all()


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


# ── fail-closed auth ────────────────────────────────────────────────────────

def test_health_needs_no_credential(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "aithershell-harness"


def test_health_states_whether_cwd_is_restricted(client):
    # Stated explicitly so "this host is trusted" is a visible posture rather
    # than an unnoticed default on a tunnel-exposed daemon.
    assert "cwd_restricted" in client.get("/health").json()


@pytest.mark.parametrize(
    "path",
    ["/harnesses", "/sessions", "/profiles", "/agents", "/awrun/queue", "/awrun/status/r-x"],
)
def test_every_data_route_denies_without_a_token(client, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("header", ["Bearer wrong", "Basic abc", "wrong", "Bearer "])
def test_malformed_or_wrong_credentials_are_refused(client, header):
    response = client.get("/harnesses", headers={"Authorization": header})
    # 401 for malformed, 403 for well-formed-but-wrong. Never 200.
    assert response.status_code in (401, 403)


def test_correct_token_is_accepted(client):
    assert client.get("/harnesses", headers=auth()).status_code == 200


# ── the 422 regression: body must parse as a BODY ───────────────────────────

def test_post_sessions_parses_a_json_body_not_a_query_param(client):
    """Guard for the annotations/local-model wiring bug.

    An unknown harness must fail with 400 from OUR validation. If FastAPI ever
    demotes the body to a query parameter again this becomes 422 "field
    required: body" and every session creation breaks.
    """
    response = client.post(
        "/sessions", headers=auth(), json={"harness": "definitely-not-a-harness"}
    )
    assert response.status_code == 400, response.text
    assert "Unknown harness" in response.text


def test_create_session_rejects_a_missing_cwd(client):
    response = client.post(
        "/sessions", headers=auth(), json={"harness": "aither", "cwd": "/no/such/dir/xyz"}
    )
    assert response.status_code == 400
    assert "cwd does not exist" in response.text


def test_model_profile_on_a_harness_that_cannot_bind_is_refused(client):
    # A silently-ignored model profile would run the wrong model under a label
    # that says otherwise. Refusing is the fail-closed behaviour.
    response = client.post(
        "/sessions", headers=auth(), json={"harness": "aither", "model_profile": "deepseek-flash"}
    )
    assert response.status_code == 400
    assert "does not support model profiles" in response.text


# ── session lifecycle over HTTP (no process spawned) ────────────────────────

def test_relay_session_lifecycle(client):
    created = client.post(
        "/sessions", headers=auth(), json={"harness": "aither", "agent": "aither", "title": "t"}
    )
    assert created.status_code == 200, created.text
    info = created.json()
    session_id = info["id"]
    assert info["harness"] == "aither"

    listed = client.get("/sessions", headers=auth()).json()["sessions"]
    assert any(s["id"] == session_id for s in listed)

    fetched = client.get(f"/sessions/{session_id}", headers=auth())
    assert fetched.status_code == 200

    events = client.get(f"/sessions/{session_id}/events?since=0", headers=auth()).json()
    # A session that produced no events at all would be indistinguishable from
    # one that never started.
    assert events["events"], "a started session must have emitted lifecycle events"
    assert events["events"][0]["kind"] == "session.starting"
    assert [e["seq"] for e in events["events"]] == sorted(e["seq"] for e in events["events"])

    stopped = client.delete(f"/sessions/{session_id}", headers=auth())
    assert stopped.status_code == 200


def test_since_cursor_returns_only_newer_events(client):
    session_id = client.post(
        "/sessions", headers=auth(), json={"harness": "aither"}
    ).json()["id"]
    first = client.get(f"/sessions/{session_id}/events?since=0", headers=auth()).json()
    last_seq = first["events"][-1]["seq"]
    again = client.get(
        f"/sessions/{session_id}/events?since={last_seq}", headers=auth()
    ).json()
    # Reconnect-by-seq is what lets a phone re-attach without replaying the
    # world. If this ever returns the full list, long sessions melt the client.
    assert all(e["seq"] > last_seq for e in again["events"])


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/sessions/nope"),
        ("delete", "/sessions/nope"),
        ("post", "/sessions/nope/interrupt"),
    ],
)
def test_unknown_session_is_404_not_500(client, method, path):
    response = getattr(client, method)(path, headers=auth())
    assert response.status_code == 404


def test_send_to_unknown_session_is_404(client):
    response = client.post("/sessions/nope/input", headers=auth(), json={"text": "hi"})
    assert response.status_code == 404


def test_resize_on_a_non_pty_session_reports_false_rather_than_erroring(client):
    session_id = client.post("/sessions", headers=auth(), json={"harness": "aither"}).json()["id"]
    response = client.post(
        f"/sessions/{session_id}/resize", headers=auth(), json={"rows": 40, "cols": 120}
    )
    assert response.status_code == 200
    assert response.json()["resized"] is False


# ── discovery surfaces ──────────────────────────────────────────────────────

def test_harnesses_lists_install_hints_for_anything_missing(client):
    harnesses = client.get("/harnesses", headers=auth()).json()["harnesses"]
    assert harnesses
    for harness in harnesses:
        if not harness["installed"]:
            assert harness.get("install_hint")


def test_agents_roster_is_served(client):
    agents = client.get("/agents", headers=auth()).json()["agents"]
    assert any(a["id"] == "aither" for a in agents)


# ── CORS / cwd containment ──────────────────────────────────────────────────

def test_wildcard_cors_origin_is_refused(monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_ALLOWED_ORIGINS", "https://a.com,*")
    # This daemon spawns coding agents with filesystem access and uses bearer
    # credentials; '*' with credentials must never be silently accepted.
    with pytest.raises(RuntimeError, match="refused"):
        allowed_origins()


def test_explicit_cors_allowlist_is_honoured(monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_ALLOWED_ORIGINS", "https://a.com, https://b.com")
    assert allowed_origins() == ["https://a.com", "https://b.com"]


def test_cwd_outside_allowed_roots_is_refused(monkeypatch, tmp_path):
    import os

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("AITHER_HARNESS_ALLOWED_ROOTS", str(allowed))
    assert validate_cwd(str(allowed)) == str(allowed.resolve())
    with pytest.raises(ManagerError, match="outside the allowed roots"):
        validate_cwd(str(tmp_path / "elsewhere"))
    # Unset allowlist means "this host is trusted" — the desktop default.
    monkeypatch.setenv("AITHER_HARNESS_ALLOWED_ROOTS", "")
    assert validate_cwd(os.getcwd()) == os.getcwd()


# ── awrun job queue ──────────────────────────────────────────────────────────

@pytest.fixture()
def awrun_client(tmp_path, monkeypatch):
    """Isolated queue dir so this suite never touches a real awrun store."""
    monkeypatch.setenv("AITHER_AWRUN_DIR", str(tmp_path / "awrun"))
    manager = SessionManager(root=tmp_path / "sessions")
    app = create_app(manager=manager, token=TOKEN)
    with TestClient(app) as test_client:
        yield test_client
    manager.stop_all()


def test_awrun_submit_queue_status_bump_cancel_roundtrip(awrun_client):
    submitted = awrun_client.post(
        "/awrun/submit",
        json={"kind": "ci", "workflow": "deploy.yml", "ref": "develop", "priority": 8},
        headers=auth(),
    ).json()
    assert submitted["priority"] == 8
    run_id = submitted["id"]

    listed = awrun_client.get("/awrun/queue", headers=auth()).json()["runs"]
    assert any(r["id"] == run_id for r in listed)

    status = awrun_client.get(f"/awrun/status/{run_id}", headers=auth()).json()
    assert status["id"] == run_id

    bumped = awrun_client.post(
        f"/awrun/bump/{run_id}", json={"priority": 10}, headers=auth(),
    ).json()
    assert bumped["priority"] == 10

    cancelled = awrun_client.post(f"/awrun/cancel/{run_id}", headers=auth()).json()
    assert cancelled["status"] == "cancelled"


def test_awrun_status_malformed_id_returns_clean_error_not_500(awrun_client):
    r = awrun_client.get("/awrun/status/not-a-real-id", headers=auth())
    assert r.status_code == 200
    assert "error" in r.json()


def test_awrun_submit_comet_deploy_is_refused_at_the_route(awrun_client):
    """The route-level fail-closed check, not queue_submit's own gate: this
    proves the daemon never reaches AITHER_SESSION_BEARER resolution for
    comet-deploy through THIS route at all, regardless of what the daemon
    process's own environment happens to hold. See the comment on
    awrun_submit in daemon.py for why forwarding a session bearer here would
    be a caller-identity mismatch, not a caller-derived decision."""
    r = awrun_client.post(
        "/awrun/submit",
        json={"kind": "comet-deploy", "service_name": "whatever"},
        headers=auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert "error" in body
    assert "comet-deploy" in body["error"]
    assert "daemon" in body["error"].lower()


def test_awrun_submit_comet_deploy_refused_even_with_a_genuinely_valid_stray_bearer(
    awrun_client, monkeypatch,
):
    """The specific failure mode this route-level check exists to prevent —
    and proves it against a bearer that would ACTUALLY be honoured, not one
    that merely looks stray. A prior version of this test set a nonsense
    token and observed a refusal; that refusal came from
    authz.resolve_session() correctly rejecting garbage, not from the
    route-level guard, so the test passed for the wrong reason even with
    the guard deleted (caught by mutation-testing the guard: removing it
    left this "stray token" version still green). Here resolve_session and
    check_permission are patched to return a GENUINE affirmative — proving
    that WITHOUT the route-level guard, this exact request would actually
    succeed and authorize as a session the real caller of this HTTP request
    never presented. The guard is what stands between that and this."""
    from awbac import Decision
    from awrun import authz

    monkeypatch.setattr(authz, "resolve_session", lambda token: "some-other-identity")
    monkeypatch.setattr(
        authz, "check_permission",
        lambda subject_id, permission="": Decision(
            allowed=True, reason="test-authorized", via="test"),
    )
    monkeypatch.setenv("AITHER_SESSION_BEARER", "a-bearer-that-would-genuinely-resolve")

    r = awrun_client.post(
        "/awrun/submit",
        json={"kind": "comet-deploy", "service_name": "whatever"},
        headers=auth(),
    )
    body = r.json()
    assert "error" in body
    assert "comet-deploy" in body["error"]
    assert "daemon" in body["error"].lower()
    assert "id" not in body, "a real submit would have returned a run id — this must never"
