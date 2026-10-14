"""adk.memory ran every sqlite call on the event loop; on the daemon that was ~15% of the
loop's busy time during a turn and fired every turn-stage timer late (2026-10-14)."""

import asyncio
import sqlite3
import time

from adk import memory as memory_mod
from adk.memory import Memory


def test_history_reads_and_writes_never_block_the_loop(tmp_path, monkeypatch):
    mem = Memory(db_path=tmp_path / "m.db", agent_name="t")
    real = sqlite3.connect

    def slow_connect(*a, **k):
        time.sleep(0.4)  # blocking-ok: a busy disk, simulated
        return real(*a, **k)

    monkeypatch.setattr(memory_mod.sqlite3, "connect", slow_connect)

    async def main():
        ticks: list = []

        async def ticker():
            for _ in range(15):
                t = time.perf_counter()
                await asyncio.sleep(0.02)
                ticks.append(time.perf_counter() - t)

        tk = asyncio.create_task(ticker())
        await asyncio.sleep(0.03)
        await mem.add_message("s", "user", "hello")
        hist = await mem.get_history("s")
        await tk
        return ticks, hist

    ticks, hist = asyncio.run(main())
    assert hist and hist[-1]["content"] == "hello"
    assert max(ticks) < 0.3, f"the loop stalled {max(ticks):.2f}s on sqlite"


def test_wal_is_set_once_not_per_query(tmp_path, monkeypatch):
    mem = Memory(db_path=tmp_path / "m.db", agent_name="t")
    seen: list = []
    real = sqlite3.connect

    class _Conn:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, *a):
            seen.append(sql)
            return self._c.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._c, name)

        def __enter__(self):
            self._c.__enter__()
            return self

        def __exit__(self, *a):
            return self._c.__exit__(*a)

    monkeypatch.setattr(memory_mod.sqlite3, "connect", lambda *a, **k: _Conn(real(*a, **k)))
    asyncio.run(mem.add_message("s", "user", "a"))
    asyncio.run(mem.add_message("s", "user", "b"))
    assert not any("journal_mode" in q for q in seen)
