"""Lead-with-the-next-action layout: the shared helper and the renderers using it.

Each status renderer is fed an overload (12 failing items) and must:
line 1 is an action, at most five items visible, ``+7 more`` names the command
that shows the rest, every line fits 80 columns, and ``--all`` shows all 12.
"""

from __future__ import annotations

import re

import pytest

from adk import _attention as att

_STEP = re.compile(r"^\s*\d+\. ")


def _assert_shape(lines, *, visible=5, more="+7 more", width=80):
    assert lines[0].startswith("Next: "), lines[0]
    assert sum(1 for ln in lines if _STEP.match(ln)) == visible
    assert any(more in ln for ln in lines), "\n".join(lines)
    too_wide = [ln for ln in lines if len(ln) > width]
    assert not too_wide, too_wide


# --------------------------------------------------------------------- helper
def test_next_line_idle_and_action():
    assert att.next_line(None) == "Nothing needs you."
    assert att.next_line(att.run("adk setup")) == "Next: run `adk setup`"
    assert att.next_line(att.run("adk setup"), "vLLM down") == "Next: run `adk setup` (vLLM down)"


def test_next_line_drops_reason_before_action():
    line = att.next_line(att.run("adk setup"), "x" * 200)
    assert line.startswith("Next: run `adk setup` (") and len(line) <= 80


def test_fit_truncates_with_marker():
    assert att.fit("a" * 79) == "a" * 79
    cut = att.fit("a" * 200)
    assert len(cut) == 80 and cut.endswith("...")


def test_cap_keeps_every_item_reachable():
    items = list(range(12))
    shown, hidden = att.cap(items)
    assert shown == [0, 1, 2, 3, 4] and hidden == 7
    assert items == list(range(12))  # the cap is presentation only
    assert att.cap(items, show_all=True) == (items, 0)


def test_numbered_caps_and_names_the_rest():
    lines = att.numbered([f"item {i}" for i in range(12)], more_cmd="adk x --all")
    assert lines[:2] == ["  1. item 0", "  2. item 1"]
    assert lines[-1] == "  +7 more (adk x --all)"
    assert len(lines) == 6


def test_numbered_pairs_print_the_remedy_under_the_step():
    lines = att.numbered([("disk full", "run `adk clean`")])
    assert lines == ["  1. disk full", "     -> run `adk clean`"]


def test_count_line_pairs_count_with_remedy():
    assert att.count_line(10, "services down", att.run("adk up")) == (
        "10 services down — run `adk up`")
    assert att.count_line(0, "services down", att.run("adk up")) == "0 services down"


def test_names_line_caps():
    line = att.names_line("Passed", [f"c{i}" for i in range(8)], more_cmd="adk doctor --all")
    assert line == "Passed (8): c0, c1, c2, c3, c4, +3 more (adk doctor --all)"


# --------------------------------------------------------------------- doctor
def _doctor_results(n_fail=12, n_pass=3):
    res = []
    for i in range(n_fail):
        sev = 2 if i % 2 else 1
        res.append({"name": f"Check{i}", "passed": False, "severity": sev,
                    "summary": f"Check{i}: broken " + "detail " * 12,
                    "output": f"  [--] Check{i}: broken\n", "remedy": f"run `adk fix{i}`"})
    for i in range(n_pass):
        res.append({"name": f"Ok{i}", "passed": True, "severity": 0, "summary": "",
                    "output": f"  [OK] Ok{i}\n", "remedy": ""})
    return res


def test_doctor_overload_leads_with_next_and_caps():
    from adk.doctor import render_doctor

    lines = render_doctor(_doctor_results(), "9.9.9")
    _assert_shape(lines)
    # Hard failures ([--], severity 2) outrank warnings: Check1 is the first hard one.
    assert lines[0].startswith("Next: run `adk fix1`")
    assert "3/15 checks passed" in lines[1]
    assert "adk doctor --all" in "\n".join(lines)
    assert lines[-1].startswith("12 checks failing — start with: run `adk fix1`")


def test_doctor_all_shows_every_failure():
    from adk.doctor import render_doctor

    lines = render_doctor(_doctor_results(), "9.9.9", show_all=True)
    assert sum(1 for ln in lines if _STEP.match(ln)) == 12
    assert not any("+7 more" in ln for ln in lines)


def test_doctor_clean_says_nothing_needs_you():
    from adk.doctor import render_doctor

    lines = render_doctor(_doctor_results(n_fail=0, n_pass=4), "9.9.9")
    assert lines[0].startswith("Nothing needs you")
    assert "4/4 checks passed" in lines[0]


