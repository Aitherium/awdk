"""adk.home.tutor_tools: guardian tools over /api/v1/tutor/family/*, JSON-string returns."""

from __future__ import annotations

import inspect
import json

import httpx
import pytest
from adk.home.tutor_tools import build_tutor_tools


@pytest.fixture()
def tools():
    calls = []
    state = {"status": 200, "payload": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, str(request.url), body, dict(request.headers)))
        return httpx.Response(state["status"], json=state["payload"])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    built = build_tutor_tools("https://genesis.test/", "guardian-bearer", client=client)
    yield {t.__name__: t for t in built}, calls, state
    client.close()


def test_names_docstrings_and_no_dangerous_prefix(tools):
    named, _calls, _st = tools
    assert set(named) == {"tutor_learners", "tutor_report", "tutor_assign", "tutor_set_focus"}
    for name, fn in named.items():
        assert not name.startswith(("file_", "shell")), name
        assert fn.__doc__, name
        # every argument has an 'arg: description' line (the docstring schema)
        for arg in inspect.signature(fn).parameters:
            assert f"{arg}:" in fn.__doc__, (name, arg)
            assert arg not in ("tenant_id", "user_id", "guardian_id"), (name, arg)
    assert list(inspect.signature(named["tutor_assign"]).parameters) == ["lid", "skill_id", "note"]


def test_learners_uses_bearer_and_returns_json_string(tools):
    named, calls, st = tools
    st["payload"] = [{"lid": "L1", "alias": "Bee"}]
    out = named["tutor_learners"]()
    assert isinstance(out, str) and json.loads(out) == [{"lid": "L1", "alias": "Bee"}]
    method, url, _b, headers = calls[0]
    assert method == "GET" and url == "https://genesis.test/api/v1/tutor/family/learners"
    assert headers["authorization"] == "Bearer guardian-bearer"
    assert not {"x-tenant-id", "x-internal-key", "x-caller-type"} & set(headers)


def test_report_encodes_lid_as_one_segment(tools):
    named, calls, st = tools
    st["payload"] = {"minutes": 10, "skills": {}}
    assert json.loads(named["tutor_report"]("a/../b"))["minutes"] == 10
    assert calls[0][1].endswith("/api/v1/tutor/family/learners/a%2F..%2Fb/report")
    assert "error" in json.loads(named["tutor_report"](""))
    assert len(calls) == 1


def test_assign_posts_skill_and_note(tools):
    named, calls, st = tools
    st["status"], st["payload"] = 201, {"assignment_id": "A1"}
    out = json.loads(named["tutor_assign"]("L1", "m.add10", "dots help"))
    assert out == {"assignment_id": "A1"}
    method, url, body, _h = calls[0]
    assert method == "POST" and url.endswith("/learners/L1/assign")
    assert body == {"skill_id": "m.add10", "note": "dots help"}
    assert "error" in json.loads(named["tutor_assign"]("L1", "m.add10", "x" * 141))
    assert "error" in json.loads(named["tutor_assign"]("L1", ""))
    assert len(calls) == 1


def test_set_focus_puts_skills_and_note(tools):
    named, calls, st = tools
    st["payload"] = {"lid": "L1", "focus": {"active": True}}
    out = json.loads(named["tutor_set_focus"]("L1", "m.make10, m.add10 m.make10", "loves space"))
    assert out["focus"]["active"] is True
    method, url, body, _h = calls[0]
    assert method == "PUT" and url.endswith("/api/v1/tutor/family/learners/L1/focus")
    assert body == {"skills": ["m.make10", "m.add10"], "note": "loves space"}
    assert list(inspect.signature(named["tutor_set_focus"]).parameters) == ["lid", "skills", "note"]
    assert "error" in json.loads(named["tutor_set_focus"]("L1", "a b c d e f g"))
    assert "error" in json.loads(named["tutor_set_focus"]("L1", "a", "x" * 281))
    assert "error" in json.loads(named["tutor_set_focus"]("", "a"))
    assert len(calls) == 1
    named["tutor_set_focus"]("L1", "")  # clears
    assert calls[1][2] == {"skills": [], "note": ""}


def test_errors_are_json_not_exceptions(tools):
    named, _calls, st = tools
    st["status"], st["payload"] = 404, {"detail": "Not Found"}
    out = json.loads(named["tutor_report"]("someone-elses"))
    assert out["status"] == 404 and "no such learner" in out["error"]

    def boom(request):
        raise httpx.ConnectError("down")

    dead = build_tutor_tools("https://x", "t", client=httpx.Client(
        transport=httpx.MockTransport(boom)))
    assert "unreachable" in json.loads(dead[0]())["error"]


def test_no_token_refuses_without_calling():
    calls = []
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: calls.append(r) or httpx.Response(200, json={})))
    learners = build_tutor_tools("https://x", "", client=client)[0]
    assert "not signed in" in json.loads(learners())["error"]
    assert calls == []


def test_no_grading_surface():
    """Correctness is server-only: the module never names an answer key or grades."""
    import adk.home.tutor_tools as mod

    src = inspect.getsource(mod)
    code = src.split('"""', 2)[2]  # skip the module docstring
    for banned in ("answer_key", "def grade", "check_answer", "correct ="):
        assert banned not in code, banned
