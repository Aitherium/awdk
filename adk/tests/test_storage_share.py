"""Storage lending is opt-in, bounded by the owner's quota, and pauses on battery/metered."""
from __future__ import annotations

import asyncio
import json

import pytest

from adk import storage_contribution as sc
from adk import storage_share_cli as cli

GIB = 1024 ** 3
IDLE = {"on_battery": False, "metered": False}
BIG = sc.DiskProbe("/pool", 2000 * GIB, 1000 * GIB)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AITHER_CONTRIBUTE_STORAGE", "AITHER_LEND", "AITHER_STORAGE_PATH",
              "AITHER_STORAGE_CONTRIB_SHARE", "AITHER_STORAGE_RESERVE_GB",
              "AITHER_ON_BATTERY", "AITHER_NETWORK_METERED"):
        monkeypatch.delenv(k, raising=False)
    yield
    sc.stop_storage_keepalive()


def _saved(**storage):
    return {"storage": storage}


# --- opt-in ------------------------------------------------------------------

def test_a_server_with_nothing_saved_lends_nothing():
    d = sc.decide_contribution("sovereign", saved={}, env={}, probe=BIG, power=IDLE)
    assert d["contribute"] is False and "opt-in" in d["reason"]


def test_saved_opt_in_lends_and_saved_opt_out_wins_over_the_lend_list():
    on = sc.decide_contribution("phone", saved=_saved(enabled=True), env={}, probe=BIG,
                                power=IDLE)
    assert on["contribute"] is True
    off = {"storage": {"enabled": False}, "lend": ["storage"]}
    assert sc.contribution_enabled("server", saved=off, env={})[0] is False


def test_the_lend_list_still_opts_in():
    assert sc.contribution_enabled("server", saved={"lend": ["storage"]}, env={})[0] is True


def test_explicit_env_opt_in_survives_the_flip():
    """Nodes that opted in explicitly before 2026-10-07 keep lending."""
    assert sc.contribution_enabled("server", env={"AITHER_CONTRIBUTE_STORAGE": "1"},
                                   saved={})[0] is True


# --- quota and path -------------------------------------------------------------

def test_quota_caps_the_share():
    d = sc.decide_contribution("desktop", saved=_saved(enabled=True, quota_gb=50), env={},
                               probe=BIG, power=IDLE)
    assert d["contributed_bytes"] == 50 * GIB and d["quota_bytes"] == 50 * GIB


def test_a_bad_quota_or_a_relative_path_is_ignored():
    s = sc.load_settings(_saved(enabled=True, quota_gb="lots", path="rel/dir"))
    assert s.quota_gb is None and s.path == ""
    assert sc.load_settings(_saved(quota_gb=-3)).quota_gb is None


def test_saved_path_is_where_the_disk_is_measured(tmp_path, monkeypatch):
    seen = {}

    def fake_measure(path=None, settings=None):
        seen["path"] = str(sc._default_path(settings)) if path is None else path
        return BIG

    monkeypatch.setattr(sc, "measure_disk", fake_measure)
    sc.decide_contribution("desktop", saved=_saved(enabled=True, path=str(tmp_path)),
                           env={}, power=IDLE)
    assert seen["path"] == str(tmp_path)


# --- pause ---------------------------------------------------------------------

@pytest.mark.parametrize("power,why", [
    ({"on_battery": True, "metered": False}, "on battery"),
    ({"on_battery": False, "metered": True}, "metered network"),
])
def test_paused_without_measuring(power, why, monkeypatch):
    monkeypatch.setattr(sc, "measure_disk", lambda *a, **k: pytest.fail("measured"))
    d = sc.decide_contribution("laptop", saved=_saved(enabled=True), env={}, power=power)
    assert d["contribute"] is False and d["paused"] == why


def test_the_owner_can_allow_battery_and_metered():
    d = sc.decide_contribution(
        "laptop", saved=_saved(enabled=True, pause_on_battery=False, pause_on_metered=False),
        env={}, probe=BIG, power={"on_battery": True, "metered": True})
    assert d["contribute"] is True and d["paused"] == ""


def test_power_state_env_overrides():
    assert sc.power_state({"AITHER_ON_BATTERY": "1", "AITHER_NETWORK_METERED": "1"}) == {
        "on_battery": True, "metered": True}
    assert sc.power_state({"AITHER_ON_BATTERY": "0", "AITHER_NETWORK_METERED": "0"}) == {
        "on_battery": False, "metered": False}


