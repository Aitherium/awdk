"""The flow toolpack: flow_run / flow_list / flow_resume over awflow's real journal.

The number that proves resume is real is how many calls reach the model. These
tests count them with a fake dispatcher (awflow's own test seam) and read the
journal awflow wrote -- they never mock awflow itself.
"""
from __future__ import annotations

import asyncio
import json
import textwrap

import pytest

pytest.importorskip("awflow")

from adk.toolpacks import flow as pack  # noqa: E402

SCRIPT = textwrap.dedent('''
    from awflow import agent

    async def main(topic="x", crash_after=None):
        first = await agent(f"outline {topic}", label="outline")
        if crash_after == 1:
            raise RuntimeError("killed after step 1")
        second = await agent(f"write {topic} from {first}", label="write")
        return [first, second]
''')


@pytest.fixture
def env(tmp_path, monkeypatch):
    calls: list[str] = []

    async def dispatch(prompt, *, model=None, schema=None, temperature=0.7, seed=None,
                       max_tokens=2048, effort=None):
        calls.append(prompt)
        return f"ans<{prompt}>", 5, None

    monkeypatch.setenv("AWFLOW_JOURNAL_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("AITHER_AWFLOW_MIRROR", "0")
    monkeypatch.setattr(pack, "_DISPATCHER", dispatch)
    script = tmp_path / "wf.py"
    script.write_text(SCRIPT, encoding="utf-8")
    return script, calls


def test_two_step_run_then_resume_replays_from_the_journal(env):
    script, calls = env
    first = asyncio.run(pack.flow_run(str(script), {"topic": "cats"}, 5000))
    assert first["ok"], first
    assert first["live_calls"] == 2 and first["status"] == "completed"
    assert first["result"] == ["ans<outline cats>", "ans<write cats from ans<outline cats>>"]
    assert len(calls) == 2

    second = asyncio.run(pack.flow_resume(first["run_id"]))
    assert second["ok"], second
    assert second["result"] == first["result"]
    assert second["replayable_calls"] == 2
    assert second["live_calls"] == 0, "resume re-asked a journaled call"
    assert len(calls) == 2, "the model was called again on resume"


def test_crashed_run_resumes_and_pays_only_for_the_missing_step(env):
    script, calls = env
    first = asyncio.run(pack.flow_run(str(script), {"topic": "dogs", "crash_after": 1}))
    assert not first["ok"] and "killed after step 1" in first["error"]
    assert first["status"] == "failed" and len(calls) == 1

    # Same run, the crash switch flipped off in its stored args.
    meta_path = pack.journal_root() / first["run_id"] / pack.META_FILE
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["args"]["crash_after"] = None
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    second = asyncio.run(pack.flow_resume(first["run_id"]))
    assert second["ok"], second
    assert second["replayable_calls"] == 1 and second["live_calls"] == 1
    assert calls == ["outline dogs", "write dogs from ans<outline dogs>"]


def test_list_shows_runs_and_status(env):
    script, _calls = env
    run = asyncio.run(pack.flow_run(str(script)))
    listed = pack.flow_list()
    assert listed["ok"] and listed["count"] == 1
    row = listed["runs"][0]
    assert row["run_id"] == run["run_id"] and row["status"] == "completed"
    assert row["agent_calls"] == 2 and row["resumable"] is True


def test_bad_inputs_refuse_before_running(env, tmp_path):
    script, calls = env
    assert not asyncio.run(pack.flow_run(str(tmp_path / "nope.py")))["ok"]
    assert not asyncio.run(pack.flow_run(str(script), budget=0))["ok"]
    assert not asyncio.run(pack.flow_run(str(script), args=["x"]))["ok"]
    assert not asyncio.run(pack.flow_resume("../etc"))["ok"]
    assert not asyncio.run(pack.flow_resume("no-such-run"))["ok"]
    assert calls == []


def test_missing_awflow_is_a_named_refusal(monkeypatch):
    monkeypatch.setattr(pack.importlib.util, "find_spec", lambda name: None)
    for res in (pack.flow_list(), asyncio.run(pack.flow_resume("x")),
                asyncio.run(pack.flow_run("x.py"))):
        assert res["ok"] is False and "awdk[flow]" in res["hint"]


def test_register_exposes_three_tools():
    from adk.tools import ToolRegistry

    reg = ToolRegistry()
    assert pack.register(reg) == 3
    assert {t.name for t in reg.list_tools()} == {"flow_run", "flow_list", "flow_resume"}
    assert reg.get("flow_run").is_async


def test_loader_discovers_the_pack():
    from adk.tool_pack_loader import ToolPackLoader

    assert "flow" in ToolPackLoader().discover()
