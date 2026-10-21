"""Hermes and OpenClaw are drivable harnesses (owner 2026-10-21: one terminal drives
Claude, Hermes and OpenClaw). No network, no real binaries."""

from __future__ import annotations

import pytest
from adk.harnesses import registry
from adk.harnesses.registry import LaunchSpec, Transport


@pytest.mark.parametrize("hid", ["hermes", "openclaw"])
def test_registered_as_one_shot_harnesses_that_carry_context(hid):
    spec = registry.get(hid)
    assert spec.transport == Transport.ONESHOT_PER_TURN
    assert spec.prompt_carries_context
    assert spec.install_hint


def test_openclaw_argv_runs_one_local_agent_turn(monkeypatch):
    monkeypatch.delenv("AITHER_HARNESS_OPENCLAW_GATEWAY", raising=False)
    monkeypatch.delenv("AITHER_HARNESS_OPENCLAW_SESSION", raising=False)
    spec = registry.get("openclaw")
    # `agent` refuses to run without a session selector: there is always one.
    assert spec.argv(LaunchSpec(prompt="hi")) == [
        "openclaw", "agent", "--local", "--session-id", "aither-harness", "--message", "hi"]
    assert spec.argv(LaunchSpec(prompt="hi", model="local/qwen", resume_session_id="s1")) == [
        "openclaw", "agent", "--local", "--model", "local/qwen", "--session-id", "s1",
        "--message", "hi"]


def test_openclaw_gateway_mode_drops_local(monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_OPENCLAW_GATEWAY", "1")
    monkeypatch.setenv("AITHER_HARNESS_OPENCLAW_SESSION", "desk")
    assert registry.get("openclaw").argv(LaunchSpec(prompt="hi")) == [
        "openclaw", "agent", "--session-id", "desk", "--message", "hi"]


def test_hermes_argv_is_a_single_query_never_the_tui():
    spec = registry.get("hermes")
    assert spec.argv(LaunchSpec(prompt="hi")) == ["hermes", "chat", "-q", "hi"]
    assert spec.argv(LaunchSpec(prompt="hi", model="m")) == [
        "hermes", "chat", "--model", "m", "-q", "hi"]


def test_detect_reports_missing_with_the_install_hint(monkeypatch):
    for hid in ("hermes", "openclaw"):
        monkeypatch.delenv(f"AITHER_HARNESS_{hid.upper()}_BIN", raising=False)
    monkeypatch.setattr(registry.shutil, "which", lambda name: None)
    rows = {r["id"]: r for r in registry.detect()}
    for hid in ("hermes", "openclaw"):
        assert rows[hid]["installed"] is False
        assert rows[hid]["install_hint"] == registry.get(hid).install_hint


def test_detect_finds_an_installed_binary(monkeypatch, tmp_path):
    exe = tmp_path / "openclaw-wrapper"
    exe.write_text("")
    monkeypatch.setenv("AITHER_HARNESS_OPENCLAW_BIN", str(exe))
    row = next(r for r in registry.detect() if r["id"] == "openclaw")
    assert row["installed"] is True and row["path"] == str(exe)
