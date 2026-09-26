"""The demo hall's community shelf must be served, anonymously, by the daemon.

Measured 2026-09-26: demo.aitherium.com showed "The live project registry could not be
reached (HTTP 404)". The /api/demos/community.json handler existed only on a rescue
branch and was lost in the develop switch; nothing tested it, so nothing noticed.
"""
from fastapi.testclient import TestClient

from adk.server import create_app


def _client(monkeypatch, upstream=""):
    monkeypatch.setenv("ADK_COMMUNITY_DIRECTORY_URL", upstream)
    return TestClient(create_app())


def test_route_is_registered():
    paths = {getattr(r, "path", "") for r in create_app().routes}
    assert "/api/demos/community.json" in paths


def test_served_without_a_session_and_never_404(monkeypatch):
    resp = _client(monkeypatch).get("/api/demos/community.json")
    assert resp.status_code != 404
    assert resp.status_code not in (401, 403)  # public: listed exactly in _skip_auth_paths
    assert resp.headers["content-type"].startswith("application/json")
