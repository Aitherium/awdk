"""W5: crystal facts with a derivable subject go through awm's reconcile_and_remember."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from tests._world_envs import awm

from adk.crystal import AwmFactStore, Crystal, derive_subject, fact_key

SCOPE = "acme:tester:task"


@pytest.mark.parametrize("fact,want", [
    ("ui.theme: dark", ("ui.theme", "dark")),
    ("`build.cache_dir` = /tmp/x", ("build.cache_dir", "/tmp/x")),
    ("user.ui_theme: switched to light mode", ("user.ui_theme", "switched to light mode")),
    ("Note: this is a sentence", None),                 # one segment
    ("reflex/state.py: BaseState recomputes", None),    # a path
    ("state.py: the cache", None),                      # a filename
    ("v1.2: released", None),                           # a version
    ("ui.theme:dark", None),                            # no space after the separator
])
def test_derive_subject(fact, want):
    assert derive_subject(fact) == want


def _store(tmp_path: Path, **kw) -> AwmFactStore:
    return AwmFactStore(SCOPE, tmp_path / "m.db", **kw)


def test_slot_facts_supersede_and_prose_keeps_legacy_keys(tmp_path):
    fs = _store(tmp_path)
    assert fs.can_reconcile
    assert fs.put_fact("ui.theme: dark", {}) == ("reconciled:add", None)
    assert fs.put_fact("ui.theme: light", {}) == ("reconciled:update", None)
    assert fs.put_fact("ui.theme: light", {}) == ("reconciled:ignore", None)
    prose = "Ran pytest tests/test_state.py -> FAILED with a KeyError on computed"
    assert fs.put_fact(prose, {"src": "compaction"}) == ("legacy", None)
    rows = {k: v for k, v, _ in fs.scan(50)}
    assert rows["ui.theme"] == "ui.theme: light"
    assert rows[fact_key(prose)] == prose
    hist = fs._store.history(awm.Scope.parse(SCOPE), "ui.theme")
    assert [h.value for h in hist] == ["dark", "light"]
    fs.close()


def test_legacy_rows_written_before_stay_readable(tmp_path):
    fs = _store(tmp_path)
    fs.put(fact_key("an old fact from before reconcile existed"),
           "an old fact from before reconcile existed", {"src": "compaction"})
    fs.put_fact("ui.theme: dark", {})
    values = [v for _, v, _ in fs.scan(50)]
    assert "an old fact from before reconcile existed" in values
    assert "ui.theme: dark" in values
    fs.close()


def test_crystallize_counts_routes(tmp_path):
    fs = _store(tmp_path)
    c = Crystal(scope=SCOPE, store=fs)
    summary = ("- ui.theme: dark mode everywhere please\n"
               "- ui.theme: light mode everywhere please\n"
               "- The failing path is in state.py, not in vars/base.py\n")
    n = asyncio.run(c.crystallize(summary))
    assert n == 3
    assert c.telemetry["reconciled"] == {"add": 1, "update": 1, "ignore": 0}
    assert c.telemetry["legacy_writes"] == 1
    got = asyncio.run(c.recall_facts("which ui.theme mode"))
    assert "ui.theme: light mode everywhere please" in got
    assert "ui.theme: dark mode everywhere please" not in got
    fs.close()


def test_a_bound_completion_decides_with_the_llm_reconciler(tmp_path):
    prompts = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps({"action": "add", "key": "ui.theme.contrast", "value": "high",
                           "reason": "a new attribute"})

    fs = _store(tmp_path)
    c = Crystal(scope=SCOPE, store=fs)
    assert c.bind_completion(complete) is True
    assert fs.put_fact("ui.theme: high contrast", {}) == ("reconciled:add", None)
    assert prompts and "ui.theme" in prompts[0]
    assert [v for k, v, _ in fs.scan(10) if k == "ui.theme.contrast"] == [
        "ui.theme.contrast: high"]
    fs.close()


def test_a_malformed_model_reply_never_loses_the_fact(tmp_path):
    fs = _store(tmp_path, complete=lambda prompt: "sure, update it")
    route, reason = fs.put_fact("ui.theme: dark", {})
    assert (route, reason) == ("legacy", "awm-reconcile:ReconcileError")
    assert fact_key("ui.theme: dark") in {k for k, _, _ in fs.scan(10)}
    fs.close()


_V1 = """
CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
  key TEXT NOT NULL, value TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'fact',
  created REAL NOT NULL, updated REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0,
  meta TEXT, UNIQUE(scope, key));
CREATE INDEX idx_scope ON memories(scope);
CREATE TABLE schema_meta (version INTEGER NOT NULL);
INSERT INTO schema_meta(version) VALUES (1);
"""


def test_an_old_file_degrades_by_feature_detection_and_is_not_migrated(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    con = sqlite3.connect(str(db))
    con.executescript(_V1)
    con.commit()
    con.close()
    monkeypatch.setenv("AWM_AUTO_MIGRATE", "1")
    fs = AwmFactStore(SCOPE, db)
    c = Crystal(scope=SCOPE, store=fs)
    asyncio.run(c.crystallize("- ui.theme: dark mode on every surface\n"))
    assert "awm-reconcile:NeedsMigration" in c.telemetry["degraded"]
    assert c.telemetry["legacy_writes"] == 1 and fs.can_reconcile is False
    assert [v for _, v, _ in fs.scan(10)] == ["ui.theme: dark mode on every surface"]
    fs.close()
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT version FROM schema_meta").fetchone()[0] == 1
    con.close()
    assert list(tmp_path.glob("old.db*")) == [db]


def test_an_awm_without_reconcile_is_detected_not_assumed(tmp_path, monkeypatch):
    import awm.store as awm_store

    real = awm_store.MemoryStore

    class OldAwm:
        """The 0.3.x surface: remember / recall / close, nothing newer."""

        def __init__(self, path, **_):
            self._s = real(path)

        def remember(self, *a, **k):
            return self._s.remember(*a, **k)

        def recall(self, *a, **k):
            return self._s.recall(*a, **k)

        def close(self):
            self._s.close()

    monkeypatch.setattr(awm_store, "MemoryStore", OldAwm)
    fs = AwmFactStore(SCOPE, tmp_path / "m.db")
    assert fs.can_reconcile is False
    assert fs.put_fact("ui.theme: dark", {}) == ("legacy", "awm-reconcile:unsupported")
    fs.close()
