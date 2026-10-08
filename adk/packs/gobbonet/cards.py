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

import json
import re
import sys
from typing import Any, Dict, List, Optional

#: Card fields that carry what the CHARACTER knows or is.
_CHARACTER_FIELDS = ("personality", "writingStyle", "greeting")

# ── the engine's card ─────────────────────────────────────────────────────────
#
# The realm engine serves a character-card-family card per NPC. Its prose fields land
# at the character's scope exactly as the pack's own fields do; its identity fields
# (who this is, how it is rated, what it is tagged) are not prose and are kept verbatim
# in ONE note, so an export can hand back the same card rather than a paraphrase of it.
#
#: Engine card fields that are PROSE the character knows or is.
ENGINE_PROSE_FIELDS = ("description", "personality", "scenario", "first_mes", "mes_example")
#: Engine card fields that identify the card rather than describe the character.
ENGINE_META_FIELDS = ("persona_id", "name", "tags", "rating")
#: The whole engine card contract this module round-trips.
ENGINE_CARD_FIELDS = tuple(dict.fromkeys(ENGINE_META_FIELDS + ENGINE_PROSE_FIELDS))
#: The one note that carries the identity fields, verbatim, as canonical JSON.
_META_PREFIX = "card-meta"

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


def canonical_card(card: Dict[str, Any]) -> str:
    """The card as ONE comparable string: canonical JSON over the engine contract.

    Sorted keys, no whitespace, non-ASCII literal — the same spelling the engine's own
    canonical JSON uses, so both halves compare a card the same way. Prose is normalised
    (surrounding whitespace stripped) because the store strips it on the way in; the
    round-trip guarantee is byte-stability in THIS form, which is the form that can
    actually be promised.
    """
    out: Dict[str, Any] = {}
    for field in ENGINE_CARD_FIELDS:
        value = card.get(field)
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, str):
            out[field] = value.strip()
        elif isinstance(value, list):
            out[field] = [str(v) for v in value]
        else:
            out[field] = value
    return json.dumps(out, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _lore_blocks(card: Dict[str, Any]) -> List[str]:
    """World-scoped text from either lorebook spelling: the pack's own `startingLore`
    string, or a character-card `character_book` / `lorebook` with entries. Entry keys,
    ordering and firing rules are NOT imported — they are matcher configuration, and
    campaign memory ranks by presence, not by trigger word."""
    blocks: List[str] = []
    lore = str(card.get("startingLore") or "").strip()
    if lore:
        blocks += [b.strip() for b in _BLOCK.split(lore) if b.strip()]
    book = card.get("character_book") or card.get("lorebook")
    if isinstance(book, dict):
        entries = book.get("entries")
        rows = (entries.values() if isinstance(entries, dict)
                else entries if isinstance(entries, list) else [])
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            content = str(entry.get("content") or "").strip()
            if content:
                blocks.append(content)
    return blocks


def card_to_memory(card: Dict[str, Any], memory: Any, *,
                   arc: str = "*") -> Dict[str, Any]:
    """Import a character card INTO campaign memory, scoped.

    Accepts both the pack's own card and the realm engine's: prose fields
    (`description`, `personality`, `scenario`, `first_mes`, `mes_example`, and the pack's
    `writingStyle` / `greeting`) land at the character's scope; a lorebook — a
    `startingLore` string or a `character_book` with entries — lands at the world scope,
    because a setting is not a secret. Identity fields (`persona_id`, `name`, `tags`,
    `rating`) are kept verbatim so `memory_to_card` can hand the same card back.

    Returns what was written and what was deliberately not.
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

    meta = {f: card[f] for f in ENGINE_META_FIELDS
            if card.get(f) not in (None, "", [])}
    # A name alone is not identity worth a note: it IS the scope, and the export
    # reconstructs it from there. Writing one anyway would add a note to every legacy
    # card that carries nothing the export could not already say.
    if set(meta) - {"name"}:
        res = memory.note(f"{_META_PREFIX}: {canonical_card(meta)}",
                          known_by=name, arc=arc)
        if res.get("ok"):
            written.append({"field": _META_PREFIX, "scope": res.get("scope")})
        else:
            dropped.append(f"{_META_PREFIX}: {res.get('error')}")

    for field in ENGINE_PROSE_FIELDS:
        text = str(card.get(field) or "").strip()
        if not text or field in _CHARACTER_FIELDS:
            continue          # personality is in both families; the loop below writes it
        res = memory.note(f"{field}: {text}", known_by=name, arc=arc)
        (written if res.get("ok") else dropped).append(
            {"field": field, "scope": res.get("scope")} if res.get("ok")
            else f"{field}: {res.get('error')}")

    for field in _CHARACTER_FIELDS:
        text = str(card.get(field) or "").strip()
        if not text:
            continue
        res = memory.note(f"{field}: {text}", known_by=name, arc=arc)
        (written if res.get("ok") else dropped).append(
            {"field": field, "scope": res.get("scope")} if res.get("ok")
            else f"{field}: {res.get('error')}")

    # The illustration reference (see `illustrate`): a gallery id the card carries so
    # the same face comes back. Restored at the character's scope, never the world's.
    ref = card.get("reference")
    if isinstance(ref, dict) and ref.get("media_id") not in (None, ""):
        from adk.packs.gobbonet.illustrate import set_reference
        res = set_reference(memory, name, ref)
        if res.get("ok"):
            written.append({"field": "reference", "scope": res.get("scope")})
        else:
            dropped.append(f"reference: {res.get('error')}")

    lore_blocks = 0
    for block in _lore_blocks(card):
        res = memory.note(block, known_by="*", arc=arc)
        if res.get("ok"):
            lore_blocks += 1
        else:
            dropped.append(f"lore block: {res.get('error')}")

    for field, why in _NOT_IMPORTED.items():
        if card.get(field):
            dropped.append(f"{field} — {why}")
    known = set(ENGINE_CARD_FIELDS) | set(_CHARACTER_FIELDS) | set(_NOT_IMPORTED) | {
        "character", "startingLore", "character_book", "lorebook", "reference"}
    for field in sorted(set(card) - known):
        dropped.append(f"{field} — outside the card contract this pack round-trips")

    return {"ok": True, "character": name, "persona_id": card.get("persona_id") or None,
            "notes_written": len(written), "lore_blocks": lore_blocks, "dropped": dropped}


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

    # A default limit would silently truncate a character with a full card plus notes,
    # and a half-exported card reads as a lossy converter rather than a clipped read.
    notes = memory.notes_for(who, arc=arc, limit=200)
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
        if value.startswith("card-ref: "):
            # The stored illustration reference — handed back as a field, never as prose.
            try:
                ref = json.loads(value[len("card-ref: "):])
            except ValueError:
                ref = None
            if isinstance(ref, dict) and ref.get("media_id") not in (None, ""):
                card["reference"] = ref
            else:
                dropped.append("reference — unreadable, not restored")
            continue
        if value.startswith(_META_PREFIX + ": "):
            # The identity fields, verbatim — this is what makes the round trip byte-stable.
            try:
                meta = json.loads(value[len(_META_PREFIX) + 2:])
            except ValueError:
                dropped.append(f"{_META_PREFIX} — unreadable, identity fields not restored")
                meta = {}
            if isinstance(meta, dict):
                for field in ENGINE_META_FIELDS:
                    if meta.get(field) not in (None, "", []):
                        card[field] = meta[field]
            continue
        for field in ENGINE_PROSE_FIELDS + _CHARACTER_FIELDS:
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


# ── the contract, as data ─────────────────────────────────────────────────────
#
# The engine serves this shape at its per-NPC card route. It lives here as a Python
# literal rather than a JSON file on purpose: a wheel ships modules, and a fixture that
# only exists in the source tree cannot be used by the self-test a stranger runs after
# `pip install`. Derived from a built-in town NPC so the prose is the engine's own.

ENGINE_CARD_FIXTURE: Dict[str, Any] = {
    "persona_id": "wren",
    "name": "Wren the Smith",
    "description": ("Soot-streaked and square-shouldered, arms like anvils. She keeps the "
                    "town forge at the end of the market row, and the fire is never out."),
    "personality": ("Blunt, unhurried, unimpressed by titles. Judges people by what they "
                    "do with their hands and says so."),
    "scenario": ("The traveller comes to the forge with a notched blade and not enough "
                 "coin."),
    "first_mes": ("Wren wipes her brow and sets down a glowing tong. \"You've the look of "
                  "someone who breaks more blades than they keep.\""),
    "mes_example": ("<START>\n{{user}}: Can you fix this?\n"
                    "{{char}}: \"Good steel is earned, not bought. Temper yourself first, "
                    "then we'll talk.\""),
    "tags": ["artisan", "smith", "town"],
    "rating": "g",
}

#: The lorebook the engine builds from the world gazetteer and the chronicle. The internal
#: shape is shown here; the embedded card-book spelling (`character_book` with an entries
#: array, or an entries map keyed by index) imports identically.
ENGINE_LOREBOOK_FIXTURE: Dict[str, Any] = {
    "name": "town gazetteer",
    "description": "Places and events the town takes for granted.",
    "scanDepth": 2,
    "tokenBudget": 1200,
    "recursive": False,
    "entries": [
        {"keys": ["forge", "smith"], "content": "The Forge sits at the end of the market "
                                                "row; its fire has not gone out in living memory.",
         "comment": "gazetteer: the forge", "enabled": True, "constant": False},
        {"keys": ["tavern", "tankard"],
         "content": "The Wandering Tankard takes travellers' coin and travellers' "
                    "rumours at the same counter.",
         "comment": "gazetteer: the tavern", "enabled": True, "constant": False},
        {"keys": ["tithe", "taxes"], "content": "The autumn tithe came in short, and the "
                                                "guard has been paid before the granary.",
         "comment": "chronicle: the short tithe", "enabled": True, "constant": False},
    ],
}


def self_test() -> int:
    """Round-trip the engine's card through campaign memory and back, byte-stably.

    Exit 0 clean, 1 a broken contract, 2 could not run (no store to round-trip through) —
    a self-test that quietly skips proves nothing, so an absent dependency is not a pass.
    """
    import tempfile
    from pathlib import Path

    failures: List[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'ok' if ok else 'FAIL'}] {label}{'' if ok or not detail else f' ({detail})'}")
        if not ok:
            failures.append(label)

    # -- pure: the comparison form is stable and covers the contract
    once = canonical_card(ENGINE_CARD_FIXTURE)
    check("canonical_card is stable", once == canonical_card(dict(ENGINE_CARD_FIXTURE)))
    check("canonical_card sorts keys", once.startswith('{"description":'), once[:24])
    check("every contract field survives it",
          all(f'"{f}"' in once for f in ENGINE_CARD_FIELDS))
    padded = dict(ENGINE_CARD_FIXTURE, scenario=f"  {ENGINE_CARD_FIXTURE['scenario']}  ")
    check("surrounding whitespace is normalised", canonical_card(padded) == once)

    # -- pure: both lorebook spellings read the same
    listed = _lore_blocks({"character_book": ENGINE_LOREBOOK_FIXTURE})
    mapped = _lore_blocks({"character_book": {"entries": {
        str(i): e for i, e in enumerate(ENGINE_LOREBOOK_FIXTURE["entries"])}}})
    check("a lorebook's entries become world blocks", len(listed) == 3, str(len(listed)))
    check("the entries-as-a-map spelling reads identically", listed == mapped)

    try:
        from adk.packs.gobbonet.campaign_memory import CampaignMemory
    except ImportError as exc:
        print(f"CANNOT RUN: campaign memory is not importable: {exc}")
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        memory = CampaignMemory("selftest", db_path=Path(tmp) / "selftest.db")
        if not memory.available():
            print(f"CANNOT RUN: {memory.unavailable_reason()}")
            return 2
        card = dict(ENGINE_CARD_FIXTURE, character_book=ENGINE_LOREBOOK_FIXTURE)
        imported = card_to_memory(card, memory)
        check("the engine card imports", bool(imported.get("ok")), str(imported.get("error")))
        check("its lorebook lands at the world scope", imported.get("lore_blocks") == 3,
              str(imported.get("lore_blocks")))
        exported = memory_to_card(ENGINE_CARD_FIXTURE["name"], memory)
        check("it exports again", bool(exported.get("ok")), str(exported.get("error")))
        got = exported.get("card") or {}
        check("the round trip is byte-stable", canonical_card(got) == once,
              canonical_card(got)[:80])
        check("identity fields come back verbatim",
              got.get("persona_id") == "wren" and got.get("tags") == ["artisan", "smith", "town"]
              and got.get("rating") == "g")
        # Release the store's file handle: on Windows an open SQLite connection makes the
        # temporary directory undeletable, and the self-test would die AFTER passing.
        closer = getattr(getattr(memory, "_store", None), "close", None)
        if callable(closer):
            closer()

    print("SELF-TEST " + ("PASS" if not failures else f"FAIL {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a command
    sys.exit(self_test())
