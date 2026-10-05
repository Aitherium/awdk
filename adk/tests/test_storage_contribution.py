"""Join = contribute: the storage contribution decision and its enroll hook."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adk import storage_contribution as sc

GIB = 1024 ** 3


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("AITHER_CONTRIBUTE_STORAGE", "AITHER_STORAGE_CONTRIB_SHARE",
              "AITHER_STORAGE_RESERVE_GB", "AITHER_STRATA_URL", "AITHER_MESH_URL",
              "AITHER_AITHERNET_URL", "AITHER_FAILURE_DOMAIN", "AITHER_STORAGE_PATH"):
        monkeypatch.delenv(k, raising=False)
    yield
    sc.stop_storage_keepalive()


# --- the bound --------------------------------------------------------------

def test_share_of_free_on_a_big_disk():
    assert sc.plan_contribution(1000 * GIB) == 300 * GIB


def test_reserve_caps_the_share_on_a_small_disk():
    # 30% of 25 GiB = 7.5 GiB, but only 5 GiB is above the 20 GiB reserve.
    assert sc.plan_contribution(25 * GIB) == 5 * GIB


def test_below_reserve_contributes_nothing():
    assert sc.plan_contribution(19 * GIB) == 0


def test_under_one_gib_contributes_nothing():
    assert sc.plan_contribution(int(20.5 * GIB)) == 0


def test_env_tunes_share_and_reserve(monkeypatch):
    monkeypatch.setenv("AITHER_STORAGE_CONTRIB_SHARE", "0.5")
    monkeypatch.setenv("AITHER_STORAGE_RESERVE_GB", "100")
    assert sc.plan_contribution(1000 * GIB) == 500 * GIB
    assert sc.plan_contribution(150 * GIB) == 50 * GIB


def test_share_is_clamped(monkeypatch):
    monkeypatch.setenv("AITHER_STORAGE_CONTRIB_SHARE", "5")
    assert sc.plan_contribution(1000 * GIB) == 900 * GIB


# --- opt-out policy ---------------------------------------------------------

@pytest.mark.parametrize("node_class,on", [
    ("sovereign", True), ("spark", True), ("desktop", True),
    ("laptop", False), ("deck", False), ("phone", False), ("mystery", False),
])
def test_default_by_class(node_class, on):
    assert sc.contribution_enabled(sc.device_class_for(node_class), env={})[0] is on


def test_env_opt_out_beats_server_default():
    assert sc.contribution_enabled("server", env={"AITHER_CONTRIBUTE_STORAGE": "0"})[0] is False


def test_env_opt_in_beats_phone_default():
    assert sc.contribution_enabled("phone", env={"AITHER_CONTRIBUTE_STORAGE": "1"})[0] is True


def test_explicit_argument_beats_env():
    env = {"AITHER_CONTRIBUTE_STORAGE": "1"}
    assert sc.contribution_enabled("server", enabled=False, env=env)[0] is False


# --- the decision -----------------------------------------------------------

def test_decide_server_contributes():
    d = sc.decide_contribution("sovereign", probe=sc.DiskProbe("/x", 2000 * GIB, 1000 * GIB),
                               env={})
    assert d["contribute"] is True and d["contributed_bytes"] == 300 * GIB
    assert d["device_class"] == "server"


def test_decide_laptop_skips_without_measuring():
    with patch.object(sc, "measure_disk", side_effect=AssertionError("measured")):
        d = sc.decide_contribution("laptop", env={})
    assert d["contribute"] is False and "default off" in d["reason"]


def test_decide_full_disk_skips():
    d = sc.decide_contribution("desktop", probe=sc.DiskProbe("/x", 500 * GIB, 10 * GIB), env={})
    assert d["contribute"] is False and "reserve" in d["reason"]


def test_decide_unmeasurable_disk_skips():
    with patch.object(sc, "measure_disk", side_effect=OSError("nope")):
        d = sc.decide_contribution("desktop", env={})
    assert d["contribute"] is False and "not measurable" in d["reason"]


def test_measure_disk_does_not_create_the_path(tmp_path):
    target = tmp_path / "a" / "b"
    probe = sc.measure_disk(str(target))
    assert probe.total_bytes > 0 and probe.free_bytes > 0
    assert not target.exists()


def test_payload_tiers_by_class():
    fixed = sc.build_storage_peer_payload("n", {"device_class": "server"}, hostname="h")
    phone = sc.build_storage_peer_payload("n", {"device_class": "phone"}, hostname="h")
    assert fixed["storage_tiers"] == ["warm", "cold"] and fixed["role"] == "edge"
    assert phone["storage_tiers"] == ["cache", "warm"] and phone["role"] == "cache"
    assert "cold" not in phone["storage_tiers"]


def test_payload_failure_domain_from_env(monkeypatch):
    monkeypatch.setenv("AITHER_FAILURE_DOMAIN", "home")
    assert sc.build_storage_peer_payload("n", {})["failure_domain"] == "home"


# --- endpoint resolution ----------------------------------------------------

def test_endpoint_none_when_unconfigured():
    assert sc.resolve_storage_endpoint({}) == ""


def test_endpoint_precedence(monkeypatch):
    assert sc.resolve_storage_endpoint({"strata_url": "https://s/"}) == "https://s"
    monkeypatch.setenv("AITHER_MESH_URL", "https://mesh:1/")
    assert sc.resolve_storage_endpoint({}) == "https://mesh:1/proxy/strata"
    monkeypatch.setenv("AITHER_STRATA_URL", "https://direct")
    assert sc.resolve_storage_endpoint({"strata_url": "https://s"}) == "https://direct"


# --- contribute_after_enroll ------------------------------------------------

def _client(status=200, body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body or {"peer_id": "peer-1"}
    resp.text = "err"
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_after_enroll_registers_a_server(monkeypatch):
    monkeypatch.setenv("AITHER_STRATA_URL", "https://strata")
    client = _client()
    with patch.object(sc, "measure_disk", return_value=sc.DiskProbe("/x", 9 * 10 ** 12,
                                                                    10 ** 12)):
        with patch("httpx.AsyncClient") as ac:
            ac.return_value.__aenter__.return_value = client
            r = await sc.contribute_after_enroll("node-1", "sovereign", "tok", keepalive=False)
    assert r["registered"] is True and r["peer_id"] == "peer-1"
    url = client.post.await_args.args[0]
    body = client.post.await_args.kwargs["json"]
    assert url == "https://strata/strata/mesh/peers/register"
    assert body["node_id"] == "node-1" and body["device_class"] == "server"
    assert body["contributed_bytes"] == int(10 ** 12 * 0.3)
    assert client.post.await_args.kwargs["headers"] == {"Authorization": "Bearer tok"}


@pytest.mark.asyncio
async def test_after_enroll_laptop_makes_no_request(monkeypatch):
    monkeypatch.setenv("AITHER_STRATA_URL", "https://strata")
    with patch("httpx.AsyncClient", side_effect=AssertionError("network")):
        r = await sc.contribute_after_enroll("node-1", "laptop", "tok")
    assert r["registered"] is False and "default off" in r["skipped"]


@pytest.mark.asyncio
async def test_after_enroll_without_endpoint_says_so():
    with patch.object(sc, "measure_disk", return_value=sc.DiskProbe("/x", 10 ** 13, 10 ** 12)):
        r = await sc.contribute_after_enroll("node-1", "sovereign", "tok")
    assert r["registered"] is False and "no storage endpoint" in r["skipped"]


@pytest.mark.asyncio
async def test_after_enroll_reports_refusal(monkeypatch):
    monkeypatch.setenv("AITHER_STRATA_URL", "https://strata")
    with patch.object(sc, "measure_disk", return_value=sc.DiskProbe("/x", 10 ** 13, 10 ** 12)):
        with patch("httpx.AsyncClient") as ac:
            ac.return_value.__aenter__.return_value = _client(status=401)
            r = await sc.contribute_after_enroll("node-1", "sovereign", "tok")
    assert r["registered"] is False and r["http_status"] == 401


@pytest.mark.asyncio
async def test_rich_enroll_reports_storage_and_stays_one_request_for_laptops():
    """Enrollment default (laptop) must not add a request — and must say why."""
    from adk import enrollment

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"tenant_id": "t", "workspace": {}}
    client = MagicMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    client.post = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient", return_value=client), \
            patch("adk.enrollment._save_workspace"), \
            patch("adk.enrollment._persist_device_cert", return_value={"success": False}):
        result = await enrollment.rich_enroll("https://id.example", "tok", "n",
                                              enable_heartbeat=False)
    assert result["enrolled"] is True
    assert client.post.await_count == 1
    assert result["storage"]["registered"] is False
    assert "default off" in result["storage"]["skipped"]
