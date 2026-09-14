"""Per-agent ledgers merging into one prime.

The interesting cases are all about what happens when agents DISAGREE or repeat
themselves, because those are the two things a naive append-everything design
gets silently wrong.
"""

from __future__ import annotations

import pytest
from adk import agent_ledger as al

pytest.importorskip("awgit", reason="awgit is an adk dependency; skip if absent")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the developer's real ~/.aither during a test run."""
    monkeypatch.setattr(al, "AITHER_HOME", tmp_path)
    monkeypatch.setattr(al, "AGENTS_ROOT", tmp_path / "agents")
    monkeypatch.setattr(al, "PRIME_ROOT", tmp_path / "prime")
    monkeypatch.setattr(al, "OUTBOX", tmp_path / "outbox")
    return tmp_path


def _published(home, name="iris"):
    """Make it look like `name` has published, without writing a real oplog."""
    (home / "agents" / name / "oplog").mkdir(parents=True, exist_ok=True)


def test_agent_names_cannot_escape_the_agents_root():
    """Agent names come from user-authored packs and personas.

    A name containing a separator would otherwise write outside the root.
    """
    assert al._safe("../../etc/passwd") == "etc-passwd"
    assert al._safe("iris") == "iris"
    assert al._safe("") == "unnamed"
    assert al._safe("///") == "unnamed"


def test_each_agent_gets_its_own_ledger(isolated_home):
    a = al.agent_log("iris")
    b = al.agent_log("hermes")
    assert a is not None and b is not None
    assert (isolated_home / "agents" / "iris" / "oplog").exists()
    assert (isolated_home / "agents" / "hermes" / "oplog").exists()


def test_prime_is_separate_from_every_agent(isolated_home):
    al.agent_log("iris")
    assert al.prime_log() is not None
    assert (isolated_home / "prime" / "oplog").exists()


def test_status_reports_the_federation(isolated_home):
    al.agent_log("iris")
    al.agent_log("hermes")
    st = al.status()
    assert st["available"] is True
    assert set(st["agents"]) == {"iris", "hermes"}
    assert st["prime_ops"] == 0


def test_merging_an_empty_ledger_is_a_no_op(isolated_home):
    al.agent_log("iris")
    report = al.merge_to_prime(["iris"])
    assert report.merged == 0
    assert report.clean


def test_a_missing_agent_is_noted_not_crashed(isolated_home):
    report = al.merge_to_prime(["never-existed"])
    assert report.merged == 0
    # Reported rather than silent: a typo'd agent name that merges nothing and
    # says nothing is indistinguishable from an agent with no work.
    assert any("no ledger" in n or "never-existed" in n for n in report.notes)


def test_report_summary_counts_every_outcome():
    r = al.MergeReport(merged=3, skipped_duplicates=2, agents=["a"])
    s = r.summary()
    assert "3 op(s) merged" in s
    assert "2 already present" in s
    assert r.clean is True


def test_conflicts_make_the_report_not_clean():
    r = al.MergeReport(conflicts=[object()])
    assert r.clean is False
    assert "CONFLICT" in r.summary()


def test_merge_reports_conflicts_and_imports_nothing(monkeypatch, isolated_home):
    """A conflict is never auto-resolved, and never HALF applied.

    Picking a side silently would make prime confidently wrong. Importing the
    non-conflicting half would leave prime holding some of an agent's work and
    silently omitting the rest — the one outcome with no honest reading.
    """
    _published(isolated_home)
    class _Op:
        git_sha = "sha-1"

    class _Log:
        def all_ops(self):
            return [_Op()]

        def has_commit(self, sha):
            return False

        def append(self, op):
            raise AssertionError("must not append while a conflict is unresolved")

    monkeypatch.setattr(al, "agent_log", lambda n: _Log())
    monkeypatch.setattr(al, "prime_log", lambda: _Log())
    monkeypatch.setattr(
        "awgit.merge_ops",
        lambda a, b, **kw: type("R", (), {"conflicts": ["boom"], "notes": []})(),
    )

    report = al.merge_to_prime(["iris"])
    assert report.merged == 0
    assert len(report.conflicts) == 1
    assert not report.clean


def test_already_merged_ops_are_skipped_not_double_counted(monkeypatch, isolated_home):
    """Agents publish on a timer, so the same ops arrive repeatedly.

    Without this, prime's history becomes a function of how often the loop ran
    rather than of what actually changed.
    """
    _published(isolated_home)
    class _Op:
        git_sha = "sha-1"

    appended = []

    class _Agent:
        def all_ops(self):
            return [_Op()]

    class _Prime:
        def all_ops(self):
            return []

        def has_commit(self, sha):
            return sha == "sha-1"  # prime already has it

        def append(self, op):
            appended.append(op)

    monkeypatch.setattr(al, "agent_log", lambda n: _Agent())
    monkeypatch.setattr(al, "prime_log", lambda: _Prime())

    report = al.merge_to_prime(["iris"])
    assert report.skipped_duplicates == 1
    assert report.merged == 0
    assert appended == [], "a duplicate op must never reach prime"


def test_clean_merge_appends_each_fresh_op(monkeypatch, isolated_home):
    _published(isolated_home)
    class _Op:
        git_sha = "new-sha"

    appended = []

    class _Agent:
        def all_ops(self):
            return [_Op()]

    class _Prime:
        def all_ops(self):
            return []

        def has_commit(self, sha):
            return False

        def append(self, op):
            appended.append(op)

    monkeypatch.setattr(al, "agent_log", lambda n: _Agent())
    monkeypatch.setattr(al, "prime_log", lambda: _Prime())
    monkeypatch.setattr(
        "awgit.merge_ops",
        lambda a, b, **kw: type("R", (), {"conflicts": [], "notes": []})(),
    )

    report = al.merge_to_prime(["iris"])
    assert report.merged == 1
    assert len(appended) == 1
    assert report.clean


def test_contributors_preserves_attribution_through_the_merge(monkeypatch, isolated_home):
    """The question a single shared log cannot answer."""
    class _Op:
        def __init__(self, who):
            self.verified_actor = who

    class _Prime:
        def ops_for_node(self, node_id):
            return [_Op("iris"), _Op("hermes"), _Op("iris")]

    monkeypatch.setattr(al, "prime_log", lambda: _Prime())
    assert al.contributors("mod:func") == {"iris": 2, "hermes": 1}


def test_everything_degrades_when_awgit_is_absent(monkeypatch, isolated_home):
    monkeypatch.setattr(al, "_oplog", lambda root: None)
    assert al.agent_log("iris") is None
    assert al.publish("iris") is None
    report = al.merge_to_prime(["iris"])
    assert report.merged == 0
    assert any("unavailable" in n for n in report.notes)
    assert al.contributors("x") == {}
