"""The `awdk` harness relays a sovereign agent to THIS host's awdk loop (2026-09-21).

Genesis publishes no host port, so the `aither` harness's default base URL can never
answer from the host daemon. `awdk` uses the same relay session and wire, pointed at
``adk serve`` (``AITHER_ADK_URL``, default 127.0.0.1:9001), and the translator must
carry adk's ``token`` stream event, which Genesis never emits.
"""

from __future__ import annotations

from adk.harnesses.agents import ADK_URL, translate_genesis
from adk.harnesses.events import EventKind
from adk.harnesses.manager import SessionManager
from adk.harnesses.registry import get as get_harness
from adk.harnesses.session import SessionConfig


def test_awdk_harness_is_registered_as_a_stream_relay():
    spec = get_harness("awdk")
    assert spec.transport == get_harness("aither").transport
    assert spec.supports_resume is True


def test_awdk_session_defaults_to_the_local_loop_and_aither_does_not(tmp_path, monkeypatch):
    from adk.harnesses import agents as agents_mod

    monkeypatch.setattr(agents_mod.AgentRelaySession, "start", lambda self: None)
    mgr = SessionManager(root=tmp_path / "sessions")

    local = mgr.create(SessionConfig(harness="awdk", agent="atlas"))
    assert local.base_url == ADK_URL.rstrip("/")
    assert local.agent == "atlas"

    fleet = mgr.create(SessionConfig(harness="aither", agent="atlas"))
    assert fleet.base_url == agents_mod.GENESIS_URL.rstrip("/")

    pinned = mgr.create(SessionConfig(harness="awdk", agent="lyra", base_url="http://x.invalid/"))
    assert pinned.base_url == "http://x.invalid"


def test_token_event_from_adk_serve_becomes_assistant_text():
    events = translate_genesis("token", {"type": "token", "t": "hel"})
    assert [e.kind for e in events] == [EventKind.TEXT_DELTA]
    assert events[0].text == "hel"
    assert translate_genesis("token", {"type": "token", "t": ""}) == []


def test_relay_skips_adk_answer_repeat_after_token_deltas(tmp_path, monkeypatch):
    """adk serve streams `token` deltas then repeats the text as `answer`: one copy reaches the UI."""
    import sys
    import types

    import adk.harnesses.agents as agents_mod
    from adk.harnesses.registry import get as get_harness
    from adk.harnesses.session import SessionConfig

    sse = [
        b"event: session_start", b'data: {"type": "session_start", "agent": "atlas"}', b"",
        b"event: token", b'data: {"type": "token", "t": "I am "}', b"",
        b"event: token", b'data: {"type": "token", "t": "Atlas."}', b"",
        b"event: answer", b'data: {"type": "answer", "answer": "I am Atlas."}', b"",
        b"event: complete", b'data: {"type": "complete", "duration_ms": 5}', b"",
    ]

    class _Resp:
        status_code = 200
        text = ""

        def read(self):
            return None

        def iter_lines(self):
            return iter(sse)

    class _Stream:
        def __init__(self, body):
            pass

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

    cfg = SessionConfig(harness="awdk", agent="atlas")
    sess = agents_mod.AgentRelaySession(get_harness("awdk"), cfg, root=tmp_path, agent="atlas")
    seen = []
    monkeypatch.setattr(sess, "_emit", lambda e, *a, **k: seen.append(e))
    monkeypatch.setattr(sess, "_observe", lambda *a, **k: None)
    sess._run_turn("who are you", 1)

    text = "".join(e.text for e in seen if e.kind == EventKind.TEXT_DELTA)
    assert text == "I am Atlas."
