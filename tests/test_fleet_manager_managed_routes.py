"""The managed driver must hit the routes Genesis actually serves.

Genesis ``routers/agent_binding.py`` mounts ``APIRouter(prefix="/v1/agent")`` with
``POST /managed/deploy`` (body ``ManagedDeployBody.agent_id``) and
``DELETE /managed?agent_id=``. The CLI once posted ``/v1/agent/binding/managed-deploy``
with ``{"agent": ...}`` — a route that never existed — so ``adk fleet create
--runtime managed`` could not succeed.
"""

from __future__ import annotations

import httpx

from adk import fleet_manager as fm


class _Resp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"ok": True, "deployed": True, "anthropic_agent_id": "agt_1"}


def test_deploy_posts_to_genesis_route_with_agent_id(monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json)
        return _Resp()

    monkeypatch.setenv("AITHER_API_URL", "https://api.example")
    monkeypatch.setattr(httpx, "post", fake_post)
    res = fm._http_managed_deploy("aither", {"model": "m", "company": "ignored"})
    assert res["deployed"] is True
    assert seen["url"] == "https://api.example/v1/agent/managed/deploy"
    assert seen["json"] == {"agent_id": "aither", "model": "m"}


def test_demigrate_deletes_genesis_route(monkeypatch):
    seen = {}

    def fake_request(method, url, params=None, headers=None, timeout=None):
        seen.update(method=method, url=url, params=params)
        return _Resp()

    monkeypatch.setenv("AITHER_API_URL", "https://api.example")
    monkeypatch.setattr(httpx, "request", fake_request)
    assert fm._http_managed_demigrate("aither", {}) is True
    assert seen == {"method": "DELETE", "url": "https://api.example/v1/agent/managed",
                    "params": {"agent_id": "aither"}}
