"""Character cards and lorebooks, both directions, against campaign memory.

GobboNet's mod-pack artifacts are character CARDS and LOREBOOKS — the format
people already trade. Until now they were inert to the agent: the harness could
keep its own notes (see `campaign_memory`) but could not read the pack a player
brought, and could not hand back what it had learned.

The mapping is the same one campaign memory already enforces, which is why it
works rather than merely converts:

    card.personality / writingStyle / greeting  -> notes KNOWN BY that character
    card.startingLore                           -> notes everyone knows (WORLD)

So importing a card is not a copy, it is a SCOPING decision: what the character
knows becomes theirs, and what the setting establishes becomes the table's. A
second character imported later cannot see the first one's notes, because they
are sibling scopes and the store will not cross them.

Export is the inverse and is deliberately LOSSY IN ONE DIRECTION ONLY: a card
built from memory carries what the harness actually knows, and says which
fields it could not fill rather than inventing them. Every function returns a
`dropped` list for exactly that reason — a converter that silently discards is
how a player loses half a character and blames the model.

`avatar` is never fetched, embedded or rewritten: it is a path or a data URI in
someone else's file, and carrying it would mean shipping their image around.
It is reported as dropped, by name, so the omission is visible.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

#: Card fields that carry what the CHARACTER knows or is.
_CHARACTER_FIELDS = ("personality", "writingStyle", "greeting")

#: Card fields deliberately not imported, and why. Named rather than ignored.
_NOT_IMPORTED = {
    "avatar": "an image path/data-URI belonging to the card's author",
    "altGreetings": "alternate openings are presentation, not knowledge",
    "altGreetingsEnabled": "a UI toggle",
    "id": "GobboNet's own identifier, meaningless outside its store",
}

#: A lorebook block is split on blank lines, same as the platform-side
#: converter. Lossy and honest about it: headers become nothing special,
#: because guessing keywords from prose invents structure the author never
#: wrote.
_BLOCK = re.compile(r"\n\s*\n")


def card_to_memory(card: Dict[str, Any], memory: Any, *,
                   arc: str = "*") -> Dict[str, Any]:
    """Import a GobboNet character card INTO campaign memory, scoped.

    The character's own traits land at their scope; `startingLore` lands at the
    world scope, because a setting is not a secret. Returns what was written
    and what was deliberately not.
    """
    if not isinstance(card, dict):
        return {"ok": False, "error": "a card must be a dict"}
    if not memory or not getattr(memory, "available", lambda: False)():
        reason = getattr(memory, "unavailable_reason", lambda: "no memory")()
        return {"ok": False, "error": reason}

    name = str(card.get("name") or card.get("character") or "").strip()
    if not name:
        return {"ok": False, "error": "the card has no name — nothing to scope it to"}

    written: List[Dict[str, Any]] = []
    dropped: List[str] = []

    for field in _CHARACTER_FIELDS:
        text = str(card.get(field) or "").strip()
        if not text:
            continue
        res = memory.note(f"{field}: {text}", known_by=name, arc=arc)
        (written if res.get("ok") else dropped).append(
            {"field": field, "scope": res.get("scope")} if res.get("ok")
            else f"{field}: {res.get('error')}")

    lore = str(card.get("startingLore") or "").strip()
    lore_blocks = 0
    if lore:
        for block in filter(None, (b.strip() for b in _BLOCK.split(lore))):
            res = memory.note(block, known_by="*", arc=arc)
            if res.get("ok"):
                lore_blocks += 1
            else:
                dropped.append(f"lore block: {res.get('error')}")

    for field, why in _NOT_IMPORTED.items():
        if card.get(field):
            dropped.append(f"{field} — {why}")

    return {"ok": True, "character": name, "notes_written": len(written),
            "lore_blocks": lore_blocks, "dropped": dropped}


def lorebook_to_memory(starting_lore: str, memory: Any, *,
                       arc: str = "*") -> Dict[str, Any]:
    """Import a bare lorebook (no card) as world-scoped notes."""
    return card_to_memory({"name": "_lore_", "startingLore": starting_lore},
                          memory, arc=arc) if starting_lore else {
        "ok": False, "error": "empty lorebook"}


def memory_to_card(character: str, memory: Any, *, arc: str = "*",
                   name: Optional[str] = None) -> Dict[str, Any]:
    """Export what the harness knows about ONE character back into a card.

    Only that character's scope and the world scope are read — the same
    boundary a scene brief respects — so exporting one character cannot leak
    another's secrets into a file the player will share.
    """
    if not memory or not getattr(memory, "available", lambda: False)():
        reason = getattr(memory, "unavailable_reason", lambda: "no memory")()
        return {"ok": False, "error": reason}

    who = (character or "").strip()
    if not who or who == "*":
        return {"ok": False, "error": "name the character to export"}

    notes = memory.notes_for(who, arc=arc)
    mine, world = [], []
    for n in notes:
        seg = str(n.get("scope", "")).split(":")
        (world if len(seg) == 3 and seg[1] == "*" else mine).append(str(n.get("value", "")))

    card: Dict[str, Any] = {"name": name or who}
    dropped: List[str] = []

    # Re-attach the fields the import prefixed, and keep anything else as
    # personality rather than discarding it.
    rest: List[str] = []
    for value in mine:
        matched = False
        for field in _CHARACTER_FIELDS:
            if value.startswith(field + ": "):
                card[field] = value[len(field) + 2:]
                matched = True
                break
        if not matched:
            rest.append(value)
    if rest:
        card["personality"] = "\n\n".join(
            filter(None, [card.get("personality", ""), *rest]))
    if world:
        card["startingLore"] = "\n\n".join(world)

    for field in _CHARACTER_FIELDS:
        if not card.get(field):
            dropped.append(f"{field} — nothing in memory to fill it")
    if not world:
        dropped.append("startingLore — no world notes recorded")
    dropped.append("avatar — never exported; it is the author's image")

    return {"ok": True, "card": card, "notes_read": len(notes),
            "dropped": dropped}


def register_card_tools(agent: Any, memory: Any) -> int:
    """Give the agent the import/export pair, beside the note-taking pair."""

    def campaign_import_card(card: Dict[str, Any]) -> dict:
        """Import a GobboNet character card into campaign memory. The
        character's traits become notes only THEY know; startingLore becomes
        world state everyone knows."""
        return card_to_memory(card, memory)

    def campaign_import_lorebook(starting_lore: str) -> dict:
        """Import a lorebook's text as world-scoped campaign notes."""
        return lorebook_to_memory(starting_lore, memory)

    def campaign_export_card(character: str, name: str = "") -> dict:
        """Build a GobboNet character card from what the harness knows about
        ONE character. Reads only their scope plus world state, so exporting
        cannot leak another character's secrets."""
        return memory_to_card(character, memory, name=name or None)

    n = 0
    for fn in (campaign_import_card, campaign_import_lorebook,
               campaign_export_card):
        try:
            agent.tools.register(fn, name=fn.__name__,
                                 description=(fn.__doc__ or "").strip())
            n += 1
        except Exception:  # noqa: BLE001 - a registry refusal must not kill chat
            continue
    return n
