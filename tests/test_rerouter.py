"""The re-router (spec section 5): continue | replan | escalate | done, on state delta."""
from __future__ import annotations

from adk import rerouter as rr
from adk.rerouter import Decision, ReRouter, is_delta, make_on_step


def read(path="a.py", ok=True):
    return {"tool": "file_read", "args": {"path": path}, "ok": ok, "result_head": "...."}


def edit(path="a.py", ok=True, changed=True):
    return {"tool": "file_edit", "args": {"path": path}, "ok": ok,
            "result_head": '{"ok": true}' if ok else '{"error": "old_text not found"}',
            "work_tree_changed": changed}


def ran_tests(head="===== 3 passed in 0.2s ====="):
    return {"tool": "run_tests", "args": {}, "ok": True, "result_head": head}


def test_delta_is_a_change_not_a_step():
    assert not is_delta(read()) and not is_delta({"tool": "code_search", "ok": True})
    assert is_delta(edit()) and is_delta({"tool": "shell_exec", "ok": True})
    assert not is_delta(edit(ok=False))          # a refused edit changed nothing
    assert not is_delta({"tool": "deep_reasoning", "ok": True})   # thinking is not acting
    # The harness's own measurement wins when it made one.
    assert is_delta({"tool": "file_read", "ok": True, "work_tree_changed": True})


def test_verdicts_are_read_without_guessing():
    assert rr.tests_green("===== 3 passed in 0.2s =====") is True
    assert rr.tests_green("1 failed, 2 passed") is False
    assert rr.tests_green("2 errors in 0.1s") is False
    assert rr.tests_green("wrote 40 lines") is None


def test_eight_reads_escalate_once_then_need_a_delta_to_re_arm():
    r = ReRouter(stuck_threshold=8, ceiling=40)
    out = [r.after_step(read(f"f{i}.py"), step_index=i + 1).action for i in range(8)]
    assert out[:7] == ["continue"] * 7 and out[7] == "escalate"
    # Still stuck, but it does not fire again: a signal on every step carries none.
    assert [r.after_step(read(f"g{i}.py"), step_index=9 + i).action for i in range(9)] \
        == ["continue"] * 9
    assert r.escalations == 1
    r.after_step(edit(), step_index=20)           # a delta re-arms it
    assert r.stuck == 0 and r.armed
    again = [r.after_step(read(f"h{i}.py"), step_index=21 + i).action for i in range(8)]
    assert again[-1] == "escalate" and r.escalations == 2


def test_the_same_failure_three_times_is_a_replan_not_an_escalation():
    r = ReRouter(stuck_threshold=99, ceiling=40)
    acts = [r.after_step(edit(ok=False), step_index=i + 1).action for i in range(3)]
    assert acts == ["continue", "continue", "replan"] and r.replans == 1
    # The streak resets, so the next identical failure does not re-fire immediately.
    assert r.after_step(edit(ok=False), step_index=4).action == "continue"


def test_three_quarters_of_the_ceiling_with_nothing_landed_is_a_replan():
    r = ReRouter(stuck_threshold=99, ceiling=40)
    for i in range(29):
        d = r.after_step(read(f"f{i}.py"), step_index=i + 1)
        assert d.action == "continue", (i, d)
    assert r.after_step(read("f30.py"), step_index=30).action == "replan"


def test_green_tests_after_a_landed_edit_are_done_but_not_before():
    r = ReRouter()
    assert r.after_step(ran_tests(), step_index=1).action == "continue"  # nothing landed yet
    r.after_step(edit(), step_index=2)
    d = r.after_step(ran_tests(), step_index=3)
    assert d.action == "done" and "1 landed edit" in d.reason
    assert r.after_step(ran_tests("1 failed, 2 passed"), step_index=4).action == "continue"


def test_messages_are_silent_on_continue_and_directive_otherwise():
    assert Decision("continue", "", 1, 0).message() == ""
    for action in ("escalate", "replan", "done"):
        assert Decision(action, "because", 1, 0).message().startswith("[RE-ROUTER] because")
    assert "deep_reasoning" in Decision("escalate", "x", 1, 8).message()


def test_the_harness_adapter_records_live_telemetry_and_injects_only_when_it_decides():
    rec: dict = {"steps": []}
    r = ReRouter(stuck_threshold=3, ceiling=40)
    on_step = make_on_step(r, rec)
    assert rec["reroute"]["counts"]["continue"] == 0
    for i in range(3):
        rec["steps"].append(read(f"f{i}.py"))
        out = on_step(rec)
    assert out and "[RE-ROUTER]" in out
    assert rec["reroute"]["escalations"] == 1
    assert rec["reroute"]["counts"] == {"continue": 2, "replan": 0, "escalate": 1, "done": 0}
    assert [d["action"] for d in rec["reroute"]["decisions"]][-1] == "escalate"
    rec["steps"].append(edit())
    assert on_step(rec) is None                  # a good step says nothing
    assert rec["reroute"]["edits_landed"] == 1
