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

* **The compare-and-set is held by an OS file lock, not a thread lock.** The
  surfaces that race are separate PROCESSES — the daemon serving a Discord
  chat-reply, the ``awask`` CLI that desk spawns, the terminal-reply hook, and
  the producer that raises on every failing pass. A ``threading.RLock`` lives
  inside one interpreter and excludes none of them: measured 2026-09-18, five
  processes answering one card produced 2-4 winners (and each winner ran the
  card's action, so a ``run_now`` spawned the job that many times), and six
  producers raising one deduped card wrote 4-6 card files for a streak that
  must produce exactly one. Every read-modify-write here therefore takes
  :class:`_DirLock` — ``flock`` on POSIX, ``msvcrt.locking`` on Windows — keyed
  on the store DIRECTORY, so two ``DecisionStore`` objects in one process
  exclude each other too. Actions are still applied outside the lock: a lock
  held across a spawn would serialise the owner's reply channel on a scheduled
  job.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

#: Exactly one of these exists on any platform this runs on. Imported at module
#: level rather than inside the lock so a platform with NEITHER is discovered
#: when the module loads, not on the first contended write.
try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

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

#: A dedupe key is a producer-chosen identity for the QUESTION, not for the card
#: ("this job, this failure streak"). It is compared, never joined onto a path,
#: but it is written by an automated producer on every pass, so its shape is
#: pinned here rather than trusted: an unbounded key would be an unbounded scan.
_DEDUPE_RE = re.compile(r"^[A-Za-z0-9:._@+-]{1,160}$")


class DecisionError(RuntimeError):
    """A card operation that could not be completed as asked."""


# ── cross-process exclusion ───────────────────────────────────────────────────

#: How long a read-modify-write waits for the directory lock before refusing.
#: Every holder releases inside one file write, so reaching this means a holder
#: died wedged rather than that the store is busy — and a refusal the caller can
#: print beats a surface that blocks forever on the owner's reply channel.
LOCK_TIMEOUT_SECONDS = 20.0

#: Name of the lock file inside the card directory. It is NOT ``d-*.json``, so
#: no listing, glob or dedupe scan ever sees it.
LOCK_FILENAME = ".decisions.lock"


class _DirLock:
    """One exclusive lock per decisions DIRECTORY, across threads and processes.

    Reentrant, because the store's own methods nest (``answer`` -> ``get``, and
    ``_expire_if_due`` -> ``get`` -> ``_read_file``): an inner ``with`` must not
    deadlock on the outer one. The reentrancy is per *lock object*, and there is
    exactly one lock object per directory in this interpreter (see
    :func:`_dir_lock`), so two ``DecisionStore`` instances pointed at one
    directory share it rather than each minting a private lock that excludes
    nobody.

    The OS primitive differs by platform and neither is advisory-for-show:
    ``flock`` on POSIX is held by the open file DESCRIPTION, so a second
    ``open()`` in the same process still blocks; Windows byte-range locks are
    held by the HANDLE, with the same consequence.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._rlock = threading.RLock()
        self._depth = 0
        self._handle: Any = None

    def __enter__(self) -> "_DirLock":
        self._rlock.acquire()
        self._depth += 1
        if self._depth == 1:
            try:
                self._acquire()
            except BaseException:
                # Never leave the thread lock held by a failed acquire: the next
                # caller in this process would block forever on a lock nobody owns.
                self._depth -= 1
                self._rlock.release()
                raise
        return self

    def __exit__(self, *_exc: Any) -> bool:
        if self._depth == 1:
            self._release()
        self._depth -= 1
        self._rlock.release()
        return False

    def _acquire(self) -> None:
        if fcntl is None and msvcrt is None:  # pragma: no cover - no such platform
            raise DecisionError(
                "this platform offers neither fcntl nor msvcrt, so the card store "
                "cannot be made safe against a second process — refusing rather "
                "than pretending the compare-and-set holds")
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        handle = None
        try:
            self._path.mkdir(parents=True, exist_ok=True)
            # "a+b": created if absent, never truncated, and opening it takes no
            # lock — the lock is the explicit call below.
            handle = open(self._path / LOCK_FILENAME, "a+b")
            while True:
                try:
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    if time.monotonic() >= deadline:
                        raise DecisionError(
                            f"could not take the card-store lock on {self._path} "
                            f"within {LOCK_TIMEOUT_SECONDS:g}s; another process is "
                            "holding it") from None
                    # Short sleep, not a spin: a contended raise is measured in
                    # milliseconds and a busy loop would starve the holder.
                    time.sleep(0.005)
                    continue
                self._handle = handle
                return
        except BaseException:
            if handle is not None:
                handle.close()
            raise

    def _release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError as exc:
            # Closing the handle drops the lock on both platforms, so a failed
            # unlock is not a leak — but it is not nothing either: it means the
            # OS disagreed with us about what we held, and the next contended
            # write is where that shows up. Said once, on stderr, rather than
            # swallowed, and never re-raised: this runs in `finally`-shaped
            # unwinding and must not mask the caller's own exception.
            print(f"[decisions] lock release on {self._path} refused: "
                  f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        finally:
            handle.close()


_DIR_LOCKS: dict[str, _DirLock] = {}
_DIR_LOCKS_GUARD = threading.Lock()


def _dir_lock(path: Path) -> _DirLock:
    """THE lock object for this directory in this interpreter."""
    try:
        resolved = Path(path).resolve()
    except OSError:  # pragma: no cover - unresolvable path
        resolved = Path(path)
    # Windows paths are case-insensitive, so two spellings of one directory must
    # not mint two locks that exclude nobody.
    key = str(resolved).lower() if os.name == "nt" else str(resolved)
    with _DIR_LOCKS_GUARD:
        lock = _DIR_LOCKS.get(key)
        if lock is None:
            lock = _DirLock(resolved)
            _DIR_LOCKS[key] = lock
        return lock


_TEST_DECISIONS_DIR: Path | None = None


def _under_pytest() -> bool:
    return bool(os.getenv("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


def decisions_dir() -> Path:
    """The card directory, honouring ``AITHER_DECISIONS_DIR`` for tests and tenants.

    Under pytest with no explicit dir, a per-process TEMP store -- never the owner's.
    Measured 2026-09-24: one night of test runs (expedition-gate tests in a worktree,
    the card-plane suites) raised 322 cards into ~/.aither/decisions, doubling the
    owner's inbox to 608 and feeding the popups that interrupt games.
    """
    global _TEST_DECISIONS_DIR
    env = os.getenv("AITHER_DECISIONS_DIR", "").strip()
    if env:
        base = Path(env)
    elif _under_pytest():
        if _TEST_DECISIONS_DIR is None:
            _TEST_DECISIONS_DIR = Path(tempfile.mkdtemp(prefix="aither-decisions-test-"))
        base = _TEST_DECISIONS_DIR
    else:
        base = Path.home() / ".aither" / "decisions"
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


# ── the steering mailbox writer ───────────────────────────────────────────────
#
# The mailbox is a DIRECTORY OF MARKDOWN FILES, and nothing about it requires a
# DecisionCard — only today's signature did. Everything that wants to put text in
# front of a running Claude Code tab (an answered card, a peer agent's request, an
# `awsh` send that found no managed pty) writes through :func:`write_steer`, so
# there is exactly one place that decides a filename, an atomicity strategy and
# an AUTHORITY stamp.

#: The two authorities that exist, most-authoritative first. The drain hook builds
#: a DIFFERENT preamble per file from this value: an ``owner`` file is injected as
#: an instruction from the person, a ``peer`` file as a request from another agent
#: that carries no authority and must be judged on its merits. Getting this wrong
#: in the peer direction hands a stranger the owner's voice, so an unrecognised
#: value is coerced to the LEAST authority rather than refused — a dropped steer
#: is recoverable, a promoted one is not.
STEER_AUTHORITIES = ("owner", "peer")

#: A session id becomes a DIRECTORY NAME under :func:`steer_dir`, so its shape is
#: validated rather than trusted — a caller-supplied id must not walk out of the
#: mailbox. Deliberately the same expression the drain hook validates with; a
#: write this accepts and the hook rejects would sit unread forever.
_STEER_SESSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

#: An origin id becomes part of a FILENAME. Unlike the session id it is folded
#: rather than refused: a peer's event id is not ours to shape, and refusing the
#: write would lose the steer to make a cosmetic point about a filename.
_STEER_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


#: The magic token a reader keys on to recognise line 1 as a steer header at
#: all. Neutralised out of every caller-supplied attribute value below — not
#: because it can re-open the comment (the quote and ``-->`` guards already
#: stop that), but because it lets forged text simply BE the token a careless
#: reader might grep for (``"aither-steer v1" in text``) rather than parse
#: with the anchored regex. Case-folded: a reader that lowercases first must
#: not be foolable by a differently-cased copy.
_STEER_TOKEN_RE = re.compile(re.escape("aither-steer"), re.IGNORECASE)


def _steer_attr(value: object) -> str:
    """One header attribute value, made safe to sit inside an HTML comment.

    🚩 Trap: these values are caller-supplied text, and the header is the ONLY
    thing downstream has to tell an owner file from a peer's. A ``"`` would end
    the attribute early and a ``-->`` would end the comment, either of which lets
    a sender continue in raw markup and forge ``authority="owner"`` on what looks
    like line 1. Both are neutralised here, at the single place that writes a
    header, rather than in each caller — plus the token itself, so a forged
    value cannot even spell ``aither-steer v1`` a second time on the line.
    """
    text = "" if value is None else str(value)
    text = " ".join(text.replace('"', "'").split())
    while "-->" in text:  # shrinks every pass, so this terminates
        text = text.replace("-->", "->")
    text = _STEER_TOKEN_RE.sub("aither_steer", text)
    return text[:200]


def write_steer(
    session_id: str,
    lines: Iterable[str],
    *,
    suffix: str,
    sender: str,
    authority: str = "peer",
    origin_id: str = "",
    kind: str = "",
) -> Optional[Path]:
    """Atomically drop one markdown file into a session's steering mailbox.

    Returns the file written, or ``None`` when ``session_id`` is missing or
    malformed (nothing is created in that case — not a partial file, not the
    session directory).

    ``authority`` must be one of :data:`STEER_AUTHORITIES`; anything else becomes
    ``"peer"``. It defaults to ``"peer"`` so a caller added later that forgets the
    argument under-claims instead of over-claiming.

    ``kind`` is a free-form classification of who sent this, stamped verbatim for
    the reader (the dispatcher stamps the target kind it resolved, e.g.
    ``claude_code``). Nothing keys behaviour on it today — ``authority`` and
    ``from`` are the parsed fields — so it is descriptive, not load-bearing.
    """
    if not session_id or not _STEER_SESSION_RE.match(session_id):
        return None
    # 🚩 Trap: the charset alone (`[A-Za-z0-9._-]`) admits a bare "." or "..",
    # which is a NAME the regex accepts but a directory the filesystem
    # interprets specially — `steer_dir() / ".."` is the mailbox's own PARENT.
    # A charset check is not a path check; a dot-segment is refused explicitly.
    if session_id in (".", ".."):
        return None
    if authority not in STEER_AUTHORITIES:
        authority = "peer"

    box = steer_dir() / session_id
    box.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    origin = _STEER_ID_UNSAFE.sub("-", str(origin_id or ""))[:64].strip("-")
    # An empty origin would leave a "--" in the middle of every name, so the
    # segment is omitted rather than rendered blank. Card writes always carry
    # one, which is why today's card filenames are unchanged.
    base = "-".join(part for part in (stamp, origin, suffix) if part)
    target = box / f"{base}.md"
    # A burst of steers inside one second must not overwrite each other.
    counter = 1
    while target.exists():
        target = box / f"{base}-{counter}.md"
        counter += 1

    header = (
        f'<!-- aither-steer v1 authority="{authority}" from="{_steer_attr(sender)}" '
        f'kind="{_steer_attr(kind)}" event="{_steer_attr(origin_id)}" -->'
    )
    body = "\n".join([header, *lines])
    # The reader polls this directory with a `*.md` glob, so the in-progress file
    # must not END in `.md` — appending `.tmp` keeps it invisible until the
    # replace publishes it whole. The pid keeps two processes racing on the same
    # second from sharing one scratch file and truncating each other's bytes.
    tmp = box / f"{target.name}.{os.getpid()}.tmp"
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, target)
    return target


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
    credential_recipe: Optional[str] = None  # id in config/credential_recipes.yaml
    #: Identity of the QUESTION this card asks, chosen by whatever raised it. The
    #: store refuses a second card carrying a key it has already seen, whatever
    #: that card's status — which is what makes an automated producer safe to run
    #: on a loop: it raises on every pass and still yields exactly one card.
    dedupe_key: str = ""
    #: The card-recipe id this card was built from, and the variables it was
    #: built with. Both are persisted because the ANSWER is applied later, in
    #: another process, from the card alone: without them the mapping from an
    #: option key to an action would need external state that may not exist by
    #: the time the owner replies.
    card_recipe: str = ""
    recipe_vars: dict[str, Any] = field(default_factory=dict)
    #: The signed audit of a credential answer — WHAT was vaulted, WHERE, by
    #: which door, and a digest of the value. Never the value itself. It exists
    #: because `CREDENTIAL_ANSWER` is a single constant string: every credential
    #: card that ever closed says exactly the same thing, so the card alone
    #: cannot distinguish "the owner supplied the GitHub secret to the user
    #: lockbox from the window" from "somebody closed the card". A receipt is
    #: the only durable record of which of those happened.
    credential_receipt: Optional[dict[str, Any]] = None

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
            credential_recipe=raw.get("credential_recipe"),
            credential_receipt=(raw.get("credential_receipt")
                                if isinstance(raw.get("credential_receipt"), dict)
                                else None),
            dedupe_key=str(raw.get("dedupe_key") or ""),
            card_recipe=str(raw.get("card_recipe") or ""),
            recipe_vars=(dict(raw["recipe_vars"])
                         if isinstance(raw.get("recipe_vars"), dict) else {}),
        )


class DecisionStore:
    """Disk-backed card store. Safe across processes; cheap enough to poll."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else decisions_dir()
        self.path.mkdir(parents=True, exist_ok=True)
        # Per DIRECTORY, not per instance: the surfaces that race are separate
        # processes, and two DecisionStore objects in one process race too.
        self._lock = _dir_lock(self.path)
        #: Closed cards the last :meth:`sweep` could not delete because a reader
        #: held them open. Zero after a sweep that removed everything it matched.
        self.last_sweep_busy = 0

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
        # Writers are serialised by the directory lock, but READERS are not: the
        # cockpit, the desk tray and the popup poll this directory constantly and
        # take no lock, by design. On Windows a reader with the file momentarily
        # open makes os.replace raise PermissionError [WinError 32], which would
        # surface as an uncaught 500 on an answer that was otherwise valid. The
        # reader's window is microseconds, so a bounded retry closes it; the
        # final attempt is left to raise so a REAL permission problem is not
        # silently retried away.
        deadline = time.monotonic() + 2.0
        while True:
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def create(self, card: DecisionCard) -> DecisionCard:
        """Persist a new card, minting an id if it has none.

        A card carrying a ``dedupe_key`` is raised AT MOST ONCE. Two guards, in
        order, and both return the card that already exists while writing
        nothing — the caller can tell the difference because the object it gets
        back is not the object it passed in:

        1. **Same key, ANY status.** A closed card with this key means the
           question was already decided. Without this, an automated producer
           raising on every pass would raise a fresh card the moment the owner
           answered the last one — a storm that answering makes worse.
        2. **No second OPEN card for the same subject**, whatever the rest of
           the key says. This covers the case the first guard cannot: a producer
           recomputing part of its own key while a card is open.

        Both guards are a read-then-write, and the producer that raises them is a
        FRESH PROCESS on every failing pass, so the scan and the write are held
        under the directory lock (an OS file lock). Without it, overlapping
        raises each read "no card yet" and each wrote one — measured at 4-6 cards
        for a single streak, which is precisely the storm the dedupe key exists
        to prevent.
        """
        with self._lock:
            if card.dedupe_key:
                existing = self.find_by_dedupe_key(card.dedupe_key)
                if existing is not None:
                    print(f"[decisions] dedupe hit {existing.id} ({existing.status})",
                          file=sys.stderr)
                    return existing
                prefix = self._dedupe_prefix(card)
                if prefix:
                    open_card = self.find_open_by_dedupe_prefix(prefix)
                    if open_card is not None:
                        print(f"[decisions] dedupe hit {open_card.id} (open, same subject)",
                              file=sys.stderr)
                        return open_card
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
    def _dedupe_prefix(card: DecisionCard) -> str:
        """The "same subject" prefix of this card's key, or "" when it has none.

        Asked of the recipe layer rather than derived textually: the key's own
        tail can legitimately contain the separator (an ISO timestamp carries
        ``+00:00``), so splitting on it here would produce a prefix that matches
        nothing and a guard that silently never fires.
        """
        if not (card.card_recipe or "").strip():
            return ""
        try:
            from adk.decisions.card_recipes import dedupe_prefix
        except ImportError as exc:
            # NOT the same as an unhappy recipe. The module being ABSENT means the
            # package was built without it — a packaging accident, in which
            # `_apply_card_recipe` will silently do nothing too, so every card
            # this producer raises will be answered and no action will ever run.
            # Swallowing it left a clean-checkout install looking healthy on
            # every local check and inert in production, so it is said out loud.
            print(f"[decisions] card_recipes is MISSING from this build ({exc}): "
                  f"dedupe guard 2 is off and answering {card.card_recipe!r} cards "
                  "will run NOTHING", file=sys.stderr)
            return ""
        try:
            return dedupe_prefix(card.card_recipe, card.recipe_vars or {})
        except Exception as exc:  # noqa: BLE001 - a raise must survive a bad recipe
            # An unhappy recipe costs the SECOND guard only; the exact-key guard
            # above has already run and needs nothing. Still named, because a
            # guard that is off is not the same as a guard that found nothing.
            print(f"[decisions] dedupe prefix unavailable for {card.card_recipe!r} "
                  f"({exc.__class__.__name__}: {exc}); guard 2 skipped",
                  file=sys.stderr)
            return ""

    def find_by_dedupe_key(self, key: str) -> Optional[DecisionCard]:
        """The card carrying this key, whatever its status, or None."""
        want = (key or "").strip()
        if not want:
            return None
        for target in sorted(self.path.glob("d-*.json")):
            card = self._read_file(target)
            if card is not None and card.dedupe_key == want:
                return card
        return None

    def find_open_by_dedupe_prefix(self, prefix: str) -> Optional[DecisionCard]:
        """The OPEN card whose key starts with this prefix, or None."""
        want = (prefix or "").strip()
        if not want:
            return None
        for target in sorted(self.path.glob("d-*.json")):
            card = self._read_file(target)
            if card is None or not card.is_open:
                continue
            if card.dedupe_key.startswith(want):
                return card
        return None

    @staticmethod
    def _validate(card: DecisionCard) -> None:
        if card.dedupe_key and not _DEDUPE_RE.match(card.dedupe_key):
            raise DecisionError(
                f"dedupe_key {card.dedupe_key!r} is not the allowed shape "
                "(letters, digits and : . _ @ + -, up to 160 characters)")
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
        try:
            card = DecisionCard.from_dict(raw)
        except Exception:
            # from_dict trusts the SHAPE of every inner field: a non-dict
            # ``source`` or ``options`` entry raises AttributeError, a
            # non-numeric ``created_at`` raises ValueError, a non-iterable
            # ``facts`` raises TypeError. Catching only json's errors above let
            # one mangled neighbour take down a whole operation — and since the
            # dedupe guards parse every neighbour BEFORE writing, that turned
            # one bad file into "no card can be raised at all", with a traceback
            # naming a file the caller never asked about. Skipping is what the
            # module docstring already promises: one fewer card, never zero.
            return None
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
        # Outside the lock and outside the `if`: an expiry IS an answer (the
        # declared default), so it goes through the same action hook every other
        # answer path does. The default's action is None by construction, so this
        # can never spawn anything — but routing it here rather than exempting it
        # is what keeps "every transition applies the answer" true with no
        # special case to remember.
        self._apply_card_recipe(card)
        return True

    # ── answering ─────────────────────────────────────────────────────────────

    def _apply_card_recipe(self, card: DecisionCard) -> None:
        """Turn a closed card's answer into the action its recipe promised.

        It lives in the TRANSITION, not in the delivery path, because these cards
        have no session: the steering-mailbox write returns early for a card with
        no session id, so a hook hung off delivery would never run for exactly
        the cards that need it. Here it runs for every answer path there is — the
        window, a chat reply, a terminal reply, a deadline — without any of them
        knowing it exists.

        Never raises, and never undoes the answer: the answer is already on disk
        before this is called, and a failed action is recorded as a note.
        """
        if not (card.card_recipe or "").strip():
            return
        try:
            try:
                from adk.decisions.card_recipes import apply_answer
            except ImportError as exc:
                # The module is ABSENT from this build. Every answer to a recipe
                # card is then recorded and nothing runs — which looked identical
                # to success on every surface, because the card really is
                # answered. The failure goes ON THE CARD, not only to a stderr
                # nobody is reading: a note is the one place all four surfaces
                # (window, chat, terminal, desk) already render.
                why, applied = (
                    f"NOT APPLIED — card_recipes is missing from this build ({exc})",
                    False,
                )
            else:
                applied, why = apply_answer(card)
            if not why:
                return
            prefix = "- Steerback: " if applied else "- Steerback FAILED: "
            with self._lock:
                fresh = self.get(card.id)
                if fresh is None:
                    return
                fresh.notes.append(DecisionNote(text=f"{prefix}{why}", via="steerback"))
                self._write(fresh)
                card.notes = fresh.notes
        except Exception as exc:  # pragma: no cover - the answer must survive this
            print(f"[decisions] card recipe hook failed for {card.id}: "
                  f"{exc.__class__.__name__}: {exc}", file=sys.stderr)

    def answer(
        self,
        card_id: str,
        choice: str,
        *,
        note: str = "",
        via: str = "cli",
        deliver: bool = True,
        receipt: Optional[dict[str, Any]] = None,
    ) -> DecisionCard:
        """Record an answer and deliver it to the raising session.

        Compare-and-set: answering an already-closed card raises rather than
        overwriting, so two surfaces racing produce one winner and one clear loser.

        The read and the write are inside ONE hold of the directory lock, which is
        an OS file lock and therefore holds across processes. That matters more
        than it reads: every successful answer routes through
        :meth:`_apply_card_recipe`, so a lost race is no longer a rewritten JSON
        file — it is the card's ACTION running twice (``run_now`` starting the
        same job N times). The loser leaves with ``DecisionError`` naming the
        winning answer.
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
            if receipt is not None:
                # Written inside the SAME lock and the same _write as the answer
                # itself. A receipt recorded by a second read-modify-write could
                # be lost to a racing writer, leaving a closed credential card
                # with no evidence of where its value went — which is exactly
                # the state the receipt exists to make impossible.
                card.credential_receipt = dict(receipt)
            self._write(card)

        # Outside the lock, and BEFORE delivery: the action the owner picked is
        # the point of the answer, and it must not be conditional on `deliver`.
        # The terminal-reply hook answers with deliver=False, and a card raised
        # by a scheduler has no session to deliver to at all.
        self._apply_card_recipe(card)

        if deliver:
            # Outside the lock: delivery touches a different tree and must never
            # hold up another session's raise.
            self.deliver_answer(card)
        return card

    def add_facts(self, card_id: str, facts: list[str]) -> DecisionCard:
        """Append measurements to a card, open or closed. Never touches status.

        A credential card is verified AFTER it closes (the value must be in the
        vault first), so this must work on an answered card — the facts are the
        post-landing proof and belong on the ask they prove.
        """
        clean = [str(f).strip() for f in facts if str(f).strip()]
        with self._lock:
            card = self.get(card_id)
            if card is None:
                raise DecisionError(f"no such card: {card_id}")
            if not clean:
                return card
            card.facts.extend(clean)
            self._write(card)
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

    def resolve(self, card_id: str, *, note: str = "") -> DecisionCard:
        """Close a card the raising agent has fulfilled itself: a promise KEPT,
        or a `blocked` card whose blocker went away.

        The CLI verb `awask resolve` shipped calling this method before it
        existed (measured 2026-09-21: `'DecisionStore' object has no attribute
        'resolve'`), so every kept promise had to be left open or `cancel`led --
        and a cancelled promise reads as WITHDRAWN, the opposite of kept. It
        closes as ANSWERED with the answer "kept": the one closed status that
        means success, and the one the promise sweep (which judges only open,
        overdue promises) will never breach. Idempotent on an already-closed
        card, like `cancel`, so a resolve racing a sweep has one clear winner.
        """
        with self._lock:
            card = self.get(card_id)
            if card is None:
                raise DecisionError(f"no such card: {card_id}")
            if card.status in CLOSED_STATUSES:
                return card
            card.status = STATUS_ANSWERED
            card.answer = "kept"
            card.answer_note = note or "resolved by the agent that raised it"
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
        """Drop one CARD file into the raising session's steering mailbox.

        All of the writing is :func:`write_steer`; the only things this adds are
        the card's identity and OWNER authority — a card file is the owner
        speaking, which is what earns it the instruction-shaped preamble on the
        way in.

        🚩 The ``suffix`` values ("answer", "steer") are load-bearing beyond the
        filename: the drain hook treats a HEADERLESS file whose name ends in one
        of them as owner-authored, for back-compat with the card files written
        before the header existed. Renaming one would silently demote every card
        file already sitting in a mailbox to peer authority.
        """
        return write_steer(
            card.source.session_id,
            lines,
            suffix=suffix,
            sender="the owner",
            authority="owner",
            origin_id=card.id,
            kind="decision_card",
        )

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

    def sweep(self, *, keep_closed_seconds: float = 7 * 24 * 3600,
              archive: Optional[Path] = None) -> int:
        """Delete closed cards older than the retention window. Returns the count.

        Open cards are NEVER swept regardless of age — an unanswered question does
        not stop mattering because it got old, and silently deleting one would make
        the store lie about what is pending.

        **One undeletable file must not end the sweep.** Measured 2026-09-18: a
        card held open by a reader raised ``PermissionError`` [WinError 32] out of
        ``unlink``, the sweep aborted on the 1,155th of 2,772 closed cards, and
        the store stayed 1,634 files deep — so every reader then walked all of
        them and the sweep failed the same way on the next run. A busy file is
        counted in :attr:`last_sweep_busy` and retried next time; anything that is
        NOT a transient busy/permission error still raises, because a sweep that
        swallowed every OSError would be indistinguishable from one that ran.

        With ``archive`` (a directory) each swept card is first appended to
        ``<archive>/cards-YYYY-MM.zip`` and only removed once the zip holds it, so
        an unattended sweep keeps the owner's answer history and stays reversible
        (``unzip`` puts a card back). The zip is read back before the unlink.
        """
        import zipfile

        removed = 0
        busy = 0
        cutoff = time.time() - keep_closed_seconds
        for target in self.path.glob("d-*.json"):
            card = self._read_file(target)
            if card is None:
                continue
            if card.status in CLOSED_STATUSES and (card.answered_at or card.created_at) < cutoff:
                if archive is not None:
                    closed_at = card.answered_at or card.created_at
                    month = time.strftime("%Y-%m", time.gmtime(closed_at))
                    bundle = Path(archive) / f"cards-{month}.zip"
                    try:
                        bundle.parent.mkdir(parents=True, exist_ok=True)
                        data = target.read_bytes()
                        with zipfile.ZipFile(bundle, "a", zipfile.ZIP_DEFLATED) as zf:
                            if target.name not in zf.namelist():
                                zf.writestr(target.name, data)
                        with zipfile.ZipFile(bundle) as zf:
                            if zf.read(target.name) != data:
                                raise DecisionError(f"archive mismatch for {target.name}")
                    except (OSError, zipfile.BadZipFile) as exc:
                        raise DecisionError(
                            f"could not archive {target} into {bundle}: {exc}") from exc
                try:
                    target.unlink()
                    removed += 1
                except PermissionError:
                    # Held open by a reader (the cockpit, a toast, the popup).
                    # It is still closed and still old; the next sweep gets it.
                    busy += 1
                except FileNotFoundError:
                    # A concurrent sweep won the race. Not this sweep's removal,
                    # and not an error either.
                    continue
                except OSError as exc:  # pragma: no cover - platform-specific
                    self.last_sweep_busy = busy
                    raise DecisionError(f"could not sweep {target}: {exc}") from exc
        self.last_sweep_busy = busy
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
