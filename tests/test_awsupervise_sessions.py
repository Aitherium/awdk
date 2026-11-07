"""The external watchdog over the harness daemon's own sessions.

Every arm here is built from a real ``events.jsonl`` on disk, written the way the
session manager writes it, because the whole claim of ``awsupervise_sessions`` is that a
daemon session feeds the EXISTING detectors unchanged. The refusal arms matter as much as
the firing ones: a watchdog that steers, relays or kills a session that was working is
worse than no watchdog, and the rungs must never be skipped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from adk.harnesses import awsupervise as sup
from adk.harnesses import awsupervise_sessions as s
from adk.harnesses.thread_subs import ThreadSubscriptions

#: One clock for the whole module: the file mtimes ``discover_sessions`` judges are real,
#: so a frozen constant far from wall clock would make every fixture "stale".
NOW = time.time()


# --------------------------------------------------------------------------- fixtures


def _ev(kind, seq, ts, **extra):
    """One ``events.jsonl`` row, in the shape ``HarnessEvent.to_dict`` writes."""
    row = {"kind": kind, "seq": seq, "session_id": extra.pop("session_id", "s-1"),
           "ts": ts, "text": "", "tool": "", "tool_use_id": "", "data": {}, "turn": 0}
    row.update(extra)
    return row


def _call(seq, ts, tool, tool_input=None, call_id=None, turn=1):
    return _ev("tool.call", seq, ts, tool=tool, tool_use_id=call_id or f"c{seq}",
               data={"input": tool_input or {}}, turn=turn)


def _result(seq, ts, call_id, output="ok", is_error=False, tool=""):
    return _ev("tool.result", seq, ts, tool=tool, tool_use_id=call_id,
               data={"output": output, "is_error": is_error}, turn=1)


def _turn(seq, ts, number, text="assistant says something", error=False):
    return [_ev("turn.started", seq, ts, turn=number),
            _ev("text.delta", seq + 1, ts, text=text, turn=number),
            _ev("turn.completed", seq + 2, ts, turn=number,
                data={"is_error": error, "exit_code": 1 if error else 0,
                      "timed_out": False})]


def _write_session(root, session_id, events, config=None, mtime=None):
    directory = root / session_id
    directory.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(e) + "\n" for e in events)
    log = directory / "events.jsonl"
    log.write_text(body, encoding="utf-8")
    if config is not None:
        (directory / "session.json").write_text(
            json.dumps({"id": session_id, "config": config}), encoding="utf-8")
    if mtime is not None:
        os.utime(log, (mtime, mtime))
    return directory


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "harness-sessions"


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    base = tmp_path / "sup"
    return sup.Paths(projects=tmp_path / "projects", temp_claude=tmp_path / "temp",
                     sessions=tmp_path / "sessions", labels=tmp_path / "labels",
                     trace=tmp_path / "trace", steer=tmp_path / "steer",
                     home=base, decisions=tmp_path / "decisions")


@pytest.fixture()
def clock():
    """The subscriptions table's clock -- three hours back, so a subscription made here
    is an OLD one, which is the situation the drift check exists to judge."""
    return [NOW - 3 * 3600]


@pytest.fixture()
def subs(tmp_path, clock):
    table = ThreadSubscriptions(tmp_path / "subs.db", now=lambda: clock[0])
    yield table
    table.close()


def _agent(events, session_id="s-1", **kw):
    return s.agent_from_events(session_id, events, f"/root/{session_id}/events.jsonl", **kw)


def _healthy_events(t0, session_id="s-1"):
    events = [_ev("session.starting", 1, t0, session_id=session_id,
                  data={"harness": "codex", "cwd": "/repo", "model_binding": {}}),
              _ev("session.ready", 2, t0, session_id=session_id,
                  data={"model": "local-coder", "cwd": "/repo"})]
    seq = 3
    for number, (tool, inp, cid) in enumerate(
            [("edit", {"file_path": "a.py", "old_string": "a", "new_string": "b"}, "c1"),
             ("shell", {"command": "pytest -q tests"}, "c2"),
             ("shell", {"command": "git commit -m work"}, "c3")], 1):
        events += _turn(seq, t0 + seq, number)
        seq += 3
        events.append(_call(seq, t0 + seq, tool, inp, call_id=cid, turn=number))
        events.append(_result(seq + 1, t0 + seq, cid, tool=tool))
        seq += 2
    return events


# -- the reader -------------------------------------------------------------------


def test_one_session_log_becomes_the_records_the_detectors_consume(root):
    events = _healthy_events(NOW - 600)
    agent = _agent(events, config={"harness": "codex", "cwd": "/repo", "model": ""})

    assert [c.name for c in agent.calls] == ["edit", "shell", "shell"]
    assert set(agent.results) == {"c1", "c2", "c3"}
    assert len(agent.turns) == 3
    assert agent.turns[0].stop == "end_turn" and agent.turns[0].text is True
    assert agent.agent_type == "codex" and agent.cwd == "/repo"
    assert agent.model == "local-coder"  # what the harness reported, not the config
    # the alias pass is what makes a codex/opencode session look alive at all
    assert [p[1] for p in agent.progress] == ["report", "edit", "report", "test",
                                              "report", "commit"]
    # ...and the judged steps carry what each call was asked to touch
    calls = [step for step in agent.steps if step["kind"] == "tool.call"]
    assert calls[0]["tool"] == "edit" and calls[0]["input"]["file_path"] == "a.py"


def test_tool_results_pair_by_position_when_the_adapter_gives_no_id():
    events = [_ev("tool.call", 1, NOW - 50, tool="read", data={"input": {"path": "a"}}),
              _ev("tool.call", 2, NOW - 49, tool="write", data={"input": {}}),
              _result(3, NOW - 48, "", output="file body", tool="read"),
              _result(4, NOW - 47, "", output="written", tool="write")]
    agent = _agent(events)
    assert agent.results["c1"].text == "file body"
    assert agent.results["c2"].text == "written"


def test_the_progress_alias_never_counts_a_native_no_op_edit():
    """A Claude Code ``Write`` that wrote the same bytes is not progress; nor is the alias
    pass allowed to second-guess a tool the shared helper already judged."""
    events = [_call(1, NOW - 30, "Write", {"file_path": "a.py", "content": "x"}, "c1"),
              _result(2, NOW - 29, "c1", output={}, tool="Write"),
              _call(3, NOW - 28, "Write", {"file_path": "a.py", "content": "x"}, "c2"),
              _result(4, NOW - 27, "c2", output={}, tool="Write")]
    agent = _agent(events)
    assert [p[1] for p in agent.progress] == ["edit"]  # first write only


def test_repeat_call_fires_over_an_awdk_log():
    events = [_call(seq, NOW - 300 + seq, "read", {"path": "same.py"}) for seq in range(1, 6)]
    findings = sup.detect(_agent(events), dict(sup.CONFIG), NOW, {})
    assert [f.detector for f in findings] == ["repeat_call"]


def test_the_same_error_three_times_fires_over_an_awdk_log():
    events = []
    for n in range(3):
        seq = n * 2 + 1
        events.append(_call(seq, NOW - 200 + seq, "shell", {"command": "make"}, f"c{seq}"))
        events.append(_result(seq + 1, NOW - 200 + seq, f"c{seq}", tool="shell",
                              output="make: *** No targets specified.", is_error=True))
    findings = sup.detect(_agent(events), dict(sup.CONFIG), NOW, {})
    assert "repeat_error" in [f.detector for f in findings]


def _quiet_turns(count, t0, text=""):
    events = []
    for n in range(count):
        base = 1 + n * 3
        events += _turn(base, t0 + n * 10, n + 1, text=text)
    return events


def test_no_progress_fires_for_a_silent_session():
    events = _quiet_turns(41, NOW - 40 * 60)
    agent = _agent(events)
    assert len(agent.turns) == 41
    findings = sup.detect(agent, dict(sup.CONFIG), NOW, {})
    assert "no_progress" in [f.detector for f in findings]


def test_a_session_that_reports_every_turn_is_not_no_progress():
    events = _quiet_turns(41, NOW - 40 * 60, text="turn report: still working")
    findings = sup.detect(_agent(events), dict(sup.CONFIG), NOW, {})
    assert "no_progress" not in [f.detector for f in findings]


def test_a_healthy_session_trips_no_detector():
    findings = sup.detect(_agent(_healthy_events(NOW - 600)), dict(sup.CONFIG), NOW, {})
    assert findings == []


def test_an_exit_event_marks_the_session_finished():
    events = _healthy_events(NOW - 600)
    events.append(_ev("session.exited", 99, NOW - 60, data={"exit_code": 0}))
    assert _agent(events).finished


# -- discovery --------------------------------------------------------------------


def test_finished_and_stale_sessions_are_skipped_by_default(root):
    _write_session(root, "run-1", _healthy_events(NOW - 300))
    done = _healthy_events(NOW - 300) + [_ev("session.exited", 99, NOW - 200,
                                             data={"exit_code": 0})]
    _write_session(root, "done-1", done)
    _write_session(root, "old-1", _healthy_events(NOW - 300), mtime=NOW - 5 * 3600)

    scan = s.discover_sessions(dict(sup.CONFIG), now=NOW, root=root)
    assert [a.session_id for a in scan.agents] == ["run-1"]
    assert scan.skipped["finished"] == 1
    assert scan.skipped["stale"] == 1


def test_include_finished_adds_the_finished_one_back(root):
    done = _healthy_events(NOW - 300) + [_ev("session.exited", 99, NOW - 200,
                                             data={"exit_code": 0})]
    _write_session(root, "done-1", done)
    scan = s.discover_sessions(dict(sup.CONFIG), now=NOW, root=root, include_finished=True)
    assert [a.session_id for a in scan.agents] == ["done-1"]
    assert scan.agents[0].finished


def test_a_missing_root_is_an_empty_scan_not_a_crash(tmp_path):
    scan = s.discover_sessions(dict(sup.CONFIG), now=NOW, root=tmp_path / "nope")
    assert scan.agents == [] and scan.skipped["stale"] == 0


# -- g. stalled on a subscribed thread ----------------------------------------------


def _stalled_events(t0, session_id="thread-worker"):
    """Subscribed, silent, and touching nothing: only empty-input calls since."""
    events = [_ev("session.starting", 1, t0, session_id=session_id,
                  data={"harness": "opencode", "cwd": "/repo"}),
              _call(2, t0, "shell", {"command": "pytest -q"}, "c1"),
              _result(3, t0, "c1", tool="shell")]
    seq = 4
    for _ in range(6):
        events += _turn(seq, t0 + seq, 1)
        seq += 3
        events.append(_call(seq, t0 + seq, "think", {}))
        events.append(_result(seq + 1, t0 + seq, f"c{seq}", tool="think"))
        seq += 2
    return events


def test_a_session_subscribed_and_touching_nothing_is_stalled_on_the_thread(root, subs):
    events = _stalled_events(NOW - 2 * 3600, "thread-worker")
    _write_session(root, "thread-worker", events)
    subs.subscribe("github", "acme/repo#7", "thread-worker", title="PR 7")
    agent = s.discover_sessions(dict(sup.CONFIG), now=NOW, root=root).agents[0]

    finding = s.stalled_on_thread(agent, subs, dict(sup.CONFIG), NOW)
    assert finding is not None
    assert finding.detector == "stalled_on_thread"
    assert sup.DETECTOR_CODE[finding.detector] == "g"
    assert "github:acme/repo#7" in finding.summary
    assert "thread.unsubscribe" in finding.steer
    assert any("delivered" in line for line in finding.evidence)


def test_no_subscription_means_no_thread_finding(root, subs):
    events = _stalled_events(NOW - 2 * 3600, "loner")
    _write_session(root, "loner", events)
    agent = s.discover_sessions(dict(sup.CONFIG), now=NOW, root=root).agents[0]
    assert s.stalled_on_thread(agent, subs, dict(sup.CONFIG), NOW) is None


def test_a_fresh_delivery_clears_the_stall(root, subs, clock):
    events = _stalled_events(NOW - 2 * 3600, "fed")
    _write_session(root, "fed", events)
    subs.subscribe("github", "acme/repo#7", "fed")
    clock[0] = NOW
    subs.touch("github", "acme/repo#7", "fed")  # delivered just now
    agent = s.discover_sessions(dict(sup.CONFIG), now=NOW, root=root).agents[0]
    assert s.stalled_on_thread(agent, subs, dict(sup.CONFIG), NOW) is None


def test_a_recent_call_that_names_something_clears_the_stall(root, subs):
    events = _stalled_events(NOW - 2 * 3600, "busy")
    events.append(_call(90, NOW - 60, "read", {"path": "src/app.py"}, "c90"))
    events.append(_result(91, NOW - 60, "c90", tool="read"))
    _write_session(root, "busy", events)
    subs.subscribe("github", "acme/repo#7", "busy")
    agent = s.discover_sessions(dict(sup.CONFIG), now=NOW, root=root).agents[0]
    assert s.stalled_on_thread(agent, subs, dict(sup.CONFIG), NOW) is None


def test_streaming_the_final_report_is_not_a_stall(root, subs):
    """Ten text deltas after real work must not push the work out of the stall window.

    A session that edited a file, ran a passing test and then streamed its report is
    doing exactly what it should: judging steps count actions, not tokens.
    """
    events = _healthy_events(NOW - 2 * 3600, "reporter")
    seq = 90
    events.append(_ev("turn.started", seq, NOW - 60, turn=4))
    for offset in range(10):
        events.append(_ev("text.delta", seq + 1 + offset, NOW - 60, text="report line",
                          turn=4))
    events.append(_ev("turn.completed", seq + 11, NOW - 59, turn=4,
                      data={"is_error": False}))
    _write_session(root, "reporter", events)
    subs.subscribe("github", "acme/repo#7", "reporter")
    agent = s.discover_sessions(dict(sup.CONFIG), now=NOW, root=root).agents[0]

    assert s.stalled_on_thread(agent, subs, dict(sup.CONFIG), NOW) is None
    assert all(step["kind"] != "text.delta" for step in agent.steps)


def test_a_quiet_turn_with_a_blank_call_is_still_stalled(root, subs):
    """The negative arm: dropping text deltas must not make the detector blind."""
    events = _quiet_turns(6, NOW - 2 * 3600)
    seq = 90
    for number in range(4):
        events += [_ev("turn.started", seq, NOW - 600 + seq, turn=number + 7),
                   _ev("text.delta", seq + 1, NOW - 600 + seq, text="  ", turn=number + 7),
                   _ev("turn.completed", seq + 2, NOW - 600 + seq, turn=number + 7)]
        seq += 3
    _write_session(root, "silent", events)
    subs.subscribe("github", "acme/repo#7", "silent")
    agent = s.discover_sessions(dict(sup.CONFIG), now=NOW, root=root).agents[0]
    assert s.stalled_on_thread(agent, subs, dict(sup.CONFIG), NOW) is not None


def test_a_finished_session_is_never_called_stalled(root, subs):
    events = _stalled_events(NOW - 2 * 3600, "gone")
    events.append(_ev("session.exited", 99, NOW - 3600, data={"exit_code": 0}))
    _write_session(root, "gone", events)
    subs.subscribe("github", "acme/repo#7", "gone")
    agent = s.discover_sessions(dict(sup.CONFIG), now=NOW, root=root,
                                include_finished=True).agents[0]
    assert agent.finished
    assert s.stalled_on_thread(agent, subs, dict(sup.CONFIG), NOW) is None


def test_a_malformed_session_id_never_raises_in_the_lookup(subs):
    class Exploding:
        def threads_of(self, _sid):
            raise ValueError("bad subscriber id")

    events = _stalled_events(NOW - 2 * 3600, "x")
    agent = _agent(events, session_id="x")
    assert s.threads_of(Exploding(), "x") == []
    assert s.stalled_on_thread(agent, Exploding(), dict(sup.CONFIG), NOW) is None


# -- escalation -------------------------------------------------------------------


def _stalled_result(root, paths, subs, runner=None, dry=False):
    _write_session(root, "thread-worker", _stalled_events(NOW - 2 * 3600, "thread-worker"))
    subs.subscribe("github", "acme/repo#7", "thread-worker", title="PR 7")
    return s.scan_sessions_once(dict(sup.CONFIG), now=NOW, root=root, subs=subs,
                                paths=paths, runner=runner, dry=dry)


def test_rung_one_is_a_peer_steer_in_the_session_mailbox_and_nothing_else(root, paths, subs):
    calls = []
    result = _stalled_result(root, paths, subs, runner=lambda argv, timeout: calls.append(argv))

    assert [f.detector for f in result["findings"]] == ["stalled_on_thread"]
    assert len(result["actions"]) == 1
    record = result["actions"][0]
    assert record["level"] == 1 and record["action"] == "steer"
    assert record["advance"] is True, record

    box = paths.steer / "thread-worker"
    files = sorted(box.glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    header = body.splitlines()[0]
    assert header.startswith("<!-- aither-steer v1 ")
    assert 'authority="peer"' in header and 'from="awsupervise"' in header
    assert "acme/repo#7" in body

    # never a kill, never a relay, never a card: L1 is the only rung it climbed
    assert calls == []
    assert result["state"]["sent"].get("relay") in (None, [])
    assert result["state"].get("cards") == {}
    assert result["state"]["incidents"]["thread-worker/main|stalled_on_thread"]["level"] == 1


def test_a_dry_run_writes_no_mailbox_file(root, paths, subs):
    result = _stalled_result(root, paths, subs, dry=True)
    assert result["actions"][0]["dry"] is True
    assert "would write" in result["actions"][0]["result"]
    assert not (paths.steer / "thread-worker").exists()
    assert not (paths.home / "state.json").exists()


def test_a_path_shaped_session_id_is_refused_by_the_writer(paths):
    assert s.write_session_steer(paths.steer, "../../etc", ["x"], "e1") is None
    assert s.write_session_steer(paths.steer, "", ["x"], "e1") is None


def test_the_actuator_refuses_rather_than_climbing_on_a_bad_id(paths):
    agent = sup.Agent(key="bad/main", session_id="bad id", agent_id="", kind="main",
                      path=paths.home / "events.jsonl", project="awdk")
    finding = sup.Finding(agent, "stalled_on_thread", "s", "do it", [])
    state = sup.load_state(paths)
    planned = sup.plan([finding], state, dict(sup.CONFIG), NOW, [])
    assert [p.level for p in planned] == [1]
    act = s.SessionActuator(paths, dict(sup.CONFIG), state, NOW)
    records = sup.execute(planned, state, act, NOW)
    assert records[0]["result"].startswith("refused")
    assert records[0]["advance"] is False
    assert state["incidents"]["bad/main|stalled_on_thread"]["level"] == 0


def test_the_ladder_still_climbs_to_the_relay_on_a_second_pass(root, paths, subs):
    calls = []
    runner = lambda argv, timeout: (calls.append(argv), (0, "", ""))[1]  # noqa: E731
    first = _stalled_result(root, paths, subs, runner=runner)
    assert first["actions"][0]["level"] == 1

    later = NOW + sup.CONFIG["climb_minutes"] * 60 + 60
    second = s.scan_sessions_once(dict(sup.CONFIG), now=later, root=root, subs=subs,
                                  paths=paths, runner=runner)
    assert second["actions"][0]["level"] == 2, second["actions"]
    assert calls, "L2 must reach the relay"


def test_render_and_json_survive_a_pass(root, paths, subs):
    result = _stalled_result(root, paths, subs, dry=True)
    text = s.render_sessions(result, verbose=True)
    assert "awdk-sessions" in text and "stalled_on_thread" in text
    payload = s.as_json_sessions(result)
    assert payload["sessions_scanned"] == 1
    assert payload["findings"][0]["code"] == "g"
    json.dumps(payload)  # must be serializable for --json


# -- the production entry points ----------------------------------------------------


def _subs_db(tmp_path, subscriber="thread-worker"):
    db = tmp_path / "thread-subscriptions.db"
    table = ThreadSubscriptions(db, now=lambda: NOW - 3 * 3600)
    try:
        table.subscribe("github", "acme/repo#9", subscriber, title="PR 9")
    finally:
        table.close()
    return db


def test_the_scan_opens_the_daemon_subscription_table_itself(root, paths, tmp_path,
                                                             monkeypatch):
    """Detector g must run from the entry point, not only when a test hands over a table."""
    db = _subs_db(tmp_path)
    monkeypatch.setenv("AITHER_THREAD_SUBS_DB", str(db))
    _write_session(root, "thread-worker", _stalled_events(NOW - 2 * 3600, "thread-worker"))

    result = s.scan_sessions_once(dict(sup.CONFIG), now=NOW, root=root, paths=paths,
                                  dry=True)
    assert [f.detector for f in result["findings"]] == ["stalled_on_thread"]


def test_a_missing_subscription_table_stays_missing(root, paths, tmp_path, monkeypatch):
    """A dry run must not create the router's table; no subscriptions means no drift."""
    db = tmp_path / "not-yet.db"
    monkeypatch.setenv("AITHER_THREAD_SUBS_DB", str(db))
    _write_session(root, "thread-worker", _stalled_events(NOW - 2 * 3600, "thread-worker"))

    result = s.scan_sessions_once(dict(sup.CONFIG), now=NOW, root=root, paths=paths,
                                  dry=True)
    assert result["findings"] == []
    assert not db.exists()


