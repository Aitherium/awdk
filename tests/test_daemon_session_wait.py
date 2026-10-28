"""`GET /sessions/{id}/wait` -- wait for an agent to become idle or to block.

The endpoint exists so an orchestrating agent does not poll `get_session` in a loop or
sleep a guessed interval. Two behaviours are the point and each has a test below:
TWO consecutive matching polls are required (a pty flickers idle between writes), and an
unknown `until` value is a 400 rather than a wait that never ends.
"""
import pytest
from fastapi.testclient import TestClient

from adk.harnesses.manager import ManagerError

TOKEN = "test-token-for-testing-only"


class _FakeSession:
    """Just enough of a session for `.info()`; `states` is consumed one poll at a time."""

    def __init__(self, states):
        self._states = list(states)
        self.polls = 0

    def info(self):
        self.polls += 1
        state = self._states[min(self.polls - 1, len(self._states) - 1)]
        return {"id": "s1", "state": state}


class _FakeManager:
    """The three manager methods `create_app` actually touches: build-time listing and
    the lookup this endpoint polls. Anything else would be inventing a session runtime."""

    def __init__(self, session=None):
        self.session = session
        self.missing = session is None

    def available_harness_ids(self):
        return ["claude"]

    def list_sessions(self):
        return [] if self.missing else [self.session.info()]

    def get_session(self, session_id):
        if self.missing:
            raise ManagerError(f"no such session: {session_id}")
        return self.session


@pytest.fixture
def make_client(monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    from adk.harnesses.daemon import create_app

    def _client(session=None):
        app = create_app(manager=_FakeManager(session), token=TOKEN)
        client = TestClient(app)
        client.headers = {"Authorization": f"Bearer {TOKEN}"}
        return client

    return _client


def test_already_idle_returns_after_the_second_poll(make_client):
    session = _FakeSession(["idle"])
    r = make_client(session).get("/sessions/s1/wait")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched"] == "idle" and body["session"]["state"] == "idle"
    assert session.polls >= 2, "returned on a single poll -- the flicker guard is gone"


def test_waits_through_a_busy_turn_then_returns_on_idle(make_client):
    session = _FakeSession(["busy", "busy", "idle", "idle"])
    r = make_client(session).get("/sessions/s1/wait?timeout=5")
    assert r.status_code == 200, r.text
    assert r.json()["matched"] == "idle"
    assert session.polls >= 4


def test_one_sample_of_idle_is_not_a_wake(make_client):
    session = _FakeSession(["idle", "busy", "busy", "busy"])
    r = make_client(session).get("/sessions/s1/wait?until=idle&timeout=1")
    assert r.status_code == 408, r.text


def test_exited_is_a_wake_by_default(make_client):
    r = make_client(_FakeSession(["busy", "exited"])).get("/sessions/s1/wait?timeout=5")
    assert r.status_code == 200 and r.json()["matched"] == "exited"


def test_unknown_session_is_404(make_client):
    r = make_client(None).get("/sessions/nope/wait")
    assert r.status_code == 404


def test_bad_until_is_400_not_a_silent_wait(make_client):
    r = make_client(_FakeSession(["idle"])).get("/sessions/s1/wait?until=napping")
    assert r.status_code == 400
    assert "napping" in r.json()["detail"]
