"""Card grouping: one card per QUESTION, however many times it was raised.

Measured 2026-10-04: 22 open cards on the owner's queue, ~8 of them repeats of
another card. Three shapes, each a rule here:

G1 SERIES      A recurring producer raises one card per run whose title differs
               only in a date, a clock time or a count -- "Pilot: 12
               action(s) waiting for approval (2026-10-03)" then "... 14
               action(s) ... (2026-10-04)". The newest card of a series
               SUPERSEDES every older open one: the older is withdrawn with the
               note "superseded by d-xxxx". A series is scoped to one producer
               (same kind, agent and raising session), so two sessions' "N
               things are waiting on you" never collapse into each other.
               Producer dedupe keys that differ only by the same volatile
               tokens (``bp-approvals-2026-10-03`` / ``-04``) are one series;
               keys that differ otherwise are two questions.

G2 SUBJECT     The same underlying subject arrives from two different sources:
               "support: platform [mail/lead] X" and "Customer error report:
               [mail/lead] X" for one email. They collapse into ONE card: the
               richer one (more options, then more facts, then more prose, then
               newer) is kept and records the other as a fact; the poorer is
               withdrawn with the note "duplicate of d-xxxx". Two cards that
               carry DIFFERENT ``ticket:``/``issue:``/``lead:`` facts are two
               tickets however alike their subjects (recurring DMARC mails) and
               never collapse; the card that stands inherits any handle the
               withdrawn one had, so the answer routine still acts on it.

G3 INFO TTL    A ``kind=info`` card has nothing to decide. It never counts
               toward "N decisions waiting" (``triage.triage`` says context
               whatever its deadline) and it is auto-closed once it is older than
               the TTL, with a note saying so.

Every withdrawal carries a note naming the rule and the card that stands. A
card is never dropped silently and never deleted -- `sweep` reclaims disk later.

Pure functions over cards (``DecisionCard`` objects or their ``to_dict()``
dicts), so every rule is testable without a store. Stdlib only.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

#: How long an info card may stay open. A digest older than a day is a report
#: about a day that has passed; the next one is raised regardless.
INFO_TTL_SECONDS = 24 * 3600

# ── reading a card of either shape ────────────────────────────────────────────


def _get(card: Any, name: str, default: Any = None) -> Any:
    if isinstance(card, dict):
        return card.get(name, default)
    return getattr(card, name, default)


def _source(card: Any, name: str) -> str:
    src = _get(card, "source")
    if src is None:
        return ""
    return str(_get(src, name, "") or "")


def _created(card: Any) -> float:
    try:
        return float(_get(card, "created_at", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_open(card: Any) -> bool:
    return str(_get(card, "status", "open") or "open") == "open"


def _kind(card: Any) -> str:
    return str(_get(card, "kind", "decision") or "decision").strip().lower()


# ── G1: the series a title belongs to ─────────────────────────────────────────

_DATE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[t ]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?)?",
    re.I)
_CLOCK_RE = re.compile(r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2})?(?![\d:])")
_DURATION_RE = re.compile(r"\(\s*\d+(?:\.\d+)?\s*(?:ms|s|m|h)\s*\)", re.I)
#: A count: a number followed by a plural noun ("12 action(s)", "3 errors").
_COUNT_RE = re.compile(r"(?<![\w#.\-/])(\d{1,5})(?=\s+[a-z][\w-]*(?:s|\(s\))(?![\w(]))", re.I)
#: A number after one of these words is an IDENTITY, not a count: "PR 11604
#: fails" and "PR 11605 fails" are two different questions and must never merge.
_ID_WORDS = frozenset({
    "pr", "pull", "issue", "pid", "port", "run", "build", "job", "ticket", "order",
    "invoice", "case", "rtx", "gpu", "node", "v", "version", "step", "phase", "no",
    "number", "id", "sha", "commit", "release", "round", "lane", "slot", "row",
})
_LETTERS_RE = re.compile(r"[a-z]{3}", re.I)
_WS_RE = re.compile(r"\s+")


def _count_sub(title: str) -> str:
    def repl(m: "re.Match[str]") -> str:
        before = title[:m.start()].rstrip()
        prev = re.search(r"([A-Za-z]+)\W*$", before)
        if prev and prev.group(1).lower() in _ID_WORDS:
            return m.group(0)
        return "#"
    return _COUNT_RE.sub(repl, title)


def series_key(title: str) -> str:
    """The series a title belongs to: dates, clock times, durations and counts
    normalised away, whitespace collapsed, casefolded. "" when what is left is
    not a name (fewer than three letters in a row), so an all-volatile title can
    never pull unrelated cards into one series.

    Ids are deliberately NOT normalised: "port needs a human: abc1234" and
    "...: def5678" are two different asks.
    """
    text = _WS_RE.sub(" ", title or "").strip()
    text = _DATE_RE.sub("<date>", text)
    text = _CLOCK_RE.sub("<time>", text)
    text = _DURATION_RE.sub("", text)
    text = _count_sub(text)
    key = _WS_RE.sub(" ", text).strip(" -:—–").casefold()
    return key if _LETTERS_RE.search(key.replace("<date>", "").replace("<time>", "")) else ""


def _answerable(card: Any) -> bool:
    return len(_get(card, "options", None) or []) >= 2


def has_volatile(title: str) -> bool:
    """Does the title carry a date, clock time, duration or count?"""
    plain = _WS_RE.sub(" ", title or "").strip(" -:—–").casefold()
    key = series_key(title)
    return bool(key) and key != plain


def _series_group(card: Any) -> Optional[tuple[str, str, str, str]]:
    """The series a card belongs to, or None.

    An ANSWERABLE card (2+ options) joins a series only when its title carries a
    volatile token: two real asks raised under one identical title by one
    producer may be two questions, and a title-only rule must never close a real
    choice on a guess (identical asks from one session are a separate sweep's).
    A report (0-1 options) joins on its title alone.
    """
    if not _is_open(card) or _kind(card) == "credential":
        return None
    title = str(_get(card, "title", "") or "")
    key = series_key(title)
    if not key:
        return None
    if _answerable(card) and not has_volatile(title):
        return None
    return (key, _kind(card), _source(card, "agent"), _source(card, "session_id"))


# ── G2: the subject two sources share ─────────────────────────────────────────

#: "<prefix> [tag] <subject>" -- a source-labelled report about a subject that
#: carries its own tag ("[mail/lead]", "[mail]", "[ticket]"). The prefix is the
#: SOURCE; the tag + subject is the thing being reported.
_SUBJECT_RE = re.compile(r"^(?P<prefix>.*?)\s*(?P<tag>\[[^\]]{2,40}\])\s*(?P<subject>.+?)\s*$")
_REPLY_PREFIX_RE = re.compile(r"^(?:(?:re|fwd?|fw|aw|tr)\s*:\s*)+", re.I)
#: Tags that announce a PIPELINE, not a subject -- their own rules handle them.
_PIPELINE_TAGS = frozenset({"[expedition]", "[action required]", "[ci]", "[probe]"})


def subject_of(title: str) -> Optional[tuple[str, str]]:
    """(source_prefix, subject_key) for a tagged report title, or None."""
    m = _SUBJECT_RE.match(_WS_RE.sub(" ", title or "").strip())
    if not m:
        return None
    tag = m.group("tag").casefold()
    if tag in _PIPELINE_TAGS:
        return None
    subject = _REPLY_PREFIX_RE.sub("", m.group("subject"))
    subject = _TRUNC_TAIL_RE.sub("", subject).strip().casefold()
    if len(subject) < 6 or not _LETTERS_RE.search(subject):
        return None
    prefix = m.group("prefix").strip(" -:—–").casefold()
    return prefix, "%s %s" % (tag, subject)


#: What a title cut at a byte budget ends in: a replacement character (a UTF-8
#: sequence split mid-way) or an ellipsis.
_TRUNC_TAIL_RE = re.compile(r"(?:\ufffd|\u2026|\.\.\.)+\s*$")
#: Two subjects that agree this far, one a prefix of the other, are one subject
#: whose titles were cut at different lengths. Measured 2026-10-04: the
#: support/customer-error pair for one DMARC mail arrived cut at 101 and 100
#: characters, so their subjects differed only in where the cut fell.
TRUNCATED_MATCH_CHARS = 32


def same_subject(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) < TRUNCATED_MATCH_CHARS:
        return False
    return a.startswith(b) or b.startswith(a)


def _subject_groups(found: list[tuple[str, str, Any]]) -> list[list[tuple[str, Any]]]:
    """Group (prefix, subject, card) by :func:`same_subject`; longest subject leads."""
    groups: list[tuple[str, list[tuple[str, Any]]]] = []
    for prefix, subj, card in sorted(found, key=lambda t: -len(t[1])):
        for lead, members in groups:
            if same_subject(lead, subj):
                members.append((prefix, card))
                break
        else:
            groups.append((subj, [(prefix, card)]))
    return [members for _lead, members in groups]


def richness(card: Any) -> tuple[int, int, int, float]:
    """How much a human can act on: options, facts, prose, then recency."""
    opts = _get(card, "options", None) or []
    facts = _get(card, "facts", None) or []
    prose = len(str(_get(card, "summary", "") or "")) + len(str(_get(card, "detail", "") or ""))
    return (len(opts), len(facts), prose, _created(card))


# ── the plan ──────────────────────────────────────────────────────────────────


class Plan:
    """What grouping wants done: withdrawals (each with its note) and facts to
    append to the cards that stand."""

    def __init__(self) -> None:
        #: card id -> (rule, note). The note is what the withdrawn card carries.
        self.withdraw: dict[str, tuple[str, str]] = {}
        #: card id -> facts to append (the record of what collapsed into it).
        self.facts: dict[str, list[str]] = {}

    def add_withdraw(self, cid: str, rule: str, note: str) -> None:
        self.withdraw.setdefault(cid, (rule, note))

    def add_fact(self, cid: str, fact: str) -> None:
        bucket = self.facts.setdefault(cid, [])
        if fact not in bucket:
            bucket.append(fact)

    def __bool__(self) -> bool:
        return bool(self.withdraw or self.facts)


def plan(cards: Iterable[Any], now: float, *,
         info_ttl: float = INFO_TTL_SECONDS) -> Plan:
    """Group the OPEN cards. Pure: no store, no clock beyond ``now``."""
    open_cards = [c for c in cards if _is_open(c) and str(_get(c, "id", "") or "")]
    out = Plan()

    # G3 first: an expired info card closes on its own merits.
    if info_ttl > 0:
        for c in open_cards:
            if _kind(c) != "info":
                continue
            deadline = _get(c, "deadline")
            if deadline is not None and float(deadline) > now:
                continue  # its own deadline governs it
            age = now - _created(c)
            if age >= info_ttl:
                out.add_withdraw(
                    str(_get(c, "id")), "G3",
                    "info card auto-closed after %.0fh (TTL %.0fh): nothing to decide"
                    % (age / 3600, info_ttl / 3600))

    # G1: newest of each series stands.
    series: dict[tuple[str, str, str, str], list[Any]] = {}
    for c in open_cards:
        grp = _series_group(c)
        if grp is not None:
            series.setdefault(grp, []).append(c)
    for grp, members in series.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda c: (_created(c), str(_get(c, "id"))))
        keep = members[-1]
        keep_id = str(_get(keep, "id"))
        for c in members[:-1]:
            if _distinct_questions(c, keep):
                continue
            out.add_withdraw(str(_get(c, "id")), "G1",
                             "superseded by %s (newer card in the series %r)"
                             % (keep_id, grp[0]))

    # G2: one card per subject across sources; the richer stands.
    tagged: list[tuple[str, str, Any]] = []
    for c in open_cards:
        if _kind(c) == "credential" or str(_get(c, "id")) in out.withdraw:
            continue
        found = subject_of(str(_get(c, "title", "") or ""))
        if found is not None:
            tagged.append((found[0], found[1], c))
    clusters = [cl for grp in _subject_groups(tagged) for cl in _split_by_handles(grp)]
    for members in clusters:
        subj = subject_of(str(_get(members[0][1], "title", "") or ""))[1]  # type: ignore[index]
        if len({prefix for prefix, _c in members}) < 2:
            continue  # one source: a repeat of itself is G1's, not a cross-source pair
        ranked = sorted(members, key=lambda pc: richness(pc[1]))
        keep = ranked[-1][1]
        keep_id = str(_get(keep, "id"))
        for _prefix, c in ranked[:-1]:
            cid = str(_get(c, "id"))
            out.add_withdraw(cid, "G2",
                             "duplicate of %s (same subject %r from another source; "
                             "the richer card stands)" % (keep_id, subj))
            out.add_fact(keep_id, "also reported as %s: %s"
                         % (cid, str(_get(c, "title", "") or "")[:160]))
            for fact in carried_handles(c, keep):
                out.add_fact(keep_id, fact)
    return out


def _key_series(key: str) -> str:
    """A dedupe key with the same date/time/count normalisation as a title.

    A daily producer stamps the day into its key -- BusinessPilot raises
    ``bp-approvals-2026-10-03`` then ``bp-approvals-2026-10-04``
    (lib/automation/approval_digest.py). Those are one series; the day only
    stops a re-run on the SAME day from raising a second card. An all-volatile
    key compares raw, so it can never merge with an unrelated one.
    """
    return series_key(key) or key.strip().casefold()


def _distinct_questions(a: Any, b: Any) -> bool:
    """Two cards whose producer gave each its OWN dedupe key are two questions
    by the producer's own account, however alike their titles read -- unless the
    keys differ only by a date, clock time or count (one series, one key per run)."""
    ka = str(_get(a, "dedupe_key", "") or "")
    kb = str(_get(b, "dedupe_key", "") or "")
    if not (ka and kb) or ka == kb:
        return False
    return _key_series(ka) != _key_series(kb)


#: Fact lines an answer routine ACTS on (lib/routines/feedback_card_actions.py
#: parse_card): the support ticket to ack/close and the GitHub issue to fix.
_HANDLE_RE = re.compile(r"^\s*(ticket|issue|lead)\s*:\s*(\S+)\s*$", re.I)


def handles(card: Any) -> dict[str, str]:
    """{"ticket": id, "issue": url, "lead": id} read from a card's facts."""
    out: dict[str, str] = {}
    for fact in _get(card, "facts", None) or []:
        m = _HANDLE_RE.match(str(fact))
        if m:
            out.setdefault(m.group(1).lower(), m.group(2))
    return out


def carried_handles(src: Any, dst: Any) -> list[str]:
    """Handle fact lines ``src`` has and ``dst`` lacks, in parse_card's shape.

    When a duplicate is withdrawn the card that stands takes over its ticket and
    issue, so the answer routine still acks, fixes and closes them."""
    have = handles(dst)
    return ["%s: %s" % (k, v) for k, v in handles(src).items() if k not in have]


def _handles_conflict(a: Any, b: Any) -> bool:
    """Do two cards point at DIFFERENT tickets/issues/leads?

    Two mails with one subject (DMARC reports, "Confirm your contact email
    address") are two tickets. Collapsing them would leave the withdrawn card's
    ticket and issue unacted forever: the answer routine reads only the handles
    on the card that stands. Only a same-handle pair (or one with none) is a
    duplicate.
    """
    ha, hb = handles(a), handles(b)
    return any(ha[k] != hb[k] for k in ha.keys() & hb.keys())


def _split_by_handles(members: list[tuple[str, Any]]) -> list[list[tuple[str, Any]]]:
    """Split a subject group into clusters with no conflicting handles; a card
    joins the first cluster none of whose members it conflicts with."""
    clusters: list[list[tuple[str, Any]]] = []
    ranked = sorted(members, key=lambda pc: (not handles(pc[1]), _created(pc[1])))
    for prefix, card in ranked:
        for cluster in clusters:
            if not any(_handles_conflict(card, other) for _p, other in cluster):
                cluster.append((prefix, card))
                break
        else:
            clusters.append([(prefix, card)])
    return clusters


def plan_for_new(new: Any, open_cards: Iterable[Any], now: float) -> tuple[Optional[Any], Plan]:
    """What raising ``new`` should do to the open queue, at create time.

    Returns ``(twin, plan)``. ``twin`` is an existing open card that is a
    cross-source duplicate at least as rich as ``new`` -- the store then keeps
    the twin, records ``new`` on it as a fact, and writes no new card (the
    dedupe-guard contract: the caller gets back a different object). Otherwise
    ``twin`` is None and ``plan`` withdraws what ``new`` supersedes or
    out-ranks. Only groups that INCLUDE ``new`` are touched: create is not the
    place to tidy the rest of the queue (:func:`plan`, run on a schedule, does that).
    """
    new_id = str(_get(new, "id", "") or "")
    others = [c for c in open_cards if _is_open(c) and str(_get(c, "id", "")) != new_id]
    out = Plan()
    if _kind(new) == "credential":
        return None, out

    found = subject_of(str(_get(new, "title", "") or ""))
    if found is not None:
        prefix, subj = found
        twins = []
        for c in others:
            if _kind(c) == "credential":
                continue
            theirs = subject_of(str(_get(c, "title", "") or ""))
            if (theirs is not None and same_subject(theirs[1], subj)
                    and theirs[0] != prefix and not _handles_conflict(c, new)):
                twins.append(c)
        if twins:
            best = max(twins, key=richness)
            if richness(best)[:3] >= richness(new)[:3]:
                return best, out
            for c in twins:
                cid = str(_get(c, "id"))
                out.add_withdraw(cid, "G2",
                                 "duplicate of %s (same subject %r from another source; "
                                 "the richer card stands)" % (new_id, subj))
                out.add_fact(new_id, "also reported as %s: %s"
                             % (cid, str(_get(c, "title", "") or "")[:160]))
                for fact in carried_handles(c, new):
                    out.add_fact(new_id, fact)

    grp = _series_group(new)
    if grp is not None:
        for c in others:
            if _series_group(c) == grp and not _distinct_questions(c, new):
                out.add_withdraw(str(_get(c, "id")), "G1",
                                 "superseded by %s (newer card in the series %r)"
                                 % (new_id, grp[0]))
    return None, out
