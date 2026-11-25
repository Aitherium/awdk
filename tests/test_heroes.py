"""The hero deck: status folding, ticker, selection hotkeys, control groups, missions,
achievements. Every arm builds real ``events.jsonl`` rows in the shape the daemon writes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from adk import heroes as h


def _ev(kind, seq, ts, **extra):
    row = {"kind": kind, "seq": seq, "session_id": "s-1", "ts": ts, "text": "", "tool": "",
           "tool_use_id": "", "data": {}, "turn": 0}
    row.update(extra)
    return row


def _call(seq, ts, tool, inp=None, cid=None):
    return _ev("tool.call", seq, ts, tool=tool, tool_use_id=cid or f"c{seq}",
               data={"input": inp or {}})


def _result(seq, ts, cid):
    return _ev("tool.result", seq, ts, tool_use_id=cid, data={"output": "ok"})


def _start(seq, ts, prompt="add oauth"):
    return _ev("turn.started", seq, ts, text=prompt, turn=1)


def _done(seq, ts, error=False, **data):
    return _ev("turn.completed", seq, ts, data={"is_error": error, **data})


def test_idle_before_any_turn():
    hero = h.fold_events("a", [_ev("session.starting", 1, 100.0, data={"harness": "claude"})])
    assert hero.state == h.IDLE and hero.harness == "claude" and hero.activity == "Idle"


def test_working_while_a_turn_is_open_and_ticker_reads_the_last_tool():
    hero = h.fold_events("a", [_start(1, 10.0),
                               _call(2, 11.0, "Read", {"file_path": "C:/x/App.tsx"})])
    assert hero.state == h.WORKING
    assert hero.ticker == "Reading App.tsx"
    assert hero.mission is not None and hero.mission.status == "active"


@pytest.mark.parametrize("tool,inp,want", [
    ("Edit", {"file_path": "/a/config.json"}, "Editing config.json"),
    ("Bash", {"command": "git status\nls"}, "Running git status"),
    ("Grep", {"pattern": "TODO"}, "Searching for TODO"),
    ("Task", {}, "Delegating to a subagent"),
])
def test_ticker_variants(tool, inp, want):
    assert h.ticker_for(tool, inp) == want


def test_ask_user_question_makes_the_hero_awaiting_until_answered():
    ev = [_start(1, 1.0), _call(2, 2.0, "AskUserQuestion", cid="q1")]
    hero = h.fold_events("a", ev)
    assert hero.state == h.AWAITING and hero.awaiting == "question"
    hero = h.fold_events("a", ev + [_result(3, 3.0, "q1")])
    assert hero.state == h.WORKING


def test_plan_mode_is_awaiting_plan_and_permission_request_resolves_by_id():
    hero = h.fold_events("a", [_start(1, 1.0), _call(2, 2.0, "ExitPlanMode", cid="p")])
    assert (hero.state, hero.awaiting) == (h.AWAITING, "plan")
    ask = _ev("permission.request", 2, 2.0, tool_use_id="perm1")
    ans = _ev("permission.response", 3, 3.0, tool_use_id="perm1")
    assert h.fold_events("a", [_start(1, 1.0), ask]).awaiting == "permission"
    assert h.fold_events("a", [_start(1, 1.0), ask, ans]).state == h.WORKING


def test_failed_turn_is_error_and_next_turn_clears_it():
    assert h.fold_events("a", [_start(1, 1.0), _done(2, 2.0, error=True)]).state == h.ERROR
    ev = [_start(1, 1.0), _done(2, 2.0, error=True), _start(3, 3.0)]
    assert h.fold_events("a", ev).state == h.WORKING


def test_exit_codes_and_completed_turn_back_to_idle():
    assert h.fold_events("a", [_start(1, 1.0), _done(2, 2.0)]).state == h.IDLE
    ok = _ev("session.exited", 3, 3.0, data={"exit_code": 0})
    bad = _ev("session.exited", 3, 3.0, data={"exit_code": 2})
    assert h.fold_events("a", [_start(1, 1.0), _done(2, 2.0), ok]).state == h.EXITED
    assert h.fold_events("a", [_start(1, 1.0), _done(2, 2.0), bad]).state == h.ERROR


def test_context_bands_use_the_documented_thresholds():
    assert h.context_band(69, 100) == h.BAND_GREEN
    assert h.context_band(70, 100) == h.BAND_YELLOW
    assert h.context_band(90, 100) == h.BAND_YELLOW
    assert h.context_band(91, 100) == h.BAND_RED
    hero = h.fold_events("a", [_start(1, 1.0),
                               _done(2, 2.0, context_tokens=190_000)],
                         config={"model": "opus"})
    assert hero.band == h.BAND_RED and hero.to_dict()["context"]["band"] == "red"


def test_mission_metrics_name_summary_and_last_mission_shown_when_idle():
    ev = [_start(1, 100.0, "add oauth login"),
          _call(2, 101.0, "Edit", {"file_path": "a.py", "old_string": "x\n",
                                   "new_string": "x\ny\nz\n"}),
          _call(3, 102.0, "Task"),
          _done(4, 160.0, usage={"input_tokens": 1000, "output_tokens": 500})]
    hero = h.fold_events("a", ev)
    m = hero.last_mission
    assert m is not None and hero.state == h.IDLE
    assert (m.lines_added, m.lines_removed, m.subagents, m.tokens) == (3, 1, 1, 1500)
    assert m.duration == 60.0 and m.files == ["a.py"] and m.status == "complete"
    assert m.short == "add oauth login" and hero.activity == m.name
    assert h.quest_name("add oauth login", "a", 1) == m.name       # deterministic


def test_summarizer_hook_is_used_and_a_failing_one_cannot_break_the_roster():
    ev = [_start(1, 1.0, "ship it now"), _done(2, 2.0)]
    ok = h.fold_events("a", ev, summarizer=lambda p: ("Auth wired", "Added OAuth."))
    assert (ok.last_mission.short, ok.last_mission.long) == ("Auth wired", "Added OAuth.")

    def boom(_):
        raise RuntimeError("model down")

    assert h.fold_events("a", ev, summarizer=boom).last_mission.short == "ship it now"


def _hero(hid, state):
    return h.Hero(id=hid, state=state)


def test_roster_priority_slots_and_cycles():
    roster = h.Roster([_hero("i1", h.IDLE), _hero("w1", h.WORKING),
                       _hero("a1", h.AWAITING), _hero("e1", h.ERROR),
                       _hero("i2", h.IDLE), _hero("a2", h.AWAITING)])
    assert [x.id for x in roster.heroes] == ["a1", "a2", "e1", "w1", "i1", "i2"]
    assert roster.select_slot(1).id == "a1" and roster.select_slot(9) is None
    assert roster.select_slot(0) is None
    # Space: awaiting first, then idle, wrapping
    seen, cur = [], None
    for _ in range(5):
        cur = roster.cycle("idle_or_awaiting", cur).id
        seen.append(cur)
    assert seen == ["a1", "a2", "i1", "i2", "a1"]
    assert roster.cycle("idle", "i1").id == "i2"
    assert roster.cycle(h.WORKING).id == "w1"
    assert roster.first(h.ERROR).id == "e1" and roster.first(h.AWAITING).id == "a1"
    assert h.Roster([_hero("x", h.IDLE)]).cycle(h.ERROR) is None
    with pytest.raises(ValueError):
        roster.cycle("nonsense")


def test_subagent_tab_cycle():
    hero = _hero("a", h.WORKING)
    hero.subagents = 3
    roster = h.Roster([hero, _hero("b", h.IDLE)])
    assert [roster.cycle_subagent("a", c) for c in (-1, 0, 1, 2)] == [0, 1, 2, 0]
    assert roster.cycle_subagent("b") == -1


def test_control_groups_assign_add_recall_and_persist(tmp_path: Path):
    path = tmp_path / "groups.json"
    groups = h.ControlGroups(path)
    groups.assign(3, ["a", "b"])
    groups.add(3, ["b", "c"])
    roster = h.Roster([_hero("a", h.IDLE), _hero("c", h.IDLE)])
    assert groups.recall(3) == ["a", "b", "c"]
    assert groups.recall(3, roster) == ["a", "c"]           # b is gone
    assert h.ControlGroups(path).recall(3) == ["a", "b", "c"]   # survived a restart
    with pytest.raises(ValueError):
        groups.assign(10, ["a"])
    path.write_text("{ not json", encoding="utf-8")
    assert h.ControlGroups(path).recall(3) == []


def _mission(hid, n, ended, status="complete"):
    return h.Mission(hero_id=hid, number=n, started=ended - 1, ended=ended, status=status)


def test_mission_log_caps_global_and_per_hero_and_persists(tmp_path: Path):
    log = h.MissionLog(tmp_path / "m.json", global_cap=5, per_hero_cap=2)
    for n in range(4):
        log.record(_mission("a", n, 100 + n))
    assert [r["number"] for r in log.recent(10, "a")] == [3, 2]       # per-hero cap
    for n in range(6):
        log.record(_mission(f"h{n}", 0, 200 + n))
    assert len(log.rows) == 5                                         # global cap
    log.record(_mission("z", 0, 999, status="active"))                # active not kept
    assert all(r["hero_id"] != "z" for r in log.rows)
    assert len(h.MissionLog(tmp_path / "m.json").rows) == 5


def test_achievements_fire_once_and_the_meta_one_settles():
    stats = {"heroes_spawned": 1, "missions_complete": 1}
    first = {a.id for a in h.evaluate(stats)}
    assert {"first-hero", "first-mission"} <= first
    assert not any(a.id in first for a in h.evaluate(stats, unlocked=first))
    big = {a.metric: a.at_least for a in h.ACHIEVEMENTS if a.metric != "achievements_unlocked"}
    assert "completionist" in {a.id for a in h.evaluate(big)}


def test_stats_from_heroes_feeds_the_catalogue():
    ev = [_start(1, 10.0), _done(2, 20.0, usage={"input_tokens": 200_000})]
    hero = h.fold_events("a", ev, {"harness": "claude"})
    stats = h.stats_from([hero], {"approvals_answered": 2})
    assert stats["missions_complete"] == 1 and stats["fastest_mission_s_inv"] == 1
    ids = {a.id for a in h.evaluate(stats)}
    assert {"speed-run", "token-whale", "first-approval"} <= ids


def test_gallery_masks_locked_secrets_only_and_card_is_valid_svg():
    rows = {r["id"]: r for r in h.gallery(["first-hero"])}
    assert rows["night-owl"]["title"] == "???"
    assert rows["night-owl"]["description"].startswith("Keep")
    assert rows["war-council"]["title"] == "War Council"        # locked, not secret: readable
    assert rows["first-hero"]["unlocked"] is True
    assert all(r["category"] == "secret" for r in h.gallery([], "secret"))
    ach = next(a for a in h.ACHIEVEMENTS if a.id == "first-hero")
    svg = h.trophy_card(ach, 1)
    assert svg.startswith("<svg") and 'width="600" height="315"' in svg
    import xml.dom.minidom
    xml.dom.minidom.parseString(svg)


def test_load_session_reads_the_daemon_files(tmp_path: Path):
    d = tmp_path / "sess-9"
    d.mkdir()
    rows = [_start(1, 5.0, "p"), _call(2, 6.0, "Bash", {"command": "pytest -q"})]
    (d / "events.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n",
                                    encoding="utf-8")
    (d / "session.json").write_text(json.dumps({"config": {"harness": "codex", "model": "x"}}),
                                    encoding="utf-8")
    hero = h.load_session(d)
    assert (hero.id, hero.harness, hero.state) == ("sess-9", "codex", h.WORKING)
    assert hero.ticker == "Running pytest -q"
    assert h._main(["--sessions-root", str(tmp_path)]) == 0
    assert h._main(["--sessions-root", str(tmp_path / "nope")]) == 2


def _worker(hid, cwd, calls, state_events=()):
    return h.fold_events(hid, [_start(1, 100.0), *calls, *state_events], {"cwd": cwd})


def test_war_table_heatmap_fog_and_intent_conflicts():
    a = _worker("a", "/repo/api", [_call(2, 110.0, "Edit", {"file_path": "/repo/api/auth.py"}),
                                   _call(3, 111.0, "Read", {"file_path": "/repo/api/db.py"})])
    b = _worker("b", "/repo/web", [_call(2, 50.0, "Read", {"file_path": "/repo/web/app.tsx"})],
                [_done(9, 60.0)])                         # finished: idle
    table = h.file_activity([a, b])
    assert table["/repo/api/auth.py"]["writers"] == ["a"]
    assert table["/repo/api/db.py"]["writers"] == []
    assert h.file_activity([a, b], since=100.0).keys() == {"/repo/api/auth.py",
                                                           "/repo/api/db.py"}
    heat = h.territory_heatmap([a, b], now=120.0)
    assert heat["repo/api"] == 1.0 and 0 < heat["repo/web"] < heat["repo/api"]
    assert h.territory_heatmap([], now=0.0) == {}
    fog = h.fog_of_war([a, b], ["repo/api", "repo/web", "repo/docs"])
    assert fog == {"repo/api": True, "repo/web": False, "repo/docs": False}
    warn = h.intent_conflicts([a, b], [{"hero": "r1", "owner": "sam", "path": "/repo/api/auth.py"},
                                       {"hero": "r2", "owner": "sam", "path": "/repo/api/db.py"}])
    assert [w["path"] for w in warn] == ["/repo/api/auth.py"]
    assert warn[0]["blocking"] is False and warn[0]["local"] == ["a"]
    assert a.to_dict()["state"] == "working"


def test_hotkey_table_and_tutorial_gating():
    keys = {r["key"] for r in h.shortcut_help()}
    assert {"Space", "Ctrl+1-9", "Alt+1-9", "V", "H", "F9", "Y", "N", "?"} <= keys
    assert {r["group"] for r in h.shortcut_help("groups")} == {"groups"}
    assert len(h.TUTORIAL) == 6
    tour = h.Tutorial()
    assert tour.step["id"] == "welcome" and tour.advance("next")
    assert not tour.advance("next")            # spawn step waits for the real action
    assert not tour.advance("prompt") and tour.advance("spawn")
    assert tour.advance("prompt") and tour.advance("next") and tour.advance("next")
    assert tour.advance("next") and tour.finished and tour.step is None
    assert not tour.advance("next")
    assert h.Tutorial([t["id"] for t in h.TUTORIAL]).finished
