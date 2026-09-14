"""Cards and lorebooks are a SCOPING decision, not a copy.

GobboNet's mod-pack artifacts are the format people already trade. Importing
one must land the character's traits at THEIR scope and the setting at the
world's — anything else quietly turns a shared lorebook into a private secret,
or a private secret into a shared one, and neither is visible in the file.

Every test asserts a positive AND its boundary, because a converter that
returns empty for everything passes every "returns nothing" assertion while
being completely inert.
"""

from __future__ import annotations

import pytest

pytest.importorskip("awm", reason="card import/export needs the memory store")

from adk.packs.gobbonet.campaign_memory import WORLD, CampaignMemory  # noqa: E402
from adk.packs.gobbonet.cards import (  # noqa: E402
    card_to_memory,
    lorebook_to_memory,
    memory_to_card,
    register_card_tools,
)

CARD = {
    "name": "Vex",
    "personality": "Sardonic, allergic to sunlight, never says why.",
    "writingStyle": "Clipped. Present tense.",
    "greeting": "You again.",
    "startingLore": "The city gates close at dusk.\n\nRent is due on the first.",
    "avatar": "data:image/png;base64,AAAA",
    "altGreetings": ["hi", "hello"],
    "id": "gobbonet-local-17",
}


@pytest.fixture()
def mem(tmp_path):
    return CampaignMemory("cardcamp", db_path=tmp_path / "c.db")


class TestImport:
    def test_traits_land_on_the_character_and_lore_on_the_world(self, mem):
        res = card_to_memory(CARD, mem)
        assert res["ok"] and res["character"] == "Vex"
        assert res["notes_written"] == 3, res
        assert res["lore_blocks"] == 2, res

        # the setting is everyone's...
        world = mem.brief([])
        assert "gates close at dusk" in world
        assert "Rent is due" in world
        # ...and the character's traits are NOT, until they are present
        assert "Sardonic" not in world

        with_vex = mem.brief(["vex"])
        assert "Sardonic" in with_vex

    def test_another_character_cannot_see_those_traits(self, mem):
        card_to_memory(CARD, mem)
        assert "Sardonic" not in mem.brief(["mira"])

    def test_unimported_fields_are_named_not_silently_dropped(self, mem):
        dropped = " ".join(card_to_memory(CARD, mem)["dropped"])
        for field in ("avatar", "altGreetings", "id"):
            assert field in dropped, f"{field} vanished without being reported"

    def test_a_nameless_card_is_refused_rather_than_guessed_at(self, mem):
        res = card_to_memory({"personality": "x"}, mem)
        assert res["ok"] is False and "name" in res["error"]

    def test_a_bare_lorebook_is_world_scoped(self, mem):
        res = lorebook_to_memory("Trains run at midnight.", mem)
        assert res["ok"], res
        assert "Trains run at midnight" in mem.brief([])

    def test_import_without_a_store_says_what_to_install(self, tmp_path):
        class NoStore:
            @staticmethod
            def available():
                return False

            @staticmethod
            def unavailable_reason():
                return "pip install awm"

        res = card_to_memory(CARD, NoStore())
        assert res["ok"] is False and "awm" in res["error"]


class TestExport:
    def test_round_trip_returns_the_traits(self, mem):
        card_to_memory(CARD, mem)
        out = memory_to_card("Vex", mem)
        assert out["ok"], out
        card = out["card"]
        assert card["name"] == "Vex"
        assert "Sardonic" in card["personality"]
        assert card["writingStyle"].startswith("Clipped")
        assert "gates close at dusk" in card["startingLore"]

    def test_export_cannot_leak_another_characters_secret(self, mem):
        """The property that matters: a card you hand to a player must not
        carry what a different character knows."""
        card_to_memory(CARD, mem)
        mem.note("The landlord is a vampire.", known_by="Mira")
        out = memory_to_card("Vex", mem)
        blob = repr(out["card"])
        assert "vampire" not in blob, "another character's secret reached the card"
        # and Mira's own export DOES carry it, so the test is not vacuous
        assert "vampire" in repr(memory_to_card("Mira", mem)["card"])

    def test_avatar_is_never_exported_and_says_so(self, mem):
        card_to_memory(CARD, mem)
        out = memory_to_card("Vex", mem)
        assert "avatar" not in out["card"]
        assert any("avatar" in d for d in out["dropped"])

    def test_empty_memory_exports_an_honest_skeleton(self, mem):
        out = memory_to_card("Nobody", mem)
        assert out["ok"] and out["card"]["name"] == "Nobody"
        assert len(out["dropped"]) >= 3, out["dropped"]

    def test_exporting_the_wildcard_is_refused(self, mem):
        assert memory_to_card("*", mem)["ok"] is False


class _Registry:
    def __init__(self):
        self.tools = {}

    def register(self, fn, name=None, description=None, **_):
        self.tools[name or fn.__name__] = fn


class _Agent:
    def __init__(self):
        self.tools = _Registry()


class TestTools:
    def test_the_agent_can_import_and_export_through_the_tools(self, mem):
        agent = _Agent()
        assert register_card_tools(agent, mem) == 3
        imp = agent.tools.tools["campaign_import_card"]
        exp = agent.tools.tools["campaign_export_card"]
        assert imp(CARD)["ok"]
        got = exp("Vex")
        assert got["ok"] and "Sardonic" in got["card"]["personality"]

    def test_a_refusing_registry_does_not_kill_chat(self, mem):
        class Refuses:
            class tools:  # noqa: N801 - shape stub
                @staticmethod
                def register(*a, **k):
                    raise RuntimeError("no")

        assert register_card_tools(Refuses(), mem) == 0
