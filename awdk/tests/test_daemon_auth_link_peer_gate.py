# SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
# © 2026 Aitherium, LLC. Original work.
"""The /auth/link gate must key on the TCP PEER, not the Host header.

Found by adversarial review 2026-08-12, on code written the same day. The daemon binds
0.0.0.0 deliberately (DEFAULT_BIND_HOST: after the podman cutover, genesis reaches the
harness across the WSL2 network and a Windows-loopback socket is unreachable from there).
That makes these routes visible to every host on that network, over plaintext HTTP.

The first implementation gated them on the Host header. Only a BROWSER is bound by Host;
an attacker with a socket simply sets it:

    curl -H "Host: 127.0.0.1:8362" -H "Authorization: Bearer <sniffed>" \
         http://<lan-ip>:8362/auth/link

That left the cleartext bearer as the sole protection on the one endpoint that starts a
device-code flow — so a sniffed token lets an attacker initiate a login the user then
unknowingly approves in the portal.

A TCP source address cannot be forged the way a header can: a spoofed SYN never completes
the handshake, so no HTTP request is ever delivered. The peer is therefore the decision;
Host remains as cheaper defence-in-depth.

These tests pin BOTH directions — the attack must stay refused, and genuine loopback must
keep working — so a future refactor cannot quietly restore the header-only gate.
"""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import Depends, FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

DEFAULT_PORT = 8362
_LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})


def _validate_localhost_origin(request: Request, host: str = Header(default="")) -> None:
    """Mirror of the daemon dependency; see adk/harnesses/daemon.py."""
    peer = getattr(getattr(request, "client", None), "host", None)
    # Fail CLOSED: an unknown peer is refused, never allowed.
    if peer not in _LOOPBACK:
        raise HTTPException(status_code=403, detail="this endpoint is reachable from loopback only")
    if not host:
        raise HTTPException(status_code=400, detail="missing Host header")
    if host not in (f"127.0.0.1:{DEFAULT_PORT}", f"localhost:{DEFAULT_PORT}"):
        raise HTTPException(status_code=403, detail=f"Host header {host} not allowed.")


@pytest.fixture()
def app() -> FastAPI:
    api = FastAPI()

    @api.post("/auth/link", dependencies=[Depends(_validate_localhost_origin)])
    async def _link() -> dict:
        return {"ok": True}

    return api


def test_spoofed_host_from_remote_peer_is_refused(app: FastAPI) -> None:
    """THE ATTACK: a perfect Host header from a non-loopback peer must still 403."""
    # TestClient's default peer is 'testclient' — i.e. not loopback.
    with TestClient(app) as client:
        res = client.post("/auth/link", headers={"Host": f"127.0.0.1:{DEFAULT_PORT}"})
    assert res.status_code == 403, (
        "a remote peer forged the Host header and was let through — this is the "
        "header-only gate the peer check exists to replace"
    )


def test_loopback_peer_is_allowed(app: FastAPI) -> None:
    """The fix must not break the real client: adk/the browser on this machine."""
    with TestClient(app, client=("127.0.0.1", 51234)) as client:
        res = client.post("/auth/link", headers={"Host": f"127.0.0.1:{DEFAULT_PORT}"})
    assert res.status_code == 200


def test_loopback_peer_with_foreign_host_is_refused(app: FastAPI) -> None:
    """Host stays as defence-in-depth (DNS-rebinding), just not as the decision."""
    with TestClient(app, client=("127.0.0.1", 51234)) as client:
        res = client.post("/auth/link", headers={"Host": "evil.example.com"})
    assert res.status_code == 403


def test_daemon_uses_the_peer_check_not_host_alone() -> None:
    """Pin the real module: the shipped dependency must read request.client."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "adk" / "harnesses" / "daemon.py"
    text = src.read_text(encoding="utf-8")
    assert "_validate_localhost_origin" in text
    assert "request.client" in text or 'getattr(getattr(request, "client"' in text, (
        "daemon.py's localhost gate no longer inspects the TCP peer — a Host-header-only "
        "check is forgeable by any non-browser client on the bind-all interface"
    )
