"""Realm journal: personas do not cross, and a tampered chain is refused.

WHY THESE CASES
---------------
This reader points at a file a live realm is writing. Two failures would be invisible and
expensive, so every arm here is one of them:

* **Leakage.** One NPC's conversation surfacing in another's scene is not a wrong answer a
  reader would notice — it is a plausible one. The isolation arms write both personas and
  ask each for the other's words.
* **Quiet repair.** A journal whose chain no longer holds has been edited or truncated. The
  only safe response is refusal: a reader that drops the bad row and carries on turns a
  detected tamper into a slightly shorter, entirely trusted history. The tamper arms assert
  the refusal AND that the file on disk is byte-identical afterwards.

The chain is built here with the module's own `row_hash`, which is the same rule the engine
computes in TypeScript — so a fixture written by this test is a journal the engine would
also verify.
"""

from __future__ import annotations

import json

import pytest
from adk.packs.gobbonet.realm_journal import (
    GENESIS_PREV,
    JOURNAL_V,
    WORLD,
    JournalError,
    RealmJournalSource,
    canonical,
    parse_jsonl,
    persona_of,
    read_journal,
    realm_journal_from_env,
    row_hash,
    text_of,
    verify_rows,
)


def make_row(prev: str, tick: int, kind: str, actor: str, payload: dict) -> dict:
    """One chained row, exactly as the engine would write it."""
    body = {
        "v": JOURNAL_V, "tick": tick, "ts": f"2026-09-21T00:00:{tick:02d}+00:00",
        "kind": kind, "actor": actor, "payload": payload, "llm": None, "prev": prev,
    }
    return {**body, "hash": row_hash(prev, body)}


def make_journal(path, rows_spec) -> str:
    """Write a chained journal.jsonl from `(tick, kind, actor, payload)` tuples."""
    prev = GENESIS_PREV
    lines = []
    for tick, kind, actor, payload in rows_spec:
        row = make_row(prev, tick, kind, actor, payload)
        prev = row["hash"]
        lines.append(canonical(row))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return prev


@pytest.fixture()
def journal_path(tmp_path):
    path = tmp_path / "realm" / "journal.jsonl"
    make_journal(path, [
        (1, "converse", "player", {"npc": "wren", "text": "the forge fire never goes out"}),
        (2, "npc.act", "realm", {"npc": "wren", "action": "hammers a notched blade flat",
                                 "role": "artisan", "source": "t1", "state_key": "forge"}),
        (3, "converse", "player", {"npc": "sable",
                                   "text": "a cloaked wanderer talks of the crags"}),
        (4, "npc.act", "realm", {"npc": "sable", "action": "slips between the market stalls",
                                 "role": "rogue", "source": "t1", "state_key": "market"}),
        (5, "chronicle", "realm", {"summary": "the autumn tithe came in short"}),
        (6, "decide.outcome", "realm", {"decision_id": "d1", "reward": 1}),
    ])
    return path


# ── the chain ─────────────────────────────────────────────────────────────────


