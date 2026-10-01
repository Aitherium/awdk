"""``adk home serve`` -- your home agent, answering only you, over relay DMs.

The owner gate, pairing, approval cards, receipts and follow-ups live in the
transport-agnostic :class:`adk.home.hearth.HearthCore`; this module is the relay
transport (:class:`OwnerRelayClient`) plus the agent/tool policy. Other channels
(Telegram, Discord, Slack via :class:`adk.home.hearth.ChannelAdapterTransport`)
plug into the same core, each with its own bound owner identity.

The agent runs on this box (its model, memory and tools stay here); the relay
only carries text. Four guards make that safe to leave running:

1. **Owner gate.** Only DMs from the nick in ``<home>/owner.json`` are handled,
   compared EXACTLY (relay nicks are case-sensitive: ``DAVID`` is another
   account). Everyone else gets no reply and one log line. The owner is bound
   once: start with ``--pair``, a 6-digit code is printed on THIS console, and
   the first DM whose WHOLE content is that code binds its sender -- only if the
   relay reports the sender as a registered account (a guest nick can be claimed
   by anyone later). A six-digit DM from a registered account that is not the
   code is a failed guess: that sender is ignored after ``MAX_PAIR_ATTEMPTS``,
   and pairing closes after ``MAX_PAIR_GUESSES_TOTAL`` from everyone together.
2. **No file or shell tools.** The agent is built with ``builtin_tools=False``
   and then given exactly ``web`` + ``decisions`` + the life tools
   (:mod:`adk.home.life_tools`) -- plus, on a signed-in home, the calendar /
   mail / to-do tools (:mod:`adk.home.connector_tools`). A ``file_*`` /
   ``shell*`` tool arriving by any other route (an env toolpack, an app proxy)
   stops serve before it starts.
3. **Approval over DM.** ``AITHER_TOOL_APPROVAL`` always includes
   :data:`~adk.home.life_tools.ALWAYS_ASK`. A gated call pauses the turn; the
   owner gets a numbered card with an 8-hex nonce, and ONLY an owner reply of
   ``yes <nonce>`` / ``no <nonce>`` resumes it, always with an explicit
   ``result``. A yes covers the carded call only: the exact arguments shown
   (``args#``) are bound, and a call of that tool with other arguments is
   refused. A card dies after ``MAX_NONCE_MISSES`` wrong nonces, after
   ``CARD_TTL_S``, or when a new turn (owner message or due follow-up) starts;
   every new turn also clears the session's recorded decisions. A resumed turn
   re-runs from its user message, so tool calls it already made are replayed
   from their first result, never executed twice.
4. **Receipts.** Every tool call and every outbound DM is appended to the signed,
   hash-chained log (:mod:`adk.receipts`); ``adk home receipts --verify`` checks it.

Follow-ups (``<home>/followups.json``) are checked at startup and on every tick.
A due row is marked fired BEFORE its DM is sent, so a crash loses one DM rather
than repeating it on every restart.

A turn started by a due follow-up is UNATTENDED: its one-shot ``remind_me`` /
``follow_up`` refuse (:attr:`FollowupStore.unattended`), so it cannot re-schedule
itself into an unapproved repeating job.

Not claimed: end-to-end encryption. The relay can read DMs, and owner identity
rests on the relay refusing anonymous claims of a registered nick.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote

import httpx

from adk.relay_client import RelayClient

from .config import HomeConfig, HomeError, compose_system_prompt, home_dir
from .hearth import (  # noqa: F401 - re-exported: the serve API predates hearth.py
    CARD_TTL_S,
    UNTRUSTED_PROMPT,
    MAX_NONCE_MISSES,
    MAX_PAIR_ATTEMPTS,
    MAX_PAIR_DM_LEN,
    NONCE_HEX,
    OWNER_NAME,
    SESSION_ID,
    HearthCore,
    OwnerRegistry,
    new_pair_code,
    owner_path,
)
from .connector_tools import home_signed_in
from .home_tools import HOME_PROMPT, build_home_tools
from .life_tools import ALWAYS_ASK, FollowupStore, build_life_tools

logger = logging.getLogger("adk.home.serve")

DEFAULT_RELAY_URL = "https://relay.aitherium.com/api/relay/v1"
RELAY_CHANNEL = "relay"
SERVE_CATEGORIES = ("web", "decisions")
#: A registered tool whose name starts with one of these refuses to serve.
FORBIDDEN_PREFIXES = ("file_", "shell")
SERVE_PROMPT = """\
## How you reach your owner
You are talking to your owner by direct message (relay, a chat app or
email). Keep replies short. To do something later use remind_me or follow_up; a repeating job is
follow_up_recurring and needs the owner's yes. When asked what you did, call
receipts -- never answer from memory.
""" + "\n" + UNTRUSTED_PROMPT
TUTOR_PROMPT = """\
## Aither Learn
Your owner is a parent; their children learn in Aither Learn. Call a child by
name -- the tools accept a name or a learner id:
- "how did Alexander do this week?" -> tutor_report(lid="Alexander"); relay its
  summary warmly and briefly, never as a score.
