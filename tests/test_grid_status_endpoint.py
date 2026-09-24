"""GET /grid/status on the ADK node server + the shared collector."""

import os
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

_SAVED = {
    "grid_nodes": {
        "reasoning": {"host": "10.0.0.5", "port": 11434, "model": "deepseek-r1:8b"},
        "cluster": [{"host": "10.0.0.9", "port": 8121}],
    }
}


def _fake_probe(host, port, timeout=5.0):
    if host == "10.0.0.5":
        return {"state": "healthy", "models": ["deepseek-r1:8b"]}
    return {"state": "unreachable", "models": [], "error": "URLError"}


def _make_app(api_key):
    with patch.dict(os.environ, {"AITHER_SERVER_API_KEY": api_key}, clear=False):
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


def test_collect_grid_status_reports_each_node():
    from adk.grid_status import collect_grid_status

    st = collect_grid_status({}, _SAVED["grid_nodes"], probe=_fake_probe)
    assert st["total"] == 2 and st["healthy"] == 1
    assert [n["role"] for n in st["nodes"]] == ["reasoning", "cluster"]
    only = collect_grid_status({}, _SAVED["grid_nodes"], target_host="10.0.0.9", probe=_fake_probe)
    assert only["total"] == 1 and only["nodes"][0]["state"] == "unreachable"


def test_collect_grid_status_flat_url_fallback():
    from adk.grid_status import collect_grid_status

    st = collect_grid_status({"cluster_url": "http://10.1.1.1:9000"}, {}, probe=_fake_probe)
    assert st["nodes"][0]["host"] == "10.1.1.1" and st["nodes"][0]["port"] == 9000


def test_grid_status_route_returns_probe_result():
    app = _make_app(api_key="k-test")
    with patch("adk.config.load_saved_config", return_value=_SAVED), \
            patch("adk.grid_status.probe_node", side_effect=_fake_probe):
        client = TestClient(app)
        resp = client.get("/grid/status", headers={"Authorization": "Bearer k-test"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2 and body["healthy"] == 1
    assert body["nodes"][0]["models"] == ["deepseek-r1:8b"]


def test_grid_status_route_requires_auth_off_box():
    app = _make_app(api_key="k-test")
    client = TestClient(app)
    resp = client.get("/grid/status")
    assert resp.status_code == 401