class TestChain:
    def test_a_written_journal_verifies(self, journal_path):
        rows = read_journal(journal_path)
        assert len(rows) == 6
        assert verify_rows(rows)["rows"] == 6

    def test_canonical_json_is_the_engine_spelling(self):
        # Sorted keys, no whitespace, non-ASCII literal — the three properties the
        # TypeScript half also produces. A space anywhere here breaks every hash.
        assert canonical({"b": 1, "a": "é"}) == '{"a":"é","b":1}'

    def test_the_hash_rule_is_prev_plus_canonical_body(self):
        body = {"v": 1, "tick": 0, "kind": "chronicle", "actor": "realm",
                "payload": {}, "llm": None, "ts": "t", "prev": GENESIS_PREV}
        import hashlib
        want = hashlib.sha256(
            (GENESIS_PREV + canonical(body)).encode("utf-8")).hexdigest()
        assert row_hash(GENESIS_PREV, body) == want

    def test_a_missing_journal_is_empty_not_an_error(self, tmp_path):
        assert read_journal(tmp_path / "nothing-here.jsonl") == []

    def test_an_unparseable_line_names_its_line_number(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        make_journal(path, [(1, "chronicle", "realm", {"summary": "a beginning"})])
        path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
        with pytest.raises(JournalError) as caught:
            read_journal(path)
        assert caught.value.line == 2


# ── refusal, not repair ───────────────────────────────────────────────────────


class TestTamper:
    def _tamper(self, path, edit):
        rows = parse_jsonl(path.read_text(encoding="utf-8"))
        edit(rows)
        path.write_text("\n".join(canonical(r) for r in rows) + "\n", encoding="utf-8")

    def test_an_edited_row_is_refused(self, journal_path):
        def edit(rows):
            rows[1]["payload"]["action"] = "hands the blade back unmended"
        self._tamper(journal_path, edit)
        with pytest.raises(JournalError) as caught:
            RealmJournalSource(journal_path).load()
        assert "edited" in str(caught.value)
        assert caught.value.line == 2

    def test_a_removed_row_is_refused(self, journal_path):
        self._tamper(journal_path, lambda rows: rows.pop(2))
        with pytest.raises(JournalError) as caught:
            RealmJournalSource(journal_path).load()
        assert "chain" in str(caught.value)

    def test_a_reordered_row_is_refused(self, journal_path):
        def edit(rows):
            rows[0], rows[1] = rows[1], rows[0]
        self._tamper(journal_path, edit)
        with pytest.raises(JournalError):
            RealmJournalSource(journal_path).load()

    def test_a_refusal_indexes_nothing_rather_than_the_good_prefix(self, journal_path):
        """The tell of a sanitising reader: it keeps the rows before the break. Those rows
        are individually valid and collectively a history someone edited."""
        def edit(rows):
            rows[3]["payload"]["action"] = "was never here"
        self._tamper(journal_path, edit)
        source = RealmJournalSource(journal_path).load_soft()
        assert source.error and "edited" in source.error
        assert source.personas() == []
        assert source.entries_for("wren") == []
        assert source.status()["indexed"] == 0

    def test_a_reload_after_a_tamper_drops_the_index_it_already_had(self, journal_path):
        """The long-lived case: a source that read a good journal, and reads it again after
        someone edited the file. Keeping the earlier index would serve a history that no
        longer verifies while reporting an error nobody has to look at — stale and
        confident, which is the worst of both."""
        source = RealmJournalSource(journal_path).load()
        assert source.entries_for("wren")

        def edit(rows):
            rows[1]["payload"]["action"] = "was never here"
        self._tamper(journal_path, edit)

        source.load_soft()
        assert source.error and "edited" in source.error
        assert source.loaded is False
        assert source.entries_for("wren") == []
        assert source.personas() == []
        assert source.status()["indexed"] == 0

    def test_a_refused_journal_is_not_rewritten(self, journal_path):
        before = journal_path.read_bytes()
        def edit(rows):
            rows[1]["payload"]["action"] = "hands the blade back unmended"
        self._tamper(journal_path, edit)
        tampered = journal_path.read_bytes()
        RealmJournalSource(journal_path).load_soft()
        with pytest.raises(JournalError):
            RealmJournalSource(journal_path).load()
        assert journal_path.read_bytes() == tampered != before

    def test_verification_can_be_declined_in_the_open(self, journal_path):
        """Reading a journal you already know is broken is allowed — but only by asking."""
        def edit(rows):
            rows[1]["payload"]["action"] = "hands the blade back unmended"
        self._tamper(journal_path, edit)
        source = RealmJournalSource(journal_path, verify=False).load()
        assert source.status()["verified"] is False
        assert source.entries_for("wren")


# ── isolation ─────────────────────────────────────────────────────────────────


class TestIsolation:
    def test_one_personas_rows_never_reach_another(self, journal_path):
        source = RealmJournalSource(journal_path).load()
        assert source.personas() == ["sable", "wren"]
        wren = " ".join(e["text"] for e in source.entries_for("wren"))
        sable = " ".join(e["text"] for e in source.entries_for("sable"))
        assert "forge fire" in wren and "notched blade" in wren
        assert "crags" not in wren and "market stalls" not in wren
        assert "crags" in sable and "forge fire" not in sable

    def test_recall_stays_inside_the_persona(self, journal_path):
        source = RealmJournalSource(journal_path).load()
        # A query that matches the OTHER persona's rows returns nothing here, rather
        # than the plausible-looking wrong line.
        assert source.recall("wren", "crags wanderer", k=5) == []
        hits = source.recall("wren", "forge fire", k=5)
        assert hits and all(h["persona_id"] == "wren" for h in hits)

    def test_the_chronicle_is_everyones(self, journal_path):
        source = RealmJournalSource(journal_path).load()
        for who in ("wren", "sable"):
            assert any("autumn tithe" in h["text"] for h in source.recall(who, "tithe"))
        assert source.recall("wren", "tithe", include_world=False) == []

    def test_a_persona_with_no_rows_recalls_nothing(self, journal_path):
        source = RealmJournalSource(journal_path).load()
        assert source.recall("nobody-here", "forge", include_world=False) == []


# ── what is indexed, and how it reads ─────────────────────────────────────────


class TestIndexing:
    def test_only_memory_kinds_are_indexed(self, journal_path):
        source = RealmJournalSource(journal_path).load()
        kinds = {e["kind"] for entries in
                 (source.entries_for(p) for p in source.personas()) for e in entries}
        assert kinds == {"converse", "npc.act", "chronicle"}
        assert source.status()["rows"] == 6      # the outcome row was read, not indexed
        assert source.status()["indexed"] == 5

    def test_a_row_with_no_words_is_not_a_memory(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        make_journal(path, [(1, "npc.act", "realm", {"npc": "wren", "role": "artisan"})])
        assert RealmJournalSource(path).load().entries_for("wren") == []

    def test_persona_and_text_resolution_is_documented_order(self):
        assert persona_of({"kind": "converse", "payload": {"persona_id": "a", "npc": "b"}}) == "a"
        assert persona_of({"kind": "converse", "payload": {"npc": "b"}}) == "b"
        assert persona_of({"kind": "converse", "payload": {}, "actor": "c"}) == "c"
        # A chronicle belongs to the world however it is addressed.
        assert persona_of({"kind": "chronicle", "payload": {"npc": "wren"}}) == WORLD
        # A path-shaped or empty id is not a persona; it falls to the world bucket.
        assert persona_of({"kind": "converse", "payload": {"npc": "../wren"}}) == WORLD
        assert text_of({"payload": {"text": "a  line", "action": "a deed"}}) == "a line — a deed"
        assert text_of({"payload": {}}) == ""

    def test_recent_is_newest_first_and_recall_is_best_first(self, journal_path):
        source = RealmJournalSource(journal_path).load()
        recent = source.recent("wren", k=2, include_world=False)
        assert [e["tick"] for e in recent] == [2, 1]
        hits = source.recall("wren", "blade notched", k=5, include_world=False)
        assert hits[0]["text"].startswith("hammers a notched blade")
        assert hits[0]["score"] >= 2

    def test_an_empty_query_recalls_nothing(self, journal_path):
        source = RealmJournalSource(journal_path).load()
        assert source.recall("wren", "   ") == []
        assert source.recall("wren", "the and of") == []      # stop words are not evidence


# ── configuration ─────────────────────────────────────────────────────────────


class TestFromEnv:
    def test_unset_is_none_not_a_guess(self):
        assert realm_journal_from_env({}) is None

    def test_a_configured_journal_loads(self, journal_path):
        source = realm_journal_from_env({"REALM_JOURNAL_PATH": str(journal_path)})
        assert source is not None and source.loaded
        assert source.personas() == ["sable", "wren"]

    def test_a_broken_journal_reports_its_reason_rather_than_raising(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        path.write_text('{"v":1,"tick":0,"prev":"x","hash":"y","kind":"chronicle",'
                        '"actor":"realm","payload":{},"llm":null,"ts":"t"}\n',
                        encoding="utf-8")
        source = realm_journal_from_env({"REALM_JOURNAL_PATH": str(path)})
        assert source is not None
        assert source.loaded is False and source.error
        assert source.status()["indexed"] == 0

    def test_a_missing_file_is_a_state_not_an_error(self, tmp_path):
        source = realm_journal_from_env({"REALM_JOURNAL_PATH": str(tmp_path / "none.jsonl")})
        assert source is not None and source.loaded and source.error is None
        assert source.status()["exists"] is False


# ── the tool surface ──────────────────────────────────────────────────────────


class TestRecallTool:
    def test_campaign_recall_reads_the_journal_beside_the_notes(self, tmp_path, journal_path):
        awm = pytest.importorskip("awm", reason="the recall tool needs the notes store")
        assert awm
        from adk.packs.gobbonet.campaign_memory import (
            CampaignMemory,
            register_campaign_tools,
        )

        class Tools:
            def __init__(self):
                self.tools = {}

            def register(self, fn, name=None, description=""):
                self.tools[name or fn.__name__] = fn

        class Agent:
            def __init__(self):
                self.tools = Tools()

        memory = CampaignMemory("journaltest", db_path=tmp_path / "camp.db")
        memory.note("Wren never works past dusk.", known_by="wren")
        agent = Agent()
        source = RealmJournalSource(journal_path).load()
        # Still two tools: the journal is a source, not a new verb.
        assert register_campaign_tools(agent, memory, journal=source) == 2
        recall = agent.tools.tools["campaign_recall"]

        got = recall("wren", scene="the forge fire")
        assert got["ok"]
        assert any("past dusk" in n["value"] for n in got["notes"])
        assert got["journal"]["ok"]
        assert any("forge fire" in e["text"] for e in got["journal"]["entries"])
        # and the boundary, through the tool surface:
        assert not any("crags" in e["text"] for e in got["journal"]["entries"])

    def test_without_a_journal_the_tool_is_unchanged(self, tmp_path):
        pytest.importorskip("awm", reason="the recall tool needs the notes store")
        from adk.packs.gobbonet.campaign_memory import (
            CampaignMemory,
            register_campaign_tools,
        )

        class Tools:
            def __init__(self):
                self.tools = {}

            def register(self, fn, name=None, description=""):
                self.tools[name or fn.__name__] = fn

        class Agent:
            def __init__(self):
                self.tools = Tools()

        agent = Agent()
        memory = CampaignMemory("nojournal", db_path=tmp_path / "camp.db")
        assert register_campaign_tools(agent, memory) == 2
        assert "journal" not in agent.tools.tools["campaign_recall"]("wren")

    def test_a_tampered_journal_surfaces_as_a_reason_not_as_silence(self, tmp_path,
                                                                   journal_path):
        pytest.importorskip("awm", reason="the recall tool needs the notes store")
        from adk.packs.gobbonet.campaign_memory import _journal_section

        rows = parse_jsonl(journal_path.read_text(encoding="utf-8"))
        rows[1]["payload"]["action"] = "was never here"
        journal_path.write_text("\n".join(canonical(r) for r in rows) + "\n",
                                encoding="utf-8")
        section = _journal_section(RealmJournalSource(journal_path), "wren", "forge")
        assert section["ok"] is False
        assert "edited" in section["error"]


def test_a_row_this_reader_writes_is_one_the_engine_would_accept(tmp_path):
    """The fixture builder uses the module's own hash rule, so this pins the rule itself:
    a three-row chain verifies, and its head is the hash of the last row."""
    path = tmp_path / "journal.jsonl"
    head = make_journal(path, [
        (0, "chronicle", "realm", {"summary": "a realm begins"}),
        (1, "converse", "player", {"npc": "wren", "text": "well met"}),
        (2, "npc.act", "realm", {"npc": "wren", "action": "nods"}),
    ])
    rows = read_journal(path)
    assert verify_rows(rows) == {"rows": 3, "head": head}
    assert rows[0]["prev"] == GENESIS_PREV
    assert all(len(r["hash"]) == 64 for r in rows)
    # and the line on disk is the canonical form, byte for byte
    first_line = path.read_text(encoding="utf-8").split("\n")[0]
    assert first_line == canonical(json.loads(first_line))
