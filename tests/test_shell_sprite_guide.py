"""/sprite guide + training subcommands render the server view and send the right calls."""
from __future__ import annotations

import asyncio

import pytest
from adk.shell.plugins.builtins import sprite as sp

VIEW = {"temperament": "bright", "available_acts": ["spar", "tend"], "offer": None,
        "stats": {"strength": 4, "dexterity": 0, "speed": 1}, "stat_cap": 25,
        "ladder": {"rung": 2, "of": 10, "next_foe": "Grumble Toad", "wins": 1, "losses": 0},
        "charms": ["ember"], "equipped": []}


@pytest.fixture()
def calls(monkeypatch):
    seen = []

    async def fake_get(path, params=None):
        seen.append(("GET", path, None))
        return dict(VIEW)

    async def fake_post(path, body=None):
        seen.append(("POST", path, body))
        if path.endswith("/challenge"):
            return {**VIEW, "result": {"foe": "Grumble Toad", "won": True, "rounds": 7}}
        if path.endswith("/train"):
            return {**VIEW, "result": {"stat": "speed", "gain": 1, "value": 2, "cap": 25}}
        return {**VIEW, "result": {"act": "spar", "offer": {"act": "spar", "stat": "speed"},
                                   "line": "Pip wants to spar."},
                "offer": {"act": "spar", "stat": "speed"}}

    monkeypatch.setattr(sp, "_get", fake_get)
    monkeypatch.setattr(sp, "_post", fake_post)
    return seen


def _run(*args):
    return asyncio.run(sp.SpritePlugin().run(list(args), {}))


def test_guide_renders_view(calls):
    out = _run("guide")
    assert "Guide: bright" in out and "Grumble Toad" in out and "ember" in out
    assert calls == [("GET", f"{sp.BASE}/me/guide", None)]


def test_beat_shows_the_offer(calls):
    out = _run("beat")
    assert "Pip wants to spar." in out and "/sprite answer yes|no" in out
    assert calls[-1] == ("POST", f"{sp.BASE}/me/guide/beat", {"act": None})


def test_train_and_challenge(calls):
    assert "speed +1" in _run("train", "speed")
    assert calls[-1] == ("POST", f"{sp.BASE}/me/train", {"stat": "speed"})
    assert "Beat Grumble Toad in 7 rounds" in _run("fight")


def test_bad_arguments_never_call_the_server(calls):
    assert "Usage" in _run("train", "charisma")
    assert "Usage" in _run("answer", "maybe")
    assert calls == []
