"""restart-lane: a host restarts only units on ITS OWN list, via systemctl, no shell."""

from __future__ import annotations

import subprocess
import time

import pytest
from adk import node_commands as nc
from adk import restartable_units as ru

KEY = "ab" * 32
NODE = "adk-host-1"


@pytest.fixture
def units(tmp_path, monkeypatch):
    f = tmp_path / "restartable-units"
    f.write_text("# lanes this host lets its owner restart\n"
                 "aither-llamacpp-bonsai.service\n"
                 "aither-comfyui.service  # on-demand\n"
                 "BAD UNIT; rm -rf /\n"
                 "../etc/passwd\n"
                 "aither-comfyui.service\n", encoding="utf-8")
    monkeypatch.setenv("AITHER_RESTARTABLE_UNITS_FILE", str(f))
    return f


def _cmd(args, verb="restart-lane", cid="cmd_1"):
    c = {"id": cid, "tenant_id": "t", "node_id": NODE, "verb": verb, "args": args,
         "issued_by": "operator:aither-operator:u", "issued_at": int(time.time()),
         "expires_at": int(time.time()) + 600}
    c["sig"] = nc.sign(KEY, nc.canonical(c))
    return c


def test_only_valid_names_deduplicated(units):
    assert ru.restartable_units() == ["aither-llamacpp-bonsai.service", "aither-comfyui.service"]


def test_list_is_capped(tmp_path, monkeypatch):
    f = tmp_path / "u"
    f.write_text("\n".join(f"lane-{i}.service" for i in range(50)), encoding="utf-8")
    monkeypatch.setenv("AITHER_RESTARTABLE_UNITS_FILE", str(f))
    assert len(ru.restartable_units()) == ru.MAX_UNITS


def test_no_file_means_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_RESTARTABLE_UNITS_FILE", str(tmp_path / "missing"))
    assert ru.restartable_units() == []


def test_verify_accepts_only_a_listed_unit(units):
    assert nc.verify(_cmd({"unit": "aither-comfyui.service"}), KEY, NODE, seen=[]) == ""
    assert "not on this host" in nc.verify(_cmd({"unit": "aither-genesis.service"}), KEY, NODE,
                                           seen=[])
    assert nc.verify(_cmd({}), KEY, NODE, seen=[])
    assert nc.verify(_cmd({"unit": "aither-comfyui.service", "x": "1"}), KEY, NODE, seen=[])


def test_the_runner_execs_systemctl_restart_for_a_listed_unit_only(units, monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw.get("shell")))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    out = nc._restart_lane({"unit": "aither-comfyui.service"})
    assert out["ok"] is True
    assert calls == [(["systemctl", "restart", "aither-comfyui.service"], None)]
    assert nc._restart_lane({"unit": "aither-genesis.service"})["ok"] is False
    assert len(calls) == 1


def test_run_commands_reports_a_signed_result(units, monkeypatch, tmp_path):
    monkeypatch.setattr(nc, "_load_seen", lambda: [])
    monkeypatch.setattr(nc, "_remember_seen", lambda cid: None)
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    [res] = nc.run_commands([_cmd({"unit": "aither-comfyui.service"})], NODE, KEY)
    assert res["ok"] is True and res["sig"]
