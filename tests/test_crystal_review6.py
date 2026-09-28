"""Regressions for the sixth independent review (crystal).

Each test failed before its fix (verified against the pre-fix tree).
"""
from __future__ import annotations

import asyncio
import sqlite3

from tests._world_envs import awm  # noqa: F401 -- skips the module without awm >= 0.5

from adk import crystal as C
from adk.crystal import AwmFactStore, Crystal

SCOPE = "acme:tester:task"
_V1 = """
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, key TEXT NOT NULL,
    value TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'fact', created REAL NOT NULL,
    updated REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '{}', UNIQUE(scope, key));
CREATE TABLE schema_meta (version INTEGER NOT NULL);
INSERT INTO schema_meta(version) VALUES (1);
"""


# -- F1 (awdk side): a crystal handle stayed off reconcile after a peer's migrate ------
def test_crystal_reconciles_again_after_the_file_is_migrated(tmp_path, monkeypatch):
    monkeypatch.delenv("AWM_AUTO_MIGRATE", raising=False)
    db = tmp_path / "old.db"
    con = sqlite3.connect(str(db))
    con.executescript(_V1)
    con.commit()
    con.close()
    fs = AwmFactStore(SCOPE, db)
    assert fs.put_fact("ui.theme: dark", {}) == ("legacy", "awm-reconcile:NeedsMigration")
    # still compat: the reason names the fix, not "unsupported"
    assert fs.put_fact("ui.font: 16", {}) == ("legacy", "awm-reconcile:NeedsMigration")
    peer = awm.MemoryStore(db)
    peer.migrate(backup=False)                     # another process migrates it
    peer.close()
    assert fs.put_fact("ui.theme: light", {})[0].startswith("reconciled:")
    assert fs.can_reconcile
    fs.close()


# -- F18: planes said awgraph True while degraded said it was unavailable ------------
class _Hidden(C.AwgraphIndex):
    def probe(self) -> bool:   # awgraph "not importable", without touching sys.meta_path
        self.reason, self._ok = "ModuleNotFoundError: No module named 'awgraph'", False
        return False


def test_planes_report_an_unimportable_awgraph_as_absent(tmp_path):
    c = Crystal(scope=SCOPE, graph=_Hidden(str(tmp_path)))
    assert c.telemetry["planes"]["awgraph"] is False
    assert any(d.startswith("awgraph:ModuleNotFoundError") for d in c.telemetry["degraded"])


class _NoIndex(C.AwgraphIndex):
    def probe(self) -> bool:
        return True

    async def _open(self) -> bool:
        self.reason, self._ok = f"no-index for {self.root}", False
        return False


def test_planes_flip_when_the_first_query_finds_no_index(tmp_path):
    c = Crystal(scope=SCOPE, graph=_NoIndex(str(tmp_path)))
    assert c.telemetry["planes"]["awgraph"] is True       # bound, not yet known bad
    assert asyncio.run(c.recall_graph("anything")) == []
    assert c.telemetry["planes"]["awgraph"] is False


def test_real_probe_is_false_when_awgraph_cannot_be_found(tmp_path, monkeypatch):
    import importlib.util as iu
    real = iu.find_spec
    monkeypatch.setattr(iu, "find_spec",
                        lambda name, *a, **k: None if name == "awgraph" else real(name, *a, **k))
    g = C.AwgraphIndex(str(tmp_path))
    assert g.probe() is False and "awgraph" in g.reason
