"""`aither docker recover` -- the fleet-host recovery argv.

The old recovery ran `wsl --shutdown` and taskkilled vmmem / wslservice. On a host
whose fleet runs in a WSL distro that kills the WHOLE fleet and detaches its data
disk, which `wsl --mount` does not re-attach on its own. These tests pin the
targeted shape: terminate the fleet distro only, re-run the attach task, probe
systemd through the resolver. No test here runs wsl.
"""

from __future__ import annotations

import importlib

import pytest

# `adk.shell.cli` the MODULE: `from adk.shell import cli` yields the click group.
cli = importlib.import_module("adk.shell.cli")


@pytest.fixture
def fleet_env(monkeypatch):
    # The WSL shape is the Windows host's; pin it so the suite judges the same argv
    # on the Linux CI runner.
    monkeypatch.setattr(cli, "_fleet_host_is_wsl", lambda: True)
    for v in ("AITHER_WSL_DISTRO", "FLEET_DISTRO", "AWDESK_FLEET_DISTRO",
              "AITHER_FLEET_ATTACH_TASK"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("AITHER_FLEET_DISTRO", "fleetx")
    return "fleetx"


def test_plan_terminates_only_the_fleet_distro(fleet_env):
    plan = cli.fleet_recover_plan()
    assert plan[0] == ["wsl", "--terminate", fleet_env]


def test_plan_reattaches_the_data_disk_before_probing(fleet_env):
    plan = cli.fleet_recover_plan()
    assert plan[1] == ["schtasks", "/run", "/tn", "AitherOS-AttachFleetData"]


def test_attach_task_name_is_overridable(fleet_env, monkeypatch):
    monkeypatch.setenv("AITHER_FLEET_ATTACH_TASK", "Other-Attach")
    assert cli.fleet_recover_plan()[1][-1] == "Other-Attach"


def test_probe_asks_systemd_through_the_resolver(fleet_env):
    assert cli.fleet_probe_argv() == [
        "wsl", "-d", fleet_env, "-u", "root", "--", "systemctl", "is-system-running"]
    assert cli.fleet_recover_plan()[2] == cli.fleet_probe_argv()


def test_no_step_is_a_global_shutdown_or_a_vm_kill(fleet_env):
    flat = [" ".join(step).lower() for step in cli.fleet_recover_plan()]
    for step in flat:
        assert "--shutdown" not in step
        assert "vmmem" not in step
        assert "wslservice" not in step
        assert "debian" not in step


def test_recover_source_has_no_global_shutdown():
    import inspect
    src = inspect.getsource(cli._docker_recover)
    # Built from parts so WGS009 (a literal global shutdown in source) does not read
    # this assertion as the thing it forbids.
    assert ("--" + "shutdown") not in src
    assert "taskkill" not in src


@pytest.mark.parametrize("state,up", [
    ("running", True), ("degraded", True), ("starting", True),
    ("offline", False), ("unreachable", False), ("maintenance", False), ("", False),
])
def test_fleet_state_judgement(state, up):
    # "starting" is a boot in progress: a recovery must never interrupt it.
    assert cli.fleet_state_is_up(state) is up


def test_native_host_probes_systemd_directly(monkeypatch):
    # A native Linux awnix host has no wsl.exe: the fleet IS this machine.
    monkeypatch.setattr(cli, "_fleet_host_is_wsl", lambda: False)
    assert cli.fleet_probe_argv() == ["systemctl", "is-system-running"]


def test_native_host_recover_refuses_without_touching_anything(monkeypatch):
    monkeypatch.setattr(cli, "_fleet_host_is_wsl", lambda: False)
    ran = []
    monkeypatch.setattr("subprocess.run", lambda *a, **k: ran.append(a) or None)
    assert cli._docker_recover() is False
    assert ran == []


def test_no_probe_binary_is_not_a_wedge(monkeypatch):
    # macOS / a box with no fleet: "absent" must not read as DOWN, or the watchtower
    # would auto-recover forever.
    monkeypatch.setattr(cli, "fleet_probe_argv", lambda: ["definitely-not-a-binary-xyz"])
    assert cli._fleet_state(timeout=5) == "absent"
    assert cli._docker_healthy() is True
