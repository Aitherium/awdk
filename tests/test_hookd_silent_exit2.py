"""hookd: a SILENT exit 2 outside PreToolUse is a failed hook, not a block.

Measured 2026-10-26 while WSL was down: the folded awsettings push hook exited 2 with no
output (settings hub 503), and hookd merged that into ``{"decision": "block", "reason":
"blocked by hook"}``, which put "PostToolUse hook blocking error" on every tool call in
every session. The fold itself had dropped the hook's ``|| true``; SOFT keeps it.
"""
from __future__ import annotations

import json

from adk.harnesses import hookd


def _r(code, out="", err=""):
    return {"code": code, "out": out, "err": err, "ms": 0}


def test_silent_exit2_does_not_block_post_tool_use_or_stop():
    assert hookd.merge("PostToolUse", [_r(0), _r(2), _r(0)]) == {}
    assert hookd.merge("Stop", [_r(2)]) == {}
    assert hookd.merge("UserPromptSubmit", [_r(2, out="   ")]) == {}


def test_exit2_with_a_reason_still_blocks():
    m = hookd.merge("PostToolUse", [_r(2, err="lint failed")])
    assert m == {"decision": "block", "reason": "lint failed"}


def test_pre_tool_use_stays_fail_closed():
    m = hookd.merge("PreToolUse", [_r(2)])
    assert m["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_silent_exit2_next_to_a_real_answer_keeps_the_answer():
    ctx = json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                             "additionalContext": "steer"}})
    m = hookd.merge("PostToolUse", [_r(2), _r(0, out=ctx)])
    assert "decision" not in m and m["hookSpecificOutput"]["additionalContext"] == "steer"


def test_convert_keeps_the_or_true_promise(tmp_path):
    scripts = {"awsettings": "ep:awsettings.cli:main"}
    soft = hookd.hook_spec({"type": "command",
                            "command": "awsettings --quiet push --debounce || true"},
                           str(tmp_path), scripts)
    assert soft == hookd.SOFT + "ep:awsettings.cli:main|--quiet|push|--debounce"
    plain = hookd.hook_spec({"type": "command", "command": "awsettings --quiet push"},
                            str(tmp_path), scripts)
    assert plain == "ep:awsettings.cli:main|--quiet|push"


def test_soft_result_reports_zero_and_keeps_the_code():
    assert hookd.soften(_r(2), True) == {**_r(2), "code": 0, "soft_code": 2}
    assert hookd.soften(_r(2), False)["code"] == 2


def test_soft_spec_parses(tmp_path):
    hooks = tmp_path / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "g.py").write_text("", encoding="utf-8")
    specs = hookd.parse_specs("h=" + hookd.SOFT + ".claude/hooks/g.py|--x", str(tmp_path))
    assert len(specs) == 1 and specs[0][1] == ["--x"] and specs[0][2] is True
