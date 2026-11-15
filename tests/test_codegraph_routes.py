"""/v1/codegraph/*: the daemon's ONE warm code graph answers other processes."""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ROUTE = "/v1/codegraph/query"

from adk import builtin_tools, codegraph_routes  # noqa: E402


class _Idx:
    def __init__(self, root, warm=True):
        self.root, self.is_warm, self.reason = root, warm, ""


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(codegraph_routes.router)
    return TestClient(app)


def _bind(monkeypatch, idx, hits=None):
    monkeypatch.setattr(builtin_tools, "_local_code_graph", lambda: idx)

    async def fake_hits(q, k):
        fake_hits.calls.append((q, k))
        return hits

    fake_hits.calls = []
    monkeypatch.setattr(builtin_tools, "_local_graph_hits", fake_hits)
    return fake_hits


def test_info_names_the_served_root(client, monkeypatch, tmp_path):
    _bind(monkeypatch, _Idx(str(tmp_path)))
    body = client.get("/v1/codegraph/info").json()
    assert body["root"] == str(tmp_path) and body["warm"] is True


def test_info_without_a_graph(client, monkeypatch):
    _bind(monkeypatch, None)
    assert client.get("/v1/codegraph/info").json()["root"] is None


def test_query_answers_from_the_daemon_graph(client, monkeypatch, tmp_path):
    hit = {"name": "f", "path": os.path.join(str(tmp_path), "a.py"), "line": 3,
           "signature": "def f()"}
    calls = _bind(monkeypatch, _Idx(str(tmp_path)),
                  {"count": 1, "results": [hit], "source": "local-awgraph"})
    r = client.post(_ROUTE, json={"q": "f", "k": 999, "root": str(tmp_path)})
    assert r.status_code == 200 and r.json()["hits"] == [hit]
    assert calls.calls == [("f", codegraph_routes.MAX_K)]


def test_another_root_is_a_409_and_never_queried(client, monkeypatch, tmp_path):
    calls = _bind(monkeypatch, _Idx(str(tmp_path)), {"results": []})
    r = client.post(_ROUTE,
                    json={"q": "f", "root": str(tmp_path / "other")})
    assert r.status_code == 409 and calls.calls == []


def test_a_warming_graph_is_a_503_loading(client, monkeypatch, tmp_path):
    _bind(monkeypatch, _Idx(str(tmp_path), warm=False),
          {"error": "local code graph is still loading after a restart; retry"})
    r = client.post(_ROUTE, json={"q": "f", "root": str(tmp_path)})
    assert r.status_code == 503 and r.json()["error"] == "loading"


def test_a_failed_query_is_a_502(client, monkeypatch, tmp_path):
    _bind(monkeypatch, _Idx(str(tmp_path)), {"error": "local code graph: boom"})
    r = client.post(_ROUTE, json={"q": "f", "root": str(tmp_path)})
    assert r.status_code == 502


def test_no_graph_and_empty_query(client, monkeypatch, tmp_path):
    _bind(monkeypatch, None)
    assert client.post(_ROUTE, json={"q": "f"}).status_code == 503
    assert client.post(_ROUTE, json={"q": " "}).status_code == 400


def test_a_worker_that_deferred_is_a_503_deferred_and_not_asked_again(
        client, monkeypatch, tmp_path):
    idx = _Idx(str(tmp_path), warm=False)
    idx._ok, idx.reason = False, "deferred: host commit 94% >= 85% (graph worker load)"
    calls = _bind(monkeypatch, idx, {"results": []})
    r = client.post(_ROUTE, json={"q": "f", "root": str(tmp_path)})
    body = r.json()
    assert r.status_code == 503 and body["deferred"] is True
    assert body["reason"].startswith("deferred: host commit 94%")
    assert calls.calls == []


def test_a_deferral_reported_by_the_query_is_a_503_deferred(client, monkeypatch, tmp_path):
    _bind(monkeypatch, _Idx(str(tmp_path)), {
        "error": "local code graph: AwgraphUnavailable: deferred: another full load "
                 "of this root is running"})
    r = client.post(_ROUTE, json={"q": "f", "root": str(tmp_path)})
    assert r.status_code == 503 and r.json()["error"] == "deferred"
    assert "another full load" in r.json()["reason"]


def test_a_deferred_worker_gets_a_background_retry_not_a_permanent_deferral(
        client, monkeypatch, tmp_path):
    idx = _Idx(str(tmp_path), warm=False)
    idx._ok, idx.reason = False, "deferred: busy: a full load of this root is running"
    opened = []

    async def _open():
        opened.append(1)
        return False

    idx._open = _open
    _bind(monkeypatch, idx, {"results": []})
    r = client.post(_ROUTE, json={"q": "f", "root": str(tmp_path)})
    assert r.status_code == 503 and r.json()["deferred"] is True
    assert opened == [1]  # the index's own retry ran; the request did not wait on it
