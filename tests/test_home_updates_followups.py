"""Follow-ups to the ``adk home updates`` review: ``household`` is required on a stored
approval row, old upgrade logs are pruned (newest kept), a busy lock names its actual
holder, and the Hearth device tools take the same lock on ``device_approvals.json`` as
``adk home updates`` -- so a guardian's vote cannot be lost to a concurrent write."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from adk import receipts
from adk.home import device_tools as dt
from adk.home import filelock
from adk.home import lan_alerts as la
from adk.home import updates as up
from adk.home import updates_cli as ucli
from adk.home.devices import notify
from adk.home.devices.ha import HAClient
from adk.home.devices.testing import FakeHomeAssistant, household_states

GOOD = {"id": "ha-aaaaaaaaaaaa", "status": "pending", "action": dict(up.ADK_ACTION),
        "digest": "d" * 64, "approvers": ["owner"], "required": 1, "votes": {},
        "approvals": 0, "expires_at": 1.0, "created_at": 0.0, "household": "local",
        "summary": "s", "policy": {}, "requested_by": {}}


def test_valid_row_requires_a_household() -> None:
    assert up.valid_row(GOOD["id"], dict(GOOD))
    no_house = {k: v for k, v in GOOD.items() if k != "household"}
    assert not up.valid_row(GOOD["id"], no_house)
    assert not up.valid_row(GOOD["id"], {**GOOD, "household": ""})
    assert not up.valid_row(GOOD["id"], {**GOOD, "household": 7})


def test_prune_keeps_the_newest_and_any_recent_log(tmp_path: Path) -> None:
    now = time.time()
    ages = {"a": 9000, "b": 8000, "c": 7000, "d": 60}       # seconds old; d is "active"
    for name, age in ages.items():
        f = tmp_path / f"{up.LOG_PREFIX}{name}.log"
        f.write_text(name, encoding="utf-8")
        os.utime(f, (now - age, now - age))
    other = tmp_path / "unrelated.log"
    other.write_text("x", encoding="utf-8")
    os.utime(other, (now - 9999, now - 9999))
    removed = up.prune_apt_logs(tmp_path, keep=1, active_s=600, now=now)
    left = sorted(p.name for p in tmp_path.iterdir())
    # d is the newest (kept); c, b, a are old and reported -> removed; others untouched.
    assert sorted(Path(r).name for r in removed) == [f"{up.LOG_PREFIX}{n}.log"
                                                     for n in ("a", "b", "c")]
    assert left == [f"{up.LOG_PREFIX}d.log", "unrelated.log"]
    # keep=2 with a fresh second file: the recent one survives even past keep.
    for name, age in (("e", 5000), ("f", 100), ("g", 50)):
        f = tmp_path / f"{up.LOG_PREFIX}{name}.log"
        f.write_text(name, encoding="utf-8")
        os.utime(f, (now - age, now - age))
    up.prune_apt_logs(tmp_path, keep=1, active_s=600, now=now)
    assert sorted(p.name for p in tmp_path.glob(up.LOG_PREFIX + "*")) == [
        f"{up.LOG_PREFIX}{n}.log" for n in ("d", "f", "g")]


def test_a_finished_upgrade_prunes_old_logs(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(up.tempfile, "gettempdir", lambda: str(tmp_path))
    old = tmp_path / f"{up.LOG_PREFIX}old.log"
    old.write_text("x", encoding="utf-8")
    os.utime(old, (time.time() - 9000,) * 2)
    newest = tmp_path / f"{up.LOG_PREFIX}new.log"
    newest.write_text("y", encoding="utf-8")
    os.utime(newest, (time.time() - 8000,) * 2)

    def runner(argv: Any, timeout: float, env: Dict[str, str]) -> Dict[str, Any]:
        return {"state": "exited", "exit": 0, "out": "ok", "err": ""}

    assert up.run_host_upgrade(0, runner)["ok"] is True
    assert not old.exists() and newest.exists()


def test_a_still_running_upgrade_prunes_nothing(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(up.tempfile, "gettempdir", lambda: str(tmp_path))
    logs = []
    for i in range(3):
        f = tmp_path / f"{up.LOG_PREFIX}{i}.log"
        f.write_text("x", encoding="utf-8")
        os.utime(f, (time.time() - 9000 - i,) * 2)
        logs.append(f)

    def runner(argv: Any, timeout: float, env: Dict[str, str]) -> Dict[str, Any]:
        return {"state": "still_running", "pid": 1, "log": str(logs[0])}

    assert up.run_host_upgrade(0, runner)["state"] == "still_running"
    assert all(f.exists() for f in logs)


def test_busy_lock_names_the_actual_holder(tmp_path: Path) -> None:
    store = tmp_path / "device_approvals.json"
    with filelock.run_lock(store, holder=dt.LOCK_HOLDER):
        assert filelock.read_holder(store).startswith(dt.LOCK_HOLDER + " pid ")
        home = ucli.Home(tmp_path, client=None)
        with pytest.raises(RuntimeError) as err:
            with filelock.run_lock(home.device_store_path(), holder=ucli.LOCK_HOLDER,
                                   timeout=0.2):
                pass
        assert dt.LOCK_HOLDER in str(err.value)
        assert "adk home devices" not in str(err.value)
    state = up.state_path(tmp_path)
    with filelock.run_lock(state, holder=ucli.LOCK_HOLDER):
        with pytest.raises(RuntimeError) as err:
            with filelock.run_lock(state, holder="someone else", timeout=0.2):
                pass
        assert f"{ucli.LOCK_HOLDER} pid {os.getpid()}" in str(err.value)
    inv = tmp_path / la.STATE_NAME
    with la.run_lock(inv):
        with pytest.raises(RuntimeError) as err:
            with la.run_lock(inv, timeout=0.2):
                pass
        assert "adk home devices pid" in str(err.value)


def test_async_lock_serialises_coroutines_without_blocking_the_loop(tmp_path: Path) -> None:
    order = []

    async def worker(name: str) -> None:
        async with filelock.arun_lock(tmp_path / "s.json", holder=name, timeout=5):
            order.append(f"{name}+")
            await asyncio.sleep(0.05)
            order.append(f"{name}-")

    async def ticker() -> int:
        n = 0
        for _ in range(5):
            await asyncio.sleep(0.01)
            n += 1
        return n

    async def main() -> int:
        res = await asyncio.gather(worker("a"), worker("b"), ticker())
        return int(res[2])

    assert asyncio.run(main()) == 5
    assert order in (["a+", "a-", "b+", "b-"], ["b+", "b-", "a+", "a-"])


@pytest.fixture
def hearth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AITHER_AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(receipts, "_signing_key", lambda key_path=None, create=True: None)
    notify.set_approval_notifier(None)
    with FakeHomeAssistant(household_states()) as ha:
        tools = {f.__name__: f for f in dt.build_device_tools(
            tmp_path / "actions.jsonl", root=tmp_path,
            client=HAClient(ha.url, lambda: ha.token), tainted=lambda: False)}
        yield {"tmp": tmp_path, "ha": ha, "tools": tools}


def test_hearth_vote_waits_for_the_updates_lock_and_is_never_lost(hearth: Dict[str, Any]
                                                                  ) -> None:
    tools, store_path = hearth["tools"], hearth["tmp"] / dt.APPROVALS_NAME
    guarded = json.loads(asyncio.run(tools["home_act"]("lock", "unlock", "lock.front_door")))
    aid, summary = guarded["approval"], guarded["suggest"]["args"]["summary"]
    # `adk home updates` holds the store: the Hearth refuses (busy, named) rather than
    # writing over it, and nothing ran.
    with filelock.run_lock(store_path, holder=ucli.LOCK_HOLDER):
        busy = json.loads(asyncio.run(asyncio.wait_for(
            tools["home_approve"](aid, summary), 30)))
    assert "busy" in busy["error"] and ucli.LOCK_HOLDER in busy["error"]
    assert not [c for c in hearth["ha"].calls if c.get("domain") == "lock"]
    # Free again: the vote lands and is persisted.
    ok = json.loads(asyncio.run(tools["home_approve"](aid, summary)))
    assert ok["ran"] is True
    rows = json.loads(store_path.read_text(encoding="utf-8"))["approvals"]
    assert rows[aid]["status"] == "executed"


def test_an_updates_save_cannot_drop_a_hearth_vote(hearth: Dict[str, Any]) -> None:
    tools = hearth["tools"]
    home = ucli.Home(hearth["tmp"], client=None)
    guarded = json.loads(asyncio.run(tools["home_act"]("lock", "unlock", "lock.front_door")))
    aid, summary = guarded["approval"], guarded["suggest"]["args"]["summary"]
    stale = home.load_device_store()                   # read before the guardian votes
    asyncio.run(tools["home_approve"](aid, summary))
    home.save_device_store(stale)                      # a stale writer saves "pending"
    rows = json.loads((hearth["tmp"] / dt.APPROVALS_NAME).read_text(encoding="utf-8"))
    assert rows["approvals"][aid]["status"] == "executed"
