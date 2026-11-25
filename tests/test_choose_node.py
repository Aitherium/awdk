"""adk.choose asks the owner's node decider first (gap C3, 2026-11-15).

The node door (the awnode node decider, :8298) answered 30% of real
decisions on the desktop's CPU Bonsai-2 4B, but no caller dialed it. decide() and
decide_batch() now ask it first with route=node (answer or abstain) when the owner
opted in, and send only the abstentions down the usual path."""
from __future__ import annotations

import json

import pytest

from adk import choose


@pytest.fixture
def node_on(tmp_path, monkeypatch):
    home = tmp_path / ".aither"
    home.mkdir()
    (home / "node-decider.json").write_text(json.dumps({"enabled": True}), encoding="utf-8")
    monkeypatch.setenv("AITHER_HOME", str(home))
    monkeypatch.delenv("AITHER_DECIDE_NODE", raising=False)
    monkeypatch.delenv("AITHER_DECIDE_URL", raising=False)
    monkeypatch.delenv("AITHER_NODE_DECIDER", raising=False)
    return home


def _answer(label, did="node-abc", src="node:bonsai2-4b"):
    return {"decision_id": did, "answer": label, "confidence": 0.93, "source": src,
            "route": {"served_by": "node", "offloaded": True}}


def test_off_without_the_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.delenv("AITHER_DECIDE_NODE", raising=False)
    monkeypatch.delenv("AITHER_DECIDE_URL", raising=False)
    monkeypatch.delenv("AITHER_NODE_DECIDER", raising=False)
    assert choose._node_door() == ""


def test_on_with_the_opt_in_and_a_named_door_wins(node_on, monkeypatch):
    assert choose._node_door() == "http://127.0.0.1:8298"
    monkeypatch.setenv("AITHER_DECIDE_URL", "https://door.example:8197")
    assert choose._node_door() == ""
    monkeypatch.delenv("AITHER_DECIDE_URL")
    monkeypatch.setenv("AITHER_DECIDE_NODE", "0")
    assert choose._node_door() == ""


def test_decide_is_answered_on_the_node(node_on, monkeypatch):
    seen = {}

    def node_post(path, body, timeout):
        seen.update(path=path, route=body.get("route"))
        return _answer("fast")

    monkeypatch.setattr(choose, "_node_post", node_post)
    monkeypatch.setattr(choose, "_post", lambda *a, **k: pytest.fail("fleet asked"))
    d = choose.decide("router", "lang:py", options=["fast", "deep"])
    assert (d.answer, d.source) == ("fast", "node:bonsai2-4b")
    assert seen == {"path": "/decide", "route": "node"}


def test_an_abstaining_node_falls_through_to_the_door(node_on, monkeypatch):
    monkeypatch.setattr(choose, "_node_post", lambda *a, **k: None)
    monkeypatch.setattr(choose, "_post", lambda path, body, timeout=30.0: {
        "decision_id": "d1", "answer": "deep", "confidence": 0.7, "source": "llm"})
    d = choose.decide("router", "lang:py", options=["fast", "deep"])
    assert (d.answer, d.source) == ("deep", "llm")


def test_batch_sends_only_the_abstentions_on(node_on, monkeypatch):
    abstained = {"error": "no lane answered", "status": 502}
    monkeypatch.setattr(choose, "_node_post", lambda path, body, timeout: {"answers": [
        _answer("a", "node-1"), abstained, _answer("c", "node-3")]})
    sent = {}

    def fleet(path, body, timeout=60.0):
        sent["states"] = [it["state"] for it in body["items"]]
        return {"answers": [{"decision_id": "f2", "answer": "b", "confidence": 0.6,
                             "source": "llm"}]}

    monkeypatch.setattr(choose, "_post", fleet)
    out = choose.decide_batch("router", [{"state": s, "options": ["a", "b", "c"]}
                                         for s in ("s1", "s2", "s3")])
    assert [d.answer for d in out] == ["a", "b", "c"]
    assert sent["states"] == ["s2"]


def test_a_node_decision_is_taught_back_through_the_node(node_on, monkeypatch):
    calls = []
    monkeypatch.setattr(choose, "_node_post",
                        lambda path, body, timeout: calls.append(path) or {"relayed_as": "teach"})
    monkeypatch.setattr(choose, "_post", lambda *a, **k: pytest.fail("fleet asked"))
    assert choose.outcome("node-1234", 1.0)["relayed_as"] == "teach"
    assert calls == ["/decide/outcome"]
