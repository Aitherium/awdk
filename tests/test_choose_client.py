"""adk.choose + /choose: the client shapes the door's contract and never
mistakes an outage for a decision."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from adk import choose as door
from adk.shell.plugins.builtins import choose as plugin


class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_decide_namespaces_the_fork_and_shapes_the_answer(monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout=0, context=None):
        sent["url"] = req.full_url
        sent["body"] = json.loads(req.data)
        return _Resp(
            {
                "decision_id": "abc",
                "answer": "deep",
                "confidence": 0.75,
                "source": "engine",
                "learned_from": 4,
                "latency_ms": 0.4,
                "alternatives": [{"answer": "deep", "value": 1.0, "n": 4}],
            }
        )

    monkeypatch.setenv("AITHER_DECIDE_URL", "http://door.test")
    monkeypatch.setattr(door.urllib.request, "urlopen", fake_urlopen)
    d = door.decide("router", "kind:code", options=["fast", "deep"])
    assert sent["url"] == "http://door.test/decide"
    assert sent["body"]["domain"] == "decide.router" and sent["body"]["options"] == ["fast", "deep"]
    assert d.answer == "deep" and d.learned and d.learned_from == 4
    assert "learned from 4" in plugin.render(d)


def test_outage_is_an_exception_not_a_decision(monkeypatch):
    def down(req, timeout=0, context=None):
        raise door.urllib.error.URLError("refused")

    monkeypatch.setenv("AITHER_DECIDE_URL", "http://door.test")
    monkeypatch.setattr(door.urllib.request, "urlopen", down)
    with pytest.raises(door.DecideUnavailableError):
        door.decide("router", "s", options=["a", "b"])


def test_422_is_a_caller_error(monkeypatch):
    import io
    import urllib.error

    def bad(req, timeout=0, context=None):
        raise urllib.error.HTTPError(
            req.full_url, 422, "unprocessable", {}, io.BytesIO(b"options must be distinct")
        )

    monkeypatch.setenv("AITHER_DECIDE_URL", "http://door.test")
    monkeypatch.setattr(door.urllib.request, "urlopen", bad)
    with pytest.raises(ValueError, match="distinct"):
        door.decide("router", "s", options=["a", "a"])


def test_outcome_and_teach_post_the_learning_signal(monkeypatch):
    calls = []

    def fake(req, timeout=0, context=None):
        calls.append((req.full_url, json.loads(req.data)))
        return _Resp(
            {"ok": True, "answer": "deep", "reward": 1.0, "observed": 1, "mode": "tabular"}
        )

    monkeypatch.setenv("AITHER_DECIDE_URL", "http://door.test")
    monkeypatch.setattr(door.urllib.request, "urlopen", fake)
    door.outcome("abc", 1)
    door.teach("router", "kind:code", "deep", -0.5)
    assert calls[0] == ("http://door.test/decide/outcome", {"decision_id": "abc", "reward": 1.0})
    assert calls[1][1] == {
        "domain": "decide.router",
        "state": "kind:code",
        "answer": "deep",
        "reward": -0.5,
    }


def test_slash_parser_grammar():
    p = plugin._parse
    assert p(["router", "kind:code", "len:long", "--", "fast", "deep"]) == {
        "fork": "router",
        "state": "kind:code len:long",
        "kind": "choice",
        "options": ["fast", "deep"],
    }
    assert p(["gate", "pr:small", "--yesno"])["kind"] == "yesno"
    assert p(["rank", "doc:7", "--score", "1", "2", "3"])["options"] == ["1", "2", "3"]
    assert p(["rank", "doc:7", "--score"])["options"] is None
    for bad in (
        ["router"],
        ["router", "s"],
        ["router", "s", "--", "only"],
        ["router", "--", "a", "b"],
    ):
        with pytest.raises(ValueError):
            p(bad)


@pytest.mark.asyncio
async def test_slash_run_routes_and_reports_outages(monkeypatch):
    monkeypatch.setattr(
        door,
        "decide",
        lambda *a, **k: SimpleNamespace(
            answer="fast",
            confidence=0.6,
            source="llm",
            learned_from=0,
            latency_ms=310.0,
            decision_id="d1",
            alternatives=[],
        ),
    )
    out = await plugin.ChoosePlugin().run(["router", "kind:chat", "--", "fast", "deep"], {})
    assert "**fast**" in out and "llm" in out and "d1" in out

    def boom(*a, **k):
        raise door.DecideUnavailableError("door unreachable at http://x")

    monkeypatch.setattr(door, "decide", boom)
    out = await plugin.ChoosePlugin().run(["router", "kind:chat", "--", "fast", "deep"], {})
    assert out.startswith("[!]") and "unreachable" in out
    assert (await plugin.ChoosePlugin().run(["router"], {})).startswith("[x]")


def test_judge_posts_criteria_and_namespaces_the_fork(monkeypatch):
    """The eval-judge shape: /judge with the fork namespaced like every other."""
    from adk import choose

    seen = {}

    def fake_post(path, body, timeout=30.0):
        seen["path"], seen["body"] = path, body
        return {"verdicts": [{"criterion": "x", "pass": None, "unknown": True}], "of": 1}

    monkeypatch.setattr(choose, "_post", fake_post)
    r = choose.judge("some log", ["the tests passed", " "], fork="ci")
    assert seen["path"] == "/judge"
    assert seen["body"]["domain"] == "decide.ci"
    assert seen["body"]["criteria"] == ["the tests passed"]
    assert r["verdicts"][0]["pass"] is None


def test_judge_outcome_requires_one_of_the_two_shapes(monkeypatch):
    from adk import choose

    monkeypatch.setattr(choose, "_post", lambda p, b, t=30.0: {"ok": True, "body": b})
    assert choose.judge_outcome("abc123", True)["body"]["verdict_was_right"] is True
    assert choose.judge_outcome(criterion="c", should_pass=False, output="o")["body"][
        "should_pass"
    ] is False
    try:
        choose.judge_outcome()
    except ValueError:
        return
    raise AssertionError("judge_outcome accepted neither shape")
