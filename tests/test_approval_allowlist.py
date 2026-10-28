"""The durable allow list stops repeat cards -- but never overrides a runtime gate."""

import pytest

from adk import approval


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_TOOL_ALLOWLIST_FILE", str(tmp_path / "allow.json"))
    monkeypatch.setenv("AITHER_ADK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AITHER_TOOL_APPROVAL", raising=False)
    approval.set_runtime_gates("t", ())
    yield
    approval.set_runtime_gates("t", ())


def test_policy_gated_tool_asks_until_allowed(monkeypatch):
    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "browser_open")
    assert approval.needs_approval("t", "browser_open") is True
    approval.allow_tool("browser_open")
    assert approval.needs_approval("t", "browser_open") is False


def test_allow_is_per_agent_or_global():
    approval.allow_tool("relay_send", agent_name="hearth")
    assert approval.needs_approval("hearth", "relay_send") is False
    assert approval.needs_approval("other", "relay_send") is True


def test_runtime_gate_still_wins_over_an_allow():
    approval.allow_tool("web_fetch")
    approval.set_runtime_gates("t", ["web_fetch"])
    assert approval.needs_approval("t", "web_fetch") is True


def test_forget_restores_the_card():
    approval.allow_tool("send_email")
    assert approval.forget_tool("send_email") is True
    assert approval.needs_approval("t", "send_email") is True
    assert approval.forget_tool("send_email") is False


def test_cli_round_trip(capsys, monkeypatch):
    from adk import approvals_cmd
    assert approvals_cmd.main(["allow", "web_search", "--note", "daily"]) == 0
    capsys.readouterr()
    assert approvals_cmd.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "web_search" in out and "daily" in out
    assert approvals_cmd.main(["forget", "web_search"]) == 0
    assert approval.list_allow() == {}
