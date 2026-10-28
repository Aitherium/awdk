"""Decision cards as first-class ADK agent tools.

Until now this whole channel was Claude-Code-shaped: a CLI an agent shelled out
to, plus a Stop hook. An ADK agent — Lyra, Demiurge, a customer's `awdk`
agent on their own laptop — had no way to reach its owner at all except
``escalate_to_human``, which in standalone mode wrote a log line and returned
``"status": "logged_locally"``. Nothing raised, nothing notified, nobody saw it.
That is the silent-no-op pattern (`security-review-patterns.md` §5) sitting in
the one tool whose entire job is "get a human".

So the primitives are tools now, and every ADK agent gets them:

* ``ask_human`` — raise a card and (optionally) BLOCK on it, with a deadline and
  a declared default so the agent is never stuck forever.
* ``check_human`` — non-blocking: has the owner answered, or typed anything at
  this card since I last looked?
* ``list_my_cards`` / ``withdraw_card`` — an agent that resolves its own question
  must withdraw the card, or it trains the owner to ignore cards.

Two properties are inherited from the store and must not be re-implemented here:
a card without a stated default is REFUSED, and an answer is delivered back to
the raising session rather than merely recorded. Both are what make a card safe
to ignore and worth answering respectively.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Optional

from adk.decisions.store import (
    CLOSED_STATUSES,
    STATUS_ANSWERED,
    DecisionCard,
    DecisionError,
    DecisionOption,
    DecisionSource,
    DecisionStore,
    get_store,
)

logger = logging.getLogger("adk.decisions.agent_tools")

#: An agent that blocks forever is a hung agent. Any ``wait_seconds`` above this
#: is clamped, and the clamp is REPORTED in the result rather than applied
#: quietly — an agent that thinks it waited an hour and actually waited ten
#: minutes will draw the wrong conclusion from a timeout.
MAX_WAIT_SECONDS = float(os.getenv("AITHER_DECISIONS_MAX_WAIT", "3600"))

POLL_SECONDS = 1.0

#: Relay target for cards raised by OFF-DESKTOP awdk sessions. Genesis's
#: `/api/v1/decisions/raise` proxies to the owner-host harness daemon, so a
#: card POSTed here lands on the owner's desktop — the "an agent anywhere
#: reaches its owner" path the daemon was built for (daemon.py:878-891).
#: Set it ONLY where the owner's desktop is NOT this machine (containers,
#: tunnel/phone sessions): on the owner's own host, the local store already
#: IS the desktop and relaying would create a second card. Fail-soft: an
#: unreachable relay must never fail or delay the local raise.
DECISIONS_RELAY_URL = os.getenv("AITHER_DECISIONS_RELAY_URL", "").strip()


def _agent_name() -> str:
    return os.getenv("AITHER_AGENT_NAME", "").strip() or "adk-agent"


def _session_id() -> str:
    return (os.getenv("AITHER_SESSION_ID", "").strip()
            or os.getenv("CLAUDE_SESSION_ID", "").strip())


def parse_options(raw: Any) -> list[DecisionOption]:
    """Accept the three shapes an LLM actually emits, and refuse the rest.

    A tool argument arrives as whatever the model decided to send: a JSON array
    of objects, a JSON array of ``"key|Label|consequence"`` strings, or one
    newline-separated string. Guessing badly here produces a card whose options
    are nonsense while every log line says success — the exact failure the pipe
    separator was introduced for in the CLI.
    """
    if raw in (None, "", []):
        return []
    value = raw
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError as exc:
                raise DecisionError(f"options is not valid JSON: {exc}") from exc
        else:
            value = [line for line in text.splitlines() if line.strip()]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise DecisionError(f"options must be a list, got {type(raw).__name__}")

    out: list[DecisionOption] = []
    for index, entry in enumerate(value, start=1):
        if isinstance(entry, dict):
            key = str(entry.get("key") or index).strip()
            out.append(DecisionOption(
                key=key,
                label=str(entry.get("label") or key).strip(),
                consequence=str(entry.get("consequence") or "").strip(),
                recommended=bool(entry.get("recommended")),
            ))
            continue
        parts = str(entry).split("|")
        key = parts[0].strip() or str(index)
        out.append(DecisionOption(
            key=key,
            label=(parts[1].strip() if len(parts) > 1 else key),
            consequence=(parts[2].strip() if len(parts) > 2 else ""),
        ))
    return out


def raise_card(
    title: str,
    *,
    summary: str = "",
    detail: str = "",
    options: Any = None,
    facts: Any = None,
    kind: str = "decision",
    urgency: str = "normal",
    recommend: str = "",
    default: str = "",
    deadline_seconds: float = 0.0,
    agent: str = "",
    session_id: str = "",
    cwd: str = "",
) -> DecisionCard:
    """Raise a card from library code. The synchronous core of every tool here."""
    from adk.decisions.notify import notify

    parsed = parse_options(options)
    if recommend:
        wanted = recommend.strip().lower()
        for option in parsed:
            if option.key.lower() == wanted:
                option.recommended = True
    default_key = (default or "").strip()
    if not default_key and parsed:
        default_key = next((o.key for o in parsed if o.recommended), parsed[0].key)

    fact_list: list[str]
    if isinstance(facts, str):
        fact_list = [line for line in facts.splitlines() if line.strip()]
    else:
        fact_list = [str(f) for f in (facts or []) if str(f).strip()]

    try:
        from adk.decisions import winproc

        session_pid = winproc.resolve_owner_pid(os.getpid())
    except (ImportError, OSError):
        session_pid = os.getpid()

    store = get_store()
    card = store.create(DecisionCard(
        id="",
        title=(title or "").strip(),
        summary=summary.strip(),
        detail=detail.strip(),
        kind=kind,
        urgency=urgency,
        options=parsed,
        default_key=default_key,
        facts=fact_list,
        source=DecisionSource(
            session_id=session_id or _session_id(),
            agent=agent or _agent_name(),
            cwd=cwd or os.getcwd(),
            session_pid=session_pid,
        ),
        deadline=(time.time() + deadline_seconds) if deadline_seconds > 0 else None,
    ))
    _relay_card_if_configured(card)
    notify(card, store)
    return card


def _relay_card_if_configured(card: DecisionCard) -> None:
    """Best-effort mirror of a raised card to the owner's desktop plane.

    Only acts when ``AITHER_DECISIONS_RELAY_URL`` is set (off-desktop sessions).
    The URL is either a genesis base (``https://host:8001``) or the full
    ``/api/v1/decisions/raise`` endpoint; genesis proxies to the owner-host
    harness daemon. Runs on a DAEMON THREAD, never the caller's: ``raise_card``
    is the synchronous core of tools an async chat loop calls, and a sync
    ``httpx.post`` there would stall every concurrent turn -- a measured class, not a
    theoretical one. Never raises and never blocks — the card is already durable locally
    by the time this runs (same contract as notify()).
    """
    if not DECISIONS_RELAY_URL:
        return
    threading.Thread(
        target=_relay_post, args=(card, DECISIONS_RELAY_URL), daemon=True
    ).start()


def _relay_post(card: DecisionCard, relay_url: str) -> None:
    """The actual POST, run off-thread. Every failure degrades to a debug line."""
    try:
        import httpx

        base = relay_url.rstrip("/")
        if base.endswith("/api/v1/decisions/raise"):
            url = base
        else:
            url = f"{base}/api/v1/decisions/raise"
        payload = {
            "title": card.title,
            "kind": card.kind,
            "urgency": card.urgency,
            "summary": card.summary,
            "facts": list(card.facts),
            "options": [
                {
                    "key": o.key,
                    "label": o.label,
                    "consequence": o.consequence,
                    "recommended": o.recommended,
                }
                for o in card.options
            ],
            "default": card.default_key or "",
            "raised_by": card.source.agent or "",
            "agent": card.source.agent or "",
            "session_id": card.source.session_id or None,
            "cwd": card.source.cwd or None,
            "via": "awdk",
        }
        resp = httpx.post(
            url,
            json=payload,
            headers={"X-Caller-Type": "platform"},
            timeout=5.0,
        )
        if resp.status_code >= 300:
            logger.debug(
                "decision relay to %s failed: HTTP %s", url, resp.status_code
            )
    except Exception as exc:  # noqa: BLE001 — relay is best-effort, never fatal
        logger.debug("decision relay to genesis failed: %s", exc)


def _card_state(card: DecisionCard) -> dict[str, Any]:
    chosen = card.option(card.answer or "")
    return {
        "id": card.id,
        "status": card.status,
        "answer": card.answer,
        "answer_label": chosen.label if chosen else "",
        "answer_consequence": chosen.consequence if chosen else "",
        "answer_note": card.answer_note or "",
        "owner_said": [n.text for n in card.notes],
        "title": card.title,
    }


# ── the tools ───────────────────────────────────────────────────────────────────


async def ask_human(
    title: str,
    summary: str = "",
    options: str = "",
    facts: str = "",
    urgency: str = "normal",
    default: str = "",
    recommend: str = "",
    wait_seconds: float = 0,
    deadline_seconds: float = 0,
) -> str:
    """Ask the owner a question they can answer from a popup, phone or terminal.

    Use this ONLY when all three hold: the readings lead to materially different
    work, a wrong guess costs real work to undo, and you cannot settle it
    yourself by reading, measuring or taking the conventional default. Otherwise
    decide it, say so in one line and keep going — a card is an interruption,
    and an agent that raises them freely destroys the channel for the one that
    mattered.

    Args:
        title: One line, the whole ask. If it needs two, it is two cards.
        summary: One or two lines of what the owner needs in order to decide.
        options: The choices. JSON array of {key,label,consequence}, or one
            "key|Label|what happens if you pick it" per line. A consequence is
            what makes a card answerable without reading the code.
        facts: What you MEASURED, one per line — never a guess.
        urgency: low | normal | high | critical. high and critical take focus.
        default: The option key applied if the owner never answers. REQUIRED for
            a real decision; the store refuses a card without one.
        recommend: The option key you would pick. Say it — you did the work.
        wait_seconds: Block until answered, up to this long. 0 returns at once
            and you poll with check_human.
        deadline_seconds: Apply the default automatically after this long.
    """
    try:
        card = raise_card(
            title, summary=summary, options=options, facts=facts, urgency=urgency,
            default=default, recommend=recommend, deadline_seconds=deadline_seconds,
        )
    except DecisionError as exc:
        # Refusals here are the store enforcing "a card must be decidable".
        # Reported as a failure the agent can fix, never swallowed.
        return json.dumps({"ok": False, "error": str(exc)})

    wait = min(float(wait_seconds or 0), MAX_WAIT_SECONDS)
    clamped = float(wait_seconds or 0) > MAX_WAIT_SECONDS
    if wait <= 0:
        return json.dumps({"ok": True, "waited": False, **_card_state(card)})

    store = get_store()
    deadline = time.time() + wait
    while time.time() < deadline:
        await asyncio.sleep(POLL_SECONDS)
        current = store.get(card.id)
        if current is None:
            return json.dumps({"ok": False, "error": "the card disappeared while waiting",
                               "id": card.id})
        if current.status in CLOSED_STATUSES or current.notes:
            return json.dumps({"ok": True, "waited": True, **_card_state(current)})

    return json.dumps({
        "ok": True, "waited": True, "timed_out": True,
        "clamped_to_seconds": MAX_WAIT_SECONDS if clamped else None,
        "guidance": (
            f"No answer in {int(wait)}s. The card is still open and the owner can "
            f"answer it later. Proceed on the declared default "
            f"('{card.default_key}') and say that you did."
        ),
        **_card_state(card),
    })


async def check_human(card_id: str = "") -> str:
    """Has the owner answered, or typed anything at, a card I raised?

    Non-blocking. Call it between steps of long work so a mid-flight steer ("do
    the other one first") reaches you before you finish the wrong thing.

    Args:
        card_id: The card to check. Empty checks every card this session raised.
    """
    store = get_store()
    if card_id:
        try:
            card = store.get(card_id)
        except DecisionError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        if card is None:
            return json.dumps({"ok": False, "error": f"no such card: {card_id}"})
        return json.dumps({"ok": True, **_card_state(card)})

    session = _session_id()
    cards = store.list(status=None, session_id=session) if session else store.list()
    return json.dumps({
        "ok": True,
        "cards": [_card_state(c) for c in cards],
        "open": sum(1 for c in cards if c.is_open),
    })


async def list_my_cards(include_closed: bool = False) -> str:
    """What is waiting on the owner right now.

    Args:
        include_closed: Also list answered/expired/withdrawn cards.
    """
    store = get_store()
    cards = store.list(status=None if include_closed else "open")
    return json.dumps({
        "ok": True,
        "count": len(cards),
        "cards": [
            {**_card_state(c), "urgency": c.urgency,
             "age_seconds": round(c.age_seconds, 1),
             "agent": c.source.agent}
            for c in cards
        ],
    })


async def withdraw_card(card_id: str, reason: str = "") -> str:
    """Withdraw a card you no longer need answered.

    Do this the moment you resolve the question yourself. A card left open after
    the agent has moved on is how the owner learns that cards can be ignored.

    Args:
        card_id: The card to withdraw.
        reason: Why it is no longer needed.
    """
    try:
        card = get_store().cancel(card_id, note=reason or "the agent resolved it")
    except DecisionError as exc:
        return json.dumps({"ok": False, "error": str(exc)})
    return json.dumps({"ok": True, "id": card.id, "status": card.status})


# ── ask_credential: one tool to get a secret, and resume afterwards ───────────
#
# Before this, an agent that needed a secret had to know four things: raise a
# kind=credential card with five fields, open the masked prompt itself, poll for
# the answer, and remember to come back. Agents got one of the four wrong every
# time, and an agent whose session ended before the owner typed the value was
# never told at all -- its answer sat in a mailbox nobody drained. Now it is one
# call: it checks the vault first (by NAME), joins an open ask for the same
# secret instead of raising a second card, opens the prompt, blocks, and returns
# only {stored, scope, receipt_digest}. If the call times out or the session
# dies, steerback delivers the "stored" answer later and resumes the session.

#: The scopes a credential card may name (same three doors as secure_prompt).
CREDENTIAL_SCOPES = ("platform", "workspace", "user")

#: A vault key: what every store on the platform accepts, and nothing a path,
#: a shell or a URL could misread.
_SECRET_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{0,127}$")

#: How long ask_credential blocks when the caller does not say. Long enough for
#: an owner at the desk; the session is resumed by steerback if it is longer.
CREDENTIAL_WAIT_DEFAULT = 300.0


def _find_recipe(secret_name: str) -> "tuple[str, dict[str, Any]]":
    """``(recipe_id, recipe)`` whose vault_name is this secret, or ``("", {})``.

    A missing recipes file or PyYAML is the common case off the owner's
    checkout, so it degrades to "no recipe" rather than failing the ask.
    """
    try:
        from adk.decisions.recipes import RecipeError, load_recipes
    except ImportError:
        return "", {}
    try:
        recipes = load_recipes()
    except RecipeError:
        return "", {}
    for recipe_id, recipe in recipes.items():
        if str(recipe.get("vault_name") or "").strip() == secret_name:
            return recipe_id, recipe
    return "", {}


def _names_in(payload: Any) -> list[str]:
    """Secret NAMES from a vault list response (the shapes adk.sync accepts)."""
    if isinstance(payload, list):
        out: list[str] = []
        for item in payload:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and (item.get("name") or item.get("key")):
                out.append(str(item.get("name") or item.get("key")))
        return out
    if isinstance(payload, dict):
        inner = payload.get("secrets", payload)
        if isinstance(inner, dict):
            return [str(k) for k in inner]
        if isinstance(inner, list):
            return _names_in(inner)
    return []


def vault_presence(secret_name: str, scope: str) -> Optional[bool]:
    """Is ``secret_name`` already stored in ``scope``? True / False / None (unknown).

    NAMES ONLY: the platform vault is asked for its name list (``GET /secrets``
    returns metadata, never values) and the user lockbox for its list. The
    workspace store has no name-only listing reachable from here, so it is
    unknown -- and unknown raises the card, because asking once more costs the
    owner a click while claiming "present" wrongly costs the agent its task.
    Reached through the fleet host's in-container path (the vault is an
    in-network name), the same one secure_prompt writes through.
    """
    try:
        from adk.decisions.secure_prompt import _host_vault_module
    except ImportError:
        return None
    host = _host_vault_module()
    if host is None:
        return None
    if scope == "user":
        try:
            found = int(host.vault_len(secret_name, scope="user"))
        except (RuntimeError, OSError, ValueError, TypeError):
            return None
        return None if found < 0 else found > 0
    if scope != "platform":
        return None
    vault_url = str(getattr(host, "VAULT_URL", "") or "").rstrip("/")
    runner = getattr(host, "_sh_in_container", None)
    if not vault_url or runner is None:
        return None
    script = ("curl -sk --max-time 15 -H \"X-API-Key: $AITHER_INTERNAL_SECRET\" "
              f"-w '%{{http_code}}' {vault_url}/secrets\n")
    try:
        _rc, output = runner(script, timeout=40)
    except (RuntimeError, OSError) as exc:
        logger.debug("vault name list unavailable: %s", type(exc).__name__)
        return None
    output = output or ""
    if len(output) < 3 or not output[-3:].startswith("2"):
        return None
    try:
        names = _names_in(json.loads(output[:-3] or "[]"))
    except ValueError:
        return None
    return secret_name in names


def _receipt_digest(receipt: Optional[dict[str, Any]]) -> str:
    """sha256 of the canonical receipt -- identifies the write, reveals nothing."""
    if not isinstance(receipt, dict) or not receipt:
        return ""
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def credential_result(card: DecisionCard) -> dict[str, Any]:
    """What a waiter learns about a credential card. Never a value, never a length."""
    receipt = card.credential_receipt if isinstance(card.credential_receipt, dict) else {}
    stored = (card.status == STATUS_ANSWERED
              and card.answer == DecisionCard.CREDENTIAL_ANSWER)
    return {
        "ok": True,
        "card_id": card.id,
        "status": card.status,
        "stored": stored,
        "secret_name": card.secret_name or "",
        "scope": (card.credential_scope or "platform"),
        "receipt_digest": _receipt_digest(receipt),
        "receipt_signed": bool(receipt.get("signed")),
    }


def _open_credential_card(store: DecisionStore, secret_name: str,
                          scope: str) -> Optional[DecisionCard]:
    for card in store.list(status="open"):
        if ((card.kind or "").strip().lower() == "credential"
                and (card.secret_name or "") == secret_name
                and (card.credential_scope or "platform") == scope):
            return card
    return None


def ensure_prompt(card: DecisionCard, notified: Any = None) -> Any:
    """Make sure the owner can SEE this ask. Returns the ``CredentialSurface``.

    The desk Inbox hosts the masked field itself: the running desk, else the desk
    STARTED with its own launcher, else the remote surfaces whose link opens it on
    the PC, else a loud ``none`` (``secure_prompt.open_credential_surface``). It runs
    on every ask, a joined one too, because a credential ask the owner cannot see is
    the "no card anywhere" failure this exists to end -- and it never opens a Python
    window (owner, 2026-10-28).
    """
    from adk.decisions.secure_prompt import open_credential_surface

    return open_credential_surface(card, notified=notified)


def lane_instead(secret_name: str, text: str) -> Optional[dict[str, Any]]:
    """The ask result for a lane that needs NO new secret, or None when none fits.

    Checked BEFORE the vault and before a card: a token that exists but cannot do
    the job (a read-only HF_TOKEN for an upload) must not be the answer either.
    """
    try:
        from adk.decisions.recipes import lane_line, match_lane
    except ImportError:
        return None
    lane_id, lane = match_lane(secret_name, text)
    if not lane_id:
        return None
    return {"ok": True, "present": False, "stored": False, "card_id": None,
            "raised": False, "secret_name": secret_name, "lane": lane_id,
            "use": str(lane.get("use") or ""), "auth": str(lane.get("auth") or ""),
            "guidance": ("No card raised: this needs no new secret. " + lane_line(lane)
                         + " If the lane truly does not fit, ask again with "
                           "lane_override='<why it does not>'.")}


def start_credential_ask(
    secret_name: str,
    scope: str = "platform",
    description: str = "",
    why: str = "",
    *,
    wait_seconds: float = 0.0,
    agent: str = "",
    session_id: str = "",
    cwd: str = "",
    task: str = "",
    harness: str = "",
    subject: str = "",
    lane_override: str = "",
    credential_format: str = "",
) -> dict[str, Any]:
    """The non-blocking half of ask_credential: lane, check, raise or join, show.

    ``subject`` is the universe object the ask concerns; the store defaults it
    to ``secret:<secret_name>`` when empty. ``lane_override`` says why a matching
    no-secret lane does not fit; it is recorded on the card for the owner to read.
    ``credential_format`` (``totp_seed`` arms the desk door's seed guard) applies
    when no recipe names one.

    Returns ``{lane: id}`` when a lane needs no new secret (no card),
    ``{present: True}`` when the vault already holds the name (no card), an
    ``{"ok": False, "error"}`` for a bad ask, or ``{card_id, waiter, prompt, ...}``
    -- ``ok`` False there too, with the card id, when no surface can show it.
    """
    from adk.decisions import steerback

    name = (secret_name or "").strip()
    want_scope = (scope or "platform").strip().lower()
    if not _SECRET_NAME_RE.match(name):
        return {"ok": False, "error": "secret_name must be a vault key like "
                                      "STRIPE_API_KEY (letters, digits, _ . -)"}
    if want_scope not in CREDENTIAL_SCOPES:
        return {"ok": False, "error": f"scope must be one of {CREDENTIAL_SCOPES}"}
    why_line = (why or "").strip()
    recipe_id, recipe = _find_recipe(name)
    desc = ((description or "").strip() or str(recipe.get("why") or "").strip()
            or why_line)
    if not desc:
        return {"ok": False, "error": "say why you need it (description or why) -- "
                                      "the owner reads that line to decide"}
    override = (lane_override or "").strip()
    if not override:
        lane = lane_instead(name, " ".join((name, why_line, desc)))
        if lane is not None:
            return {**lane, "scope": want_scope}

    present = vault_presence(name, want_scope)
    if present:
        return {"ok": True, "present": True, "stored": True, "secret_name": name,
                "scope": want_scope, "card_id": None,
                "guidance": "already in the vault; read it from there, never print it"}

    who = (agent or _agent_name()).strip()
    sid = (session_id or _session_id()).strip()
    try:
        from adk.decisions import winproc

        session_pid = winproc.resolve_owner_pid(os.getpid())
    except (ImportError, OSError):
        session_pid = os.getpid()

    store = get_store()
    created = False
    # The scan and the write under ONE hold of the directory lock (an OS file
    # lock, reentrant in-process): two agents asking at once must find each
    # other's card, never each raise one -- three overlapping Proton cards is
    # what that race looked like on the owner's desk.
    with store._lock:  # noqa: SLF001 - the store's own cross-process lock
        card = _open_credential_card(store, name, want_scope)
        if card is None:
            title = str(recipe.get("title") or "").strip() or f"Enter {name}"
            detail = ""
            summary = why_line or desc
            if recipe:
                from adk.decisions.recipes import render_detail, render_summary

                summary = render_summary(recipe) or summary
                detail = render_detail(recipe)
            facts = [f"asked by {who}" + (f" (session {sid[:12]})" if sid else "")
                     + (f": {why_line}" if why_line else "")]
            if override:
                facts.append(f"a no-secret lane matched and was declined: {override}"[:240])
            candidate = DecisionCard(
                id="",
                title=title,
                summary=summary,
                detail=detail,
                kind="credential",
                urgency="normal",
                facts=facts,
                source=DecisionSource(session_id=sid, agent=who,
                                      cwd=cwd or os.getcwd(), session_pid=session_pid),
                secret_name=name,
                credential_format=str(recipe.get("format") or credential_format
                                      or "api_key"),
                credential_scope=want_scope,
                credential_description=desc,
                credential_recipe=recipe_id or None,
                subject=(subject or "").strip(),
            )
            try:
                card = store.create(candidate)
            except DecisionError as exc:
                return {"ok": False, "error": str(exc)}
            created = card is candidate
        elif why_line:
            store.add_facts(card.id, [f"also asked by {who}: {why_line}"[:240]])
        waiter = steerback.register_waiter(store, card.id, {
            "session_id": sid,
            "agent": who,
            "why": why_line,
            "task": (task or why_line or desc)[:600],
            "cwd": cwd or os.getcwd(),
            "session_pid": session_pid,
            "harness": harness,
            "waiter_pid": os.getpid() if wait_seconds > 0 else 0,
            "waiting_until": (time.time() + wait_seconds) if wait_seconds > 0 else 0,
        })

    notified = None
    if created:
        from adk.decisions.notify import notify

        _relay_card_if_configured(card)
        notified = notify(card, store)
    shown = ensure_prompt(card, notified)
    out: dict[str, Any] = {
        "ok": shown.shown, "present": False, "card_id": card.id, "joined": not created,
        "waiter": waiter, "secret_name": name, "scope": want_scope,
        "prompt": shown.surface, "surface_detail": shown.detail,
        "presence_known": present is not None}
    if shown.started_desk:
        out["started_desk"] = True
    if shown.link:
        out["link"] = shown.link
    if shown.remote:
        out["remote"] = list(shown.remote)
    if not shown.shown:
        # Loud, with the card id: the ask is recorded and stays open, but nothing
        # can put the field in front of the owner, so waiting on it would be a lie.
        out["error"] = shown.detail
    return out


def _finish_wait(card_id: str, waiter: str) -> Optional[dict[str, Any]]:
    """One poll: the waiter's result once the card has closed, else None."""
    store = get_store()
    current = store.get(card_id)
    if current is None:
        return {"ok": False, "error": "the card disappeared while waiting",
                "card_id": card_id}
    if current.status in CLOSED_STATUSES:
        from adk.decisions import steerback

        steerback.mark_waiter(store, card_id, waiter, "returned")
        return credential_result(current)
    return None


def _timed_out(card_id: str, waiter: str, wait: float, clamped: bool) -> dict[str, Any]:
    from adk.decisions import steerback

    store = get_store()
    steerback.mark_waiter(store, card_id, waiter, "timed_out")
    current = store.get(card_id)
    out = credential_result(current) if current is not None else {"ok": True}
    out.update({
        "timed_out": True, "stored": False, "card_id": card_id,
        "clamped_to_seconds": MAX_WAIT_SECONDS if clamped else None,
        "guidance": (f"No answer in {int(wait)}s. The ask stays open; when the owner "
                     "stores it this session is told (or resumed if it has ended). "
                     "Do not ask again -- carry on with work that does not need it."),
    })
    return out


def ask_credential_sync(
    secret_name: str,
    scope: str = "platform",
    description: str = "",
    why: str = "",
    wait_seconds: float = CREDENTIAL_WAIT_DEFAULT,
    **source: Any,
) -> dict[str, Any]:
    """Blocking ask_credential for a synchronous caller (the awsh MCP server)."""
    wait = min(max(float(wait_seconds or 0), 0.0), MAX_WAIT_SECONDS)
    clamped = float(wait_seconds or 0) > MAX_WAIT_SECONDS
    started = start_credential_ask(secret_name, scope, description, why,
                                   wait_seconds=wait, **source)
    if (not started.get("ok") or started.get("present") or not started.get("card_id")
            or wait <= 0):
        return started
    card_id, waiter = started["card_id"], started["waiter"]
    deadline = time.time() + wait
    while time.time() < deadline:
        done = _finish_wait(card_id, waiter)
        if done is not None:
            return {**done, "prompt": started.get("prompt")}
        time.sleep(POLL_SECONDS)
    return _timed_out(card_id, waiter, wait, clamped)


async def ask_credential(
    secret_name: str,
    scope: str = "platform",
    description: str = "",
    why: str = "",
    wait_seconds: float = CREDENTIAL_WAIT_DEFAULT,
    lane_override: str = "",
) -> str:
    """Ask the owner to store a secret, and wait until it is in the vault.

    One call does all of it: first it checks whether the job needs a secret at
    all (a known lane -- mirroring a model is `awrtifact mirror` on the gh login --
    comes back as {lane, use} with no card), then the vault for the NAME (returns
    present=true and raises nothing if it is already there), joins an open ask
    for the same secret instead of raising a second card, puts the masked
    field in front of the owner (the awdesk Inbox, starting the desk if it is
    installed and down; else the remote surfaces), and blocks. You get back
    {stored, scope, receipt_digest} -- never the value.
    Then read the secret from the vault yourself. If this times out, do not ask
    again: you are told (or resumed) when the owner stores it.

    Args:
        secret_name: The vault key, e.g. STRIPE_API_KEY.
        scope: platform | workspace | user -- which store it belongs in.
        description: What the secret is, shown to the owner.
        why: One line: what you will do with it. Shown on the card.
        wait_seconds: How long to block. 0 raises and returns at once.
        lane_override: Only when a returned lane does not fit: say why. The owner
            reads it on the card.
    """
    loop = asyncio.get_running_loop()
    wait = min(max(float(wait_seconds or 0), 0.0), MAX_WAIT_SECONDS)
    clamped = float(wait_seconds or 0) > MAX_WAIT_SECONDS
    started = await loop.run_in_executor(None, lambda: start_credential_ask(
        secret_name, scope, description, why, wait_seconds=wait,
        lane_override=lane_override))
    if (not started.get("ok") or started.get("present") or not started.get("card_id")
            or wait <= 0):
        return json.dumps(started)
    card_id, waiter = started["card_id"], started["waiter"]
    deadline = time.time() + wait
    while time.time() < deadline:
        done = _finish_wait(card_id, waiter)
        if done is not None:
            return json.dumps({**done, "prompt": started.get("prompt")})
        await asyncio.sleep(POLL_SECONDS)
    return json.dumps(_timed_out(card_id, waiter, wait, clamped))


#: Registered as the ``decisions`` category by ``adk.builtin_tools``.
DECISION_TOOLS = [ask_human, check_human, list_my_cards, withdraw_card, ask_credential]


def _self_test() -> int:
    """Prove the option parsing, the refusals and the wait/timeout all behave."""
    import tempfile
    from pathlib import Path

    problems: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if condition else 'FAIL'} {name}{'' if condition else ' ' + detail}")
        if not condition:
            problems.append(name)

    parsed = parse_options('[{"key":"a","label":"A","consequence":"c","recommended":true}]')
    check("parses JSON objects", len(parsed) == 1 and parsed[0].recommended)
    parsed = parse_options('["a|Keep it|nothing changes","b|Fix it|needs a restart"]')
    check("parses a JSON array of pipe strings",
          len(parsed) == 2 and parsed[1].consequence == "needs a restart")
    parsed = parse_options("a|Keep it|nothing changes\nb|Fix it|needs a restart")
    check("parses newline-separated pipe strings", len(parsed) == 2)
    parsed = parse_options(r"repoint|Repoint to C:\Projects\Example|drops the D: dep")
    check("an option can carry a Windows path",
          parsed[0].label == r"Repoint to C:\Projects\Example", parsed[0].label)
    try:
        parse_options("[not json")
        check("refuses malformed JSON", False, "it was accepted")
    except DecisionError:
        check("refuses malformed JSON", True)

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AITHER_DECISIONS_DIR"] = str(Path(tmp) / "cards")
        os.environ["AITHER_STEER_DIR"] = str(Path(tmp) / "steer")
        os.environ["AITHER_DECISIONS_POPUP"] = "0"   # no window in a self-test
        os.environ["AITHER_SESSION_ID"] = "agent-selftest"
        import adk.decisions.store as store_module

        store_module._STORE = None  # noqa: SLF001 - rebind the singleton at the temp dir

        bare = json.loads(asyncio.run(ask_human("no options at all")))
        check("refuses an optionless decision card",
              not bare.get("ok") and "options" in bare.get("error", ""), str(bare))

        raised = json.loads(asyncio.run(ask_human(
            "Keep the 5090 in the pool while the DGX rebuilds?",
            summary="Both are defensible and they diverge by days of work.",
            options="keep|Keep it pooled|throughput holds, DGX rebuild is slower\n"
                    "drain|Drain the 5090|rebuild finishes tonight, pool loses 40%",
            facts="pool throughput measured at 41 tok/s\nDGX rebuild ETA 6h",
            recommend="keep", default="keep",
        )))
        check("raises a real card", raised.get("ok") and raised["id"].startswith("d-"),
              str(raised))
        check("does not wait when not asked", raised.get("waited") is False)

        card_id = raised["id"]
        listed = json.loads(asyncio.run(list_my_cards()))
        check("lists the open card", listed["count"] == 1)

        checked = json.loads(asyncio.run(check_human(card_id)))
        check("check_human sees it open", checked["status"] == "open")

        # A wait that nobody answers must TIME OUT with guidance, never hang and
        # never claim an answer.
        started = time.time()
        timed = json.loads(asyncio.run(ask_human(
            "will not be answered", options="a|A|nothing\nb|B|nothing",
            default="a", wait_seconds=2,
        )))
        check("a wait times out rather than hanging", time.time() - started < 20)
        check("the timeout says what to do next",
              timed.get("timed_out") and "default" in timed.get("guidance", ""), str(timed))

        # An answer that lands WHILE the agent is waiting must be returned by the
        # wait itself — not merely recorded for a later poll. That is the whole
        # difference between a blocking ask and a survey.
        store = store_module.get_store()
        subject = "answered while the agent waits"

        async def answer_midflight() -> str:
            for _ in range(100):
                await asyncio.sleep(0.2)
                match = [c for c in store.list() if c.title == subject]
                if match:
                    store.answer(match[0].id, "b", note="do it tonight", via="selftest")
                    return match[0].id
            return ""

        async def race():
            answering = asyncio.create_task(answer_midflight())
            waited = await ask_human(
                subject, options="a|A|nothing\nb|B|nothing", default="a", wait_seconds=15,
            )
            await answering
            return json.loads(waited)

        raced = asyncio.run(race())
        check("a blocking wait returns the answer it was waiting for",
              raced.get("answer") == "b" and not raced.get("timed_out"), str(raced))
        check("the owner's note comes back with it",
              raced.get("answer_note") == "do it tonight", str(raced))
        after = json.loads(asyncio.run(check_human(card_id)))
        check("an unanswered card is still open afterwards", after["status"] == "open")

        withdrawn = json.loads(asyncio.run(withdraw_card(timed["id"], "resolved it myself")))
        check("withdrawing works", withdrawn.get("ok") and withdrawn["status"] == "cancelled")
        asyncio.run(withdraw_card(card_id, "done with it"))
        gone = json.loads(asyncio.run(list_my_cards()))
        check("a withdrawn card leaves the open list", gone["count"] == 0, str(gone))

    print()
    if problems:
        print(f"agent-tools self-test FAILED - {', '.join(problems)}")
        return 1
    print("agent-tools self-test passed - cards raise, wait, time out and withdraw")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
