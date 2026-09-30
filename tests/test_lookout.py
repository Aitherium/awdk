"""Lookout: the judge decides when to step in, the loop respects its budgets."""

from __future__ import annotations

import json

import pytest
from adk.lookout import (
    Event,
    JsonlSource,
    LinearSource,
    Lookout,
    SlackSource,
    judge,
    main,
)


def ev(text, **kw):
    kw.setdefault("source", "slack")
    kw.setdefault("id", "1")
    kw.setdefault("channel", "C1")
    return Event(text=text, **kw)


# ── judge ───────────────────────────────────────────────────────────────────


def test_traceback_engages():
    v = judge(ev("deploy died: Traceback (most recent call last): KeyError 'x'"))
    assert v.engage, v.reasons


def test_social_chatter_is_left_alone():
    for t in ("thanks!", "lol", "lgtm", ":tada:", "sounds good"):
        assert not judge(ev(t)).engage, t


def test_opt_out_beats_any_score():
    v = judge(ev("Traceback (most recent call last) CI is red — no bots please"))
    assert not v.engage and "opt-out" in v.reasons[0]


def test_bot_messages_skipped():
    assert not judge(ev("error: build failed", is_bot=True)).engage


def test_already_engaged_thread_skipped():
    e = ev("error: build failed again", thread="T9")
    assert not judge(e, engaged_threads={e.thread_key}).engage


def test_unanswered_question_engages_answered_one_does_not():
    q = "why does the staging login loop forever?"
    assert judge(ev(q, age_s=3600, replies=0)).engage
    assert not judge(ev(q, age_s=60, replies=0)).engage
    assert not judge(ev(q, age_s=3600, replies=4)).engage


def test_linear_unowned_bug_engages_owned_does_not():
    assert judge(ev("ENG-1 Export crashes on empty sheet", source="linear")).engage
    assert not judge(
        ev("ENG-1 Export crashes on empty sheet", source="linear", assignee="sam")
    ).engage


def test_llm_judge_only_moves_borderline_and_survives_errors():
    border = ev("how do we rotate the staging cert?", age_s=0)  # score 1.0 → borderline
    assert judge(border, llm_judge=lambda e, v: True).engage
    clear = ev("lol")
    assert not judge(clear, llm_judge=lambda e, v: True).engage

    def boom(e, v):
        raise RuntimeError("model down")

    v = judge(border, llm_judge=boom)
    assert not v.engage and any("llm judge failed" in r for r in v.reasons)


# ── sources ────────────────────────────────────────────────────────────────


def test_slack_source_normalises_and_advances_cursor():
    calls = []

    def fetch(url, headers, body=None):
        calls.append(url)
        return {
            "ok": True,
            "messages": [
                {"ts": "100.1", "user": "U1", "text": "error: x"},
                {"ts": "100.2", "bot_id": "B1", "text": "I am a bot"},
            ],
        }

    s = SlackSource(["C1"], token="t", fetch=fetch)
    events, cur = s.poll(None)
    assert [e.is_bot for e in events] == [False, True]
    assert json.loads(cur)["C1"] == "100.2"
    s.poll(cur)
    assert "oldest=100.2" in calls[-1]


def test_slack_error_raises():
    s = SlackSource(
        ["C1"], token="t", fetch=lambda *a, **k: {"ok": False, "error": "not_in_channel"}
    )
    with pytest.raises(RuntimeError, match="not_in_channel"):
        s.poll(None)


