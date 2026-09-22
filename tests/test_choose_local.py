"""adk.choose's LOCAL backend: a stranger with no decision door still gets an
answer, and can always tell it did not come from the door."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from adk import choose as door

pytest.importorskip("awdecide", reason="the local backend is the optional extra awdk[decide]")


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    """Every test gets its own sqlite ledger and no brain unless it asks."""
    monkeypatch.setenv("AWDECIDE_DB", str(tmp_path / "decide.db"))
    monkeypatch.delenv("AWDECIDE_LLM_URL", raising=False)
    monkeypatch.delenv("AWDECIDE_LLM_MODEL", raising=False)
    monkeypatch.delenv("AITHER_DECIDE_URL", raising=False)
    monkeypatch.delenv("AITHER_DECIDE_LOCAL", raising=False)
    door._LOCAL_IDS.clear()


class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _brain(monkeypatch, label, counter):
    """A fake OpenAI-wire endpoint for awdecide's ChatBackend, counting calls."""
    monkeypatch.setenv("AWDECIDE_LLM_URL", "http://brain.test/v1")
    monkeypatch.setenv("AWDECIDE_LLM_MODEL", "tiny")

    def fake_urlopen(req, timeout=0, context=None):
        counter.append(req.full_url)
        return _Resp({"choices": [{"message": {"content": label}}]})

    monkeypatch.setattr(door.urllib.request, "urlopen", fake_urlopen)


def _door_is_down(monkeypatch):
    def down(req, timeout=0, context=None):
        raise door.urllib.error.URLError("refused")

    monkeypatch.setattr(door.urllib.request, "urlopen", down)


# ---------------------------------------------------------------- the fallback
def test_door_down_with_no_door_named_is_answered_locally(monkeypatch):
    """No AITHER_DECIDE_URL (the stranger) + the default door refusing =
    an in-process answer, labelled as one."""
    _door_is_down(monkeypatch)
    d = door.decide("router", "kind:code", options=["fast", "deep"])
    assert d.answer is None and d.confidence == 0.0
    assert d.source == "local:none"  # no brain, no evidence: never a guess
    assert d.decision_id == ""


def test_a_named_door_that_is_down_stays_an_outage(monkeypatch):
    """A caller who NAMED a door is owed the error, not a different answer."""
    monkeypatch.setenv("AITHER_DECIDE_URL", "http://door.test")
    _door_is_down(monkeypatch)
    with pytest.raises(door.DecideUnavailableError):
        door.decide("router", "kind:code", options=["fast", "deep"])

    monkeypatch.setenv("AITHER_DECIDE_LOCAL", "1")  # ... unless it opts in
    assert door.decide("router", "kind:code", options=["fast", "deep"]).source == "local:none"


def test_local_url_skips_the_network_entirely(monkeypatch):
    calls = []

    def never(req, timeout=0, context=None):
        calls.append(req.full_url)
        raise AssertionError("the local path opened a socket")

    monkeypatch.setenv("AITHER_DECIDE_URL", "local")
    monkeypatch.setattr(door.urllib.request, "urlopen", never)
    d = door.decide("router", "kind:code", options=["fast", "deep"])
    assert d.source == "local:none" and calls == []


# ------------------------------------------------------------------- learning
def test_a_taught_outcome_answers_the_next_identical_call_without_the_brain(monkeypatch):
    seen = []
    monkeypatch.setenv("AITHER_DECIDE_URL", "local")
    _brain(monkeypatch, "deep", seen)

    first = door.decide("router", "kind:code,len:long", options=["fast", "deep"])
    assert first.answer == "deep" and first.source == "local:chat"
    assert first.decision_id and len(seen) == 1

    assert door.outcome(first.decision_id, 1.0)["ok"] is True

    second = door.decide("router", "kind:code,len:long", options=["fast", "deep"])
    assert second.answer == "deep"
    assert second.source == "local:evidence" and second.learned
    assert second.learned_from == 1
    assert len(seen) == 1, "the brain was asked again for a decision already taught"


