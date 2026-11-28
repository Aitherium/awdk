"""Tests for M2.2 F11a: ``rc_routes.py`` (``/rc/status``, ``/rc/off``), ``AWCONNECT_PATHS``, and ``session_verbs``."""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from adk.harnesses import pairing, rc_routes, session_verbs  # noqa: E402

PINNED = "chrome-extension://hlmfknhcfhjjngckfpacgleffckpmphe"
LOOP = {"host": "127.0.0.1:8362"}
ROOT = "root-bearer-for-rc-routes-tests"


@pytest.fixture
def rc_app(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", ROOT)
    monkeypatch.delenv("AITHER_RC_STATE_FILE", raising=False)
    monkeypatch.delenv("AITHER_TRUSTED_EXTENSION_IDS", raising=False)

    import adk.decisions.store as store_mod
    import adk.harnesses.daemon as daemon
    from adk import fleet_enroll

    if hasattr(store_mod, "_STORE"):
        monkeypatch.setattr(store_mod, "_STORE", None, raising=False)
    reg = tmp_path / "harness_tokens.json"
    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", reg)
    monkeypatch.setattr(fleet_enroll, "_node_link", None, raising=False)
    monkeypatch.setattr(fleet_enroll, "_node_link_task", None, raising=False)
    try:
        import adk.decisions.notify as notify_mod

        monkeypatch.setattr(notify_mod, "notify", lambda *a, **k: None)
    except ImportError:
        pass

    app = daemon.create_app(token=ROOT)
    # Mount rc_routes via the M1<->M2 contract if daemon.py hasn't mounted it yet.
    if not any(getattr(r, "path", "") == "/rc/status" for r in app.routes):
        # Extract auth / _require_owner through a tiny check or re-mount on a fresh app?
        # Wait: daemon.create_app already holds the closures; let's inspect or mount via helper
        # if M1 hasn't wired daemon.py yet. Let's check:
        pass

    return {"app": app, "daemon": daemon, "reg": reg, "tmp": tmp_path, "fleet": fleet_enroll}


def _build_client_with_rc_routes(rc_app, *, link_box=None, stop_cb=None):
    """Build a TestClient guaranteeing rc_routes is mounted with daemon's real auth."""
    app = rc_app["app"]
    daemon = rc_app["daemon"]
    has_rc = any(getattr(r, "path", "") == "/rc/status" for r in app.routes)
    if has_rc and link_box is None and stop_cb is None:
        return TestClient(app)

    # Mount onto a clean FastAPI app using daemon's exact auth/is_owner logic when testing custom providers,
    # OR attach directly using daemon's Principal checks so AWCONNECT_PATHS & SCOPED_LINK_PATHS are exercised!
    from fastapi import Depends, FastAPI, Header, HTTPException, Request

    sub_app = FastAPI()

    def auth(
        request: Request,
        authorization: str = Header(default=""),
    ) -> daemon.Principal:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        tok = authorization.partition(" ")[2]
        principal = daemon.resolve_principal(tok, ROOT)
        if principal is None:
            raise HTTPException(status_code=403, detail="invalid token")
        if not principal.may_reach(request.url.path, request.method):
            raise HTTPException(status_code=403, detail="path not permitted")
        return principal

    def require_owner(principal: daemon.Principal = Depends(auth)) -> daemon.Principal:
        if not daemon.is_owner(principal):
            raise HTTPException(status_code=403, detail="owner credential required")
        return principal

    rc_routes.mount(
        sub_app,
        auth,
        require_owner,
        link_provider=(lambda: link_box["link"]) if link_box is not None else None,
        stop_link_fn=stop_cb,
    )
    return TestClient(sub_app)


def test_awconnect_paths_includes_get_rc_status_only():
    assert "GET /rc/status" in pairing.AWCONNECT_PATHS
    assert "/rc/off" not in pairing.AWCONNECT_PATHS
    assert "POST /rc/off" not in pairing.AWCONNECT_PATHS


def test_rc_status_and_off_auth_gates(rc_app):
    daemon = rc_app["daemon"]
    reg = rc_app["reg"]
    client = _build_client_with_rc_routes(rc_app)

    # 401 unauthenticated
    assert client.get("/rc/status").status_code == 401
    assert client.post("/rc/off").status_code == 401

    # Scoped agent token without /rc/status -> 403
    agent_tok = daemon.mint_scoped_token("agent:p1", plan="agent", paths=("/sessions",), path=reg)
    assert (
        client.get("/rc/status", headers={"Authorization": f"Bearer {agent_tok}"}).status_code
        == 403
    )

    # Paired awconnect token -> 200 on GET /rc/status, 403 on POST /rc/off
    awconnect_tok = daemon.mint_scoped_token(
        "awconnect:ext", plan="awconnect", paths=pairing.AWCONNECT_PATHS, path=reg
    )
    aw_hdr = {"Authorization": f"Bearer {awconnect_tok}"}
    r_status = client.get("/rc/status", headers=aw_hdr)
    assert r_status.status_code == 200
    assert r_status.json() == {
        "active": False,
        "instance_id": None,
        "node_id": None,
        "rc_url": None,
        "app_url": None,
        "reach": "offline",
        "runtime_tier": "standard",
        "zero_libc": False,
    }
    assert client.post("/rc/off", headers=aw_hdr).status_code == 403

    # Scoped link token -> 403 on POST /rc/off
    link_tok = daemon.mint_scoped_token("node:n1", plan="link", path=reg)
    assert (
        client.post("/rc/off", headers={"Authorization": f"Bearer {link_tok}"}).status_code == 403
    )


def test_awconnect_full_pairing_can_read_rc_status(rc_app):
    daemon_client = TestClient(rc_app["app"])
    started = daemon_client.post("/pair/start", headers={"origin": PINNED, **LOOP}).json()
    daemon_client.post(
        "/pair/approve",
        json={"code": started["code"]},
        headers={"Authorization": f"Bearer {ROOT}"},
    )
    tok = daemon_client.post(
        "/pair/poll",
        json={"pair_id": started["pair_id"]},
        headers={"origin": PINNED, **LOOP},
    ).json()["token"]

    rc_client = _build_client_with_rc_routes(rc_app)
    resp = rc_client.get("/rc/status", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 200
    assert resp.json()["reach"] == "offline"
    assert rc_client.post("/rc/off", headers={"Authorization": f"Bearer {tok}"}).status_code == 403


def test_rc_status_reconciles_live_link_dead_pid_and_stop_rc(rc_app):
    tmp = rc_app["tmp"]
    state_file = tmp / "rc-state.json"
    # Dead PID in state -> active=False
    state_file.write_text(
        json.dumps({
            "instance_id": "stale-box",
            "node_id": "n-stale",
            "rc_url": "https://tunnel.aitherium.com/r/stale-box",
            "app_url": "https://app.aitherium.com/code/n-stale",
            "pid": 999999999,
        }),
        encoding="utf-8",
    )
    assert rc_routes.get_rc_status(
        state_path=state_file, link_provider=lambda: None, pid_alive_fn=lambda _p: False
    ) == {
        "active": False,
        "instance_id": None,
        "node_id": None,
        "rc_url": None,
        "app_url": None,
        "reach": "offline",
        "runtime_tier": "standard",
        "zero_libc": False,
    }

    # Live in-process link -> active=True, reach="tunnel"
    link_box = {
        "link": SimpleNamespace(
            connected=True,
            node_id="node-live",
            instance_id="swift-otter-1111",
            rc_url="",
            app_url="",
        )
    }

    def _stop():
        link_box["link"] = None

    client = _build_client_with_rc_routes(rc_app, link_box=link_box, stop_cb=_stop)
    owner_hdr = {"Authorization": f"Bearer {ROOT}"}

    live = client.get("/rc/status", headers=owner_hdr).json()
    assert live == {
        "active": True,
        "instance_id": "swift-otter-1111",
        "node_id": "node-live",
        "rc_url": "https://a30.link/r/swift-otter-1111",
        "app_url": "https://app.aitherium.com/code/node-live",
        "reach": "tunnel",
        "runtime_tier": "standard",
        "zero_libc": False,
    }

    # Owner POST /rc/off stops link and clears state file
    off = client.post("/rc/off", headers=owner_hdr)
    assert off.status_code == 200
    assert off.json() == {"ok": True, "active": False}
    assert not state_file.exists()

    after = client.get("/rc/status", headers=owner_hdr).json()
    assert after["active"] is False
    assert after["reach"] == "offline"


def test_session_verbs_safe_remote_url_and_rc_resolution():
    # Valid URLs (a30.link primary + tunnel.aitherium.com compatibility alias)
    assert (
        session_verbs.safe_remote_url("https://a30.link/r/swift-falcon")
        == "https://a30.link/r/swift-falcon"
    )
    assert (
        session_verbs.safe_remote_url("https://tunnel.aitherium.com/r/swift-falcon")
        == "https://tunnel.aitherium.com/r/swift-falcon"
    )
    assert (
        session_verbs.safe_remote_url("https://app.aitherium.com/code/node-1")
        == "https://app.aitherium.com/code/node-1"
    )
    assert (
        session_verbs.safe_remote_url("https://claude.ai/code/session_abc")
        == "https://claude.ai/code/session_abc"
    )
    assert (
        session_verbs.safe_remote_url("https://beta.claude.ai/code/session_abc")
        == "https://beta.claude.ai/code/session_abc"
    )

    # Rejected URLs
    for bad in (
        "",
        None,
        "http://tunnel.aitherium.com/r/foo",
        "javascript:alert(1)",
        "https://tunnel.aitherium.com.evil.com/r/foo",
        "https://user:pass@tunnel.aitherium.com/r/foo",
        "https://tunnel.aitherium.com/r/../secret",
        "https://tunnel.aitherium.com/r/%2e%2e/secret",
    ):
        assert session_verbs.safe_remote_url(bad) == ""

    # remote_url resolution priority & backwards compatibility
    assert (
        session_verbs.remote_url({"rc_url": "https://tunnel.aitherium.com/r/my-box"})
        == "https://tunnel.aitherium.com/r/my-box"
    )
    assert (
        session_verbs.remote_url({"instance_id": "swift-falcon-0001"})
        == "https://a30.link/r/swift-falcon-0001"
    )
    assert (
        session_verbs.remote_url({"bridge_session_id": "session_abc"})
        == "https://claude.ai/code/session_abc"
    )
    assert (
        session_verbs.remote_url({"node_id": "node-123"})
        == "https://app.aitherium.com/code/node-123"
    )
    assert session_verbs.remote_url({"instance_id": ".."}) == ""
    assert session_verbs.remote_url({"bridge_session_id": ".."}) == ""
