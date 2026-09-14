"""Ranking decides ORDER inside the scope, and can never widen it.

Presence already decides which notes are eligible. Once a campaign has more
notes than the brief can hold, the cap is filled in store order — so the fact
the scene is actually about loses its place to an older, duller one, and
nobody notices, because a mediocre brief still looks like a brief.

The dangerous half is the other one: a retrieval layer bolted onto a scoped
store is the obvious place for the scope to quietly stop mattering. The test
that matters here is not "does it rank well", it is "can a devastatingly
relevant note belonging to an ABSENT character reach the brief". It must not.
"""

from __future__ import annotations

import pytest

from adk.packs.gobbonet.retrieval import rank, rank_for_scene, tokenize


def _notes(*pairs):
    return [{"scope": s, "key": str(i), "value": v, "updated": i}
            for i, (s, v) in enumerate(pairs)]


class TestRanking:
    def test_the_relevant_note_comes_first(self):
        notes = _notes(
            ("c:*:*", "The bakery opens at dawn."),
            ("c:*:*", "The harbour gate is guarded by two mercenaries."),
            ("c:*:*", "Rain is expected on market day."),
        )
        out = rank(notes, "We approach the harbour gate at night.")
        assert "harbour gate" in out[0]["value"], [n["value"] for n in out]

    def test_membership_is_never_changed(self):
        notes = _notes(("c:*:*", "alpha"), ("c:*:*", "beta"), ("c:*:*", "gamma"))
        out = rank(notes, "beta")
        assert sorted(n["value"] for n in out) == ["alpha", "beta", "gamma"]

    def test_an_empty_scene_keeps_store_order_rather_than_shuffling(self):
        notes = _notes(("c:*:*", "one"), ("c:*:*", "two"))
        assert [n["value"] for n in rank(notes, "")] == ["one", "two"]

    def test_no_shared_terms_is_stable_not_random(self):
        notes = _notes(("c:*:*", "one"), ("c:*:*", "two"))
        first = [n["value"] for n in rank(notes, "zzz qqq")]
        second = [n["value"] for n in rank(notes, "zzz qqq")]
        assert first == second, "ranking is unstable between identical turns"

    def test_a_term_in_every_note_does_not_score_negative(self):
        """Plain IDF goes negative when a term appears everywhere, which would
        silently bury the campaign's most common subject."""
        notes = _notes(("c:*:*", "the gate is shut"), ("c:*:*", "the gate is open"))
        out = rank(notes, "gate")
        assert len(out) == 2 and all("gate" in n["value"] for n in out)

    def test_ties_break_toward_the_newer_note(self):
        notes = [
            {"scope": "c:*:*", "key": "a", "value": "the vault is sealed", "updated": 1},
            {"scope": "c:*:*", "key": "b", "value": "the vault is sealed", "updated": 9},
        ]
        assert rank(notes, "vault")[0]["key"] == "b"

    def test_limit_truncates_after_ranking_not_before(self):
        notes = _notes(
            ("c:*:*", "irrelevant one"), ("c:*:*", "irrelevant two"),
            ("c:*:*", "the lighthouse keeper lies"),
        )
        top = rank(notes, "lighthouse", limit=1)
        assert len(top) == 1 and "lighthouse" in top[0]["value"]

    def test_scene_is_taken_from_recent_turns(self):
        notes = _notes(("c:*:*", "the ferry runs at noon"), ("c:*:*", "unrelated"))
        msgs = [{"role": "user", "content": "where is the ferry?"}]
        assert "ferry" in rank_for_scene(notes, msgs)[0]["value"]

    def test_stopwords_do_not_drive_the_ranking(self):
        assert "the" not in tokenize("The the THE")
        notes = _notes(("c:*:*", "the the the"), ("c:*:*", "dragon"))
        assert rank(notes, "the dragon")[0]["value"] == "dragon"


class TestScopeIsNotWidened:
    """The property the whole design rests on."""

    def test_an_absent_characters_note_cannot_be_retrieved(self, tmp_path):
        awm = pytest.importorskip("awm")
        del awm
        from adk.packs.gobbonet.campaign_memory import CampaignMemory

        mem = CampaignMemory("rank", db_path=tmp_path / "r.db")
        mem.note("The lighthouse keeper is the murderer.", known_by="Vex")
        mem.note("The lighthouse was built in 1802.", known_by="*")

        # A scene screaming the exact terms of Vex's secret...
        scene = "lighthouse keeper murderer lighthouse keeper murderer"
        without = mem.brief([], scene=scene)
        assert "murderer" not in without, (
            "ranking reached across a scope boundary — the whole point of the "
            "store is that it cannot")
        assert "1802" in without, "world note should still be there"

        # ...and with Vex present, it IS retrievable, so this is not vacuous.
        assert "murderer" in mem.brief(["vex"], scene=scene)

    def test_ranking_failure_leaves_the_brief_working(self, tmp_path, monkeypatch):
        """Ranking is an enhancement. If it raises, the brief must still be a
        brief — a retrieval bug must not take chat down with it."""
        awm = pytest.importorskip("awm")
        del awm
        import adk.packs.gobbonet.retrieval as r
        from adk.packs.gobbonet.campaign_memory import CampaignMemory

        mem = CampaignMemory("rankfail", db_path=tmp_path / "rf.db")
        mem.note("The bridge is out.")
        monkeypatch.setattr(r, "rank", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert "bridge is out" in mem.brief([], scene="bridge")