- "have Athena practice short vowels" -> tutor_assign(lid="Athena",
  skill_id="short vowels"); the owner confirms it first.
- "set Athena's focus to rhymes this week" -> tutor_set_focus(lid="Athena",
  skills="rhymes", note=""); a weekly focus plus an optional coach note for the
  tutor's tone; the owner confirms it first.
- tutor_learners lists the children when a name is unclear.
"""
#: ``1``/``on`` always gives Hearth the tutor tools, ``0``/``off`` never does; unset =
#: whenever the owner holds an Aitherium bearer (a saved sign-in or a platform token).
TUTOR_FLAG_ENV = "AITHER_HOME_TUTOR"
#: Where the tutor tools reach Genesis (``/api/v1/tutor/family/*``); the first set wins.
TUTOR_URL_ENV = ("AITHER_TUTOR_URL", "AITHER_GENESIS_URL", "GENESIS_URL")
DEFAULT_TUTOR_URL = "https://api.aitherium.com"
#: Said (logged by serve, raised by ``teach setup --keep-model``) when the classroom
#: tools are on but the model is not on this computer.
TEACHER_NEEDS_LOCAL = ("classroom tools need the local model: student records never go "
                       "to a bring-your-own-key or remote model. Run: adk home model "
                       "--local bonsai")
TEACHER_PROMPT = """\
## Aither Classroom
Your owner is a teacher. Name classes and students as the teacher does ("Room 4",
"Ana"); the tools resolve them on the teacher's own roster.
- "what is Room 4 finding hard?" -> struggle_report(class_name="Room 4"); relay the
  OBSERVATIONS with their evidence. Never diagnose, never label a child, never name a
  condition, never compare children.
- "how is Ana doing?" -> struggle_report(class_name=..., student="Ana").
- Never grade a child. grade_assist only suggests rubric notes; the teacher sets
  every score in the console.
- lesson_draft and differentiate save DRAFTS; say so. Publishing is the teacher's
  action. The owner confirms each first.
- A note to a parent: parent_note_draft first, show the draft, then parent_note_send
  only with the teacher's words; the owner confirms it first.
"""
#: ``1``/``on`` gives Hearth the classroom tools, ``0``/``off`` never does; unset =
#: whatever ``adk home teach setup`` recorded in ``<home>/teacher.json`` (default off:
#: only a teacher wants them).
TEACHER_FLAG_ENV = "AITHER_HOME_TEACHER"
#: Where the classroom tools reach Genesis (``/api/v1/classroom/*``); this env, then
#: the URL ``teach setup`` saved, then the tutor URL.
TEACHER_URL_ENV = ("AITHER_CLASSROOM_URL",)
TEACHER_STATE = "teacher.json"
#: A home agent reachable from chat apps: inbound A2A calls must be signed by a
#: trusted key unless the operator chose otherwise.
A2A_TRUST_ENV = "AITHER_A2A_REQUIRE_TRUST"


# ── state files ───────────────────────────────────────────────────────────────

def load_owner(root: Optional[Path] = None) -> str:
    """The relay owner's nick ("" when none is bound)."""
    return OwnerRegistry(owner_path(root)).owner(RELAY_CHANNEL)


