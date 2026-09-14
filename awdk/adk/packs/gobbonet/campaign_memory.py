"""Campaign memory — the harness keeps the notes, scoped by who knows them.

Long roleplay campaigns decay, and the community named the failure precisely:
after ~300 messages the model loops and forgets ("single thread declination"),
per-NPC knowledge is hand-managed, and the working fix today is a HUMAN
curating concise notes between scenes. This module makes the harness do that
curation, on top of `awm` — the portable scoped memory — so the notes are
durable, listable, editable, and above all SCOPED.

The mapping is exact, not analogical:

    awm scope segment      campaign meaning
    tenant                 the campaign
    user                   the character who KNOWS the fact ("*" = everyone)
    project                the arc/thread ("*" = campaign-wide)

Three awm properties carry the whole design:

- **A write lands at exactly one scope.** A secret recorded as known by Vex is
  Vex's, not the table's.
- **Siblings never see each other.** Two characters are sibling scopes, so one
  NPC's knowledge structurally cannot leak into a scene the other is carrying.
  That IS theory-of-mind — enforced by the store, not prompt discipline.
- **A read includes ancestors.** Recalling for a character surfaces their facts
  plus campaign-wide world state, nearest-weighted.

Recall is triggered by PRESENCE: a character's notes enter the brief only when
that character is present in (named by) the recent turns — "information entries
tied to specific triggers, like certain characters being present or spoken
about", which is the community's own trigger-RAG design, given a real store.

`awm` is an optional dependency (pip install awm). Everything here fails SOFT
and SAYS SO: without awm the pack works exactly as before, the tools report
what to install, and the brief is empty rather than wrong.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("adk.gobbonet.campaign")

#: The "everyone knows this" character segment — awm's own wildcard.
WORLD = "*"

#: How many trailing messages are scanned for character presence.
_PRESENCE_WINDOW = 12

#: The brief must not eat the context window it exists to protect.
_BRIEF_BUDGET_CHARS = 2000

try:  # optional dependency — the pack must work (minus memory) without it
    import awm as _awm
except ImportError:  # pragma: no cover - exercised via sys.modules in tests
    _awm = None


def _default_db(campaign: str) -> Path:
    root = Path(os.environ.get("AITHER_GOBBONET_HOME", "")
                or (Path.home() / ".aither" / "gobbonet"))
    return root / "campaigns" / f"{campaign}.db"


def _slug(text: str) -> str:
    """A stable key for a note, so re-recording an identical fact upserts."""
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:12]


class CampaignMemory:
    """Scoped notes for one campaign, backed by one awm SQLite file."""

    def __init__(self, campaign: str = "default",
                 db_path: Optional[Path] = None) -> None:
        # Scope segments may not contain ':' (awm's separator); normalise rather
        # than throw — a campaign named after a thread title must not crash boot.
        self.campaign = re.sub(r"[^A-Za-z0-9_.-]", "_", campaign or "default")[:64]
        self._store = None
        if _awm is not None:
            path = db_path or _default_db(self.campaign)
            self._store = _awm.MemoryStore(path)

    # -- availability ------------------------------------------------------

    def available(self) -> bool:
        return self._store is not None

    @staticmethod
    def unavailable_reason() -> str:
        return ("campaign memory needs the awm package — pip install awm "
                "(scoped agent memory; SQLite, no service)")

    # -- writes ------------------------------------------------------------

    def note(self, text: str, *, known_by: str = WORLD,
             arc: str = "*") -> Dict[str, Any]:
        """Record one durable campaign fact at exactly one knowledge scope."""
        if not self.available():
            return {"ok": False, "error": self.unavailable_reason()}
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "an empty note records nothing"}
        who = _norm_character(known_by)
        scope = _awm.Scope(self.campaign, who, _norm_character(arc))
        self._store.remember(scope, _slug(text), text, kind="campaign-note")
        return {"ok": True, "scope": str(scope), "known_by": who, "note": text}

    # -- reads -------------------------------------------------------------

    def notes_for(self, character: str = WORLD, *, arc: str = "*",
                  limit: int = 20) -> List[Dict[str, Any]]:
        """Notes this ONE character knows: theirs plus campaign-wide world state."""
        if not self.available():
            return []
        scope = _awm.Scope(self.campaign, _norm_character(character),
                           _norm_character(arc))
        return [m.to_dict() for m in self._store.recall(scope, limit=limit)]

    def known_characters(self) -> List[str]:
        """Every character any note is scoped to. Presence detection keys on this."""
        if not self.available():
            return []
        seen = set()
        # awm has no scope-enumeration API yet, so read the store's own scope
        # column. The match is made EXACT in Python (segment compare, never a
        # LIKE prefix) — awm's own header explains why: 'acme%' also matches
        # 'acmecorp', and that leak is silent.
        try:
            rows = self._store._db.execute(  # noqa: SLF001
                "SELECT DISTINCT scope FROM memories").fetchall()
        except Exception as e:  # noqa: BLE001 - enumeration is an optimisation
            log.debug("campaign scope enumeration failed: %s", e)
            rows = []
        for r in rows:
            parts = str(r["scope"]).split(":")
            if len(parts) == 3 and parts[0] == self.campaign:
                seen.add(parts[1])
        seen.discard(WORLD)
        return sorted(seen)

    def present_in(self, messages: List[Dict[str, Any]]) -> List[str]:
        """Characters PRESENT in the recent turns — named in the last few
        messages, matched as whole words, case-insensitive. The community's own
        trigger design: knowledge enters when its holder is present or spoken
        about, and not before."""
        known = self.known_characters()
        if not known:
            return []
        window = " \n ".join(
            str(m.get("content") or "")
            for m in messages[-_PRESENCE_WINDOW:]
            if m.get("role") in ("user", "assistant", "system"))
        low = window.lower()
        present = []
        for who in known:
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(who.lower())
                         + r"(?![A-Za-z0-9])", low):
                present.append(who)
        return present

    def brief(self, present: List[str], *, arc: str = "*",
              budget_chars: int = _BRIEF_BUDGET_CHARS,
              scene: str = "") -> str:
        """The scene brief: world notes + notes of each PRESENT character.

        Absent characters' knowledge is structurally excluded — not filtered
        out of a flat list, never fetched: their scopes are siblings and the
        store will not cross them.
        """
        if not self.available():
            return ""
        lines: List[str] = []
        seen_keys = set()

        def add(owner: str, items: List[Dict[str, Any]]) -> None:
            for it in items:
                dedup = (it["scope"], it["key"])
                if dedup in seen_keys:
                    continue
                seen_keys.add(dedup)
                tag = "everyone" if owner == WORLD else f"known to {owner}"
                lines.append(f"- ({tag}) {it['value']}")

        # Presence decides ELIGIBILITY; relevance decides ORDER within it. The
        # candidate set below is unchanged — ranking never adds a note, so an
        # absent character's secret stays unreachable however well it matches.
        candidates = [(WORLD, self.notes_for(WORLD, arc=arc))]
        for who in present:
            candidates.append((who, [it for it in self.notes_for(who, arc=arc)
                                     if it["scope"].split(":")[1] != WORLD]))
        if scene:
            try:
                from adk.packs.gobbonet.retrieval import rank
                candidates = [(owner, rank(items, scene))
                              for owner, items in candidates]
            except Exception as e:  # noqa: BLE001 - ranking is an enhancement
                log.debug("scene ranking unavailable: %s", e)
        for owner, items in candidates:
            add(owner, items)
        if not lines:
            return ""
        head = ("CAMPAIGN NOTES — durable facts the harness keeps between "
                "scenes, scoped to who is present. Stay consistent with them; "
                "record new lasting facts with the campaign_note tool.\n")
        out = head
        for ln in lines:
            if len(out) + len(ln) + 1 > budget_chars:
                break
            out += ln + "\n"
        return out.rstrip()


def _norm_character(name: str) -> str:
    name = (name or WORLD).strip()
    if name in ("", WORLD, "everyone", "all"):
        return WORLD
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:48].lower()


def register_campaign_tools(agent: Any, memory: CampaignMemory) -> int:
    """Give the agent the pen: it records and consults the notes itself."""

    def campaign_note(text: str, known_by: str = "*", arc: str = "*") -> dict:
        """Record a durable campaign fact. known_by scopes WHO knows it: '*'
        for world state everyone knows, or one character's name for a secret —
        other characters structurally cannot see it."""
        return memory.note(text, known_by=known_by, arc=arc)

    def campaign_recall(character: str = "*", arc: str = "*") -> dict:
        """List the durable notes ONE character knows (their own plus world
        state). Ask before writing a scene from that character's viewpoint."""
        if not memory.available():
            return {"ok": False, "error": memory.unavailable_reason()}
        return {"ok": True, "character": _norm_character(character),
                "notes": memory.notes_for(character, arc=arc)}

    n = 0
    for fn in (campaign_note, campaign_recall):
        try:
            agent.tools.register(fn, name=fn.__name__,
                                 description=(fn.__doc__ or "").strip())
            n += 1
        except Exception:  # noqa: BLE001 - a registry refusal must not kill chat
            # The loop still works without the pen; the brief still injects.
            continue
    return n
