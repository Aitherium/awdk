"""The VRoid Hub loopback catcher bounces code+state to Persona and does nothing else.

Arms that must REFUSE: a non-loopback bind, a malformed code/state, a return origin
off the allowlist, and the daemon route without its loopback + first-party guard.
One live arm binds a real socket on an ephemeral loopback port and follows the
302 the way a browser would, then proves the listener shut down after it.
"""

from __future__ import annotations

import http.client
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest

from adk import vroid_callback as vc

GOOD_ORIGIN = "https://aitherium.com"
LOOPBACK = ("127.0.0.1", 41234)


# ── pure request handling ───────────────────────────────────────────────────


def test_callback_redirects_to_persona_on_the_allowlisted_origin():
    status, headers, consumed = vc.handle_callback("/callback?code=abc-123_X.~&state=s7ate", GOOD_ORIGIN)
    assert status == 302 and consumed
    loc = urlsplit(headers["Location"])
    assert f"{loc.scheme}://{loc.netloc}" == GOOD_ORIGIN
    assert loc.path == "/"
    assert parse_qs(loc.query) == {"app": ["persona"], "vroid_code": ["abc-123_X.~"],
                                   "vroid_state": ["s7ate"]}


def test_vroid_refusal_bounces_the_error_not_a_code():
    status, headers, consumed = vc.handle_callback("/callback?error=access_denied&state=s1", GOOD_ORIGIN)
    assert status == 302 and consumed
    q = parse_qs(urlsplit(headers["Location"]).query)
    assert q["vroid_error"] == ["access_denied"] and "vroid_code" not in q


@pytest.mark.parametrize("target", [
    "/callback",                                      # nothing
    "/callback?code=abc",                             # no state
    "/callback?state=abc",                            # no code
    "/callback?code=a%20b&state=s",                   # space
    "/callback?code=a&state=%3Cscript%3E",            # markup
    "/callback?code=a&state=s&state=t",               # repeated param
    "/callback?code=" + "a" * 513 + "&state=s",       # too long
    "/callback?code=a&state=s%0d%0aSet-Cookie:x",     # header injection
    "/callback?error=x&code=a&state=s",               # error AND code
])
def test_bad_params_are_400_and_do_not_consume_the_callback(target):
    status, headers, consumed = vc.handle_callback(target, GOOD_ORIGIN)
    assert (status, headers, consumed) == (400, {}, False)


def test_other_paths_are_404():
    assert vc.handle_callback("/?code=a&state=b", GOOD_ORIGIN)[0] == 404
    assert vc.handle_callback("/callback/../x?code=a&state=b", GOOD_ORIGIN)[0] == 404


# ── origin + bind allowlists ────────────────────────────────────────────────


@pytest.mark.parametrize("origin", [
    "https://evil.example", "https://aitherium.com.evil.example",
    "http://aitherium.com", "https://tenant.aitherium.com",
])
def test_redirect_only_to_allowlisted_origins(origin, monkeypatch):
    monkeypatch.delenv("AITHER_VROID_RETURN_ORIGIN", raising=False)
    with pytest.raises(vc.CatcherError):
        vc.persona_url(origin, {"code": "a", "state": "b"})
    with pytest.raises(vc.CatcherError):
        vc.VRoidCallbackCatcher(origin, port=0)
    # An unlisted asking page falls back to the configured default, never to itself.
    assert vc.resolve_return_origin(origin) == "https://aitherium.com"


def test_desktop_and_localhost_dev_are_allowed(monkeypatch):
    monkeypatch.delenv("AITHER_VROID_RETURN_ORIGIN", raising=False)
    assert vc.resolve_return_origin("https://desktop.aitherium.com") == "https://desktop.aitherium.com"
    assert vc.resolve_return_origin("http://localhost:3000") == "http://localhost:3000"


def test_configured_origin_is_held_to_the_same_allowlist(monkeypatch):
    monkeypatch.setenv("AITHER_VROID_RETURN_ORIGIN", "https://evil.example")
    with pytest.raises(vc.CatcherError):
        vc.resolve_return_origin(None)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "192.168.1.5", "localhost"])
