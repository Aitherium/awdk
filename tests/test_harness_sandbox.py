"""Harnesses that run inside an awnix agent sandbox (agent_sandbox.py): Hades always,
OpenClaw when its sandbox is up. Hermes resolves from its installer's path. No WSL,
no podman, no network: sandbox_up and the filesystem are stubbed."""

from __future__ import annotations

import pytest
from adk.harnesses import registry
from adk.harnesses.registry import LaunchSpec, Transport


@pytest.fixture
def up(monkeypatch):
    state = {"up": True}
    monkeypatch.setattr(registry, "sandbox_up", lambda name, ttl=30.0: state["up"])
    monkeypatch.setattr(registry, "_sandbox_prefix",
                        lambda: ["wsl", "-d", "awnix", "-u", "root", "podman"])
    for var in ("AITHER_HARNESS_OPENCLAW_BIN", "AITHER_HARNESS_HADES_BIN",
                "AITHER_HARNESS_HADES_POLICY", "AITHER_HARNESS_HADES_SESSION",
                "AITHER_HARNESS_OPENCLAW_GATEWAY", "AITHER_HARNESS_OPENCLAW_SESSION"):
        monkeypatch.delenv(var, raising=False)
    return state


def test_hades_is_a_caged_one_shot_turn_in_its_sandbox(up):
    spec = registry.get("hades")
    assert spec.transport == Transport.ONESHOT_PER_TURN and spec.sandbox == "hades"
    assert spec.argv(LaunchSpec(prompt="fix it", resume_session_id="s1")) == [
        "wsl", "-d", "awnix", "-u", "root", "podman", "exec", "-i", "-w", "/work",
        "sbx-hades-hades", "hades", "turn", "--session", "s1", "fix it",
        "--model", "bonsai-27b", "--root", "/work", "--policy", "guarded", "--cage", "on"]


def test_hades_is_not_installed_when_its_sandbox_is_down(up, monkeypatch):
    monkeypatch.setattr(registry.shutil, "which", lambda b: "C:/Windows/System32/wsl.exe")
    assert registry.resolve_binary(registry.get("hades")) == "C:/Windows/System32/wsl.exe"
    up["up"] = False
    assert registry.resolve_binary(registry.get("hades")) is None


def test_openclaw_prefers_its_sandbox_and_drops_the_host_model(up):
    argv = registry.get("openclaw").argv(LaunchSpec(prompt="hi", model="local/qwen"))
    assert argv[:11] == ["wsl", "-d", "awnix", "-u", "root", "podman", "exec", "-i", "-w",
                         "/work", "sbx-openclaw-openclaw"]
    assert argv[11:] == ["openclaw", "agent", "--local", "--session-id", "aither-harness",
                         "--message", "hi"]


def test_openclaw_falls_back_to_the_host_binary_when_the_sandbox_is_down(up):
    up["up"] = False
    assert registry.get("openclaw").argv(LaunchSpec(prompt="hi"))[0] == "openclaw"


def test_bin_override_beats_the_sandbox_in_argv_and_resolution(up, monkeypatch, tmp_path):
    wrapper = tmp_path / "openclaw-node22.cmd"
    wrapper.write_text("@echo off\n")
    monkeypatch.setenv("AITHER_HARNESS_OPENCLAW_BIN", str(wrapper))
    spec = registry.get("openclaw")
    assert registry.resolve_binary(spec) == str(wrapper)
    assert spec.argv(LaunchSpec(prompt="hi"))[0] == "openclaw"


def test_hermes_resolves_from_its_install_dir_when_not_on_path(monkeypatch, tmp_path):
    exe = tmp_path / "hermes.exe"
    exe.write_text("")
    spec = registry.get("hermes")
    monkeypatch.delenv("AITHER_HARNESS_HERMES_BIN", raising=False)
    monkeypatch.setattr(registry.shutil, "which", lambda b: None)
    monkeypatch.setattr(spec, "fallback_paths", [str(tmp_path / "missing.exe"), str(exe)])
    assert registry.resolve_binary(spec) == str(exe)


def test_sandbox_up_caches_and_reads_running_state(monkeypatch):
    calls = []

    class Done:
        returncode = 0
        stdout = b"true\n"

    def fake_run(argv, **kw):
        calls.append(argv)
        return Done()

    registry._SANDBOX_SEEN.clear()
    monkeypatch.setattr(registry.shutil, "which", lambda b: "/usr/bin/" + b)
    monkeypatch.setattr(registry.subprocess, "run", fake_run)
    assert registry.sandbox_up("hades") and registry.sandbox_up("hades")
    assert len(calls) == 1 and calls[0][-1] == "sbx-hades-hades"
    registry._SANDBOX_SEEN.clear()
