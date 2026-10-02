"""Engine cards: what the realm serves imports as scoped notes and exports back unchanged.

WHY THESE CASES
---------------
A converter is judged on what it LOSES, and a lossy converter passes every "it returned a
card" assertion. So the central arm is byte-stability: import the engine's card, export it,
and compare the canonical form character for character. Around that:

* the identity fields (`persona_id`, `tags`, `rating`) must come back as themselves — a list
  that returns as a string, or a rating that returns as `None`, is how a character quietly
  loses its content gate;
* the scoping must hold, because a card is where a leak would be least visible: the
  character's prose is theirs, the lorebook is the table's, and a second character imported
  later must not see the first one's;
* a field outside the contract must be REPORTED, not dropped in silence — the module's own
  standing promise is that it says what it could not carry.
"""

from __future__ import annotations

import pytest

awm = pytest.importorskip("awm", reason="card import/export needs the notes store")

from adk.packs.gobbonet.campaign_memory import (  # noqa: E402
    CampaignMemory,
)
from adk.packs.gobbonet.cards import (  # noqa: E402
    ENGINE_CARD_FIELDS,
    ENGINE_CARD_FIXTURE,
    ENGINE_LOREBOOK_FIXTURE,
    ENGINE_META_FIELDS,
    ENGINE_PROSE_FIELDS,
    canonical_card,
    card_to_memory,
    memory_to_card,
    self_test,
)


@pytest.fixture()
def mem(tmp_path):
    store = CampaignMemory("cardtest", db_path=tmp_path / "camp.db")
    yield store
    closer = getattr(getattr(store, "_store", None), "close", None)
    if callable(closer):
        closer()


@pytest.fixture()
def card():
    return dict(ENGINE_CARD_FIXTURE)


# ── the round trip ────────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_the_engine_card_comes_back_byte_stable(self, mem, card):
        assert card_to_memory(card, mem)["ok"]
        exported = memory_to_card(card["name"], mem)
        assert exported["ok"]
        assert canonical_card(exported["card"]) == canonical_card(card)

    def test_identity_fields_return_as_themselves(self, mem, card):
        card_to_memory(card, mem)
        got = memory_to_card(card["name"], mem)["card"]
        assert got["persona_id"] == "wren"
        assert got["tags"] == ["artisan", "smith", "town"]
        assert got["rating"] == "g"
        assert got["name"] == "Wren the Smith"

    def test_every_prose_field_survives(self, mem, card):
        card_to_memory(card, mem)
        got = memory_to_card(card["name"], mem)["card"]
        for field in ENGINE_PROSE_FIELDS:
            assert got[field] == card[field], field

    def test_a_partial_card_round_trips_without_inventing_fields(self, mem):
        sparse = {"persona_id": "mara", "name": "Mara the Herbalist",
                  "description": "Surrounded by hanging bundles of dried herbs."}
        assert card_to_memory(sparse, mem)["ok"]
        got = memory_to_card("Mara the Herbalist", mem)["card"]
        assert canonical_card(got) == canonical_card(sparse)
        for absent in ("scenario", "first_mes", "mes_example", "tags", "rating"):
            assert absent not in got

    def test_the_round_trip_survives_non_ascii_and_quotes(self, mem):
        card = {"persona_id": "sable", "name": "Sable",
                "description": "A cloaked figure — eyes like a cat's, \"never\" still.",
                "tags": ["outsider"], "rating": "r18"}
        card_to_memory(card, mem)
        got = memory_to_card("Sable", mem)["card"]
        assert canonical_card(got) == canonical_card(card)
        assert "—" in got["description"] and '"never"' in got["description"]

    def test_a_card_whose_only_identity_is_its_name_still_round_trips(self, mem):
        """No identity note is written for a name — it is the scope. The export must
        reconstruct it from there rather than losing it."""
        card = {"name": "Bryony the Matron",
                "description": "A warm, broad-hipped woman tending a full hearth."}
        assert card_to_memory(card, mem)["notes_written"] == 1
        got = memory_to_card("Bryony the Matron", mem)["card"]
        assert canonical_card(got) == canonical_card(card)

    def test_rating_is_not_lost_when_it_is_the_only_meta(self, mem):
        """A rating that vanishes is a content gate that silently opens."""
        card = {"name": "Unrated One", "rating": "r18", "description": "…"}
        card_to_memory(card, mem)
        assert memory_to_card("Unrated One", mem)["card"]["rating"] == "r18"


