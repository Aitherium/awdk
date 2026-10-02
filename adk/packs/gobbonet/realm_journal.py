"""Realm journal — reading a hash-chained world log as campaign memory, read-only.

A realm writes every event it survives into `<dataDir>/realm/journal.jsonl`: one JSON
object per line, hash-chained, so an edited or truncated log is DETECTABLE rather than
merely different. That file is the campaign's actual history — what an NPC did, what was
said to them, what the world recorded — and until now the harness could not read a word of
it.

This module is the reader. Three rules make it safe to point at a live realm:

1. **It never writes.** No repair, no re-chaining, no compaction, no "fix the hash". The
   file is opened for reading and nothing else; the source of truth belongs to the realm.
2. **A broken chain is REFUSED, not cleaned.** `verify_rows` walks from the genesis prev
   and raises on the FIRST break — a wrong `prev`, a hash that no longer matches its row
   (an edit), a tick that went backwards. A tampered journal must not become a slightly
   shorter valid one, because a silently-dropped row is a memory the campaign loses without
   anyone being told. `verify=False` is available and named, for reading a journal you
   already know is broken.
3. **Personas do not cross.** Rows are bucketed by the persona they belong to, and a recall
   reads ONE bucket plus the world bucket. Two NPCs are siblings, exactly as two characters
   are in campaign memory: one's conversation cannot surface in the other's scene.

THE CHAIN RULE
--------------
`hash = sha256(prev + canonical(row_without_hash))`, hex, where `canonical` is JSON with
sorted keys, no whitespace and non-ASCII emitted literally — the spelling
`json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` produces. The
engine's TypeScript half computes the same string for the same row, which is what makes a
row written by either half verifiable by both.

🚩 One number rule falls out of that and is easy to break: **a row must never carry an
integral float.** JSON has one number type and JavaScript cannot tell `1.0` from `1`, so a
reward of `1.0` written from Python chains differently from the same row written in
TypeScript. Write integers as integers; keep floats for genuinely fractional values.

WHAT IS INDEXED
---------------
`converse` (something was said), `npc.act` (an NPC did something) and `chronicle` (the
world recorded something). Everything else — outcome rows, generator rows, telemetry — is
skipped: a memory containing every event is a log, not a memory.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: The prev of the first row in a chain.
GENESIS_PREV = "0" * 64
#: The row version this reader understands.
JOURNAL_V = 1

#: Row kinds that carry campaign memory. The rest of the journal is machinery.
INDEXED_KINDS: Tuple[str, ...] = ("converse", "npc.act", "chronicle")
#: Kinds whose rows belong to the world rather than to one persona, whatever they name.
WORLD_KINDS: Tuple[str, ...] = ("chronicle",)
#: The "everyone" bucket — the same wildcard campaign memory uses for world state.
WORLD = "*"

#: Where a row names its persona, best first. `npc` is what an act row carries; the rest
#: are the spellings a conversation row may use. Falling through to the row's `actor` is
#: last on purpose: an actor is who ACTED, which is only sometimes whose memory it is.
PERSONA_KEYS: Tuple[str, ...] = ("persona_id", "npc", "who", "speaker", "target", "actor")
#: Where a row carries its words, best first.
TEXT_KEYS: Tuple[str, ...] = ("text", "line", "say", "reply", "summary", "entry", "action")

#: A persona id, the same grammar the avatar plane joins on.
PERSONA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PERSONA_ID_MAX = 128

_WORD = re.compile(r"[a-z0-9']+")
#: Words too common to be evidence of anything.
_STOP = frozenset((
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "had", "has", "have",
    "he", "her", "his", "i", "in", "is", "it", "its", "me", "of", "on", "or", "our", "she",
    "that", "the", "their", "them", "they", "this", "to", "was", "were", "what", "when",
    "who", "will", "with", "you", "your",
))


class JournalError(ValueError):
    """A journal that cannot be trusted. `line` names where, when the reader knows."""

    def __init__(self, message: str, line: Optional[int] = None) -> None:
        super().__init__(message)
        self.line = line


# ── the chain ─────────────────────────────────────────────────────────────────


def canonical(value: Any) -> str:
    """The one JSON spelling both halves of the chain agree on."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def row_hash(prev: str, row_without_hash: Dict[str, Any]) -> str:
    """`sha256(prev + canonical(row_without_hash))`, hex — the whole chain rule."""
    return hashlib.sha256((str(prev) + canonical(row_without_hash)).encode("utf-8")).hexdigest()