def save_owner(nick: str, root: Optional[Path] = None) -> Path:
    """Bind ``nick`` as the relay owner (other channels' owners are kept)."""
    registry = OwnerRegistry(owner_path(root))
    registry.bind(RELAY_CHANNEL, nick)
    return registry.path


def receipts_path(root: Optional[Path] = None) -> Path:
    """``$AITHER_RECEIPTS_PATH``, else ``<home>/actions.jsonl``."""
    env = (os.environ.get("AITHER_RECEIPTS_PATH") or "").strip()
    return Path(env) if env else (root or home_dir()) / "actions.jsonl"


# ── policy + agent ──────────────────────────────────────────────────────────────

def apply_approval_policy() -> List[str]:
    """Add :data:`ALWAYS_ASK` to ``AITHER_TOOL_APPROVAL`` (never removes a name)."""
    raw = os.environ.get("AITHER_TOOL_APPROVAL", "")
    names = [p.strip() for p in raw.split(",") if p.strip()]
    if "*" not in names:
        for n in ALWAYS_ASK:
            if n.lower() not in {x.lower() for x in names}:
                names.append(n)
    os.environ["AITHER_TOOL_APPROVAL"] = ",".join(names)
    return names


def apply_a2a_trust_default() -> str:
    """``AITHER_A2A_REQUIRE_TRUST`` defaults to ``true`` under serve (an explicit value
    -- ``audit`` / ``false`` -- is kept). Returns the mode in effect."""
    return os.environ.setdefault(A2A_TRUST_ENV, "true")