# ── scoping ───────────────────────────────────────────────────────────────────


class TestScoping:
    def test_the_lorebook_is_the_tables_and_the_prose_is_the_characters(self, mem, card):
        result = card_to_memory(dict(card, character_book=ENGINE_LOREBOOK_FIXTURE), mem)
        assert result["lore_blocks"] == 3
        # World lore reaches a character who was never imported…
        assert "autumn tithe" in mem.brief([])
        # …and the card's own prose does not.
        assert "unimpressed by titles" not in mem.brief([])
        assert "unimpressed by titles" in mem.brief(["wren the smith"])

    def test_a_second_card_cannot_see_the_first(self, mem, card):
        card_to_memory(card, mem)
        card_to_memory({"persona_id": "sable", "name": "Sable the Wanderer",
                        "description": "A cloaked figure with eyes like a cat's."}, mem)
        sable = memory_to_card("Sable the Wanderer", mem)["card"]
        assert "anvils" not in canonical_card(sable)
        assert sable.get("persona_id") == "sable"

    def test_exporting_one_character_cannot_leak_another(self, mem, card):
        card_to_memory(card, mem)
        mem.note("Sable is the one who took the ledger.", known_by="Sable the Wanderer")
        exported = canonical_card(memory_to_card(card["name"], mem)["card"])
        assert "ledger" not in exported

    def test_the_lorebook_entries_map_spelling_imports_identically(self, mem, card):
        book = {"entries": {str(i): e for i, e
                            in enumerate(ENGINE_LOREBOOK_FIXTURE["entries"])}}
        assert card_to_memory(dict(card, character_book=book), mem)["lore_blocks"] == 3


# ── honesty about what it did not carry ───────────────────────────────────────


class TestDropped:
    def test_a_field_outside_the_contract_is_named(self, mem, card):
        result = card_to_memory(dict(card, creator="someone", extensions={"coc": {}}), mem)
        dropped = " ".join(result["dropped"])
        assert "creator" in dropped and "extensions" in dropped

    def test_the_avatar_is_never_carried_and_says_so(self, mem, card):
        result = card_to_memory(dict(card, avatar="/path/to/portrait.png"), mem)
        assert any("avatar" in d for d in result["dropped"])
        exported = memory_to_card(card["name"], mem)
        assert any("avatar" in d for d in exported["dropped"])
        assert "avatar" not in exported["card"]

    def test_a_nameless_card_is_refused_rather_than_scoped_to_nothing(self, mem):
        assert card_to_memory({"persona_id": "x", "description": "…"}, mem)["ok"] is False

    def test_a_card_that_is_not_a_dict_is_refused(self, mem):
        assert card_to_memory("not a card", mem)["ok"] is False


# ── the contract itself ───────────────────────────────────────────────────────


class TestContract:
    def test_the_field_lists_agree(self):
        assert set(ENGINE_CARD_FIELDS) == set(ENGINE_META_FIELDS) | set(ENGINE_PROSE_FIELDS)
        assert len(ENGINE_CARD_FIELDS) == len(set(ENGINE_CARD_FIELDS))

    def test_the_fixture_exercises_every_field(self):
        """A fixture that omits a field is a contract nobody tested."""
        for field in ENGINE_CARD_FIELDS:
            assert ENGINE_CARD_FIXTURE.get(field), field

    def test_canonical_card_ignores_anything_outside_the_contract(self):
        assert canonical_card(dict(ENGINE_CARD_FIXTURE, creator="x")) == \
            canonical_card(ENGINE_CARD_FIXTURE)

    def test_the_pack_self_test_passes(self, capsys):
        assert self_test() == 0
        assert "SELF-TEST PASS" in capsys.readouterr().out