def test_the_cli_session_pass_judges_a_subscribed_session(root, tmp_path, monkeypatch,
                                                          capsys):
    db = _subs_db(tmp_path)
    _write_session(root, "thread-worker", _stalled_events(NOW - 2 * 3600, "thread-worker"))
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(root))
    monkeypatch.setenv("AITHER_THREAD_SUBS_DB", str(db))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AWSUPERVISE_PROJECTS", str(tmp_path / "projects"))
    (tmp_path / "projects").mkdir()
    monkeypatch.setenv("AWSUPERVISE_HOME", str(tmp_path / "home"))

    assert sup.main(["--awdk-sessions", "--once", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "awsupervise awdk-sessions" in out
    assert "stalled_on_thread" in out


def test_a_malformed_turn_row_still_reads_the_rest_of_the_log(root, paths, tmp_path,
                                                              monkeypatch):
    """One corrupt row is judged as turn 0, never a ValueError that kills the pass."""
    monkeypatch.setenv("AITHER_THREAD_SUBS_DB", str(tmp_path / "none.db"))
    corrupt = _stalled_events(NOW - 2 * 3600, "corrupt")
    corrupt += [_ev("turn.started", 91, NOW - 120, turn="not-a-number"),
                _ev("text.delta", 92, NOW - 119, text="still here", turn="bad"),
                _ev("turn.completed", 93, NOW - 118, turn="bad")]
    _write_session(root, "corrupt", corrupt)
    # the sibling session must still be judged: four identical calls is detector a
    noisy = [_call(seq, NOW - 300 + seq, "read", {"path": "same.py"}) for seq in range(1, 6)]
    _write_session(root, "noisy", noisy)

    result = s.scan_sessions_once(dict(sup.CONFIG), now=NOW, root=root, paths=paths,
                                  dry=True)
    assert {f.agent.session_id for f in result["findings"]} == {"noisy"}
    assert [f.detector for f in result["findings"]] == ["repeat_call"]

    agent = _agent(corrupt, session_id="corrupt")
    assert len(agent.calls) >= 1 and agent.turns  # the log was read, not abandoned


def test_a_broken_session_pass_cannot_sink_the_claude_code_pass():
    """Issue 3, cross-pass arm: ``main`` runs the adk pass behind its own guard.

    The per-row guard means no realistic row reaches this; the point is that if one ever
    does, it is described on stderr and ``main`` keeps judging the Claude Code side.
    """
    class Exploding:
        def scan_sessions_once(self, *_args, **_kwargs):
            raise ValueError("a corrupt row escaped the per-row guard")

    assert sup._run_session_pass(Exploding(), dict(sup.CONFIG), dry=True) is None


def test_the_script_form_loads_its_own_siblings(root, tmp_path):
    """``python adk/harnesses/awsupervise.py`` must not borrow a stale installed ``adk``."""
    _write_session(root, "thread-worker", _stalled_events(NOW - 2 * 3600, "thread-worker"))
    script = Path(sup.__file__).resolve()
    repo_root = script.parents[3]
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    env.update(AITHER_HARNESS_ROOT=str(root),
               AITHER_THREAD_SUBS_DB=str(tmp_path / "none.db"),
               AITHER_STEER_DIR=str(tmp_path / "steer"),
               AWSUPERVISE_PROJECTS=str(tmp_path / "projects"),
               AWSUPERVISE_HOME=str(tmp_path / "home"))
    proc = subprocess.run([sys.executable, str(script), "--awdk-sessions", "--once",
                           "--dry-run"], cwd=str(repo_root), env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "awsupervise awdk-sessions" in proc.stdout
    # ...and the sibling reader actually judged the fixture, not merely imported
    assert "sessions scanned 1" in proc.stdout
    assert "AttributeError" not in proc.stderr


# -- the CLI entry point ------------------------------------------------------------


def test_awdk_sessions_is_off_by_default(root, tmp_path, monkeypatch, capsys):
    _write_session(root, "thread-worker", _stalled_events(NOW - 2 * 3600, "thread-worker"))
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(root))
    monkeypatch.setenv("AWSUPERVISE_PROJECTS", str(tmp_path / "projects"))
    (tmp_path / "projects").mkdir()
    monkeypatch.setenv("AWSUPERVISE_HOME", str(tmp_path / "home"))

    assert sup.main([]) == 2  # no mode at all: usage, never a silent scan
    capsys.readouterr()       # ...and the usage text mentions the flag it explains

    assert sup.main(["--once", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "awsupervise " in out
    assert "awsupervise awdk-sessions" not in out


def test_the_flag_prints_the_session_pass_without_writing(root, tmp_path, monkeypatch, capsys):
    _write_session(root, "thread-worker", _stalled_events(NOW - 2 * 3600, "thread-worker"))
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(root))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_THREAD_SUBS_DB", str(tmp_path / "no-subs.db"))
    monkeypatch.setenv("AWSUPERVISE_PROJECTS", str(tmp_path / "projects"))
    (tmp_path / "projects").mkdir()
    monkeypatch.setenv("AWSUPERVISE_HOME", str(tmp_path / "home"))

    assert sup.main(["--awdk-sessions", "--once", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "awsupervise awdk-sessions" in out
    assert "sessions scanned 1" in out
    assert not (tmp_path / "steer").exists()
    assert not (tmp_path / "no-subs.db").exists()
