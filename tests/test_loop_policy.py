"""The loop policy: the four measured nudges, lifted from the SWE-bench harness (2026-09-21).

Each arm mirrors the harness's own self-test so the two cannot drift silently; the last
arm asserts the agent loop actually consults the policy after every tool result.
"""

from __future__ import annotations

from pathlib import Path

from adk.loop_policy import LoopPolicy, result_ok

ERR = '{"error": "old_text not found in file"}'
OK_EDIT = '{"ok": true, "path": "a.py"}'
GREEN = "===== 3 passed in 0.2s ====="
RED = "===== 1 failed, 2 passed in 0.2s ====="


def test_result_ok_reads_the_builtin_error_envelope():
    assert result_ok(OK_EDIT) and result_ok("plain text") and result_ok({"ok": 1})
    assert not result_ok(ERR) and not result_ok('  {"error": "x"}')
    assert not result_ok({"error": "x"})


def test_read_before_edit_fires_on_an_unread_path_at_most_twice():
    p = LoopPolicy()
    p.record("file_search", {"pattern": "x"}, "hits")
    p.record("file_edit", {"path": "a.py", "old_text": "x", "new_text": "y"}, ERR)
    n = p.nudges()
    assert n and "without ever reading it" in n[0] and "a.py" in n[0]
    p.record("file_edit", {"path": "b.py", "old_text": "x", "new_text": "y"}, ERR)
    assert "without ever reading it" in p.nudges()[0]
    p.record("file_edit", {"path": "c.py", "old_text": "x", "new_text": "y"}, ERR)
    assert not any("without ever reading" in t for t in p.nudges()), "capped at two"


def test_read_before_edit_is_quiet_after_a_read():
    p = LoopPolicy()
    p.record("file_read", {"path": "a.py"}, "content")
    p.record("file_edit", {"path": "a.py", "old_text": "x", "new_text": "y"}, OK_EDIT)
    n = p.nudges()
    assert n and "RUN THE TESTS" in n[0], "the verify nudge takes the step instead"


def test_edit_budget_fires_from_edit_by_every_three_until_an_edit_lands():
    p = LoopPolicy(edit_by=3, ceiling=10, every=3)
    for i in range(2):
        p.record("file_search", {"pattern": str(i)}, "hits")
        assert p.nudges() == []
    p.record("file_read", {"path": "z.py"}, "content")           # step 3 == edit_by
    n = p.nudges()
    assert n and "[Budget] 3 of 10" in n[0] and "`z.py`" in n[0] and "7 left" in n[0]
    p.record("file_search", {"pattern": "q"}, "hits")             # step 4: quiet
    assert p.nudges() == []
    p.record("file_search", {"pattern": "q"}, "hits")             # step 5: quiet
    assert p.nudges() == []
    p.record("file_search", {"pattern": "q"}, "hits")             # step 6: fires again
    assert "[Budget] 6 of 10" in p.nudges()[0]
    p.record("file_edit", {"path": "z.py", "old_text": "a", "new_text": "b"}, OK_EDIT)
    assert "RUN THE TESTS" in p.nudges()[0]
    for _ in range(4):
        p.record("file_search", {"pattern": "q"}, "hits")
        assert p.nudges() == [], "quiet forever after a landed edit"


def test_budget_names_no_read_when_nothing_was_read():
    p = LoopPolicy(edit_by=1, ceiling=5)
    p.record("shell_exec", {"command": "ls"}, "files")
    assert "have not read any file" in p.nudges()[0]


def test_verify_fires_once_on_the_first_successful_edit_only():
    p = LoopPolicy(edit_by=0)
    p.record("file_read", {"path": "a.py"}, "content")
    p.record("file_edit", {"path": "a.py", "old_text": "x", "new_text": "y"}, ERR)
    assert p.nudges() == [], "a FAILED edit does not count as landing one"
    p.record("file_edit", {"path": "a.py", "old_text": "x", "new_text": "y"}, OK_EDIT)
    assert "RUN THE TESTS" in p.nudges()[0]
    p.record("file_edit", {"path": "a.py", "old_text": "y", "new_text": "z"}, OK_EDIT)
    assert p.nudges() == [], "fires once"


def test_regression_fires_once_after_a_green_test_run_only():
    p = LoopPolicy(edit_by=0)
    p.record("run_tests", {"path": "tests/t.py"}, RED)
    assert p.nudges() == [], "a failing run does not trigger it"
    p.record("run_tests", {"path": "tests/t.py"}, "ERROR: collection failed")
    assert p.nudges() == []
    p.record("run_tests", {"path": "tests/t.py"}, GREEN)
    assert "WHOLE test file" in p.nudges()[0]
    p.record("run_tests", {"path": "tests/"}, GREEN)
    assert p.nudges() == [], "fires once"


def test_nudge_order_read_first_then_budget_then_verify():
    p = LoopPolicy(edit_by=1, ceiling=5)
    p.record("file_edit", {"path": "u.py", "old_text": "x", "new_text": "y"}, OK_EDIT)
    n = p.nudges()
    assert len(n) == 1 and "without ever reading it" in n[0], "read-before-edit outranks verify"


def test_switches_turn_each_nudge_off():
    p = LoopPolicy(edit_by=0, read_before_edit=False, verify_loop=False, regression=False)
    p.record("file_edit", {"path": "u.py", "old_text": "x", "new_text": "y"}, OK_EDIT)
    p.record("run_tests", {"path": "t"}, GREEN)
    assert p.nudges() == []
    assert LoopPolicy(edit_by=0).budget_line() == "" and "by call 6" in LoopPolicy().budget_line()


def test_agent_loop_consults_the_policy_after_every_tool_result():
    import adk.agent as agent_mod

    src = Path(agent_mod.__file__).read_text(encoding="utf-8", errors="replace")
    assert "loop_policy: \"LoopPolicy | None\" = None" in src or "loop_policy=" in src
    assert "self.loop_policy.record(tc.name, tc.arguments, result)" in src
    assert "_deferred_nudges.extend(self.loop_policy.nudges())" in src
