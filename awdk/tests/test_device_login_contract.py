"""AitherIdentity device-flow wire contract.

Every advertised ACP auth method runs this code — `aither-device` calls it from
`ACPServer.handle_auth_login`, `aither-terminal` from `adk acp login`. It was
broken FOUR ways at once in 3.0.2 and no test could see any of them, because a
mocked endpoint answers whatever the mock was written to answer:

  1. `DEFAULT_PORTAL_URL` was `https://api.aitheros.ai`, which does not resolve
     (`getaddrinfo failed`). Every developer box here has AITHERIDENTITY_URL set,
     so the dead default only ever hit strangers.
  2. `POST /oauth/device/code` — 404 on AitherIdentity. The real path is
     `/auth/device/code`, which this file's OWN sibling `autonomous_agent_login`
     had always used; the two had silently diverged.
  3. form-encoded body — 422 ("Input should be a valid dictionary"), because the
     endpoint is a FastAPI model, not an RFC 8628 form endpoint.
  4. the token poll read only `error`, but a pending poll answers **HTTP 200**
     with `{"status": "authorization_pending"}`. `err` was therefore "" on every
     tick and the loop raised "device login failed: 200" on the FIRST poll —
     so even with 1-3 fixed, no login could complete.

Mocking is the right tool for 2-4 (the SHAPES), and it is useless for 1. The
live probe at the bottom covers the dead-host class and is opt-in so CI without
egress does not flake.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from adk.auth import (
    DEFAULT_PORTAL_URL,
    AuthError,
    begin_device_login,
    finish_device_login,
)

BASE = "https://idp.example.test/identity"

_CHALLENGE = {
    "device_code": "dc-1", "user_code": "ABCD-1234",
    "verification_uri": "https://portal.example.test/link",
    "verification_uri_complete": "https://portal.example.test/link?code=ABCD-1234",
    "expires_in": 900, "interval": 1,
}


@respx.mock
async def test_begin_posts_json_to_auth_device_code():
    """Path AND encoding. `/oauth/device/code` 404s; a form body 422s."""
    route = respx.post(f"{BASE}/auth/device/code").mock(
        return_value=httpx.Response(200, json=_CHALLENGE)
    )
    ch = await begin_device_login(identity_url=BASE)

    assert route.called, "wrong path — the real endpoint is /auth/device/code"
    req = route.calls[0].request
    assert req.headers.get("content-type", "").startswith("application/json"), (
        "form-encoded body -> 422 model_attributes_type from the FastAPI endpoint"
    )
    assert ch.user_code == "ABCD-1234"
    assert ch.verification_uri_complete.endswith("code=ABCD-1234")


@respx.mock
async def test_pending_poll_is_http_200_with_a_status_field():
    """The bug that made every login fail on its FIRST poll."""
    respx.post(f"{BASE}/auth/device/code").mock(
        return_value=httpx.Response(200, json=_CHALLENGE)
    )
    respx.post(f"{BASE}/auth/device/token").mock(
        side_effect=[
            # AitherIdentity's pending shape: 200, no `error` key at all.
            httpx.Response(200, json={"status": "authorization_pending", "interval": 1}),
            httpx.Response(200, json={"status": "authorization_pending", "interval": 1}),
            httpx.Response(200, json={"access_token": "tok", "token_type": "bearer",
                                      "expires_in": 3600}),
        ]
    )
    ch = await begin_device_login(identity_url=BASE)
    creds = await finish_device_login(ch, identity_url=BASE, store=_MemStore())
    assert creds.access_token == "tok"


@respx.mock
async def test_rfc8628_pending_shape_still_works():
    """Accepting AitherIdentity's shape must not drop the standard one."""
    respx.post(f"{BASE}/auth/device/code").mock(
        return_value=httpx.Response(200, json=_CHALLENGE)
    )
    respx.post(f"{BASE}/auth/device/token").mock(
        side_effect=[
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(200, json={"access_token": "tok2", "token_type": "bearer"}),
        ]
    )
    ch = await begin_device_login(identity_url=BASE)
    creds = await finish_device_login(ch, identity_url=BASE, store=_MemStore())
    assert creds.access_token == "tok2"


@respx.mock
async def test_a_real_failure_names_the_endpoint_and_the_reason():
    """"device login failed: 400" named neither, which is what made this slow."""
    respx.post(f"{BASE}/auth/device/code").mock(
        return_value=httpx.Response(200, json=_CHALLENGE)
    )
    respx.post(f"{BASE}/auth/device/token").mock(
        return_value=httpx.Response(400, json={"detail": "device code expired"})
    )
    ch = await begin_device_login(identity_url=BASE)
    with pytest.raises(AuthError) as exc:
        await finish_device_login(ch, identity_url=BASE, store=_MemStore())
    msg = str(exc.value)
    assert "/auth/device/token" in msg, f"error names no endpoint: {msg}"
    assert "device code expired" in msg, f"error drops the server's reason: {msg}"


def test_default_identity_host_is_resolvable():
    """The dead-default class, without needing egress to the endpoint itself.

    `api.aitheros.ai` shipped as the baked default and does not resolve at all —
    DNS is enough to catch that, and it is the half that broke every stranger's
    first login.
    """
    import socket
    from urllib.parse import urlparse

    host = urlparse(DEFAULT_PORTAL_URL).hostname
    assert host, f"DEFAULT_PORTAL_URL has no host: {DEFAULT_PORTAL_URL!r}"
    try:
        socket.gethostbyname(host)
    except OSError as e:
        pytest.fail(
            f"DEFAULT_PORTAL_URL host {host!r} does not resolve ({e}). Every "
            f"device login on a machine without AITHERIDENTITY_URL set — i.e. "
            f"every new user, and both advertised ACP auth methods — fails here."
        )


@pytest.mark.skipif("not config.getoption('--run-network', default=False)")
async def test_live_identity_issues_a_challenge():
    """Opt-in end-to-end: the shipped default really issues a device code.

    This is the only assertion that covers the DEPLOYED contract; the mocks above
    prove our shape, not theirs.
    """
    ch = await begin_device_login()
    assert ch.user_code and len(ch.user_code) >= 8
    assert ch.verification_uri.startswith("https://")


class _MemStore:
    """AuthStore stand-in — these tests must not touch ~/.aither/auth.json."""

    def __init__(self) -> None:
        self.profiles: dict = {}

    def set_profile(self, name: str, profile: dict) -> None:
        self.profiles[name] = profile
