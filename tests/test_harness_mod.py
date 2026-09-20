"""The Claude Code mod, from the daemon's side.

The hooks module is TypeScript that only Claude Code can run, so nothing here
executes it. What these tests hold still is every place the two halves must agree
without a compiler to tell them they stopped: the event names, the depth limit, the
env var, the plugin's file layout, and that the daemon never rewrites a settings
file it could not read.
"""
from __future__ import annotations

import json
import re

import pytest
from adk.harnesses import mod
from adk.harnesses.events import EventKind
from adk.harnesses.session import scrub_nested_claude_markers

REGISTER = (mod.mod_dir() / "hooks" / "register.ts").read_text(encoding="utf-8")
AGENT = (mod.mod_dir() / "agents" / "aw.md").read_text(encoding="utf-8")


def test_the_plugin_files_the_installer_points_at_exist():
    assert mod.mod_present()
    hooks = json.loads((mod.mod_dir() / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    for module in hooks["modules"]:
        assert (mod.mod_dir() / "hooks" / module).is_file()


def test_plugin_and_marketplace_agree_on_name_and_version():
    plugin = json.loads(
        (mod.mod_dir() / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads(
        (mod.mod_dir() / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert plugin["name"] == mod.PLUGIN_NAME and market["name"] == mod.MARKETPLACE_NAME
    assert [p["version"] for p in market["plugins"]] == [plugin["version"]]


def test_every_event_kind_the_mod_reads_is_one_the_daemon_emits():
    read = set(re.findall(r"event\.kind === '([a-z_.]+)'", REGISTER))
    assert read, "the pattern found nothing, so this test would pass on any file"
    assert read <= {kind.value for kind in EventKind}
    # The three a turn cannot be followed without.
    assert {"text.delta", "turn.completed", "session.exited"} <= read


def test_every_session_state_the_mod_waits_on_is_a_real_one():
    from adk.harnesses.session import SessionState

    states = {v for k, v in vars(SessionState).items() if k.isupper()}
    working = re.search(r"WORKING_STATES = \[([^\]]+)\]", REGISTER)
    assert working and set(re.findall(r"'([a-z]+)'", working.group(1))) <= states


def test_the_depth_limit_is_one_number_in_two_languages():
    found = re.search(r"^const MAX_DEPTH = (\d+)$", REGISTER, re.M)
    assert found and int(found.group(1)) == mod.MAX_DEPTH
    assert f"'{mod.DEPTH_ENV}'" in REGISTER


def test_a_failed_turn_is_never_signed_as_a_model_s_answer():
    # The parent is told "no signature = not awsh's work". A signed failure
    # ("answered by awsh, codex/unreported") claimed a model answered when none did.
    signing = re.search(r"const report = run\.failed\s*\?\s*answer\s*:\s*`[^`]*answered by awsh",
                        REGISTER)
    assert signing, "the signature is no longer conditional on the run having an answer"
    assert REGISTER.count("answered by awsh, ${") == 1, "a second, unconditional signature"


def test_the_agent_tells_the_user_the_same_env_var_the_installer_sets():
    assert mod.HOOKS_ENV in AGENT
    # The fallback body is what a model sees when the module did NOT load; it must
    # refuse the task, or a Claude answer is presented as another model's.
    assert "Do not carry it out" in AGENT and "your entire reply is exactly" in AGENT


def test_a_claude_session_gets_the_plugin_after_the_scrub_removed_its_switch():
    env = scrub_nested_claude_markers({mod.HOOKS_ENV: "1", "PATH": "x"})
    assert mod.HOOKS_ENV not in env
    args = mod.apply_to_launch("claude", "", env, ["--foo"])
    assert env[mod.HOOKS_ENV] == "1"
    assert args == ["--foo", "--plugin-dir", str(mod.mod_dir())]
    assert mod.apply_to_launch("claude", "", env, args) == args, "added twice"


@pytest.mark.parametrize("harness", ["gemini", "codex", "opencode", "terminal", "aither"])
def test_a_harness_that_is_not_claude_code_is_left_alone(harness):
    env: dict[str, str] = {}
    assert mod.apply_to_launch(harness, "", env, ["-x"]) == ["-x"]
    assert env == {}


def test_the_opt_out_is_honoured(monkeypatch):
    monkeypatch.setenv(mod.OPT_OUT_ENV, "0")
    env: dict[str, str] = {}
    assert mod.apply_to_launch("claude", "", env, []) == []
    assert mod.HOOKS_ENV not in env


def test_depth_rides_the_owner_into_the_child_for_any_harness():
    assert mod.depth_of_owner("claude-code:a1b2@d2") == 2
    assert mod.depth_of_owner("claude-code:a1b2") == 0
    assert mod.depth_of_owner("someone@d9") == 0, "only the mod's own owners count"
    env: dict[str, str] = {}
    mod.apply_to_launch("claude", "claude-code:a1b2@d1", env, [])
    assert env[mod.DEPTH_ENV] == "1"


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(mod, "claude_settings_path", lambda: path)
    calls: list[tuple[str, ...]] = []

    def fake_claude(*args: str):
        calls.append(args)
        return 0, ""

    monkeypatch.setattr(mod, "_claude", fake_claude)
    return path, calls


def test_install_adds_one_env_var_and_keeps_everything_else(settings):
    path, calls = settings
    path.write_text(json.dumps({"model": "x", "env": {"A": "1"}, "hooks": {"Stop": []}}),
                    encoding="utf-8")
    assert mod.install()["ok"]
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after == {"model": "x", "env": {"A": "1", mod.HOOKS_ENV: "1"},
                     "hooks": {"Stop": []}}
    assert ("plugin", "install", "awsh@awsh") in calls


def test_install_refuses_a_settings_file_it_cannot_parse(settings):
    path, calls = settings
    path.write_text("{ not json", encoding="utf-8")
    result = mod.install()
    assert not result["ok"] and "unreadable" in result["error"]
    assert path.read_text(encoding="utf-8") == "{ not json"
    assert not any(call[:2] == ("plugin", "install") for call in calls)


def test_status_is_inactive_until_every_half_is_true(settings):
    path, _ = settings
    path.write_text(json.dumps({"env": {mod.HOOKS_ENV: "1"}}), encoding="utf-8")
    # fake_claude lists no plugins, so the hooks switch alone must not read active.
    assert mod.status()["function_hooks_enabled"] is True
    assert mod.status()["active"] is False