def parse_jsonl(text: str) -> List[Dict[str, Any]]:
    """Rows from JSONL text. A blank line is skipped; an unparseable one names its line
    number and stops the read, because a journal with a hole in it is not a shorter
    journal."""
    rows: List[Dict[str, Any]] = []
    for number, line in enumerate(str(text or "").split("\n"), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise JournalError(f"journal line {number} is unparseable: {exc}", number) from exc
        if not isinstance(row, dict):
            raise JournalError(f"journal line {number} is not an object", number)
        rows.append(row)
    return rows


def verify_rows(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Walk the chain from the genesis prev. Returns `{"rows": n, "head": hash}`.

    Raises on the FIRST break and names it. Nothing is repaired, skipped or rewritten:
    the caller decides what to do about a journal that has been edited.
    """
    prev = GENESIS_PREV
    count = 0
    last_tick = -1
    for row in rows:
        count += 1
        stated_prev = str(row.get("prev") or "")
        if stated_prev != prev:
            raise JournalError(
                f"row {count}: prev {stated_prev[:12] or '(none)'} does not follow the chain "
                f"at {prev[:12]} — a row was inserted, removed or reordered", count)
        body = {k: v for k, v in row.items() if k != "hash"}
        if str(row.get("hash") or "") != row_hash(prev, body):
            raise JournalError(
                f"row {count}: the hash does not match its contents — the row was edited "
                f"after it was written", count)
        tick = int(row.get("tick") or -1)
        if tick < last_tick:
            raise JournalError(
                f"row {count}: tick {tick} went backwards from {last_tick} — a journal is "
                f"append-only", count)
        last_tick = tick
        prev = str(row.get("hash") or "")
    return {"rows": count, "head": prev}


def read_journal(path: Any, *, verify: bool = True) -> List[Dict[str, Any]]:
    """Every row at `path`, verified unless told otherwise. A missing file is an EMPTY
    journal (a realm's first tick has written nothing); an unreadable one is an error,
    because "I could not look" and "there is nothing there" must not read the same."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise JournalError(f"cannot read the journal at {target.name}: {exc}") from exc
    rows = parse_jsonl(text)
    if verify:
        verify_rows(rows)
    return rows


# ── reading rows as memory ────────────────────────────────────────────────────


def persona_of(row: Dict[str, Any]) -> str:
    """Whose memory this row belongs to, or the world bucket.

    A chronicle row is the world's by KIND, however it is addressed — the world's history
    is not one character's secret.
    """
    kind = str(row.get("kind") or "")
    if kind in WORLD_KINDS:
        return WORLD
    payload = row.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    for key in PERSONA_KEYS:
        value = payload.get(key) if key != "actor" else (payload.get(key) or row.get(key))
        candidate = str(value or "").strip()
        if candidate and len(candidate) <= PERSONA_ID_MAX and PERSONA_ID_RE.match(candidate):
            return candidate
    return WORLD


def text_of(row: Dict[str, Any]) -> str:
    """The words in a row, or "" when it carries none."""
    payload = row.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    parts: List[str] = []
    for key in TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(" ".join(value.split()))
    return " — ".join(parts)


def _terms(text: str) -> List[str]:
    return [w for w in _WORD.findall(str(text).lower()) if w not in _STOP and len(w) > 1]


class RealmJournalSource:
    """One realm's journal, indexed per persona and read by keyword.

    Construct it with a path and call `load()`. Everything after that is a dictionary
    lookup: the file is read once, verified once, and never touched again.
    """

    def __init__(self, path: Any, *, verify: bool = True,
                 kinds: Iterable[str] = INDEXED_KINDS) -> None:
        self.path = Path(path)
        self.verify = bool(verify)
        self.kinds = tuple(kinds)
        self._by_persona: Dict[str, List[Dict[str, Any]]] = {}
        self._head = GENESIS_PREV
        self._rows = 0
        self._indexed = 0
        self.error: Optional[str] = None
        self.loaded = False

    # -- loading -----------------------------------------------------------

    def load(self) -> "RealmJournalSource":
        """Read, verify and index. Raises `JournalError` on a chain that does not hold —
        a tampered journal is refused, never quietly trimmed to the part that still
        verifies."""
        rows = read_journal(self.path, verify=self.verify)
        if self.verify and rows:
            self._head = str(rows[-1].get("hash") or GENESIS_PREV)
        self._rows = len(rows)
        index: Dict[str, List[Dict[str, Any]]] = {}
        for position, row in enumerate(rows):
            if str(row.get("kind") or "") not in self.kinds:
                continue
            text = text_of(row)
            if not text:
                continue
            entry = {
                "persona_id": persona_of(row),
                "kind": str(row.get("kind") or ""),
                "tick": int(row.get("tick") or 0),
                "ts": str(row.get("ts") or ""),
                "actor": str(row.get("actor") or ""),
                "text": text,
                "hash": str(row.get("hash") or ""),
                "position": position,
            }
            index.setdefault(entry["persona_id"], []).append(entry)
        self._by_persona = index
        self._indexed = sum(len(v) for v in index.values())
        self.loaded = True
        self.error = None
        return self

    def load_soft(self) -> "RealmJournalSource":
        """`load()` with the refusal captured instead of raised, for a caller that must
        keep working (a chat turn) while still being TOLD. `error` holds the reason and the
        index stays empty — never a partial one."""
        try:
            return self.load()
        except (JournalError, OSError) as exc:
            self._by_persona = {}
            self._rows = self._indexed = 0
            self.loaded = False
            self.error = str(exc)
        return self

    # -- reading -----------------------------------------------------------

    def personas(self) -> List[str]:
        """Every persona with at least one indexed row, world bucket excluded."""
        return sorted(p for p in self._by_persona if p != WORLD)

    def entries_for(self, persona_id: str, *, include_world: bool = True) -> List[Dict[str, Any]]:
        """One persona's rows, newest last. The world bucket is included by default: a
        chronicle is common knowledge. No other persona's rows are ever in here."""
        own = list(self._by_persona.get(str(persona_id or "").strip(), ()))
        if include_world and str(persona_id or "").strip() != WORLD:
            own += list(self._by_persona.get(WORLD, ()))
        return sorted(own, key=lambda e: (e["tick"], e["position"]))

    def recall(self, persona_id: str, query: str, k: int = 5, *,
               include_world: bool = True) -> List[Dict[str, Any]]:
        """The k rows of ONE persona that best match `query`, best first.

        Keyword scoring, deliberately: this reads a journal that may be thousands of rows
        long on a box with no embedder and no model, and a wrong answer that looks
        confident is worse than a narrow one. A query matching nothing returns `[]` —
        `recent()` is the honest way to ask for "whatever is latest".
        """
        wanted = set(_terms(query))
        if not wanted:
            return []
        scored: List[Tuple[float, int, Dict[str, Any]]] = []
        for entry in self.entries_for(persona_id, include_world=include_world):
            terms = _terms(entry["text"])
            if not terms:
                continue
            present = wanted & set(terms)
            if not present:
                continue
            # Distinct query terms matched dominates; repetition is a light tie-breaker.
            score = len(present) + min(0.5, sum(terms.count(t) for t in present) / 100.0)
            scored.append((score, entry["tick"], dict(entry, score=round(score, 4))))
        scored.sort(key=lambda row: (-row[0], -row[1]))
        return [entry for _, _, entry in scored[:max(1, int(k))]]

    def recent(self, persona_id: str, k: int = 5, *,
               include_world: bool = True) -> List[Dict[str, Any]]:
        """The last k rows for one persona, newest first."""
        entries = self.entries_for(persona_id, include_world=include_world)
        return list(reversed(entries[-max(1, int(k)):]))

    def status(self) -> Dict[str, Any]:
        """What the source read, and why it read nothing when it did."""
        return {
            "path": str(self.path), "exists": self.path.exists(), "loaded": self.loaded,
            "verified": self.verify, "rows": self._rows, "indexed": self._indexed,
            "personas": len(self.personas()), "head": self._head, "error": self.error,
        }


#: The engine's own override for where the journal lives. Same name on both sides, so a
#: realm and a harness pointed at one campaign do not need a second setting to agree.
JOURNAL_PATH_ENV = "REALM_JOURNAL_PATH"


def realm_journal_from_env(env: Optional[Dict[str, str]] = None, *,
                           verify: bool = True) -> Optional[RealmJournalSource]:
    """A loaded source when a realm journal is configured and readable, else None.

    Reads `REALM_JOURNAL_PATH`. Soft by design — a harness that cannot read the realm's
    log must still chat — but never silent: a refusal is on `source.error`, which
    `status()` reports.
    """
    import os

    environ = os.environ if env is None else env
    configured = str(environ.get(JOURNAL_PATH_ENV) or "").strip()
    if not configured:
        return None
    return RealmJournalSource(configured, verify=verify).load_soft()


__all__ = [
    "GENESIS_PREV",
    "JOURNAL_PATH_ENV",
    "INDEXED_KINDS",
    "JOURNAL_V",
    "JournalError",
    "PERSONA_KEYS",
    "RealmJournalSource",
    "TEXT_KEYS",
    "WORLD",
    "WORLD_KINDS",
    "canonical",
    "parse_jsonl",
    "persona_of",
    "read_journal",
    "realm_journal_from_env",
    "row_hash",
    "text_of",
    "verify_rows",
]
