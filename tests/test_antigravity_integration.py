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
    mod_hooks = json.loads((mod_dir / "hooks.json").read_text(encoding="utf-8"))["awdk-bridge"]
    assert mod_hooks["PreToolUse"][0]["matcher"] != "*"
    assert "call_mcp_tool" in mod_hooks["PreToolUse"][0]["matcher"]
    assert mod_hooks["PostToolUse"][0]["matcher"] != "*"


def test_mcp_tool_translation_unwraps_routed_aither_call():
    name, inp = agh.translate_tool_input(
        "call_mcp_tool",
        {
            "ServerName": "aitheros_aitheros",
            "ToolName": "aither_call",
            "Arguments": {"tool": "set_secret", "arguments": {"name": "FOO"}},
        },
    )
    assert name == "mcp__aitheros__set_secret"
    assert inp == {"name": "FOO"}

    direct_name, direct_inp = agh.translate_tool_input(
        "call_mcp_tool",
        {"ServerName": "aitheros_aither-local", "ToolName": "research", "Arguments": {"q": "x"}},
    )
    assert direct_name == "mcp__aither-local__research"
    assert direct_inp == {"q": "x"}


def test_setup_defaults_to_routed_mcp_and_scoped_hooks_and_doctor_warns_flat(tmp_path: Path):
    from adk.antigravity_setup import build_mcp_config

    bridge = tmp_path / "mcp_stdio_bridge.py"
    bridge.write_text("# stub\n", encoding="utf-8")

    # Default setup enables --routed and scoped hook matchers -> no AGD007 warnings.
    cfg = tmp_path / "cfg"
    res = setup_antigravity(cfg, mcp_bridge_script=str(bridge), include_bundled_skills=False)
    assert res["ok"] is True
    pdir = cfg / "plugins" / DEFAULT_PLUGIN_NAME
    mcp_doc = json.loads((pdir / "mcp_config.json").read_text(encoding="utf-8"))
    assert "--routed" in mcp_doc["mcpServers"]["aitheros"]["args"]
    hooks_doc = json.loads((pdir / "hooks.json").read_text(encoding="utf-8"))["aitheros-bridge"]
    assert hooks_doc["PreToolUse"][0]["matcher"] == "run_command|edit_file|write_file|call_mcp_tool"
    assert hooks_doc["PostToolUse"][0]["matcher"] == "run_command|edit_file|write_file"

    rep = doctor_antigravity(cfg, run_live=False)
    assert not any(f.code == "AGD007" for f in rep.findings)

    # Flat bridge without --routed trips AGD007 warning.
    flat = build_mcp_config(python_exe="python3", mcp_bridge_script=str(bridge), routed_mcp=False)
    assert "--routed" not in flat["mcpServers"]["aitheros"]["args"]
    (pdir / "mcp_config.json").write_text(json.dumps(flat), encoding="utf-8")
    rep_flat = doctor_antigravity(cfg, run_live=False)
    assert any(f.code == "AGD007" for f in rep_flat.findings)


def test_fast_allow_skips_pretooluse_overhead(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    called = []
    monkeypatch.setattr(agh, "_hookd_post", lambda *a, **kw: called.append(a) or None)
    ans = agh.handle_event(
        "PreToolUse",
        {"conversationId": "c-fast", "toolCall": {"name": "read_file", "args": {"Path": "/tmp/a"}}},
    )
    assert ans == {"decision": "allow"}
    assert called == []


def test_sync_routed_mcp_all_harnesses_and_claude_runner_default(tmp_path: Path, monkeypatch):
    from adk.claude_runner import _default_mcp_config
    from adk.mcp_setup import sync_routed_mcp_all_harnesses

    # Seed every harness config with pre-existing user entries that must be preserved.
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"aitherium": {"type": "http", "url": "https://mcp.aitherium.com/mcp"}}}),
        encoding="utf-8",
    )
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        'model = "gpt-5.3-codex"\n\n[mcp_servers.awdecide]\ncommand = "awdecide"\nargs = ["mcp"]\n',
        encoding="utf-8",
    )
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "settings.json").write_text(
        json.dumps({"ide": {"enabled": True}}),
        encoding="utf-8",
    )
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    (tmp_path / ".config" / "opencode").mkdir(parents=True)
    (tmp_path / ".config" / "opencode" / "opencode.jsonc").write_text(
        '{\n  // comment\n  "model": "aither/bonsai2-27b-5090"\n}\n',
        encoding="utf-8",
    )
    (tmp_path / "AppData" / "Roaming" / "Code" / "User").mkdir(parents=True)
    (tmp_path / "AppData" / "Roaming" / "Code" / "User" / "mcp.json").write_text(
        json.dumps({"servers": {"Roboflow": {"url": "https://example.test/mcp", "type": "http"}}}),
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"aitheros": {"type": "stdio", "command": "python", "args": ["old"]}}}),
        encoding="utf-8",
    )

    # Run twice to assert idempotency.
    for _ in range(2):
        res = sync_routed_mcp_all_harnesses(
            home=tmp_path,
            repo_root=repo,
            python_exe=r"C:\Python312\python.exe",
            bridge_script=r"C:\Repo\AitherOS\dev\tools\mcp_stdio_bridge.py",
            awdelegate_script=r"C:\Repo\tools\claude-backend\awdelegate.py",
            routed=True,
            win_style=True,
        )
    assert res["ok"] is True
    assert set(res["harnesses"].keys()) == {
        "claude-code", "repo", "codex", "gemini", "cursor", "opencode", "vscode",
    }

    claude_doc = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert "--routed" in claude_doc["mcpServers"]["aitheros"]["args"]
    assert "aitherium" in claude_doc["mcpServers"]
    assert "aither-local" in claude_doc["mcpServers"]

    codex_txt = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert codex_txt.count("[mcp_servers.aitheros]") == 1
    assert "[mcp_servers.awdecide]" in codex_txt
    assert '"--routed"' in codex_txt

    gemini_doc = json.loads((tmp_path / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    assert gemini_doc["ide"]["enabled"] is True
    assert "--routed" in gemini_doc["mcpServers"]["aitheros"]["args"]

    cursor_doc = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert "--routed" in cursor_doc["mcpServers"]["aitheros"]["args"]

    oc_doc = json.loads((tmp_path / ".config" / "opencode" / "opencode.jsonc").read_text(encoding="utf-8"))
    assert oc_doc["model"] == "aither/bonsai2-27b-5090"
    assert oc_doc["mcp"]["aitheros"]["type"] == "local"
    assert "--routed" in oc_doc["mcp"]["aitheros"]["command"]

    vscode_doc = json.loads((tmp_path / "AppData" / "Roaming" / "Code" / "User" / "mcp.json").read_text(encoding="utf-8"))
    assert "Roboflow" in vscode_doc["servers"]
    assert "--routed" in vscode_doc["servers"]["aitheros"]["args"]

    # claude_runner._default_mcp_config() includes --routed by default and honors opt-out.
    monkeypatch.delenv("AITHER_CLAUDE_RUNNER_FLAT_MCP", raising=False)
    default_cfg = _default_mcp_config()
    if default_cfg.get("mcpServers"):
        assert "--routed" in default_cfg["mcpServers"]["aitheros"]["args"]
        monkeypatch.setenv("AITHER_CLAUDE_RUNNER_FLAT_MCP", "1")
        assert "--routed" not in _default_mcp_config()["mcpServers"]["aitheros"]["args"]