def test_run_check_captures_tag_and_remedy():
    from adk import doctor

    def broken():
        doctor._fail("Autostart: MISSING")
        return False

    r = doctor._run_check("Autostart", broken)
    assert r["passed"] is False and r["severity"] == 2
    assert r["summary"] == "Autostart: MISSING"
    assert r["remedy"] == "run `adk up`"

    def raises():
        raise RuntimeError("boom")

    r = doctor._run_check("GPU", raises)
    assert r["passed"] is False and "boom" in r["summary"]


# -------------------------------------------------------------- claude doctor
def test_claude_doctor_overload():
    from adk.claude_code_doctor import Report, render_report

    rep = Report()
    for i in range(10):
        rep.add("CCD005", f"unknown settings key 'k{i}' in user", "warning")
    rep.add("CCD008", "2 hook(s) route through hookd and it is not answering on :1 -- "
            "they are skipped silently; run `adk claude hookd ensure`")
    rep.add("CCD001", "autoMode in project settings is ignored")
    lines = render_report(rep)
    _assert_shape(lines)
    # Violations outrank warnings; a backticked command in the message is THE remedy.
    assert lines[0].startswith("Next: run `adk claude hookd ensure`")
    assert "adk claude doctor --all" in "\n".join(lines)
    assert len(render_report(rep, show_all=True)) > len(lines)


def test_claude_doctor_json_unchanged(capsys):
    import json

    from adk.claude_code_doctor import Report, print_report

    rep = Report()
    rep.add("CCD001", "x")
    rep.notes.append("n")
    print_report(rep, as_json=True, show_all=True)
    assert json.loads(capsys.readouterr().out) == {
        "exit": 1, "findings": [{"code": "CCD001", "message": "x", "severity": "violation"}],
        "notes": ["n"]}


# ---------------------------------------------------------------- expeditions
def test_expedition_status_overload():
    from adk.jobs_render import render_expedition_status

    tasks = [{"id": f"t{i}", "status": "failed", "title": "probe " * 20,
              "error": "timeout " * 20} for i in range(12)]
    tasks += [{"id": "ok", "status": "completed", "title": "fine"}]
    lines = render_expedition_status("exp123", {"status": "running", "loops": 3}, tasks)
    _assert_shape(lines, more="+8 more")
    assert "adk jobs steer exp123" in lines[0]
    assert "adk jobs status exp123 --remote --all" in "\n".join(lines)
    assert lines[-1].startswith("12 tasks failed — run `adk jobs steer exp123")


def test_job_list_overload_ranks_failures_first():
    from adk.jobs_render import render_job_list

    jobs = [{"id": f"done{i:04d}", "status": "completed", "query": "q"} for i in range(3)]
    jobs += [{"id": f"bad{i:05d}", "status": "failed", "query": "x" * 200} for i in range(12)]
    lines = render_job_list(jobs)
    assert lines[0] == "Next: run `adk jobs status bad00000` (it failed)"
    rows = [ln for ln in lines if ln.startswith("  bad") or ln.startswith("  done")]
    assert len(rows) == 5 and all(r.startswith("  bad") for r in rows)
    assert any("+10 more (adk jobs list --all)" in ln for ln in lines)
    assert all(len(ln) <= 80 for ln in lines)
    assert len([ln for ln in render_job_list(jobs, show_all=True)
                if ln.startswith(("  bad", "  done"))]) == 15


def test_remote_job_list_more_command_keeps_remote():
    from adk.jobs_render import render_job_list

    exps = [{"id": f"e{i}", "status": "running", "title": "t"} for i in range(9)]
    lines = render_job_list(exps, remote=True)
    assert lines[0].startswith("Nothing needs you.")
    assert any("adk jobs list --remote --all" in ln for ln in lines)


def test_local_failed_job_offers_retry():
    from adk.jobs_render import render_local_status

    lines = render_local_status({"id": "abcdef123", "status": "failed", "query": "do it",
                                 "error": "boom"})
    assert lines[0] == "Next: run `adk jobs start 'do it'` (retry it)"


# ----------------------------------------------------------------------- grid
def test_grid_health_overload():
    from adk.grid_status import collect_grid_status, grid_next_action, render_grid_health

    nodes = {"cluster": [{"host": f"10.0.0.{i}", "port": 8121} for i in range(12)]}
    status = collect_grid_status({}, nodes, probe=lambda h, p: {"state": "unreachable",
                                                                "models": []})
    lines = [att.next_line(grid_next_action(status))] + render_grid_health(status)
    _assert_shape(lines)
    assert "adk grid test 10.0.0.0" in lines[0]
    assert lines[-1].startswith("12 nodes down — start the llama-server on 10.0.0.0")


@pytest.mark.parametrize("state", ["healthy"])
def test_grid_all_healthy_needs_nothing(state):
    from adk.grid_status import collect_grid_status, grid_next_action

    status = collect_grid_status({}, {"cluster": [{"host": "h", "port": 1}]},
                                 probe=lambda h, p: {"state": state, "models": []})
    assert grid_next_action(status) is None
