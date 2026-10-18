"""A turn's recall never waits behind a write holding the store lock.

Live 2026-10-14: every turn's crystal stage read "TimeoutError: over 7.0s; still running:
read:scan 7.0s" -- AwmFactStore.put_fact holds the store lock through an LLM reconcile,
and scan() waited on the same lock.
"""

from __future__ import annotations

import threading
import time

import pytest

awm = pytest.importorskip("awm.store")

from adk.crystal import AwmFactStore  # noqa: E402


def test_scan_does_not_wait_for_a_held_write_lock(tmp_path):
    store = AwmFactStore("aitherium:t:p", db=tmp_path / "m.db")
    store.put("f:1", "the orchestrator lane was shared with training", {})
    release = threading.Event()

    def _writer_holding_the_lock():
        with store._lock:  # a reconcile waiting on an LLM
            release.wait(10)

    w = threading.Thread(target=_writer_holding_the_lock)
    w.start()
    time.sleep(0.1)
    try:
        t0 = time.monotonic()
        rows = store.scan(10)
        elapsed = time.monotonic() - t0
    finally:
        release.set()
        w.join()
        store.close()
    assert elapsed < 2.0, f"scan waited {elapsed:.1f}s behind the write lock"
    assert any("orchestrator lane" in r[1] for r in rows)