def test_sources_refuse_without_credentials(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    with pytest.raises(ValueError):
        SlackSource(["C1"])
    with pytest.raises(ValueError):
        LinearSource("ENG")


def test_linear_source_maps_assignee_and_cursor():
    def fetch(url, headers, body=None):
        assert body["variables"]["team"] == "ENG"
        return {
            "data": {
                "issues": {
                    "nodes": [
                        {
                            "id": "a",
                            "identifier": "ENG-1",
                            "title": "Crash",
                            "description": "boom",
                            "url": "u",
                            "updatedAt": "2026-09-29T10:00:00Z",
                            "createdAt": "2026-09-29T09:00:00Z",
                            "assignee": {"name": "sam"},
                            "comments": {"nodes": [{"id": "c"}]},
                        },
                    ]
                }
            }
        }

    events, cur = LinearSource("ENG", api_key="k", fetch=fetch).poll(None)
    assert events[0].assignee == "sam" and events[0].replies == 1
    assert cur == "2026-09-29T10:00:00Z"


def test_jsonl_source_reads_only_new_complete_lines(tmp_path):
    f = tmp_path / "ops.jsonl"
    f.write_text(
        json.dumps({"text": "error: disk full"}) + "\n" + '{"text": "partial', encoding="utf-8"
    )
    src = JsonlSource(str(f))
    events, cur = src.poll(None)
    assert [e.text for e in events] == ["error: disk full"]
    with f.open("a", encoding="utf-8") as h:
        h.write(' line"}\n')
    events, _ = src.poll(cur)
    assert [e.text for e in events] == ["partial line"]


# ── loop ────────────────────────────────────────────────────────────────────


class ListSource:
    name = "fake"

    def __init__(self, batches):
        self.batches = list(batches)

    def poll(self, cursor):
        return (self.batches.pop(0) if self.batches else []), str(int(cursor or 0) + 1)


def test_loop_dispatches_once_per_thread_and_ledgers_skips(tmp_path):
    sent = []
    e1 = ev("error: build failed", id="a", thread="T1")
    e2 = ev("error: build failed still", id="b", thread="T1")
    e3 = ev("thanks", id="c", thread="T2")
    ship = Lookout(
        [ListSource([[e1, e3], [e2]])],
        lambda e: sent.append(e.id) or {"id": "r1"},
        state_dir=tmp_path,
    )
    ship.tick(now=1000.0)
    ship.tick(now=1001.0)
    assert sent == ["a"]
    rows = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert [r["action"] for r in rows] == ["engage", "skip", "skip"]
    # state survives a restart
    assert Lookout([], lambda e: {}, state_dir=tmp_path).state["cursors"]["fake"] == "2"


def test_hourly_cap_holds(tmp_path):
    sent = []
    batch = [ev(f"error: failure {i}", id=str(i), thread=f"T{i}") for i in range(5)]
    Lookout(
        [ListSource([batch])], lambda e: sent.append(e.id) or {}, state_dir=tmp_path, hourly_cap=2
    ).tick(now=5000.0)
    assert len(sent) == 2


def test_dry_run_never_dispatches(tmp_path):
    def nope(e):
        raise AssertionError("dispatched in dry-run")

    rows = Lookout(
        [ListSource([[ev("error: x failed")]])], nope, state_dir=tmp_path, dry_run=True
    ).tick()
    assert rows[0]["dispatch"] == "dry-run"


def test_source_and_dispatch_errors_do_not_stop_the_loop(tmp_path):
    class Dead:
        name = "dead"

        def poll(self, cursor):
            raise RuntimeError("401")

    def fail(e):
        raise RuntimeError("awrun down")

    rows = Lookout(
        [Dead(), ListSource([[ev("error: x failed")]])], fail, state_dir=tmp_path
    ).tick()
    assert [r["action"] for r in rows] == ["source-error", "dispatch-error"]


def test_cli_judge_and_nothing_to_watch(capsys):
    assert main(["judge", "Traceback (most recent call last)"]) == 0
    assert capsys.readouterr().out.startswith("ENGAGE")
    assert main(["run", "--once"]) == 2


# ── CLI verb ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("verb", ["lookout", "mothership"])
def test_cli_verb_and_its_3_8_31_alias_reach_lookout(verb, monkeypatch):
    # 3.8.31 shipped the verb as `mothership`; the rename must not break it.
    import sys

    from adk import cli

    monkeypatch.setattr(sys, "argv", ["adk", verb, "judge", "Traceback (most recent call last)"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0


def test_3_8_31_module_name_still_imports():
    # PVR002: 3.8.31 shipped adk/mothership.py; dropping it breaks installed importers.
    from adk import lookout, mothership

    assert mothership.Mothership is lookout.Lookout
    assert mothership.judge is lookout.judge
