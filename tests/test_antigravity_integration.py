"""Tests for Google Antigravity 2-way integration in awdk.

Covers:
  * adk.harnesses.antigravity_hooks (payload/response translation, steer mailbox drain,
    live session recording, trace append, Stop continuationActive anti-loop)
  * adk.antigravity_setup (setup_antigravity, doctor_antigravity AGD001-AGD004, self_test)
  * adk.harnesses.registry + adapters + discovery (antigravity harness + session discovery)
  * adk.agent_binding_client (Google managed agent BYOK key request)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from adk import agent_binding_client as abc
from adk.antigravity_setup import (
    DEFAULT_PLUGIN_NAME,
    doctor_antigravity,
    self_test,
    setup_antigravity,
)
from adk.harnesses import adapters, discovery, registry
from adk.harnesses import antigravity_hooks as agh


def test_tool_and_event_translation():
    ag_payload = {
        "conversationId": "conv-123",
        "hookEventName": "PreToolUse",
        "cwd": "/tmp/work",
        "transcriptPath": "/tmp/transcript.jsonl",
        "toolCall": {
            "id": "tc-1",
            "name": "run_command",
            "args": {"CommandLine": "git status"},
        },
    }
    cl = agh.to_claude_payload(ag_payload, "PreToolUse")
    assert cl["hook_event_name"] == "PreToolUse"
    assert cl["session_id"] == "conv-123"
    assert cl["tool_name"] == "Bash"
    assert cl["tool_input"]["command"] == "git status"
    assert cl["source_harness"] == "antigravity"

    edit_name, edit_in = agh.translate_tool_input(
        "edit_file", {"Path": "/a/b.py", "Target": "x", "Replacement": "y"}
    )
    assert edit_name == "Edit"
    assert edit_in["file_path"] == "/a/b.py"
    assert edit_in["old_string"] == "x"
    assert edit_in["new_string"] == "y"


def test_response_translation_and_stop_antiloop():
    deny = agh.from_claude_response(
        "PreToolUse",
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "awgov blocked",
            }
        },
    )
    assert deny == {"decision": "deny", "reason": "awgov blocked"}

    inject = agh.from_claude_response(
        "PreInvocation",
        {"hookSpecificOutput": {"additionalContext": "recalled fact"}},
        extra_context=["owner steer"],
    )
    assert "injectSteps" in inject
    msg = inject["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "recalled fact" in msg and "owner steer" in msg

    stop_cont = agh.from_claude_response(
        "Stop", {"decision": "block", "reason": "open gate"}, continuation_active=False
    )
    assert stop_cont == {"decision": "continue", "reason": "open gate"}

    stop_no_loop = agh.from_claude_response(
        "Stop", {"decision": "block", "reason": "open gate"}, continuation_active=True
    )
    assert stop_no_loop == {}


def test_steer_drain_and_discovery_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setenv("AWSETTINGS_HOOKS_DISABLED", "1")
    monkeypatch.setenv("AWRELAY_HOOKS_DISABLED", "1")
    monkeypatch.setenv("AITHER_HOOKD_URL", "http://127.0.0.1:1")

    cid = "ag-conv-42"
    box = tmp_path / "steer" / cid
    box.mkdir(parents=True)
    (box / "001-owner.md").write_text("Focus on tests first.", encoding="utf-8")

    out = agh.handle_event(
        "PreInvocation",
        {
            "conversationId": cid,
            "hookEventName": "PreInvocation",
            "cwd": str(tmp_path),
            "lastUserInput": "Run checks",
            "modelName": "gemini-pro",
        },
    )
    assert "injectSteps" in out
    assert "Focus on tests first." in out["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert (box / "delivered" / "001-owner.md").is_file()

    # Discovery should immediately pick up the live Antigravity session.
    found = discovery.discover_antigravity_sessions(tmp_path / "antigravity_sessions")
    assert len(found) == 1
    assert found[0].id == cid
    assert found[0].harness == "antigravity"
    assert found[0].harness_label == "Antigravity"

    # PostInvocation writes a trace row.
    agh.handle_event("PostInvocation", {"conversationId": cid, "cwd": str(tmp_path), "invocationNum": 1})
    traces = list((tmp_path / "agent-trace").glob("antigravity-*.jsonl"))
    assert len(traces) == 1
    row = json.loads(traces[0].read_text(encoding="utf-8").strip())
    assert row["harness"] == "antigravity"
    assert row["conversation_id"] == cid


def test_antigravity_harness_registry_and_adapter():
    spec = registry.get("antigravity")
    assert spec.adapter == "antigravity"
    assert spec.supports_resume is True

    argv_new = spec.argv(registry.LaunchSpec(prompt="hello", model="pro", title="T"))
    assert argv_new == ["agentapi", "new-conversation", "--model=pro", "--title=T", "hello"]

    argv_resume = spec.argv(registry.LaunchSpec(prompt="follow up", resume_session_id="conv-9"))
    assert argv_resume == ["agentapi", "send-message", "conv-9", "follow up"]

    evs = adapters.ADAPTERS["antigravity"]({"conversation": {"id": "conv-99"}})
    assert evs and evs[0].data["harness_session_id"] == "conv-99"


def test_setup_and_doctor_selftest(tmp_path: Path):
    assert self_test() == 0
    res = setup_antigravity(tmp_path / "cfg", repo_root=tmp_path / "repo", include_bundled_skills=False)
    assert res["ok"] is True
    assert (tmp_path / "cfg" / "plugins" / DEFAULT_PLUGIN_NAME / "hooks.json").is_file()
    assert (tmp_path / "repo" / ".agents" / "hooks.json").is_file()
    rep = doctor_antigravity(tmp_path / "cfg", run_live=False)
    assert rep.exit_code == 0


def test_google_managed_byok_request():
    args = argparse.Namespace(
        agent_command="managed",
        managed_command="byok",
        provider="google",
        from_env="",
        environment_id="env-g1",
    )
    method, path, body, params = abc.build_request(args, prompt=lambda _: "AIza-test-key")
    assert method == "POST"
    assert path == "/managed/byok"
    assert body == {"provider": "google", "google_api_key": "AIza-test-key", "environment_id": "env-g1"}
    assert params is None


def test_antigravity_toolpack_and_bundled_mod(tmp_path: Path, monkeypatch):
    from adk.toolpacks import antigravity as ag_pack
    from adk.toolpacks.antigravity import tools as ag_tools

    monkeypatch.setenv("AITHER_HOME", str(tmp_path))

    class _FakeReg:
        def __init__(self):
            self.fns = {}

        def register(self, fn):
            self.fns[fn.__name__] = fn

    reg = _FakeReg()
    count = ag_pack.register(reg)
    assert count == 5
    assert set(reg.fns) == {
        "antigravity_sessions",
        "antigravity_spawn",
        "antigravity_steer",
        "antigravity_doctor",
        "antigravity_setup_status",
    }

    st = ag_tools.antigravity_steer("conv-pack-1", "Check tests", mode="mailbox")
    assert st["ok"] is True
    assert Path(st["path"]).is_file()

    status = ag_tools.antigravity_setup_status(str(tmp_path / "cfg"))
    assert status["ok"] is True
    assert status["bundled_mod_present"] is True

    # Validate bundled antigravity_mod JSON manifests parse cleanly.
    mod_dir = Path(status["bundled_mod_dir"])
    for fname in ("plugin.json", "mcp_config.json", "hooks.json"):
        doc = json.loads((mod_dir / fname).read_text(encoding="utf-8"))
        assert isinstance(doc, dict)

