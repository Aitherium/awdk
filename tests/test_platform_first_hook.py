"""The plugin's platform-first gate: one delegation, no rules copy, fail-open.

``hooks/platform_first.py`` is the awsh plugin's PreToolUse(Bash|PowerShell) hook for
the owner's "never hand-roll anything" rule. It must (a) hand the host's payload to
``awsettings-guard`` untouched and pass the verdict back, (b) be a silent exit-0 no-op
when awsettings is not installed, and (c) carry no rule of its own -- the rules live in
awsettings, and a second copy here is exactly the drift the one-table design forbids.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

MOD = Path(__file__).resolve().parent.parent / "adk" / "harnesses" / "claude_mod"
HOOK = MOD / "hooks" / "platform_first.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------- the contract
def test_self_test_passes():
    assert _load(HOOK, "platform_first").main(["--self-test"]) == 0


def test_a_machine_without_awsettings_allows_silently(tmp_path):
    """The real process, PATH scrubbed: nothing on stdout, nothing on stderr, exit 0."""
    import os

    env = {**os.environ, "PATH": str(tmp_path)}
    r = subprocess.run([sys.executable, "-S", str(HOOK)], input=b'{"tool_name":"Bash"}',
                       capture_output=True, env=env, timeout=30)
    assert r.returncode == 0
    assert r.stdout == b"" and r.stderr == b""


def test_delegation_passes_payload_and_verdict_through_untouched():
    hook = _load(HOOK, "platform_first")
    payload = b'{"tool_name":"PowerShell","tool_input":{"command":"Start-Process x"}}'
    deny = (b'{"hookSpecificOutput":{"permissionDecision":"deny",'
            b'"permissionDecisionReason":"PF-001 use awrun"}}')
    seen = []

    class Done:
        returncode, stdout, stderr = 2, deny, b"denied: PF-001"

    def runner(cmd, **kw):
        seen.append((cmd, kw))
        return Done()

    out, err = io.BytesIO(), io.BytesIO()
    code = hook.main([], stdin=io.BytesIO(payload), stdout=out, stderr=err,
                     which=lambda _n: "/opt/bin/awsettings-guard", runner=runner)
    assert code == 2
    assert out.getvalue() == deny and err.getvalue() == b"denied: PF-001"
    (cmd, kw), = seen
    assert cmd == ["/opt/bin/awsettings-guard"]
    assert kw["input"] == payload and kw["capture_output"] is True


def test_a_guard_that_cannot_start_is_a_silent_allow():
    hook = _load(HOOK, "platform_first")

    def explode(cmd, **kw):
        raise OSError("guard vanished between which() and spawn")

    out, err = io.BytesIO(), io.BytesIO()
    code = hook.main([], stdin=io.BytesIO(b"not json at all"), stdout=out, stderr=err,
                     which=lambda _n: "/opt/bin/awsettings-guard", runner=explode)
    assert (code, out.getvalue(), err.getvalue()) == (0, b"", b"")


def test_unreadable_stdin_never_fails_the_tool_call():
    hook = _load(HOOK, "platform_first")

    class Broken:
        def read(self):
            raise OSError("closed pipe")

    assert hook.main([], stdin=Broken(), stdout=io.BytesIO(), stderr=io.BytesIO(),
                     which=lambda _n: "/x", runner=lambda *a, **k: None) == 0


def test_it_resolves_the_console_script_by_name_not_a_path():
    hook = _load(HOOK, "platform_first")
    assert hook.GUARD == "awsettings-guard"
    asked = []
    assert hook.resolve(lambda name: asked.append(name) or "/bin/awsettings-guard")
    assert asked == ["awsettings-guard"]


def test_it_ships_no_rules_of_its_own():
    """A rules copy here would drift from awsettings' table; the gate asserts parity."""
    source = HOOK.read_text(encoding="utf-8")
    for shape in ("PF-0", "Start-Process", "ffmpeg", "nsenter", "gh release",
                  "AW_PLATFORM_FIRST_OK"):
        assert shape not in source, f"the delegating hook grew a rule: {shape}"


# ------------------------------------------------------------------ the wiring
def test_plugin_registers_the_gate_after_awgit_ensure():
    pre = _json(MOD / "hooks" / "portable.json")["hooks"]["PreToolUse"]
    assert len(pre) == 1 and "Bash" in pre[0]["matcher"] and "PowerShell" in pre[0]["matcher"]
    entries = pre[0]["hooks"]
    assert len(entries) == 2, "the PreToolUse group holds exactly the two documented hooks"
    assert entries[0]["args"][-1] == "pre-bash"                      # awgit_ensure first
    gate = entries[1]
    assert gate["command"] == "python"
    assert gate["args"] == ["-S", "${CLAUDE_PLUGIN_ROOT}/hooks/platform_first.py"]
    assert gate["timeout"] == 10
    assert (MOD / "hooks" / "platform_first.py").is_file()


def test_plugin_json_makes_portable_json_the_hooks_file():
    plugin = _json(MOD / ".claude-plugin" / "plugin.json")
    assert plugin["hooks"] == "./hooks/portable.json"


def test_hooks_json_stays_the_function_module_manifest():
    """hooks.json is the awsh harness's module manifest, NOT a hook config.

    Adding a PreToolUse group here would be silently ignored by Claude Code and would
    break the harness loader (which reads ``modules``). The gate belongs in the file
    plugin.json names -- portable.json -- and this pins the split.
    """
    data = _json(MOD / "hooks" / "hooks.json")
    assert data["modules"] == ["./register.ts"]
    assert "PreToolUse" not in data and "hooks" not in data


def test_plugin_and_marketplaces_agree_on_the_bumped_version():
    plugin = _json(MOD / ".claude-plugin" / "plugin.json")
    nested = _json(MOD / ".claude-plugin" / "marketplace.json")
    root = _json(MOD.parent.parent.parent / ".claude-plugin" / "marketplace.json")
    entry = next(p for p in root["plugins"] if p["name"] == "awsh")
    assert plugin["version"] == nested["plugins"][0]["version"] == entry["version"]
    assert plugin["version"] != "0.2.2", "a cache-lagging version never refreshes"