def test_keepalive_skips_a_paused_round_and_stops_when_turned_off(monkeypatch):
    rounds = iter([
        {"paused": "on battery", "contribute": False, "reason": "paused: on battery"},
        {"paused": "", "contribute": False, "reason": "opted out (adk storage off)"},
    ])
    monkeypatch.setattr(sc, "decide_contribution", lambda *a, **k: next(rounds))

    async def boom(*a, **k):
        raise AssertionError("registered while paused or off")

    monkeypatch.setattr(sc, "register_storage_peer", boom)
    asyncio.run(asyncio.wait_for(
        sc._keepalive_loop("https://s", "t", "n", "laptop", None, 0), timeout=2))


# --- the heartbeat's view (B2) ---------------------------------------------------

def test_capability_view_names_quota_and_pause_in_at_most_six_scalars(tmp_path):
    (tmp_path / "blob").write_bytes(b"x" * 4096)
    view = sc.storage_capability("desktop", saved=_saved(enabled=True, quota_gb=10,
                                                          path=str(tmp_path)),
                                 env={}, power=IDLE,
                                 probe=sc.DiskProbe(str(tmp_path), 500 * GIB, 400 * GIB))
    assert view["opted_in"] and view["serving"]
    d = view["detail"]
    assert d["quota_gib"] == 10 and d["contributed_gib"] == 10 and d["paused"] == ""
    assert len(d) <= 6 and all(not isinstance(v, (dict, list)) for v in d.values())
    paused = sc.storage_capability("phone", saved=_saved(enabled=True), env={},
                                   power={"on_battery": True, "metered": False})
    assert paused["opted_in"] and not paused["serving"]
    assert paused["detail"]["paused"] == "on battery"


def test_advertise_lends_storage_on_saved_opt_in_and_withdraws_it_when_paused(monkeypatch):
    from adk import node_capabilities as nc

    facts = nc.HostFacts(cpu_cores=8, ram_gib=16, disk_free_gib=500)
    monkeypatch.setattr(sc, "measure_disk", lambda *a, **k: BIG)
    monkeypatch.setattr(sc, "power_state", lambda env=None: dict(IDLE))
    out = nc.advertise(facts, saved=_saved(enabled=True, quota_gb=20), env={})
    assert "storage" in out["capabilities"]
    assert out["capability_detail"]["storage"]["detail"]["quota_gib"] == 20
    monkeypatch.setattr(sc, "power_state", lambda env=None: {"on_battery": True,
                                                            "metered": False})
    out = nc.advertise(facts, saved=_saved(enabled=True, quota_gb=20), env={})
    assert "storage" not in out["capabilities"]
    assert "paused" in out["capability_detail"]["storage"]["reason"]
    none = nc.advertise(facts, saved={}, env={})
    assert "storage" not in none["capabilities"]


# --- the CLI -------------------------------------------------------------------

@pytest.fixture
def store(monkeypatch):
    data: dict = {}
    import adk.config as cfg

    def load(config_path=None):
        return json.loads(json.dumps(data))

    def save(update, config_path=None):
        data.update(update)
        return "mem"

    monkeypatch.setattr(cfg, "load_saved_config", load)
    monkeypatch.setattr(cfg, "save_saved_config", save)
    return data


def test_cli_on_requires_a_quota_and_saves_it(store, capsys):
    with pytest.raises(SystemExit):
        cli.main(["on"])
    assert cli.main(["on", "--quota", "30", "--allow-metered"]) == 0
    assert store["storage"] == {"enabled": True, "quota_gb": 30.0, "pause_on_battery": True,
                                "pause_on_metered": False}
    assert "30 GiB" in capsys.readouterr().out


def test_cli_rejects_a_relative_path_and_zero_quota(store):
    assert cli.main(["on", "--quota", "0"]) == 2
    assert cli.main(["on", "--quota", "5", "--path", "relative"]) == 2
    assert "storage" not in store


def test_cli_off_keeps_the_quota_and_status_says_why(store, capsys):
    cli.main(["on", "--quota", "12"])
    assert cli.main(["off"]) == 0
    assert store["storage"]["enabled"] is False and store["storage"]["quota_gb"] == 12.0
    capsys.readouterr()
    assert cli.main(["status", "--json"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["opted_in"] is False and "adk storage off" in view["reason"]


def test_adk_storage_share_is_routed_before_the_awstorage_pass_through(store):
    from adk import storage_cmd
    assert storage_cmd.main(["share", "on", "--quota", "8"]) == 0
    assert store["storage"]["quota_gb"] == 8.0