def test_catcher_refuses_a_non_loopback_bind(host):
    with pytest.raises(vc.CatcherError):
        vc.VRoidCallbackCatcher(GOOD_ORIGIN, host=host, port=0)


def test_registered_redirect_uri_is_unchanged():
    assert vc.REDIRECT_URI == "http://127.0.0.1:47835/callback"


# ── live socket: one callback, then gone ────────────────────────────────────


def _get(port: int, target: str) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", target)
    return conn.getresponse()


def test_live_catcher_bounces_once_then_stops():
    catcher = vc.VRoidCallbackCatcher(GOOD_ORIGIN, port=0, timeout_s=30)
    catcher.start()
    try:
        port = catcher.bound_port
        assert catcher._server.server_address[0] == "127.0.0.1"
        bad = _get(port, "/callback?code=a%20b&state=s")
        assert bad.status == 400
        bad.read()
        assert catcher.running  # a malformed hit does not use up the callback
        res = _get(port, "/callback?code=good&state=st")
        assert res.status == 302
        assert res.getheader("Location") == (
            "https://aitherium.com/?app=persona&vroid_code=good&vroid_state=st")
        assert res.getheader("Cache-Control") == "no-store"
        res.read()
        for _ in range(50):
            if not catcher.running:
                break
            import time
            time.sleep(0.05)
        assert not catcher.running
        with pytest.raises(OSError):
            _get(port, "/callback?code=again&state=st")
    finally:
        catcher.stop()


def test_live_catcher_times_out():
    catcher = vc.VRoidCallbackCatcher(GOOD_ORIGIN, port=0, timeout_s=0.2)
    catcher.start()
    try:
        import time
        for _ in range(50):
            if not catcher.running:
                break
            time.sleep(0.05)
        assert not catcher.running
    finally:
        catcher.stop()


# ── the daemon route ────────────────────────────────────────────────────────


def _app():
    from adk.config import Config
    from adk.server import create_app

    config = Config()
    config.gateway_url = ""
    config.aither_api_key = ""
    agent = MagicMock()
    agent.name = "test"
    agent.llm = MagicMock()
    agent.llm.provider_name = "test"
    agent._identity = MagicMock()
    agent._identity.name = "test"
    agent._identity.description = "Test"
    agent._identity.skills = []
    agent._tools = MagicMock()
    agent._tools.list_tools = MagicMock(return_value=[])
    agent._safety = None
    return create_app(agent=agent, identity="test", config=config)


@pytest.fixture()
def started(monkeypatch):
    calls = []

    def fake_start(origin):
        calls.append(origin)
        return {"listening": True, "redirect_uri": vc.REDIRECT_URI,
                "return_origin": vc.resolve_return_origin(origin), "expires_in": 600}

    monkeypatch.setattr(vc, "start_catcher", fake_start)
    monkeypatch.delenv("AITHER_BROWSER_HANDOFF", raising=False)
    monkeypatch.delenv("AITHER_VROID_RETURN_ORIGIN", raising=False)
    return calls


def test_route_starts_the_catcher_for_a_first_party_loopback_page(started):
    from fastapi.testclient import TestClient

    client = TestClient(_app(), client=LOOPBACK)
    r = client.post("/avatars/vroid/catcher", headers={"Origin": GOOD_ORIGIN})
    assert r.status_code == 200, r.text
    assert r.json()["redirect_uri"] == "http://127.0.0.1:47835/callback"
    assert started == [GOOD_ORIGIN]


def test_route_refuses_a_stranger_origin(started):
    from fastapi.testclient import TestClient

    client = TestClient(_app(), client=LOOPBACK)
    r = client.post("/avatars/vroid/catcher", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert started == []


def test_route_refuses_a_non_loopback_peer(started):
    from fastapi.testclient import TestClient

    client = TestClient(_app(), client=("10.0.0.7", 5555))
    r = client.post("/avatars/vroid/catcher", headers={"Origin": GOOD_ORIGIN})
    assert r.status_code in (401, 403)
    assert started == []
