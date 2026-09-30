"""/tutor focus in the AitherShell tutor plugin (guardian weekly focus + coach note)."""

from __future__ import annotations

import asyncio

import pytest
from adk.shell.plugins.builtins import tutor as mod


@pytest.fixture()
def fake(monkeypatch):
    calls = []
    state = {"status": 200, "data": {}}

    async def fake_request(ctx, method, path, body=None, params=None):
        calls.append((method, path, body))
        return state["status"], state["data"]

    monkeypatch.setattr(mod, "_request", fake_request)
    return calls, state


def _run(*args):
    return asyncio.run(mod.TutorPlugin().run(list(args), {}))


def test_focus_puts_skills_and_note(fake):
    calls, state = fake
    state["data"] = {"focus": {"active": True, "active_until": "2026-10-07",
                               "skills": [{"skill_id": "math.make_ten_strategy",
                                           "kid_title": "Make a ten"}],
                               "note": "loves space"}}
    out = _run("focus", "L1", "math.make_ten_strategy", "--note", "loves", "space")
    assert calls == [("PUT", "/learners/L1/focus",
                      {"skills": ["math.make_ten_strategy"], "note": "loves space"})]
    assert "Make a ten" in out and "Coach note: loves space" in out


def test_focus_show_lists_catalog(fake):
    calls, state = fake
    state["data"] = {"focus": {}, "catalog": [
        {"skill_id": "math.make_ten_strategy", "kid_title": "Make a ten", "ready": True},
        {"skill_id": "math.add_within_20", "kid_title": "Add to 20", "ready": False}]}
    out = _run("focus", "a/b", "--show")
    assert calls == [("GET", "/learners/a%2Fb/focus", None)]
    assert "No weekly focus set." in out and "* math.make_ten_strategy" in out


def test_focus_limits_and_usage(fake):
    calls, _state = fake
    assert "Usage" in _run("focus")
    assert "At most" in _run("focus", "L1", *[f"s{i}" for i in range(7)])
    assert "280" in _run("focus", "L1", "--note", "x" * 281)
    assert calls == []


def test_focus_clear_and_errors(fake):
    calls, state = fake
    state["status"], state["data"] = 404, {"detail": "Not found"}
    assert "No such learner" in _run("focus", "L2")
    assert calls[-1] == ("PUT", "/learners/L2/focus", {"skills": [], "note": ""})
