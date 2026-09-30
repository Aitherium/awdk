"""/tutor is a registered shell builtin that maps each subcommand onto /api/v1/tutor/family/*."""

from __future__ import annotations

import asyncio
import re

import pytest
from adk.shell.plugins import PluginRegistry, SlashCommand


@pytest.fixture()
def wire(monkeypatch):
    state = {"calls": [], "resp": (200, {})}

    async def fake_request(ctx, method, path, body=None, params=None):
        state["calls"].append((method, path, body, params))
        return state["resp"]

    reg = PluginRegistry([])
    reg.load_all()
    cmd = reg.get("tutor")
    assert isinstance(cmd, SlashCommand), "/tutor is not registered"
    # the registry loads builtins from their file under its own module name, so
    # patch the globals the REGISTERED command actually runs with
    monkeypatch.setitem(type(cmd).run.__globals__, "_request", fake_request)
    return cmd, state


def _run(cmd, *args):
    return asyncio.run(cmd.run(list(args), {}))


def test_registered_under_tutor_and_learn():
    reg = PluginRegistry([])
    reg.load_all()
    assert reg.get("tutor") is not None
    assert reg.get("learn") is reg.get("tutor")


def test_learners_lists_own_learners(wire):
    cmd, st = wire
    st["resp"] = (200, [{"lid": "L1", "alias": "Bee", "grade": 1, "age_band": "6-7",
                         "claimed": False, "settings": {}}])
    out = _run(cmd, "learners")
    assert st["calls"] == [("GET", "/learners", None, None)]
    assert "L1" in out and "Bee" in out and "waiting for pair code" in out


def test_enroll_requires_consent_then_posts_contract_body(wire):
    cmd, st = wire
    out = _run(cmd, "enroll", "Bee", "--grade", "1", "--age-band", "6-7")
    assert "--consent" in out and st["calls"] == []  # notice first, nothing sent
    st["resp"] = (201, {"lid": "L1", "pair_code": "PAIR-1234", "expires_at": "2026-10-01"})
    out = _run(cmd, "enroll", "Bee", "Two", "--grade", "1", "--age-band", "6-7", "--consent")
    method, path, body, _p = st["calls"][0]
    assert (method, path) == ("POST", "/learners")
    assert body == {"alias": "Bee Two", "grade": 1, "age_band": "6-7",
                    "guardian_consent": {"notice_read": True, "consent": True}}
    assert out.count("PAIR-1234") == 1  # printed once


@pytest.mark.parametrize("args", [
    ("enroll", "Bee!", "--grade", "1", "--age-band", "6-7", "--consent"),
    ("enroll", "A" * 25, "--grade", "1", "--age-band", "6-7", "--consent"),
    ("enroll", "Bee", "--grade", "3", "--age-band", "6-7", "--consent"),
    ("enroll", "Bee", "--grade", "1", "--age-band", "10-11", "--consent"),
    ("enroll", "Bee", "--grade", "1"),
])
def test_enroll_rejects_bad_input_without_calling(wire, args):
    cmd, st = wire
    _run(cmd, *args)
    assert st["calls"] == []


def test_code_rotates_and_prints_once(wire):
    cmd, st = wire
    st["resp"] = (200, {"pair_code": "NEW-9876", "expires_at": "2026-10-01"})
    out = _run(cmd, "code", "L/../1")
    assert st["calls"] == [("POST", "/learners/L%2F..%2F1/pair-code", None, None)]
    assert out.count("NEW-9876") == 1


def test_report_renders_and_passes_week(wire):
    cmd, st = wire
    st["resp"] = (200, {"minutes": 42, "sessions": 5, "attempts": 60, "breaks_used": 2,
                        "skills": {"m.add10": {"state": "secure", "kid_title": "Adding to 10",
                                               "score": 0.9}},
                        "edge": ["m.sub10"], "at_risk_reviews": [], "observations":
                        ["Enjoys counting on"], "notes": []})
    out = _run(cmd, "report", "L1", "--week", "2026-W40")
    assert st["calls"] == [("GET", "/learners/L1/report", None, {"week": "2026-W40"})]
    assert "Adding to 10" in out and "m.sub10" in out and "Enjoys counting on" in out
    assert "Usage" in _run(cmd, "report")
    assert "2026-W40" in _run(cmd, "report", "L1", "--week", "40")


def test_transcript_is_read_only(wire):
    cmd, st = wire
    st["resp"] = (200, [{"id": 1, "ts": "2026-09-29T10:00:00Z", "kind": "enrolled",
                         "actor": "guardian", "text": "Enrolled Bee"}])
    out = _run(cmd, "transcript", "L1", "--limit", "5")
    assert st["calls"] == [("GET", "/learners/L1/transcript", None, {"limit": 5})]
    assert "guardian  enrolled: Enrolled Bee" in out
    assert all(c[0] == "GET" for c in st["calls"])


def test_assign_with_note(wire):
    cmd, st = wire
    st["resp"] = (201, {"assignment_id": "A1"})
    out = _run(cmd, "assign", "L1", "m.add10", "--note", "try", "the", "dots")
    assert st["calls"] == [("POST", "/learners/L1/assign",
                            {"skill_id": "m.add10", "note": "try the dots"}, None)]
    assert "A1" in out
    _run(cmd, "assign", "L1", "m.add10", "--note", "x" * 141)
    assert len(st["calls"]) == 1


def test_set_validates_and_patches_settings(wire):
    cmd, st = wire
    st["resp"] = (200, {})
    _run(cmd, "set", "L1", "quest_minutes=5", "ask_enabled=false", "focus=math")
    assert st["calls"] == [("PATCH", "/learners/L1",
                            {"settings": {"quest_minutes": 5, "ask_enabled": False,
                                          "focus": "math"}}, None)]
    for bad in ("quest_minutes=11", "daily_cap_minutes=4", "focus=art", "streak=1", "nokey",
                "theme=" + "x" * 25):
        _run(cmd, "set", "L1", bad)
    assert len(st["calls"]) == 1


def test_not_found_reads_as_not_your_learner(wire):
    cmd, st = wire
    st["resp"] = (404, {"detail": "Not Found"})
    assert "No such learner" in _run(cmd, "report", "other-family")


def test_no_timer_copy_in_any_output(wire):
    """The parent CLI never prints a countdown/timer (pedagogy rule)."""
    cmd, st = wire
    st["resp"] = (200, {"minutes": 3, "skills": {}})
    outputs = [cmd.get_help(), _run(cmd, "report", "L1")]
    timer = re.compile(r"\b(timer|countdown|seconds left|time left|hurry)\b", re.I)
    assert not any(timer.search(o) for o in outputs)
