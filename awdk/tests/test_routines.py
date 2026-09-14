"""Tests for adk.routines — agent heartbeat + self-programmed routines,
plus the AitherAgent opt-in integration (routines / memory_maintenance flags).

Offline: fake fire callbacks, tmp-dir registries, injectable clocks.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from adk.routines import (
    DEFAULT_MAX_ROUTINES,
    Routine,
    RoutineStore,
    build_routine_tools,
    register_routine_tools,
    routine_tool_defs,
)

_TOOL_NAMES = {
    "routine_create", "routine_list", "routine_update", "routine_pause",
    "routine_resume", "routine_delete", "routine_run_now",
}


def _store(tmp_path, **kw):
    return RoutineStore("tester", path=tmp_path / "routines.json", **kw)


# ─── store round-trip / rehydration ─────────────────────────────────────────

def test_store_roundtrip(tmp_path):
    store = _store(tmp_path)
    r = store.create("daily-brief", "0 9 * * *", "Summarise the day", tags=["news"])
    assert r.enabled and r.created_at

    reloaded = _store(tmp_path)
    got = reloaded.get("daily-brief")
    assert got is not None
    assert got.cron == "0 9 * * *"
    assert got.instruction == "Summarise the day"
    assert got.tags == ["news"]
    assert got.enabled is True
    assert got.direct is False


def test_store_corrupt_file_tolerated(tmp_path):
    path = tmp_path / "routines.json"
    path.write_text("{not json!", encoding="utf-8")
    store = RoutineStore("tester", path=path)
    assert store.list() == []
    store.create("x", "0 9 * * *", "i")  # still writable
    assert RoutineStore("tester", path=path).get("x") is not None


def test_create_guards(tmp_path):
    store = _store(tmp_path, max_routines=2)
    store.create("a", "0 9 * * *", "i")
    store.create("b", "0 9 * * *", "i")
    with pytest.raises(ValueError):
        store.create("c", "0 9 * * *", "i")           # max_routines
    with pytest.raises(ValueError):
        store.create("a2", "not a cron", "i")         # bad cron
    with pytest.raises(ValueError):
        store.create("a", "0 9 * * *", "i")           # duplicate name
    with pytest.raises(ValueError):
        store.create("", "0 9 * * *", "i")            # empty name


def test_update_pause_resume_delete(tmp_path):
    store = _store(tmp_path)
    store.create("job", "0 9 * * *", "old")
    r = store.update("job", cron="30 8 * * *", instruction="new", tags=["t"])
    assert (r.cron, r.instruction, r.tags) == ("30 8 * * *", "new", ["t"])
    assert store.pause("job").enabled is False
    assert store.resume("job").enabled is True
    with pytest.raises(ValueError):
        store.update("nope", cron="0 9 * * *")
    with pytest.raises(ValueError):
        store.update("job", cron="99 99 * * *")
    assert store.delete("job") is True
    assert store.delete("job") is False


async def test_rehydrates_into_cron_scheduler(tmp_path):
    store = _store(tmp_path)
    store.create("hot", "0 9 * * *", "i")
    store.create("cold", "0 10 * * *", "i")
    store.pause("cold")

    reloaded = _store(tmp_path)
    await reloaded.start()
    try:
        names = {j.name for j in reloaded._scheduler.list_jobs()}
        assert names == {"hot"}  # only enabled routines are scheduled
        # resume while running → scheduled live
        reloaded.resume("cold")
        assert {j.name for j in reloaded._scheduler.list_jobs()} == {"hot", "cold"}
        # pause while running → unscheduled live
        reloaded.pause("hot")
        assert {j.name for j in reloaded._scheduler.list_jobs()} == {"cold"}
    finally:
        await reloaded.stop()


# ─── firing ──────────────────────────────────────────────────────────────────

async def test_fire_runs_agent_chat_with_instruction(tmp_path):
    calls = []

    async def fire(instruction):
        calls.append(instruction)
        return "done: " + instruction

    store = _store(tmp_path, fire=fire)
    store.create("job", "0 9 * * *", "hello future self")
    out = await store.run_now("job")
    assert calls == ["hello future self"]
    assert out == "done: hello future self"
    r = store.get("job")
    assert r.last_result == "done: hello future self"
    assert r.last_run is not None
    # ledger survives a reload
    assert _store(tmp_path).get("job").last_result == "done: hello future self"


async def test_min_interval_guard_on_scheduled_fires(tmp_path):
    now = [1_000_000.0]
    calls = []

    async def fire(instruction):
        calls.append(instruction)
        return "ok"

    store = _store(tmp_path, fire=fire, clock=lambda: now[0], min_interval_s=300)
    store.create("job", "* * * * *", "x")
    assert await store._fire("job") == "ok"
    assert await store._fire("job") == "skipped: min-interval guard"
    assert len(calls) == 1
    now[0] += 301
    assert await store._fire("job") == "ok"
    assert len(calls) == 2
    # run_now is an explicit ask — bypasses the interval guard
    assert await store.run_now("job") == "ok"
    assert len(calls) == 3


async def test_fire_timeout_is_ledgered(tmp_path):
    async def slow(instruction):
        await asyncio.sleep(5)
        return "too late"

    store = _store(tmp_path, fire=slow, fire_timeout_s=0.05)
    store.create("job", "0 9 * * *", "x")
    out = await store.run_now("job")
    assert out.startswith("timeout")
    assert store.get("job").last_result.startswith("timeout")


async def test_fire_error_is_ledgered_and_truncated(tmp_path):
    async def boom(instruction):
        raise RuntimeError("kaboom " + "x" * 5000)

    store = _store(tmp_path, fire=boom, result_max_chars=50)
    store.create("job", "0 9 * * *", "x")
    out = await store.run_now("job")
    assert out.startswith("error: kaboom")
    assert len(store.get("job").last_result) <= 50


async def test_fire_guards_missing_and_paused(tmp_path):
    store = _store(tmp_path, fire=lambda i: "ok")
    assert (await store._fire("ghost")).startswith("skipped: unknown")
    store.create("job", "0 9 * * *", "x")
    store.pause("job")
    assert await store._fire("job") == "skipped: routine is paused"


async def test_direct_routine_bypasses_self_prompt(tmp_path):
    prompted = []
    fired = []

    async def fire(instruction):
        prompted.append(instruction)
        return "chat"

    store = _store(tmp_path, fire=fire)
    store.register_direct(
        "maint", lambda: fired.append(1) or "direct-ok",
        cron="0 */2 * * *", instruction="maintenance", tags=["memory"],
    )
    out = await store.run_now("maint")
    assert out == "direct-ok"
    assert fired == [1] and prompted == []
    assert store.get("maint").direct is True
    # rehydrated direct routines keep user-edited state; callable re-attaches
    store2 = _store(tmp_path, fire=fire)
    store2.update("maint", cron="0 */4 * * *")
    r = store2.register_direct(
        "maint", lambda: "direct-again", cron="0 */2 * * *",
        instruction="maintenance",
    )
    assert r.cron == "0 */4 * * *"  # persisted user edit wins over the default


# ─── self-management tools ───────────────────────────────────────────────────

def test_tool_defs_are_openai_format():
    defs = routine_tool_defs()
    names = {d["function"]["name"] for d in defs}
    assert names == _TOOL_NAMES
    for d in defs:
        assert d["type"] == "function"
        assert "parameters" in d["function"]


async def test_tools_crud_only_touch_the_store(tmp_path):
    calls = []

    async def fire(instruction):
        calls.append(instruction)
        return "ran"

    store = _store(tmp_path, fire=fire)
    reg = build_routine_tools(store)
    assert {t.name for t in reg.list_tools()} == _TOOL_NAMES

    out = json.loads(await reg.execute("routine_create", {
        "name": "n", "cron": "0 9 * * *", "instruction": "do it",
        "tags": "a, b"}))
    assert out["ok"] and store.get("n").tags == ["a", "b"]

    out = json.loads(await reg.execute("routine_list", {}))
    assert [r["name"] for r in out["routines"]] == ["n"]

    out = json.loads(await reg.execute("routine_update", {
        "name": "n", "cron": "5 9 * * *"}))
    assert out["ok"] and store.get("n").cron == "5 9 * * *"
    assert store.get("n").instruction == "do it"  # empty field = unchanged

    assert json.loads(await reg.execute("routine_pause", {"name": "n"}))["ok"]
    assert store.get("n").enabled is False
    assert json.loads(await reg.execute("routine_resume", {"name": "n"}))["ok"]
    assert store.get("n").enabled is True

    out = json.loads(await reg.execute("routine_run_now", {"name": "n"}))
    assert out["ok"] and out["result"] == "ran" and calls == ["do it"]

    out = json.loads(await reg.execute("routine_delete", {"name": "n"}))
    assert out["ok"] and out["deleted"] is True and store.list() == []

    # errors come back as structured refusals, never exceptions
    out = json.loads(await reg.execute("routine_update", {"name": "ghost"}))
    assert out["ok"] is False and "error" in out


# ─── AitherAgent integration (flags default OFF = byte-identical) ────────────

def test_agent_default_flags_off_byte_identical():
    from adk.agent import AitherAgent

    agent = AitherAgent("selfmgmt-off")
    assert agent._routine_store is None
    assert agent._memory_wiki is None
    assert agent._routines_started is False
    names = {t.name for t in agent.tools.list_tools()}
    assert not (_TOOL_NAMES & names)


def test_agent_routines_flag_registers_store_and_tools():
    from adk.agent import AitherAgent

    agent = AitherAgent("selfmgmt-on", routines=True)
    assert agent._routine_store is not None
    names = {t.name for t in agent.tools.list_tools()}
    assert _TOOL_NAMES <= names
    assert agent._routines_started is False  # starts lazily on first chat
    # user routines land in the durable store
    agent._routine_store.create("mine", "0 8 * * *", "morning check")
    assert agent._routine_store.get("mine") is not None


def test_agent_memory_maintenance_registers_default_routines():
    from adk.agent import AitherAgent

    agent = AitherAgent("selfmgmt-maint", memory_maintenance=True)
    assert agent._routine_store is not None
    routines = {r.name: r for r in agent._routine_store.list()}
    assert {"wiki_consolidate", "wiki_lint", "wiki_prune",
            "graph_sweep"} <= set(routines)
    assert all(r.direct for r in routines.values())
    # manageable through the same tools
    names = {t.name for t in agent.tools.list_tools()}
    assert "routine_list" in names


async def test_agent_maintenance_direct_fires_run_without_llm():
    from adk.agent import AitherAgent

    agent = AitherAgent("selfmgmt-fire", memory_maintenance=True)
    out = await agent._routine_store.run_now("graph_sweep")
    data = json.loads(out)
    assert "examined" in data and "archived" in data

    out = await agent._routine_store.run_now("wiki_lint")
    assert json.loads(out) == []  # empty wiki lints clean, zero LLM calls

    out = await agent._routine_store.run_now("wiki_prune")
    data = json.loads(out)
    assert data["examined"] == 0 and data["hard_deleted"] == 0


async def test_agent_start_and_stop_routines():
    from adk.agent import AitherAgent

    agent = AitherAgent("selfmgmt-heartbeat", routines=True)
    await agent.start_routines()
    try:
        assert agent._routines_started is True
        # idempotent
        await agent.start_routines()
    finally:
        await agent.stop_routines()
    assert agent._routines_started is False


def test_routine_dataclass_ignores_unknown_fields():
    r = Routine.from_dict({"name": "n", "cron": "0 9 * * *",
                           "instruction": "i", "bogus": True})
    assert r.name == "n" and not hasattr(r, "bogus")


def test_default_limits_documented():
    assert DEFAULT_MAX_ROUTINES == 12