def test_teach_lands_without_a_prior_decision(monkeypatch):
    monkeypatch.setenv("AITHER_DECIDE_URL", "local")
    door.teach("router", "kind:docs", "fast", 1.0)
    d = door.decide("router", "kind:docs", options=["fast", "deep"])
    assert d.answer == "fast" and d.source == "local:evidence"


def test_an_unknown_decision_id_is_a_value_error(monkeypatch):
    monkeypatch.setenv("AITHER_DECIDE_URL", "local")
    with pytest.raises(ValueError, match="unknown decision_id"):
        door.outcome("nosuchid", 1.0)


def test_a_local_decision_id_routes_back_to_the_local_ledger(monkeypatch):
    """The id was issued locally, so outcome() resolves it locally even though
    a door is named and reachable."""
    seen = []
    monkeypatch.setenv("AITHER_DECIDE_URL", "local")
    _brain(monkeypatch, "fast", seen)
    d = door.decide("router", "kind:chat", options=["fast", "deep"])

    monkeypatch.setenv("AITHER_DECIDE_URL", "http://door.test")

    def boom(req, timeout=0, context=None):
        raise AssertionError("a local decision id was posted to the door")

    monkeypatch.setattr(door.urllib.request, "urlopen", boom)
    assert door.outcome(d.decision_id, -1.0)["ok"] is True


# -------------------------------------------------------------------- batching
def test_decide_batch_is_answered_locally_in_order(monkeypatch):
    seen = []
    monkeypatch.setenv("AITHER_DECIDE_URL", "local")
    _brain(monkeypatch, "deep", seen)
    out = door.decide_batch(
        "router",
        [
            {"state": "a:1", "options": ["fast", "deep"]},
            {"state": "a:2", "options": ["fast", "deep"]},
        ],
    )
    assert [d.answer for d in out] == ["deep", "deep"]
    assert all(d.source == "local:chat" for d in out)
    assert out[0].decision_id != out[1].decision_id


# --------------------------------------------------------------- the door wins
def test_a_live_door_is_never_second_guessed_locally(monkeypatch):
    """A real socket to a real (tiny) door: the local backend is not even imported."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_POST(self):  # noqa: N802 - http.server's own name
            body = json.dumps(
                {"decision_id": "from-the-door", "answer": "deep", "confidence": 0.9,
                 "source": "engine", "learned_from": 7}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("AITHER_DECIDE_URL", f"http://127.0.0.1:{srv.server_port}")

        def never():
            raise AssertionError("the local backend was consulted while the door was up")

        monkeypatch.setattr(door, "_import_awdecide", never)
        d = door.decide("router", "kind:code", options=["fast", "deep"])
        assert d.answer == "deep" and d.source == "engine" and d.learned
        assert d.decision_id == "from-the-door"
        assert door._LOCAL_IDS == set()
    finally:
        srv.shutdown()
        srv.server_close()


def test_importing_the_client_does_not_import_awdecide():
    """The guard is lazy as well as guarded: a plain `import adk.choose` must not
    drag in the optional package, even on a machine that has it."""
    import subprocess
    import sys

    probe = ("import sys, adk.choose; "
             "sys.exit(17 if 'awdecide' in sys.modules else 0)")
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-400:]


# ------------------------------------------------------------- awdecide absent
def test_without_awdecide_the_error_names_the_fix(monkeypatch):
    monkeypatch.setattr(door, "_import_awdecide", lambda: None)
    monkeypatch.setenv("AITHER_DECIDE_URL", "local")
    with pytest.raises(door.DecideUnavailableError, match=r"awdk\[decide\]"):
        door.decide("router", "kind:code", options=["fast", "deep"])

    monkeypatch.delenv("AITHER_DECIDE_URL")
    _door_is_down(monkeypatch)
    with pytest.raises(door.DecideUnavailableError) as e:
        door.decide("router", "kind:code", options=["fast", "deep"])
    assert "awdk[decide]" in str(e.value) and "unreachable" in str(e.value)
