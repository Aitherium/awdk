"""Durable, cross-process store for decision cards.

Design notes that are consequences of real constraints, not preference:

* **One file per card.** Concurrent Claude Code sessions raise cards at the same
  time. A single shared JSON document would make every raise a read-modify-write
  against a file another session is also rewriting, and the loser's card vanishes
  silently — the exact failure this whole feature exists to prevent. One file per
  card means two raises never touch the same bytes.

* **Atomic replace, never in-place write.** A reader (the daemon, the cockpit, a
  toast) polls this directory constantly. A partially-written file would be read as
  corrupt JSON, and the card would flicker out of the list and back. ``os.replace``
  is atomic on both POSIX and Windows.

* **A corrupt card is skipped, never fatal.** ``list()`` is called by surfaces that
  must keep working. One unreadable file must not empty the cockpit — that would
  turn "one bad write" into "you have no pending decisions", which reads as done.

* **Answering is compare-and-set.** Two surfaces can answer the same card at once
  (you click the toast on the desktop while the phone card is open). The first
  answer wins and the second is told it lost, rather than silently overwriting.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

STATUS_OPEN = "open"
STATUS_ANSWERED = "answered"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"

#: Terminal statuses. A card in one of these is never re-answerable.
CLOSED_STATUSES = frozenset({STATUS_ANSWERED, STATUS_EXPIRED, STATUS_CANCELLED})

URGENCIES = ("low", "normal", "high", "critical")

#: Card ids are typed by humans ("adk decide answer d-7f3a 2"), so they are short
#: and use an unambiguous alphabet — no 0/o/1/l.
_ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
_ID_RE = re.compile(r"^d-[" + _ID_ALPHABET + r"]{4,12}$")


class DecisionError(RuntimeError):
    """A card operation that could not be completed as asked."""


def decisions_dir() -> Path:
    """The card directory, honouring ``AITHER_DECISIONS_DIR`` for tests and tenants."""
    env = os.getenv("AITHER_DECISIONS_DIR", "").strip()
    base = Path(env) if env else (Path.home() / ".aither" / "decisions")
    base.mkdir(parents=True, exist_ok=True)
    return base


def steer_dir() -> Path:
    """Root of the steering mailbox the answer round-trip writes into.

    This is the path ``COCKPIT-DESIGN.md`` specifies and the UserPromptSubmit hook
    drains. Keeping the two in one place stops them drifting apart.
    """
    env = os.getenv("AITHER_STEER_DIR", "").strip()
    base = Path(env) if env else (Path.home() / ".aither" / "steer")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _new_id() -> str:
    return "d-" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(4))


@dataclass
class DecisionOption:
    """One thing the owner can choose.

    ``consequence`` is separate from ``label`` on purpose: a label answers "what is
    this called", a consequence answers "what happens to my machine if I pick it",
    and only the second one lets somebody decide from a phone lock screen.
    """

    key: str
    label: str
    consequence: str = ""
    recommended: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DecisionOption":
        return cls(
            key=str(raw.get("key") or "").strip(),
            label=str(raw.get("label") or "").strip(),
            consequence=str(raw.get("consequence") or "").strip(),
            recommended=bool(raw.get("recommended")),
        )


@dataclass
class DecisionSource:
    """Where the card came from — enough to walk back to the blocked session."""

    session_id: str = ""
    agent: str = ""
    cwd: str = ""
    branch: str = ""
    host: str = field(default_factory=socket.gethostname)
    pid: int = field(default_factory=os.getpid)
    #: The LONG-LIVED process that owns the terminal tab — not ``pid``.
    #: ``pid`` is whoever ran ``adk decide ask``, which for a hook-raised card is
    #: a detached helper that exits in milliseconds; walking up from a dead pid
    #: finds nothing, so the terminal could never be located later. This is
    #: resolved at RAISE time and is what the card's terminal controls act on.
    session_pid: int = 0
    #: Where a live steer can be delivered: "harness" (a daemon PTY session),
    #: "adk" (an in-flight /chat/stream turn) or "" (mailbox only).
    steer_channel: str = ""
    #: The session transcript, so the card's context is EXPLORABLE rather than
    #: merely asserted. An anchor, not a snapshot: the panel harvests at click
    #: time (adk.decisions.context), because a snapshot taxes every raise and is
    #: stale by the time anybody reads it.
    transcript: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DecisionSource":
        return cls(
            session_id=str(raw.get("session_id") or ""),
            agent=str(raw.get("agent") or ""),
            cwd=str(raw.get("cwd") or ""),
            branch=str(raw.get("branch") or ""),
            host=str(raw.get("host") or ""),
            pid=int(raw.get("pid") or 0),
            session_pid=int(raw.get("session_pid") or 0),
            steer_channel=str(raw.get("steer_channel") or ""),
            transcript=str(raw.get("transcript") or ""),
        )


@dataclass
class DecisionNote:
    """A free-text steer the owner typed at the card, without answering it.

    This exists because an option list is a closed question, and the owner's
    answer is very often "none of those — do this instead". Before notes, that
    answer had nowhere to go: the only control on the card was a button, so the
    owner had to walk to the terminal and type there, which is the exact journey
    the card was supposed to remove.

    A note does NOT close the card. Steering and deciding are different acts:
    "also check the staging box" is guidance, not a verdict, and swallowing the
    card on the first typed sentence would lose the decision it was raised for.
    """

    text: str
    at: float = field(default_factory=time.time)
    via: str = "popup"
    #: True when this note reached the raising session live (a PTY write or an
    #: in-flight steer) rather than only landing in the mailbox for later.
    delivered_live: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DecisionNote":
        return cls(
            text=str(raw.get("text") or ""),
            at=float(raw.get("at") or 0.0),
            via=str(raw.get("via") or "popup"),
            delivered_live=bool(raw.get("delivered_live")),
        )


# ── decidability (DC008) ──────────────────────────────────────────────────────
# A card exists to make an ask DECIDABLE. Two shapes defeat that, and both used to
# be storable:
#
#   1. options that are STATUS, not choices ("both are running", "waiting on it").
#      The owner reads them and has nothing to pick — the card pages a human to
#      tell them a fact.
#   2. a card labelled kind='info' that carries options anyway. `kind` only selects
#      the popup HEADER (KIND_HEADER.get in popup.py) — the buttons render either
#      way — while every rule below used to be guarded by kind == "decision". So
#      the label alone bought a rendered chooser with no default_key and no status
#      check: a decidability bypass that looked like a quieter card.
#
# So the rules key on CARRYING OPTIONS, never on the label. If the owner is shown
# buttons, they are being asked to choose, whatever the card calls itself.

_STATUS_ONLY = (
    r"\b(?:is|are)\s+(?:both\s+|still\s+|currently\s+)?running\b",
    r"\bthe only open items?\b",
    r"\beverything else is\b",
    r"\bevery other item\b",
    r"\b(?:is|are)\s+(?:an?\s+)?in[- ]progress\b",
    r"\bwaiting (?:on|for) (?:it|them|that|the run|completion)\b",
    r"\b(?:watched\s+)?background run\b",
)

# Anchored to the START on purpose. An unanchored search exempted labels like
# "I'll finish the merge, restart the worker, and report" — that is the AGENT's
# future work described in passing, not an action the OWNER is being offered.
_ACTION_VERB = re.compile(
    r"^\s*(?:cancel|stop|kill|abort|retry|rerun|re-run|restart|merge|revert|roll ?back|"
    r"deploy|publish|tag|delete|remove|approve|reject|skip|proceed|wait for|escalate|"
    r"pause|resume|force|override|rebuild|redeploy)\b",
    re.IGNORECASE,
)
_START_WITH = re.compile(r"^\s*start with:\s*", re.IGNORECASE)


def _status_only_phrase(label: str) -> str | None:
    """Return the status phrase making `label` undecidable, or None if it is a choice."""
    text = _START_WITH.sub("", label or "")
    if not text.strip():
        return None
    if _ACTION_VERB.search(text):
        return None
    for pattern in _STATUS_ONLY:
        found = re.search(pattern, text, re.IGNORECASE)
        if found:
            return found.group(0)
    return None


@dataclass
class DecisionCard:
    """A single thing an agent needs a human to decide, know, or unblock."""

    id: str
    title: str
    summary: str = ""
    detail: str = ""
    kind: str = "decision"          # decision | blocked | info | credential
    urgency: str = "normal"
    options: list[DecisionOption] = field(default_factory=list)
    default_key: str = ""
    facts: list[str] = field(default_factory=list)
    notes: list[DecisionNote] = field(default_factory=list)
    source: DecisionSource = field(default_factory=DecisionSource)
    created_at: float = field(default_factory=time.time)
    deadline: Optional[float] = None
    status: str = STATUS_OPEN
    answer: Optional[str] = None
    answer_note: Optional[str] = None
    answered_at: Optional[float] = None
    answered_via: Optional[str] = None
    # Credential card fields (kind="credential" only)
    secret_name: Optional[str] = None      # Vault key (e.g., "STRIPE_API_KEY")
    credential_format: Optional[str] = None  # "password" | "api_key" | "totp_seed" | "custom"
    credential_scope: Optional[str] = None   # "platform" | "workspace" | "user"
    credential_description: Optional[str] = None  # Why we need it (displayed to owner)
    credential_preset: Optional[str] = None  # e.g., "proton" for multi-field

    # ── derived helpers ────────────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self.status == STATUS_OPEN

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.created_at)

    @property
    def seconds_left(self) -> Optional[float]:
        if self.deadline is None:
            return None
        return self.deadline - time.time()

    def option(self, key: str) -> Optional[DecisionOption]:
        want = (key or "").strip().lower()
        for opt in self.options:
            if opt.key.lower() == want:
                return opt
        return None

    def recommended_key(self) -> str:
        """The recommended option, falling back to the declared default."""
        for opt in self.options:
            if opt.recommended:
                return opt.key
        return self.default_key

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["options"] = [o.to_dict() for o in self.options]
        data["source"] = self.source.to_dict()
        data["notes"] = [n.to_dict() for n in self.notes]
        return data

    #: What a credential card's answer is allowed to say. The VALUE never travels
    #: through a card — the card is the ask, the vault is the transport.
    CREDENTIAL_ANSWER = "credential_provided"

    def to_dict_safe(self) -> dict[str, Any]:
        """Serialise for anywhere a secret must not go: a log, a wire, a UI.

        `kind="credential"` cards ask the owner for an API key, token or password.
        The value is meant to reach the vault directly and never touch the card —
        but `answer` and `answer_note` are free text on a durable JSON file that
        the daemon serves over HTTP, the popup renders, and the steering mailbox
        copies into a session transcript. One agent writing the secret into the
        answer would persist it to all four at once.

        So this scrubs those two fields, plus any typed note, whenever the card is
        a credential ask. Every other kind is returned unchanged: a normal
        decision's answer is the whole point of reading it.

        `to_dict()` stays verbatim on purpose — the store must round-trip a card
        exactly, and a serialiser that silently dropped fields would corrupt the
        file it just read. Use this at the BOUNDARY, not for persistence.
        """
        data = self.to_dict()
        if (self.kind or "").strip().lower() != "credential":
            return data
        if data.get("answer"):
            data["answer"] = self.CREDENTIAL_ANSWER
        data["answer_note"] = ""
        data["notes"] = [
            {**note, "text": "[redacted — credential card]"}
            for note in (data.get("notes") or [])
        ]
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DecisionCard":
        return cls(
            id=str(raw.get("id") or ""),
            title=str(raw.get("title") or ""),
            summary=str(raw.get("summary") or ""),
            detail=str(raw.get("detail") or ""),
            kind=str(raw.get("kind") or "decision"),
            urgency=str(raw.get("urgency") or "normal"),
            options=[DecisionOption.from_dict(o) for o in (raw.get("options") or [])],
            default_key=str(raw.get("default_key") or ""),
            facts=[str(f) for f in (raw.get("facts") or [])],
            notes=[DecisionNote.from_dict(n) for n in (raw.get("notes") or [])
                   if isinstance(n, dict)],
            source=DecisionSource.from_dict(raw.get("source") or {}),
            created_at=float(raw.get("created_at") or 0.0),
            deadline=(float(raw["deadline"]) if raw.get("deadline") is not None else None),
            status=str(raw.get("status") or STATUS_OPEN),
            answer=raw.get("answer"),
            answer_note=raw.get("answer_note"),
            answered_at=(
                float(raw["answered_at"]) if raw.get("answered_at") is not None else None
            ),
            answered_via=raw.get("answered_via"),
            secret_name=raw.get("secret_name"),
            credential_format=raw.get("credential_format"),
            credential_scope=raw.get("credential_scope"),
            credential_description=raw.get("credential_description"),
            credential_preset=raw.get("credential_preset"),
        )


class DecisionStore:
    """Disk-backed card store. Safe across processes; cheap enough to poll."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else decisions_dir()
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ── paths ─────────────────────────────────────────────────────────────────

    def _file(self, card_id: str) -> Path:
        if not _ID_RE.match(card_id or ""):
            # Card ids reach this from HTTP and from a CLI argv. Validating the
            # shape here is what stops "../../etc/passwd" being a card id.
            raise DecisionError(f"not a valid card id: {card_id!r}")
        return self.path / f"{card_id}.json"

    # ── writes ────────────────────────────────────────────────────────────────

    def _write(self, card: DecisionCard) -> None:
        target = self._file(card.id)
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(card.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, target)

    def create(self, card: DecisionCard) -> DecisionCard:
        """Persist a new card, minting an id if it has none."""
        with self._lock:
            if not card.id:
                for _ in range(50):
                    candidate = _new_id()
                    if not (self.path / f"{candidate}.json").exists():
                        card.id = candidate
                        break
                else:
                    raise DecisionError("could not mint an unused card id")
            self._validate(card)
            self._write(card)
            return card

    @staticmethod
    def _validate(card: DecisionCard) -> None:
        if not card.title.strip():
            raise DecisionError("a card must have a title — that is the whole point")
        if card.urgency not in URGENCIES:
            raise DecisionError(f"urgency must be one of {URGENCIES}, got {card.urgency!r}")

        # Credential-specific validation
        if card.kind == "credential":
            if card.options:
                raise DecisionError(
                    'kind="credential" must not have options; '
                    'credential value is entered via secure_prompt.py, not from card options'
                )
            if card.default_key:
                raise DecisionError(
                    'kind="credential" must not have default_key; '
                    'credentials are not optional answers'
                )
            if not card.secret_name:
                raise DecisionError(
                    'kind="credential" requires secret_name field '
                    '(vault key like "STRIPE_API_KEY")'
                )
            if not card.credential_format:
                raise DecisionError(
                    'kind="credential" requires credential_format '
                    '("password", "api_key", "totp_seed", or "custom")'
                )
            if not card.credential_description:
                raise DecisionError(
                    'kind="credential" requires credential_description '
                    '(explain to owner why we need this credential)'
                )
            # Scope is validated against CallerContext at raise time, not here
            return  # Skip the rest of validation for credential cards

        # Decision/info/blocked card validation
        keys = [o.key.lower() for o in card.options]
        if len(keys) != len(set(keys)):
            raise DecisionError("two options share a key; the owner could not pick between them")
        if card.kind == "decision" and not card.options:
            raise DecisionError("a decision card with no options is prose — use kind='info'")
        if card.kind == "info" and card.options:
            raise DecisionError(
                "an info card with options is a decision — the popup renders those buttons "
                "regardless of kind, so use kind='decision' and satisfy its rules"
            )
        for opt in card.options:
            phrase = _status_only_phrase(opt.label)
            if phrase:
                raise DecisionError(
                    f"option {opt.key!r} states status, not a choice ({phrase!r}). A card "
                    f"whose options the owner cannot pick between is a notification — "
                    f"use kind='info' with no options"
                )
        if card.default_key and not card.option(card.default_key):
            raise DecisionError(
                f"default_key {card.default_key!r} is not one of the options "
                f"({', '.join(o.key for o in card.options) or 'none'})"
            )
        if card.options and not card.default_key:
            # The default is what makes a card safe to ignore. Refusing to store a
            # card without one is what keeps that property true in practice.
            raise DecisionError(
                "a card offering options must declare default_key — 'what happens if the owner "
                "never answers' is the field that makes the card safe to ignore"
            )

    # ── reads ─────────────────────────────────────────────────────────────────

    def get(self, card_id: str) -> Optional[DecisionCard]:
        target = self._file(card_id)
        if not target.exists():
            return None
        return self._read_file(target)

    @staticmethod
    def _read_file(target: Path) -> Optional[DecisionCard]:
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A half-written or hand-mangled card must not take the list down with
            # it. The caller sees one fewer card, not zero cards.
            return None
        if not isinstance(raw, dict):
            return None
        card = DecisionCard.from_dict(raw)
        return card if card.id else None

    def list(
        self,
        *,
        status: str | Iterable[str] | None = STATUS_OPEN,
        session_id: str = "",
        newest_first: bool = False,
    ) -> list[DecisionCard]:
        """Cards matching a filter, oldest first by default.

        Oldest-first is deliberate: the card that has been blocking longest is the
        one you most need to see, so it must not be pushed off the bottom of a
        cockpit row or a toast that shows only the first entry.
        """
        wanted: Optional[set[str]]
        if status is None:
            wanted = None
        elif isinstance(status, str):
            wanted = {status}
        else:
            wanted = set(status)

        out: list[DecisionCard] = []
        for target in sorted(self.path.glob("d-*.json")):
            card = self._read_file(target)
            if card is None:
                continue
            if self._expire_if_due(card):
                pass  # status is now 'expired'; the filter below decides visibility
            if wanted is not None and card.status not in wanted:
                continue
            if session_id and card.source.session_id != session_id:
                continue
            out.append(card)
        out.sort(key=lambda c: c.created_at, reverse=newest_first)
        return out

    def _expire_if_due(self, card: DecisionCard) -> bool:
        """Flip an overdue open card to expired, applying its default. Returns True
        if this call performed the transition."""
        if not card.is_open or card.deadline is None:
            return False
        if time.time() < card.deadline:
            return False
        with self._lock:
            fresh = self.get(card.id)
            if fresh is None or not fresh.is_open:
                return False
            fresh.status = STATUS_EXPIRED
            fresh.answer = fresh.default_key or None
            fresh.answered_at = time.time()
            fresh.answered_via = "deadline"
            fresh.answer_note = "no answer before the deadline; the declared default applied"
            self._write(fresh)
            card.status = fresh.status
            card.answer = fresh.answer
            card.answered_at = fresh.answered_at
            card.answered_via = fresh.answered_via
            card.answer_note = fresh.answer_note
            return True

    # ── answering ─────────────────────────────────────────────────────────────

    def answer(
        self,
        card_id: str,
        choice: str,
        *,
        note: str = "",
        via: str = "cli",
        deliver: bool = True,
    ) -> DecisionCard:
        """Record an answer and deliver it to the raising session.

        Compare-and-set: answering an already-closed card raises rather than
        overwriting, so two surfaces racing produce one winner and one clear loser.
        """
        with self._lock:
            card = self.get(card_id)
            if card is None:
                raise DecisionError(f"no such card: {card_id}")
            if card.status in CLOSED_STATUSES:
                raise DecisionError(
                    f"card {card_id} is already {card.status}"
                    + (f" (answer: {card.answer})" if card.answer else "")
                )
            picked = (choice or "").strip()
            # A credential card closes ONLY with the marker — never with free
            # text. The store's durable JSON is served over HTTP, rendered by
            # the popup, and copied into session transcripts; a value written
            # here would persist to all four despite to_dict_safe() scrubbing
            # at the boundaries. The value's only door is secure_prompt.py:
            # masked input -> vault, then this marker. (DC008)
            if (card.kind or "").strip().lower() == "credential":
                if picked != DecisionCard.CREDENTIAL_ANSWER:
                    raise DecisionError(
                        f"credential card {card_id} closes only with "
                        f"{DecisionCard.CREDENTIAL_ANSWER!r} — the value goes to "
                        "the vault via secure_prompt, never the card")
            if card.options:
                opt = card.option(picked)
                if opt is None:
                    valid = ", ".join(o.key for o in card.options)
                    raise DecisionError(
                        f"{picked!r} is not an option for {card_id} — pick: {valid}"
                    )
                picked = opt.key
            card.status = STATUS_ANSWERED
            card.answer = picked
            card.answer_note = note or None
            card.answered_at = time.time()
            card.answered_via = via
            self._write(card)

        if deliver:
            # Outside the lock: delivery touches a different tree and must never
            # hold up another session's raise.
            self.deliver_answer(card)
        return card

    def cancel(self, card_id: str, *, note: str = "") -> DecisionCard:
        """Withdraw a card the agent no longer needs answered.

        This matters more than it looks: an agent that resolves a question on its
        own and leaves the card open trains the owner to ignore cards.
        """
        with self._lock:
            card = self.get(card_id)
            if card is None:
                raise DecisionError(f"no such card: {card_id}")
            if card.status in CLOSED_STATUSES:
                return card
            card.status = STATUS_CANCELLED
            card.answer_note = note or "withdrawn by the agent that raised it"
            card.answered_at = time.time()
            card.answered_via = "agent"
            self._write(card)
            return card

    def steer(self, card_id: str, text: str, *, via: str = "popup") -> DecisionCard:
        """Send the owner's OWN words to the raising session, card still open.

        This is the half the first version did not have, and its absence is the
        loudest complaint the feature got: the only control on a card was a
        button, so "none of those, do X instead" had nowhere to go and the owner
        had to walk to the terminal — the journey the card exists to remove.

        Steering deliberately does NOT close the card. A typed sentence is
        guidance, not a verdict; swallowing the decision on the first note would
        silently drop the question the agent is actually blocked on.
        """
        body = (text or "").strip()
        if not body:
            raise DecisionError("a steer with no text steers nothing")
        with self._lock:
            card = self.get(card_id)
            if card is None:
                raise DecisionError(f"no such card: {card_id}")
            note = DecisionNote(text=body, via=via)
            card.notes.append(note)
            self._write(card)

        delivered_live, _how = self.deliver_steer(card, body)
        if delivered_live:
            with self._lock:
                fresh = self.get(card_id)
                if fresh is not None and fresh.notes:
                    fresh.notes[-1].delivered_live = True
                    self._write(fresh)
                    card = fresh
        return card

    def deliver_steer(self, card: DecisionCard, text: str) -> tuple[bool, str]:
        """Push a free-text steer at the raising session. ``(reached_it_live, how)``.

        The mailbox write always happens — it is the only channel that survives
        the session being mid-turn, asleep, or between prompts. The live tiers
        are attempted first and reported separately, because "queued for the
        next prompt" and "the agent has it now" are different facts and only the
        second one actually interrupts.
        """
        session = card.source.session_id
        live, how = False, "mailbox only"
        if session:
            try:
                from adk.decisions import steerback

                live, how = steerback.deliver(card, text)
            except ImportError as exc:  # pragma: no cover - packaging accident
                how = f"live steering unavailable: {exc}"
        self._write_mailbox(
            card,
            suffix="steer",
            lines=[
                f"# Owner steered you mid-card {card.id}",
                "",
                f"**{card.title}**",
                "",
                "The owner typed this at the decision card. Treat it as an "
                "instruction from them, and do not re-ask what it already answers.",
                "",
                "> " + "\n> ".join(text.splitlines()),
                "",
                f"_(card {card.id} is still OPEN — answer it or cancel it once you "
                f"have acted on this.)_",
                "",
            ],
        )
        return live, how

    def _write_mailbox(
        self, card: DecisionCard, *, suffix: str, lines: list[str]
    ) -> Optional[Path]:
        """Atomically drop one markdown file into the session's steering mailbox."""
        session = card.source.session_id
        if not session or not re.match(r"^[A-Za-z0-9._-]{1,128}$", session):
            return None
        box = steer_dir() / session
        box.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        target = box / f"{stamp}-{card.id}-{suffix}.md"
        # A burst of steers inside one second must not overwrite each other.
        counter = 1
        while target.exists():
            target = box / f"{stamp}-{card.id}-{suffix}-{counter}.md"
            counter += 1
        tmp = target.with_suffix(".md.tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        os.replace(tmp, target)
        return target

    def deliver_answer(self, card: DecisionCard) -> Optional[Path]:
        """Write the answer into the raising session's steering mailbox.

        Returns the mailbox file, or None when the card names no session (a card
        raised by a script rather than a session — nothing to steer).

        The mailbox is the only path into an interactive Claude Code tab: its TUI
        has no IPC, but its UserPromptSubmit/Stop hooks read this directory. Without
        this step a card is a survey, not a control.
        """
        chosen = card.option(card.answer or "")
        lines = [
            f"# Owner answered decision card {card.id}",
            "",
            f"**{card.title}**",
            "",
            f"- Answer: `{card.answer}`" + (f" — {chosen.label}" if chosen else ""),
            f"- Answered via: {card.answered_via or 'unknown'}",
        ]
        if chosen and chosen.consequence:
            lines.append(f"- Consequence acknowledged: {chosen.consequence}")
        if card.answer_note:
            lines.append(f"- Owner note: {card.answer_note}")
        for note in card.notes:
            # Notes typed before the click are part of the answer, not history.
            # Dropping them here would lose the owner's actual instruction and
            # keep only which button it arrived with.
            lines.append(f"- Owner also said: {note.text}")
        lines += ["", "Proceed on this answer. Do not re-ask.", ""]
        written = self._write_mailbox(card, suffix="answer", lines=lines)
        if written is None:
            return None
        # Live tiers are best-effort and never gate the mailbox write: a daemon
        # that is down must not lose the answer.
        try:
            from adk.decisions import steerback

            steerback.deliver(card, "\n".join(lines[3:]))
        except ImportError as exc:  # pragma: no cover - packaging accident
            import sys as _sys

            # The answer is already durable in the mailbox, so this is a
            # downgrade rather than a loss — but a silent downgrade is how
            # "the agent reacts instantly" quietly becomes "at the next prompt".
            _sys.stderr.write(f"[decision-card] live steering unavailable: {exc}\n")
        return written

    # ── housekeeping ──────────────────────────────────────────────────────────

    def sweep(self, *, keep_closed_seconds: float = 7 * 24 * 3600) -> int:
        """Delete closed cards older than the retention window. Returns the count.

        Open cards are NEVER swept regardless of age — an unanswered question does
        not stop mattering because it got old, and silently deleting one would make
        the store lie about what is pending.
        """
        removed = 0
        cutoff = time.time() - keep_closed_seconds
        for target in self.path.glob("d-*.json"):
            card = self._read_file(target)
            if card is None:
                continue
            if card.status in CLOSED_STATUSES and (card.answered_at or card.created_at) < cutoff:
                try:
                    target.unlink()
                    removed += 1
                except OSError as exc:  # pragma: no cover - platform-specific
                    raise DecisionError(f"could not sweep {target}: {exc}") from exc
        return removed


_STORE: Optional[DecisionStore] = None
_STORE_LOCK = threading.Lock()


def get_store() -> DecisionStore:
    """Process-wide store singleton."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = DecisionStore()
        return _STORE
