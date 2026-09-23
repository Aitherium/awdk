"""Cloud-mode fleet sync must hit routes the gateway actually serves.

In cloud mode the fleet target is ``{gateway}/v1/memory``. The gateway registers
ONLY ``/v1/memory/teach`` and ``/v1/memory/recall`` (apps/awnode/mcp_gateway.py);
``/ingest`` and ``/search`` there are 404s, so every push was silently dropped
and fleet search always returned nothing.
"""

import sqlite3

import httpx
import pytest

from adk.memory import Memory

GATEWAY = "https://gw.test"
SERVED = {f"{GATEWAY}/v1/memory/teach", f"{GATEWAY}/v1/memory/recall"}


class _FakeClient:
    calls: list = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.calls.append((url, json, headers))
        req = httpx.Request("POST", url)
        if url not in SERVED:
            return httpx.Response(404, json={"error": "not found"}, request=req)
        if url.endswith("/teach"):
            return httpx.Response(
                200, json={"success": True, "memory_id": json["memory_id"]}, request=req,
            )
        return httpx.Response(
            200,
            json={"results": [{"title": "remote_fact", "content": "from the fleet"}]},
            request=req,
        )


@pytest.fixture
def cloud_mem(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_CLOUD_MODE", "cloud_only")
    monkeypatch.setenv("AITHER_GATEWAY_URL", GATEWAY)
    monkeypatch.setenv("AITHER_FLEET_SYNC", "true")
    monkeypatch.setenv("AITHER_TENANT_ID", "tnt_mine")
    monkeypatch.setenv("AITHER_SPIRIT_ENABLED", "false")
    monkeypatch.delenv("AITHER_FLEET_MEMORY_URL", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls = []
    return Memory(db_path=tmp_path / "m.db", agent_name="agent-a")


@pytest.mark.asyncio
async def test_cloud_push_lands_on_a_served_route_and_marks_synced(cloud_mem):
    with sqlite3.connect(cloud_mem._db_path) as conn:
        conn.execute(
            "INSERT INTO kv_store (key, value, category, timestamp) VALUES (?,?,?,0)",
            ("k1", "v1", "general"),
        )
    await cloud_mem._fleet_push("k1", "k1: v1", "general")

    urls = [c[0] for c in _FakeClient.calls]
    assert urls == [f"{GATEWAY}/v1/memory/teach"], urls
    body = _FakeClient.calls[0][1]
    # Tenant is the gateway's to decide, from auth — never carried in the body.
    assert "tenant_id" not in body
    assert "tenant_id" not in (body.get("metadata") or {})
    assert body["upsert"] is True and body["memory_id"]
    assert await cloud_mem.fleet_sync_pending() == 0


@pytest.mark.asyncio
async def test_cloud_push_is_idempotent_per_key(cloud_mem):
    await cloud_mem._fleet_push("k1", "k1: v1")
    await cloud_mem._fleet_push("k1", "k1: v2")
    ids = {c[1]["memory_id"] for c in _FakeClient.calls}
    assert len(ids) == 1


@pytest.mark.asyncio
async def test_cloud_search_lands_on_a_served_route(cloud_mem):
    results = await cloud_mem._fleet_search("fact", limit=3)
    assert [c[0] for c in _FakeClient.calls] == [f"{GATEWAY}/v1/memory/recall"]
    assert "tenant_id" not in _FakeClient.calls[0][1]
    assert results and results[0]["title"] == "remote_fact"


@pytest.mark.asyncio
async def test_direct_nexus_target_keeps_ingest_and_search(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_FLEET_MEMORY_URL", "https://nexus.test:8122")
    monkeypatch.delenv("AITHER_CLOUD_MODE", raising=False)
    monkeypatch.setenv("AITHER_SPIRIT_ENABLED", "false")
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls = []
    mem = Memory(db_path=tmp_path / "n.db", agent_name="agent-a")
    await mem._fleet_push("k", "k: v")
    await mem._fleet_search("q")
    assert [c[0] for c in _FakeClient.calls] == [
        "https://nexus.test:8122/ingest",
        "https://nexus.test:8122/search",
    ]