def tutor_enabled() -> bool:
    """Does this owner get the Aither Learn tools? See :data:`TUTOR_FLAG_ENV`."""
    from .connector_tools import owner_bearer

    flag = (os.environ.get(TUTOR_FLAG_ENV) or "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    return bool(owner_bearer())


def tutor_url() -> str:
    for name in TUTOR_URL_ENV:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value.rstrip("/")
    return DEFAULT_TUTOR_URL


def _teacher_state(root: Optional[Path] = None) -> Dict[str, Any]:
    import json

    try:
        data = json.loads(((root or home_dir()) / TEACHER_STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def teacher_enabled(root: Optional[Path] = None) -> bool:
    """Does this owner get the Aither Classroom tools? See :data:`TEACHER_FLAG_ENV`."""
    flag = (os.environ.get(TEACHER_FLAG_ENV) or "").strip().lower()
    if not flag:
        flag = str(_teacher_state(root).get(TEACHER_FLAG_ENV) or "").strip().lower()
    return flag in ("1", "true", "yes", "on")


def teacher_url(root: Optional[Path] = None) -> str:
    for name in TEACHER_URL_ENV:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value.rstrip("/")
    saved = str(_teacher_state(root).get("url") or "").strip()
    return saved.rstrip("/") if saved else tutor_url()


def tool_names(agent: Any) -> List[str]:
    return sorted(td.name for td in agent._tools.list_tools())


def forbidden_tools(agent: Any) -> List[str]:
    return [n for n in tool_names(agent) if n.lower().startswith(FORBIDDEN_PREFIXES)]


def register_serve_tools(agent: Any, store: FollowupStore,
                         receipts_file: Optional[Path] = None,
                         connectors: Optional[bool] = None,
                         tutor: Optional[bool] = None,
                         teacher: Optional[bool] = None,
                         local_model: bool = False,
                         planner_root: Optional[Path] = None) -> List[str]:
    """``web`` + ``decisions`` + life tools; refuses if a file/shell tool is present.

    Every home gets the calendar / to-do / mail tools (:mod:`adk.home.home_tools`):
    the built-in calendar and to-do list, the calendars subscribed by link and the
    app-password mailbox need no sign-in. ``connectors`` adds the signed-in home's
    OAuth accounts (:mod:`adk.home.connector_tools`) to those same tools, and the
    Aither Learn guardian tools (:mod:`adk.home.tutor_tools`). None means "when
    this home is signed in": an unsigned home has no bearer to resolve a connection
    or call the tutor router with.

    ``tutor`` decides the tutor tools on their own; None follows ``connectors``
    when that was given, else :func:`tutor_enabled`. ``tutor_assign`` is in
    :data:`ALWAYS_ASK`, so a parent's "have Athena practise X" is a card first.

    ``teacher`` adds the Aither Classroom tools (:mod:`adk.home.teacher_tools`); None
    means :func:`teacher_enabled`. Their writers (lesson_draft, differentiate,
    parent_note_send) are in :data:`ALWAYS_ASK`. They are registered ONLY when
    ``local_model`` is true (``models.is_local(cfg.model)``): every tool result enters
    the agent's conversation, so with a bring-your-own-key model student records
    would go to a third-party API. Not local means no classroom tool at all.
    """
    from adk.builtin_tools import register_builtin_tools

    from .connector_tools import owner_bearer
    from .planner import Planner
    from .tutor_tools import build_tutor_tools

    register_builtin_tools(agent, categories=list(SERVE_CATEGORIES))
    for fn in build_life_tools(store, receipts_file):
        agent._tools.register(fn)
    with_accounts = home_signed_in() if connectors is None else connectors
    for fn in build_home_tools(Planner(planner_root), remote=bool(with_accounts)):
        agent._tools.register(fn)
    if tutor is None:
        tutor = tutor_enabled() if connectors is None else connectors
    if tutor:
        for fn in build_tutor_tools(tutor_url(), owner_bearer()):
            agent._tools.register(fn)
    if teacher_enabled() if teacher is None else teacher:
        if local_model:
            from .teacher_tools import build_teacher_tools, llm_generate

            generate = llm_generate(getattr(agent, "llm", None), local=True)
            for fn in build_teacher_tools(teacher_url(), owner_bearer(), generate=generate):
                agent._tools.register(fn)
        else:
            logger.warning(TEACHER_NEEDS_LOCAL)
    bad = forbidden_tools(agent)
    if bad:
        raise HomeError(f"refusing to serve: the agent holds {', '.join(bad)}; a "
                        "relay-reachable agent never gets file or shell tools (check "
                        "AITHER_TOOL_PACKS / ADK_APP_PROXY_URL)")
    return tool_names(agent)


def build_serve_agent(cfg: HomeConfig, store: FollowupStore, root: Optional[Path] = None,
                      llm: Any = None, memory: Any = None,
                      receipts_file: Optional[Path] = None) -> Any:
    """An ``AitherAgent`` with no default tools, then exactly the serve set."""
    from adk.agent import AitherAgent

    if llm is None:
        from .models import build_llm

        llm = build_llm(cfg.model)
    with_connectors = home_signed_in()
    with_tutor = tutor_enabled()
    from .models import is_local

    local_model = is_local(cfg.model)
    with_teacher = teacher_enabled(root)
    if with_teacher and not local_model:
        # No tools and no prompt: the agent must not claim a classroom it cannot reach.
        logger.warning(TEACHER_NEEDS_LOCAL)
        with_teacher = False
    prompt = compose_system_prompt(root) + "\n\n" + SERVE_PROMPT
    prompt += "\n" + HOME_PROMPT
    if with_tutor:
        prompt += "\n" + TUTOR_PROMPT
    if with_teacher:
        prompt += "\n" + TEACHER_PROMPT
    agent = AitherAgent(
        name=cfg.name, llm=llm, memory=memory, system_prompt=prompt,
        builtin_tools=False, user_mcp=False, load_packs=False,
    )
    # Ship every schema every turn. The default core-set trimming offers only
    # web/file tools + ``load_tools``; a small local model never loads the rest, so
    # it never saw remind_me/follow_up/receipts and answered "I set a reminder"
    # with nothing on disk (measured live on Bonsai 8B, 2026-09-29).
    agent.tool_selection = "all"
    register_serve_tools(agent, store, receipts_file, connectors=with_connectors,
                         tutor=with_tutor, teacher=with_teacher, local_model=local_model,
                         planner_root=root)
    return agent


# ── the relay transport ─────────────────────────────────────────────────────────

class OwnerRelayClient(RelayClient):
    """The relay as a Hearth transport: a :class:`RelayClient` whose DMs feed a
    :class:`HearthCore` (built here, or passed as ``core``) as channel ``relay``.

    Relay-specific guards stay here: a DM whose ``from_nick`` is not its thread
    partner is dropped before the core sees it, and pairing requires the relay to
    report the sender as a REGISTERED account (:meth:`verify_identity`).
    """

    name = RELAY_CHANNEL
    unverified_reply = ("That code is right, but this nick is not a registered relay "
                        "account, so anyone could use it later. Register or sign in, "
                        "then send the code again.")

    def __init__(self, base_url: str, token: str, nick: str, agent: Any, *,
                 owner_nick: str = "", pair_code: str = "",
                 root: Optional[Path] = None,
                 store: Optional[FollowupStore] = None,
                 receipts_file: Optional[Path] = None,
                 session_id: str = SESSION_ID,
                 poll_interval: float = 4.0,
                 verify: Union[bool, str, None] = None,
                 core: Optional[HearthCore] = None):
        super().__init__(base_url, token, nick, agent, poll_interval=poll_interval,
                         verify=verify)
        self.root = root
        #: The httpx client in use (a poll pass's, or the one :meth:`start` opened).
        self._http: Optional[httpx.AsyncClient] = None
        self._poll_task: Optional["asyncio.Future[None]"] = None
        if core is None:
            core = HearthCore(agent,
                              store or FollowupStore((root or home_dir()) / "followups.json"),
                              receipts_file or receipts_path(root),
                              root=root, pair_code=pair_code, session_id=session_id)
        elif pair_code and not core.pair_code:
            core._pair = {"code": pair_code, "channel": "", "expires": 0.0}
        self.core = core
        core.add_transport(self)
        if owner_nick:
            core.registry.remember(RELAY_CHANNEL, owner_nick)

    # ── views onto the core (the pre-hearth attribute API) ─────────────────
    @property
    def owner_nick(self) -> str:
        return self.core.registry.owner(RELAY_CHANNEL)

    @property
    def pair_code(self) -> str:
        return self.core.pair_code

    @property
    def _pair_attempts(self) -> int:
        return self.core._pair_attempts

    @property
    def awaiting(self) -> Optional[Dict[str, Any]]:
        return self.core.awaiting

    @property
    def store(self) -> FollowupStore:
        return self.core.store

    @store.setter
    def store(self, value: FollowupStore) -> None:
        self.core.store = value

    @property
    def receipts_file(self) -> Path:
        return self.core.receipts_file

    @property
    def session_id(self) -> str:
        return self.core.session_id

    def is_owner(self, partner: str, from_nick: str) -> bool:
        """EXACT match: relay nicks are case-sensitive, so ``DAVID`` is not ``david``."""
        if from_nick and from_nick != partner:
            return False
        return self.core.is_owner(RELAY_CHANNEL, partner)

    # ── Transport ──────────────────────────────────────────────────────────
    async def send(self, user_id: str, text: str) -> bool:
        if self._http is not None:
            return await self._post_dm(self._http, user_id, text)
        async with httpx.AsyncClient(follow_redirects=True, timeout=30,
                                     verify=self.verify) as client:
            return await self._post_dm(client, user_id, text)

    async def _post_dm(self, client: httpx.AsyncClient, to_nick: str, content: str) -> bool:
        r = await client.post(f"{self.base_url}/dms", headers=self._headers(),
                              json={"to_nick": to_nick, "content": content})
        if r.status_code not in (200, 201):
            logger.error("hearth: relay DM to %s refused (HTTP %s)", to_nick, r.status_code)
            return False
        return True

    async def verify_identity(self, user_id: str) -> bool:
        if self._http is not None:
            return await self._is_registered(self._http, user_id)
        async with httpx.AsyncClient(follow_redirects=True, timeout=30,
                                     verify=self.verify) as client:
            return await self._is_registered(client, user_id)

    async def _is_registered(self, client: httpx.AsyncClient, nick: str) -> bool:
        """Does the relay hold a registered account for exactly this nick?

        A guest nick is claimable by anyone once its holder goes offline, so it
        can never be the owner. Unknown or unreachable counts as not registered.
        """
        try:
            data = await self._get(client, f"/nick/{quote(nick, safe='')}/status")
        except httpx.HTTPError as exc:
            logger.warning("hearth: could not check that %s is registered: %s", nick, exc)
            return False
        return (isinstance(data, dict) and data.get("status") == "registered"
                and str(data.get("nick") or nick) == nick)

    async def start(self, core: HearthCore) -> None:
        """Multi-channel mode: join, prime, and poll DMs in the background while
        ``core.run()`` fires the follow-ups. (``stop()`` is the base class's.)"""
        if core is not self.core:
            raise ValueError("this relay transport belongs to another HearthCore")
        self._running = True
        client = httpx.AsyncClient(follow_redirects=True, timeout=30, verify=self.verify)
        self._http = client
        try:
            await self._join_and_prime(client)
        except BaseException:
            self._http = None
            await client.aclose()
            raise
        self._poll_task = asyncio.ensure_future(self._poll_loop(client))

    async def _poll_loop(self, client: httpx.AsyncClient) -> None:
        try:
            while self._running:
                try:
                    await self.poll_once(client)
                except httpx.HTTPError as exc:
                    logger.warning("hearth: poll error (continuing): %s", exc)
                await asyncio.sleep(self.poll_interval)
        finally:
            self._http = None
            await client.aclose()

    # ── polling ────────────────────────────────────────────────────────────
    async def poll_once(self, client: httpx.AsyncClient) -> int:
        """One pass over DMs. Only the owner (or a correct pairing DM) is answered."""
        self._http = client
        replies = 0
        for p in self._rows(await self._get(client, "/dms/partners")):
            partner = str(p.get("nick") or p.get("partner") or "")
            if not partner or partner.lower() == self.nick.lower():
                continue
            thread = self._rows(await self._get(client, f"/dms/{partner}"))
            if not thread:
                continue
            last = thread[-1]
            last_id = str(last.get("id") or last.get("message_id") or "")
            if not last_id or self._seen.get(partner) == last_id:
                continue
            self._seen[partner] = last_id
            # Exact, as the relay stamped it: nicks are case-sensitive accounts.
            from_nick = str(last.get("from_nick") or last.get("from") or "")
            if from_nick.lower() == self.nick.lower():
                continue
            if from_nick and from_nick != partner:
                logger.warning("hearth: ignored a DM from %s in %s's thread (not the owner)",
                               from_nick, partner)
                continue
            if await self.core.on_message(RELAY_CHANNEL, partner,
                                          str(last.get("content", ""))):
                replies += 1
        return replies

    async def fire_due(self, client: httpx.AsyncClient, now: Optional[float] = None) -> int:
        """Run every due follow-up once and message the owner. No owner = nothing claimed."""
        self._http = client
        return await self.core.fire_due(now)

    async def _prime(self, client: httpx.AsyncClient) -> None:
        """Mark existing threads as seen so a restart never answers the backlog."""
        for p in self._rows(await self._get(client, "/dms/partners")):
            partner = str(p.get("nick") or p.get("partner") or "")
            thread = self._rows(await self._get(client, f"/dms/{partner}")) if partner else []
            if thread:
                self._seen[partner] = str(
                    thread[-1].get("id") or thread[-1].get("message_id") or "")

    async def _join_and_prime(self, client: httpx.AsyncClient) -> None:
        if not await self.join(client):
            raise RuntimeError("relay join failed -- check the token / nick")
        self._joined = True
        await self._prime(client)

    async def startup(self, client: httpx.AsyncClient) -> None:
        await self._join_and_prime(client)
        await self.fire_due(client)

    async def tick(self, client: httpx.AsyncClient) -> int:
        return await self.poll_once(client) + await self.fire_due(client)

    async def run(self) -> None:
        """Relay-only mode: join, then serve the owner's DMs and due follow-ups forever.

        Deliberately NOT the base loop: ``poll_channel_once`` answers anyone who
        @-mentions the agent in a channel, which is exactly the non-owner path
        this client exists to close.
        """
        self._running = True
        async with httpx.AsyncClient(follow_redirects=True, timeout=30,
                                     verify=self.verify) as client:
            await self.startup(client)
            while self._running:
                try:
                    await self.tick(client)
                except httpx.HTTPError as exc:
                    logger.warning("hearth: poll error (continuing): %s", exc)
                await asyncio.sleep(self.poll_interval)
