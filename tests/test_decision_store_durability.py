"""Decision store durability: fsync before publish, corrupt quarantine, tmp sweep,
the pytest steer-dir seam, and the bounded ``GET /decisions`` list."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from adk.decisions import store as store_mod
from adk.decisions.store import (
    CORRUPT_DIRNAME,
    CORRUPT_MIN_AGE_SECONDS,
    DecisionCard,
    DecisionStore,
    write_steer,
)


def _age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_card_write_fsyncs_before_replace(tmp_path, monkeypatch):
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        calls.append("fsync")
        return real_fsync(fd)

    def replace(src, dst):
        calls.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(store_mod.os, "fsync", fsync)
    monkeypatch.setattr(store_mod.os, "replace", replace)
    store = DecisionStore(tmp_path)
    store.create(DecisionCard(id="", title="durable", kind="info"))
    assert "fsync" in calls and "replace" in calls
    assert calls.index("fsync") < calls.index("replace")


def test_steer_write_fsyncs_before_replace(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    calls: list[str] = []
    real_fsync = os.fsync

    def fsync(fd):
        calls.append("fsync")
        return real_fsync(fd)

    monkeypatch.setattr(store_mod.os, "fsync", fsync)
    target = write_steer("sess-1", ["hello"], suffix="answer", sender="test",
                         authority="owner", origin_id="d-abcd")
    assert target is not None and target.exists() and calls == ["fsync"]


def test_nul_filled_card_is_quarantined_and_reported(tmp_path, caplog):
    store = DecisionStore(tmp_path)
    good = store.create(DecisionCard(id="", title="still here", kind="info"))
    torn = tmp_path / "d-depe.json"
    torn.write_bytes(b"\x00" * 1466)
    _age(torn, CORRUPT_MIN_AGE_SECONDS + 5)

    with caplog.at_level("WARNING", logger="adk.decisions.store"):
        cards = store.list(status=None)

    assert [c.id for c in cards] == [good.id]
    assert not torn.exists()
    assert (tmp_path / CORRUPT_DIRNAME / "d-depe.json").read_bytes() == b"\x00" * 1466
    assert store.corrupt_cards() == ["d-depe.json"]
    assert any("d-depe.json" in r.getMessage() for r in caplog.records)


def test_open_filter_also_quarantines(tmp_path):
    store = DecisionStore(tmp_path)
    torn = tmp_path / "d-pjg7.json"
    torn.write_text('{"id": "d-pjg7", "title": "trunc', encoding="utf-8")
    _age(torn, CORRUPT_MIN_AGE_SECONDS + 5)
    assert store.list() == []
    assert store.corrupt_cards() == ["d-pjg7.json"]


def test_young_corrupt_card_is_left_alone(tmp_path):
    store = DecisionStore(tmp_path)
    fresh = tmp_path / "d-wxyz.json"
    fresh.write_bytes(b"\x00" * 10)
    assert store.list(status=None) == []
    assert fresh.exists()
    assert store.corrupt_cards() == []


def test_shape_error_is_not_quarantined(tmp_path):
    """Parseable JSON that from_dict rejects may be a newer schema -- not damage."""
    store = DecisionStore(tmp_path)
    odd = tmp_path / "d-mnpq.json"
    odd.write_text(json.dumps({"id": "d-mnpq", "source": "not-a-dict",
                               "options": ["x"]}), encoding="utf-8")
    _age(odd, CORRUPT_MIN_AGE_SECONDS + 5)
    store.list(status=None)
    assert odd.exists()
    assert store.corrupt_cards() == []


def test_sweep_quarantines_and_removes_stale_tmp(tmp_path):
    store = DecisionStore(tmp_path)
    torn = tmp_path / "d-depe.json"
    torn.write_bytes(b"\x00" * 8)
    _age(torn, CORRUPT_MIN_AGE_SECONDS + 5)
    stale = tmp_path / "d-abcd.1234.tmp"
    stale.write_text("{}", encoding="utf-8")
    _age(stale, store_mod.STALE_TMP_SECONDS + 5)
    young = tmp_path / "d-efgh.1234.tmp"
    young.write_text("{}", encoding="utf-8")

    store.sweep()

    assert not stale.exists()
    assert young.exists()
    assert store.corrupt_cards() == ["d-depe.json"]


def test_steer_dir_never_the_owners_under_pytest(monkeypatch):
    monkeypatch.delenv("AITHER_STEER_DIR", raising=False)
    got = store_mod.steer_dir()
    assert got != Path.home() / ".aither" / "steer"
    assert got.is_dir()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", "test-token-for-testing-only")
    # These cards are info cards dated 1970 (a stable sort key); the info TTL
    # would close them all on read, which is not what the listing tests measure.
    monkeypatch.setenv("AITHER_DECISIONS_INFO_TTL_HOURS", str(200 * 365 * 24))
    monkeypatch.setattr(store_mod, "_STORE", None)
    from fastapi.testclient import TestClient

    from adk.harnesses.daemon import create_app

    c = TestClient(create_app())
    c.headers = {"Authorization": "Bearer test-token-for-testing-only"}
    return c


def test_list_limit_keeps_newest_n_oldest_first(client):
    store = store_mod.get_store()
    made = []
    for i in range(6):
        # Not kind="info": an info card this old closes on its own (INFO_TTL),
        # which would stamp every card answered_at=now and reorder the history.
        card = DecisionCard(id="", title=f"card {i}", kind="blocked")
        card.created_at = 1_000_000.0 + i
        made.append(store.create(card).id)

    resp = client.get("/decisions", params={"status": "all", "limit": 3})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 3
    assert data["total"] == 6
    assert data["truncated"] is True
    assert [c["id"] for c in data["decisions"]] == made[3:]
    assert data["corrupt"] == []


def test_list_limit_is_clamped_not_refused(client):
    # Genesis forwards its own limit; an out-of-range one must never 422.
    assert client.get("/decisions", params={"limit": 0}).status_code == 200
    assert client.get("/decisions", params={"limit": 501}).status_code == 200
    data = client.get("/decisions").json()
    assert data["truncated"] is False


def _make_open(store, n):
    made = []
    for i in range(n):
        # kind="blocked", not "info": a 1970 info card closes on its own (INFO_TTL).
        card = DecisionCard(id="", title=f"open {i}", kind="blocked")
        card.created_at = 1_000_000.0 + i
        made.append(store.create(card).id)
    return made


def test_default_open_list_is_unbounded_and_keeps_the_oldest(client):
    # 113 open cards were live when the default limit was 50: the default call
    # must still return every one, the oldest (longest-blocking) first.
    made = _make_open(store_mod.get_store(), 60)
    data = client.get("/decisions", params={"status": "open"}).json()
    assert data["count"] == 60
    assert data["total"] == 60
    assert data["truncated"] is False
    assert [c["id"] for c in data["decisions"]] == made
    # And with no status at all (the default is open).
    assert client.get("/decisions").json()["count"] == 60


def test_explicit_open_limit_keeps_the_oldest_n(client):
    made = _make_open(store_mod.get_store(), 60)
    data = client.get("/decisions", params={"status": "open", "limit": 50}).json()
    assert data["count"] == 50
    assert data["total"] == 60
    assert data["truncated"] is True
    assert [c["id"] for c in data["decisions"]] == made[:50]


def test_default_history_list_is_bounded_to_the_newest_50(client):
    made = _make_open(store_mod.get_store(), 60)
    data = client.get("/decisions", params={"status": "all"}).json()
    assert data["count"] == 50
    assert data["total"] == 60
    assert data["truncated"] is True
    assert [c["id"] for c in data["decisions"]] == made[10:]


def test_history_window_ranks_by_answered_at_not_created_at(client):
    # 60 answered cards; the OLDEST-created one is answered LAST. Ranked by
    # created_at it fell out of the default 50, so an act-once consumer polling
    # status=answered never saw the card it was waiting for.
    store = store_mod.get_store()
    made = _make_open(store, 60)
    for i, cid in enumerate(made):
        store.answer(cid, "ok")
        card = store.get(cid)
        # Newest-created answered first; the oldest-created answered last of all.
        card.answered_at = 2_000_000.0 + (10_000 if i == 0 else len(made) - i)
        store._write(card)

    data = client.get("/decisions", params={"status": "answered"}).json()
    ids = [c["id"] for c in data["decisions"]]
    assert data["count"] == 50
    assert data["total"] == 60
    assert data["truncated"] is True
    # Window is oldest-first by close time, so the card answered last is last.
    assert ids[-1] == made[0]
    # The 10 dropped are the 10 answered EARLIEST: the newest-created ones.
    assert set(made[-10:]).isdisjoint(ids)
