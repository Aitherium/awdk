"""A failed hosted create must keep the platform instance id so `adk fleet rm`
can tear it down.

Genesis saves the record as ``failed`` and raises (504 not_ready, 502
loop/hostname, 500 meter) with ``detail.instance``. That row still counts toward
the tenant's instance quota. Before this fix the client dropped the id on any
4xx/5xx, ``HostedDriver.remove`` saw no ref and did nothing, and three failed
creates locked the tenant out with 429 instance_quota.
"""
from __future__ import annotations

import httpx

from adk.fleet_manager import FleetManager, FleetStore, HostedDriver


def _resp(status: int, body) -> httpx.Response:
    req = httpx.Request("POST", "https://gw.example/v1/instances")
    return httpx.Response(status, json=body, request=req)


def test_failed_create_keeps_ref_and_rm_deletes_it(tmp_path, monkeypatch):
    body = {"detail": {"error": "not_ready", "detail": {"last": "no /health 200"},
                       "instance": {"id": "inst-abc123", "status": "failed"}}}
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _resp(504, body))
    removed: list[str] = []
    drv = HostedDriver(remove_fn=lambda ref: removed.append(ref) or True)
    mgr = FleetManager(store=FleetStore(tmp_path / "fleet.json"), drivers={"hosted": drv})

    m = mgr.create("hosted", "slowpoke")
    assert m.status == "failed"
    assert "504" in m.error
    assert m.ref == "inst-abc123"

    assert mgr.remove(m.id) is True
    assert removed == ["inst-abc123"]


def test_failed_create_without_instance_has_no_ref(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _resp(429, {"detail": {"error": "instance_quota"}}))
    removed: list[str] = []
    drv = HostedDriver(remove_fn=lambda ref: removed.append(ref) or True)
    mgr = FleetManager(store=FleetStore(tmp_path / "fleet.json"), drivers={"hosted": drv})
    m = mgr.create("hosted", "over-quota")
    assert m.status == "failed" and not m.ref
    mgr.remove(m.id)
    assert removed == []


def test_non_json_error_body_is_tolerated(monkeypatch):
    from adk.fleet_manager import _http_instance_create

    req = httpx.Request("POST", "https://gw.example/v1/instances")
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(502, text="bad gateway", request=req))
    res = _http_instance_create("x", {})
    assert res["ok"] is False and "instance_id" not in res
