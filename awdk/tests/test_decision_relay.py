"""Tests for the off-desktop decision-card relay (``AITHER_DECISIONS_RELAY_URL``).

The gap it closes: a card raised by an awdk session on the FLEET (container,
tunnel/phone) landed only in that machine's local store and never reached the
owner's desktop. Genesis's ``/api/v1/decisions/raise`` already proxies to the
owner-host harness daemon (the "an agent anywhere reaches its owner" path), so
the relay mirrors a raised card there when configured. Fail-soft and opt-in:
unset env = byte-identical behaviour to before.
"""

from __future__ import annotations

import importlib
import sys
import threading

import pytest


@pytest.fixture
def at(monkeypatch):
    monkeypatch.delenv("AITHER_DECISIONS_RELAY_URL", raising=False)
    import adk.decisions.agent_tools as at

    importlib.reload(at)
    return at


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeHttpx:
    """Fake httpx module — the relay's only use is ``httpx.post(...)``.

    The relay runs on a daemon thread, so the fake signals a threading.Event
    when post is called; tests wait on it instead of racing the thread.
    """

    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.calls: list[tuple[str, dict, dict]] = []
        self.called = threading.Event()

    def post(self, url: str, *, json: dict, headers: dict, timeout: float):
        self.calls.append((url, json, headers))
        self.called.set()
        return _FakeResponse(self.status_code)

    def wait(self, timeout: float = 3.0):
        assert self.called.wait(timeout), "relay thread never posted"


@pytest.fixture
def fake_httpx(monkeypatch):
    fake = _FakeHttpx()
    monkeypatch.setitem(sys.modules, "httpx", fake)  # lazy in-function import
    return fake


def test_relay_unset_env_is_noop(at):
    """No env → no POST, no exception — the pre-existing behaviour exactly."""
    card = at.raise_card("test no relay", summary="s", kind="info")
    assert card.title == "test no relay"


def test_relay_posts_full_endpoint_when_base_given(at, monkeypatch, fake_httpx):
    """A genesis base URL gets the /api/v1/decisions/raise path appended."""
    monkeypatch.setenv("AITHER_DECISIONS_RELAY_URL", "https://genesis.example:8001")
    importlib.reload(at)
    at.raise_card(
        "ship it",
        summary="summary text",
        options=[{"key": "yes", "label": "Yes", "consequence": "deploys"}],
        recommend="yes",
        default="yes",
        agent="test-agent",
        session_id="sess-1",
    )
    fake_httpx.wait()
    assert len(fake_httpx.calls) == 1
    url, payload, headers = fake_httpx.calls[0]
    assert url == "https://genesis.example:8001/api/v1/decisions/raise"
    assert headers == {"X-Caller-Type": "platform"}
    assert payload["title"] == "ship it"
    assert payload["kind"] == "decision"
    assert payload["summary"] == "summary text"
    assert payload["options"] == [
        {"key": "yes", "label": "Yes", "consequence": "deploys", "recommended": True}
    ]
    assert payload["default"] == "yes"
    assert payload["via"] == "awdk"
    assert payload["agent"] == "test-agent"
    assert payload["session_id"] == "sess-1"


def test_relay_full_url_not_doubled(at, monkeypatch, fake_httpx):
    """A URL already ending in /api/v1/decisions/raise is used verbatim."""
    monkeypatch.setenv(
        "AITHER_DECISIONS_RELAY_URL",
        "https://genesis.example:8001/api/v1/decisions/raise",
    )
    importlib.reload(at)
    at.raise_card(
        "ship it",
        options=[{"key": "yes", "label": "Yes"}],
        kind="decision",
    )
    fake_httpx.wait()
    assert fake_httpx.calls[0][0] == "https://genesis.example:8001/api/v1/decisions/raise"


def test_relay_never_breaks_the_local_raise(at, monkeypatch):
    """Unreachable relay + non-2xx response → card still created locally."""
    card = at.raise_card("still local", summary="s", kind="info")
    assert card.title == "still local"


def test_relay_exception_is_swallowed(at, monkeypatch, fake_httpx):
    """A raising transport must not fail the raise — same contract as notify()."""
    def boom(*args, **kwargs):
        raise RuntimeError("connection refused")

    fake_httpx.post = boom
    monkeypatch.setenv("AITHER_DECISIONS_RELAY_URL", "https://genesis.example:8001")
    importlib.reload(at)
    card = at.raise_card("resilient", summary="s", kind="info")
    assert card.title == "resilient"
