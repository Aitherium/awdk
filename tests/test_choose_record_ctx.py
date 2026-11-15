"""adk.choose carries caller context WITH the decision: into awdecide's
DecisionRecord side table on the local backend, as body `metadata` at the door."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from adk import choose as door

pytest.importorskip("awdecide", reason="the local backend is the optional extra awdk[decide]")

_RECORD = (Path(__file__).resolve().parents[2] / "AitherOS" / "packages" / "awdecide"
           / "awdecide" / "record.py")


def _record_from_tree():
    """The tree's record.py, whatever awdecide build is installed."""
    spec = importlib.util.spec_from_file_location("_awdecide_record_under_test", _RECORD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    if not _RECORD.is_file():
        pytest.skip("awdecide/record.py is not in this tree")
    monkeypatch.setenv("AWDECIDE_DB", str(tmp_path / "decide.db"))
    monkeypatch.setenv("AITHER_DECIDE_URL", "local")
    for var in ("AWDECIDE_LLM_URL", "AWDECIDE_LLM_MODEL", "AITHER_DECIDE_LOCAL",
                "AITHER_DECIDE_CALLER"):
        monkeypatch.delenv(var, raising=False)
    rec = _record_from_tree()
    monkeypatch.setattr(door, "_import_record", lambda: rec)
    door._LOCAL_IDS.clear()
    return rec


def _ctx(tmp_path, decision_id):
    con = sqlite3.connect(tmp_path / "decide.db")
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM decision_ctx WHERE decision_id=?",
                      (decision_id,)).fetchone()
    con.close()
    return dict(row) if row else None


def test_local_decision_lands_caller_rung_latency_and_metadata(tmp_path):
    door.teach("router", "kind:code", "deep", +1.0)
    d = door.decide("router", "kind:code", options=["fast", "deep"], caller="unit-test",
                    metadata={"session_id": "s-1", "turn_id": "t-2", "request_id": "r-3",
                              "parent_id": "p-4", "lane": "x"})
    assert d.answer == "deep" and d.source == "local:evidence"
    assert d.latency_ms > 0
    row = _ctx(tmp_path, d.decision_id)
    assert row is not None
    assert (row["caller"], row["session_id"], row["turn_id"], row["request_id"],
            row["parent_id"]) == ("unit-test", "s-1", "t-2", "r-3", "p-4")
    assert row["rung"] == "evidence"  # awdecide's own backend name
    assert row["latency_ms"] == pytest.approx(d.latency_ms)
    assert row["state_text"] == "kind:code"
    assert json.loads(row["options_json"]) == ["fast", "deep"]
    assert json.loads(row["features_json"]) == {"lane": "x"}


def test_the_joined_record_carries_the_outcome(tmp_path, _isolated):
    door.teach("router", "kind:code", "deep", +1.0)
    d = door.decide("router", "kind:code", options=["fast", "deep"],
                    metadata={"session_id": "s-9"})
    door.outcome(d.decision_id, +1.0)
    recs = [r for r in _isolated.iter_records(tmp_path / "decide.db")
            if r["id"] == d.decision_id]
    assert recs and recs[0]["outcome"] == 1 and recs[0]["ctx"]["session_id"] == "s-9"
    assert recs[0]["ctx"]["caller"] == "adk.choose"  # the default caller


def test_batch_items_carry_their_own_metadata(tmp_path):
    door.teach("router", "a", "fast", +1.0)
    door.teach("router", "b", "deep", +1.0)
    out = door.decide_batch("router", [
        {"state": "a", "options": ["fast", "deep"], "metadata": {"turn_id": "t-a"}},
        {"state": "b", "options": ["fast", "deep"]},
    ], caller="batcher", metadata={"session_id": "s-b"})
    rows = [_ctx(tmp_path, d.decision_id) for d in out]
    assert [r["turn_id"] for r in rows] == ["t-a", None]
    assert {r["session_id"] for r in rows} == {"s-b"} and {r["caller"] for r in rows} == {
        "batcher"}


def test_a_broken_record_module_never_costs_the_decision(tmp_path, monkeypatch):
    class Broken:
        @staticmethod
        def attach(*a, **k):
            raise RuntimeError("disk on fire")

    monkeypatch.setattr(door, "_import_record", lambda: Broken)
    door.teach("router", "kind:code", "deep", +1.0)
    d = door.decide("router", "kind:code", options=["fast", "deep"])
    assert d.answer == "deep"


def test_an_old_awdecide_without_record_still_decides(tmp_path, monkeypatch):
    monkeypatch.setattr(door, "_import_record", lambda: None)
    door.teach("router", "kind:code", "deep", +1.0)
    d = door.decide("router", "kind:code", options=["fast", "deep"])
    assert d.answer == "deep"
    con = sqlite3.connect(tmp_path / "decide.db")
    assert con.execute("SELECT name FROM sqlite_master WHERE name='decision_ctx'"
                       ).fetchone() is None


def test_the_door_receives_metadata_in_the_body(monkeypatch):
    monkeypatch.setenv("AITHER_DECIDE_URL", "http://door.test")
    seen = []

    class _Resp:
        def read(self):
            return json.dumps({"decision_id": "d1", "answer": "fast", "confidence": 0.9,
                               "source": "engine"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0, context=None):
        seen.append(json.loads(req.data.decode()))
        return _Resp()

    monkeypatch.setattr(door.urllib.request, "urlopen", fake_urlopen)
    door.decide("router", "s", options=["fast", "deep"], caller="c",
                metadata={"session_id": "s-1", "request_id": "r-1"})
    assert seen[0]["metadata"] == {"caller": "c", "session_id": "s-1", "request_id": "r-1"}
