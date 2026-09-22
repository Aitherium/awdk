"""The AitherShell harness daemon — one API for every front-end.

This is what makes "AitherShell on my desktop" and "AitherShell on
aitherium.com" the same thing rather than two lookalikes: both are clients of
this daemon. The desktop CLI talks to it over loopback; the browser talks to it
through AitherTunnel. A session started in one is visible and attachable in the
other, because there is exactly one manager behind this API.

Security posture (fail-closed, per .claude/rules/security-review-patterns.md)
----------------------------------------------------------------------------
This daemon spawns coding agents with filesystem access. It is therefore
treated as a privileged surface:

- It REFUSES to start without a bearer token. There is no "no auth in dev"
  mode — that is how an unauthenticated port ends up exposed through a tunnel.
- Every route except ``/health`` denies on missing (401) or wrong (403)
  credentials, and the comparison is constant-time.
- CORS is an explicit ALLOWLIST. ``*`` is rejected outright when credentials
  are involved, and the allowlist is printed at startup so an over-broad one is
  visible rather than discovered later.
- ``cwd`` is validated against an allowlist of roots when
  ``AITHER_HARNESS_ALLOWED_ROOTS`` is set, so a tunnel-exposed daemon cannot be
  pointed at ``C:\\`` by a caller. Unset means "this host is trusted", which is
  the desktop default and is stated in ``/health`` rather than assumed.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from adk.harnesses.manager import ManagerError, SessionManager, default_manager
from adk.harnesses.models import ProfileError, list_profiles
from adk.harnesses.registry import get as get_harness
from adk.harnesses.character_recall import register as register_character_recall
from adk.harnesses.rooms import DEFAULT_ROOM, RoomError, default_registry
from adk.harnesses.session import SessionConfig
from adk.harnesses.spool import default_tailer
from adk.harnesses.steer_dispatch import frame_peer_text
from adk.harnesses.steer_dispatch import register as register_steer_dispatcher
from adk.harnesses.transcript_bridge import default_bridge
from adk.harnesses.well import default_well

#: Bind address. Loopback-only was correct while the fleet ran on Docker Desktop, which
#: gave containers `host.docker.internal` straight to the Windows loopback. After the
#: podman cutover that path is GONE: genesis now runs in a podman container inside the
#: WSL2 distro, and a Windows-loopback socket is unreachable from there. Measured
#: 2026-08-11 from inside `aitheros-genesis` — `host.docker.internal`,
#: `host.containers.internal`, `gateway.containers.internal` and `10.0.2.2` ALL returned
#: 000, while the daemon answered 401 (i.e. alive) on the host's own 127.0.0.1. Genesis
#: therefore served `/api/v1/decisions/*` as a 503 "daemon unreachable" — the card
#: channel was mounted and inert.
#:
#: `0.0.0.0` is the fleet-reachable default rather than a specific interface because the
#: WSL2 gateway address is DHCP-assigned and changes across reboots, so pinning it would
#: break silently every restart. Exposure is bounded by the daemon's own bearer auth —
#: every route depends on `auth`, and an unauthenticated request gets 401, which is what
#: the 401 above actually demonstrates. Set AITHER_HARNESS_HOST=127.0.0.1 to restore
#: loopback-only on a machine that runs no containers.
DEFAULT_HOST = os.environ.get("AITHER_HARNESS_HOST", "127.0.0.1")

#: What the SERVER binds to — deliberately separate from DEFAULT_HOST, which is what
#: CLIENTS connect to (`cli.py:_base_url` builds `http://{DEFAULT_HOST}:{PORT}`). Setting
#: one value to `0.0.0.0` would point every local client at a bind-all address, which is
#: not connectable. `daemon_endpoint.py` already draws this distinction the same way
#: (`advertised_host = DEFAULT_HOST if host in ("0.0.0.0", "::", "")`).
DEFAULT_BIND_HOST = os.environ.get(
    "AITHER_HARNESS_BIND_HOST", os.environ.get("AITHER_HARNESS_HOST", "0.0.0.0"),
)  # noqa: S104
DEFAULT_PORT = int(os.environ.get("AITHER_HARNESS_PORT", "8362"))
TOKEN_PATH = Path.home() / ".aither" / "harness_token"

#: OPTIONAL token->principal registry. Absent on every existing install, and its
#: absence is the pre-2026-09-06 behaviour exactly: one shared bearer, one
#: implicit owner. Present, it lets ONE daemon serve several callers and still
#: answer "who is this" -- which /workforce, per-tenant rooms and any
#: licensed-for-<agent> check need before they can mean anything.
#:
#: Shape (token is stored HASHED; the daemon never holds the plaintext):
#:   {"<sha256 hex of the bearer token>": {
#:       "principal":    "tenant:user",
#:       "plan":         "pro",
#:       "entitlements": ["atlas", "fleet"],
#:       "expires_at":   1788999999      # epoch seconds; 0/absent = no expiry
#:   }}
#: Overridable via AITHER_HARNESS_PRINCIPALS, like the host/port/token above.
#: Needed to exercise the registry without writing to a live ~/.aither, and it
#: lets an operator keep the file on a mounted secret volume instead of $HOME.
PRINCIPALS_PATH = Path(
    os.environ.get("AITHER_HARNESS_PRINCIPALS", "").strip()
    or (Path.home() / ".aither" / "harness_tokens.json")
)

#: Per-name in-flight slot for POST /wakes/{name}/run: name -> child pid. Held
#: until the CHILD exits (a reaper thread pops it), not until the request
#: returns, so a retry or a double-click cannot start a second run through this
#: window. Per daemon process only; awrise's own overlap policy stays the
#: cross-process authority.
_RUNNING: dict[str, int] = {}
_RUNNING_LOCK = threading.Lock()

#: Answer keys whose action is a RUN (as opposed to enable/disable), so the CARD
#: path can apply the same per-name in-flight refusal `/wakes/{name}/run` does.
#: Named here rather than derived from the argv, because the argv is built inside
#: card_recipes and this check has to happen before anything is built.
_WAKE_RUN_CHOICES = frozenset({"run_now", "run"})

#: What a card whose recipe THIS BUILD CANNOT CLASSIFY demands before it may be
#: raised or answered. Mirrors `card_recipes.UNKNOWN_RECIPE_ENTITLEMENT` and is
#: duplicated on purpose: the daemon must still deny when that module is absent
#: or broken, which is exactly when it cannot be imported to ask.
#: `test_the_unknown_recipe_entitlement_matches_the_registry` keeps the two equal.
UNKNOWN_RECIPE_ENTITLEMENT = "decisions:recipe:unknown"

#: Longest a /wakes/{name}/run caller may hold the request open. The handler
#: polls with asyncio.sleep (no worker thread is held), but an unbounded wait
#: still pins a connection; the child is never killed at the deadline.
WAKE_RUN_WAIT_MAX_S = 120.0
WAKE_RUN_WAIT_DEFAULT_S = 20.0

#: Browser origins allowed to call this daemon. AitherShell-in-the-browser is
#: served from these; anything else is refused.
DEFAULT_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://aitherium.com",
    "https://www.aitherium.com",
    "https://api.aitherium.com",
    "https://api.aitherium.com",
    "https://tunnel.aitherium.com",
    # The Aither Hub (gobbonet.aitherium.com + apex /gobbonet/): the browser
    # page on the user's machine reaches this daemon over 127.0.0.1:8362 —
    # the same local-first pattern as the GobboNet probe ladder. Tenant
    # portal mirrors (per-tenant subdomains) use AITHER_HARNESS_ALLOWED_ORIGINS; the
    # daemon cannot enumerate them.
    "https://gobbonet.aitherium.com",
    # Vite dev server for the hub frontend.
    "http://127.0.0.1:5173",
)


def allowed_origins() -> list[str]:
    raw = os.environ.get("AITHER_HARNESS_ALLOWED_ORIGINS", "")
    if not raw.strip():
        return list(DEFAULT_ORIGINS)
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if "*" in origins:
        raise RuntimeError(
            "AITHER_HARNESS_ALLOWED_ORIGINS='*' is refused: this daemon spawns "
            "coding agents with filesystem access and uses bearer credentials. "
            "List the origins explicitly."
        )
    return origins


def allowed_roots() -> list[Path]:
    raw = os.environ.get("AITHER_HARNESS_ALLOWED_ROOTS", "")
    return [Path(p.strip()).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]


def resolve_token(explicit: str = "") -> str:
    """Resolve the daemon bearer token, minting and persisting one if needed.

    Order: explicit > env > on-disk. A generated token is written with
    owner-only permissions so it is not world-readable on a shared box.
    """
    if explicit:
        return explicit.strip()
    env_token = os.environ.get("AITHER_HARNESS_TOKEN", "").strip()
    if env_token:
        return env_token
    if TOKEN_PATH.exists():
        existing = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    try:
        os.chmod(TOKEN_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        sys.stderr.write(f"[harness] could not restrict {TOKEN_PATH}: {exc}\n")
    return token


class Principal:
    """Who is calling, and what they are allowed to reach.

    The daemon authenticated with ONE shared bearer, so it could prove a caller
    knew the secret and nothing else. That is why daemon.py refuses to relay
    comet-deploy over HTTP ("the daemon cannot tell which tenant queued this
    work") and why requires_plan: sits in every addon manifest unread -- there
    was no subject to check a plan against.

    OWNER IS NOT A BYPASS THAT GREW BY ACCIDENT. With no registry the daemon is
    single-user by construction: whoever holds the bearer already owns the box,
    the sessions and the filesystem those sessions can write to. Modelling that
    caller as a principal with "*" keeps ONE authorization path instead of
    sprinkling `if no_registry: allow` through the routes -- the shape where an
    unrelated later edit silently turns a check off for everyone.
    """

    __slots__ = ("id", "plan", "entitlements", "paths")

    def __init__(self, id: str, plan: str = "free", entitlements: frozenset = frozenset(),
                 paths: tuple = ()):
        self.id = id
        self.plan = plan
        self.entitlements = entitlements
        #: PATH SCOPE. Empty = unrestricted (the owner). Non-empty = this
        #: principal may reach ONLY these path prefixes, checked on every request
        #: before the handler runs.
        self.paths = tuple(paths or ())

    def has(self, entitlement: str) -> bool:
        return "*" in self.entitlements or entitlement in self.entitlements

    def may_reach(self, path: str) -> bool:
        """Is ``path`` inside this principal's scope?

        FAIL CLOSED for a scoped principal: an unrecognised path is refused, so
        adding a route to this daemon never silently widens an existing token.
        That ordering matters -- the reverse-link token exists precisely so a
        remote caller CANNOT reach `/awrun/*`, `/rooms/*` or the fs routes, and a
        default-allow would hand it all of them the moment a new route lands.

        Matching is on whole path SEGMENTS (`/sessions` matches `/sessions` and
        `/sessions/x`, never `/sessions-admin`), and any `..` segment is refused
        outright rather than normalised -- normalising is where traversal bugs
        live.
        """
        if not self.paths:
            return True
        p = "/" + (path or "").strip("/")
        if ".." in p.split("/"):
            return False
        low = p.lower()
        for allowed in self.paths:
            a = "/" + str(allowed).strip("/").lower()
            if a == "/":
                continue
            if low == a or low.startswith(a + "/"):
                return True
        return False

    def __repr__(self) -> str:  # never render the token, only the subject
        return f"Principal(id={self.id!r}, plan={self.plan!r})"


#: The caller when no registry exists: the box's owner, everything allowed.
#: `paths=()` is the unrestricted marker -- the owner holds the box, the sessions
#: and the filesystem those sessions write to, so scoping them here would be
#: theatre. Every SCOPED principal is a registry entry with a non-empty `paths`.
OWNER_PRINCIPAL = Principal(id="owner", plan="owner", entitlements=frozenset({"*"}))

#: What a per-node reverse-link token may reach. This is the D1 contract: the
#: phone app needs the session list, one session's stream and input, the pending
#: decision cards, and the fleet status tile. It must NEVER get the daemon's ROOT
#: token, which can spawn a coding agent with filesystem access on this machine.
SCOPED_LINK_PATHS = ("/sessions", "/decisions", "/desk/fleet/status")


def load_principals(path: Path = None) -> dict:
    """Read the token->principal registry. Unreadable or malformed = empty.

    A registry that fails to parse must NOT be treated as "deny everyone": that
    turns one typo into a daemon nobody can reach, including the owner holding
    the real bearer. It degrades to the single-bearer behaviour instead, and says
    so on stderr, because a loud downgrade is recoverable and a silent lockout is
    a support call.
    """
    p = path or PRINCIPALS_PATH
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        sys.stderr.write(
            f"[harness] {p} is unreadable ({exc}); falling back to the single "
            f"shared bearer. Per-client identity is OFF until this parses.\n"
        )
        return {}


def mint_scoped_token(
    principal_id: str,
    paths: tuple = SCOPED_LINK_PATHS,
    *,
    ttl_days: int = 30,
    plan: str = "link",
    entitlements: tuple = (),
    path: Path = None,
) -> str:
    """Mint a PATH-SCOPED bearer for this daemon and persist its hash.

    Returns the plaintext token ONCE. The registry stores only sha256, so a
    reader of the file learns who exists, not how to authenticate as them.

    This exists so the reverse link never carries the daemon's ROOT token. That
    token can `POST /sessions` a coding agent with filesystem access on this
    machine; a phone that wants to read a session list must not be one stolen
    header away from that. The scope defaults to :data:`SCOPED_LINK_PATHS`.

    The registry is rewritten from a FRESH read every time, and expired entries
    are dropped on the way through, so the file cannot grow without bound and a
    concurrent mint cannot be clobbered by a stale in-memory copy.
    """
    target = path or PRINCIPALS_PATH
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    reg = load_principals(target)
    reg = {
        k: v for k, v in reg.items()
        if not (isinstance(v, dict) and v.get("expires_at")
                and float(v["expires_at"]) <= now)
    }
    reg[digest] = {
        "principal": principal_id,
        "plan": plan,
        "entitlements": list(entitlements),
        "paths": list(paths),
        "expires_at": now + ttl_days * 86400 if ttl_days else 0,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    try:
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        sys.stderr.write(f"[harness] could not restrict {target}: {exc}\n")
    return token


def resolve_principal(value: str, bearer: str, registry: dict = None) -> Optional[Principal]:
    """Map a presented bearer token to a Principal, or None if it is not valid.

    Registry entries are matched on sha256 of the presented token, so the file
    holds no usable secret: someone who reads it learns who exists, not how to
    authenticate as them.

    The owner bearer is checked SECOND but is never removed. Locking the operator
    out of their own daemon by adding a registry would make the feature something
    people avoid, and the owner already holds the box.
    """
    presented = (value or "").strip()
    if not presented:
        return None

    reg = load_principals() if registry is None else registry
    if reg:
        digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
        entry = reg.get(digest)
        if isinstance(entry, dict):
            expires = entry.get("expires_at") or 0
            if expires and float(expires) <= time.time():
                return None                      # expired: not "fall through to owner"
            return Principal(
                id=str(entry.get("principal") or "unknown"),
                plan=str(entry.get("plan") or "free"),
                entitlements=frozenset(entry.get("entitlements") or ()),
                paths=tuple(entry.get("paths") or ()),
            )

    # constant-time: a wrong token must not reveal how much of it was right
    if hmac.compare_digest(presented, bearer):
        return OWNER_PRINCIPAL
    return None


def validate_cwd(cwd: str) -> str:
    """Confirm ``cwd`` is inside an allowed root. Empty allowlist = host trusted."""
    if not cwd:
        return cwd
    roots = allowed_roots()
    if not roots:
        return cwd
    target = Path(cwd).expanduser().resolve()
    for root in roots:
        try:
            target.relative_to(root)
            return str(target)
        except ValueError:
            continue
    raise ManagerError(
        f"cwd {target} is outside the allowed roots for this daemon "
        f"({os.pathsep.join(str(r) for r in roots)})"
    )


try:  # pragma: no cover - import guard
    from pydantic import BaseModel, Field
except ImportError:  # The daemon needs fastapi+pydantic; the rest of the
    # package must stay importable without them so the CLI can still run
    # sessions in-process on a box with no web stack.
    BaseModel = None  # type: ignore[assignment]
    Field = None  # type: ignore[assignment]

try:  # pragma: no cover - import guard
    # MODULE level, and it has to be: this file uses `from __future__ import
    # annotations`, so every annotation is a STRING that FastAPI resolves against
    # the MODULE globals. `Request` imported only inside create_app() therefore
    # does not resolve -- FastAPI cannot tell it is the request object, treats it
    # as a query parameter, and every authed route answers 422 instead of 200.
    from fastapi import Request
except ImportError:
    Request = None  # type: ignore[assignment]


class DeviceFlowState:
    """In-memory tracker for device flow approvals (short-lived, cleaned up regularly)."""

    def __init__(self):
        self._state: dict[str, dict[str, Any]] = {}

    def get(self, device_code: str) -> Optional[dict[str, Any]]:
        """Get current state of a device code. Returns None if expired/unknown."""
        entry = self._state.get(device_code)
        if entry is None:
            return None
        if time.time() > entry.get("expires_at", 0):
            del self._state[device_code]
            return None
        return entry

    def set(self, device_code: str, status: str, expires_at: float) -> None:
        """Update state for a device code."""
        self._state[device_code] = {
            "status": status,
            "expires_at": expires_at,
            "updated_at": time.time(),
        }

    def cleanup(self) -> None:
        """Remove expired entries. Called periodically."""
        now = time.time()
        expired = [k for k, v in self._state.items() if now > v.get("expires_at", 0)]
        for k in expired:
            del self._state[k]


# Global device flow state (short-lived, renewed on each daemon restart).
_device_flow_state = DeviceFlowState()


if BaseModel is not None:

    class CreateSession(BaseModel):  # type: ignore[misc]
        """Request body for POST /sessions.

        Defined at MODULE level on purpose. This file uses
        ``from __future__ import annotations``, so every endpoint annotation is
        a string that FastAPI resolves against the endpoint's MODULE globals —
        a model defined inside ``create_app`` is invisible there, and FastAPI
        silently degrades it to a query parameter (a 422 that reads like a
        client bug rather than a wiring bug).
        """

        harness: str = "claude"
        cwd: str = ""
        model_profile: str = ""
        model: str = ""
        permission_mode: str = ""
        resume_session_id: str = ""
        system_prompt_append: str = ""
        add_dirs: list[str] = Field(default_factory=list)
        allowed_tools: list[str] = Field(default_factory=list)
        mcp_config: str = ""
        target: str = ""
        extra_args: list[str] = Field(default_factory=list)
        title: str = ""
        owner: str = ""
        #: Opt-in at spawn: may a PEER agent's steer be typed straight into this
        #: session's pty? Default off (``SessionConfig.allow_peer_input``); the owner
        #: still lands immediately either way.
        allow_peer_input: bool = False
        #: Relay-session fields. ``SessionConfig`` accepted ``agent`` and
        #: ``participants`` and the CLI sent them, but this model did not declare
        #: them, so ``model_dump()`` dropped both and EVERY ``--harness aither
        #: --agent atlas`` session ran as aither (measured 2026-09-21). A field that
        #: exists on the config must exist here -- check_door_relay.py DOOR001.
        agent: str = ""
        participants: list[str] = Field(default_factory=list)
        base_url: str = ""
        #: A skill or slash command from ``.claude/skills`` / ``.claude/commands``
        #: (cwd first, then ~), rendered into ``system_prompt_append``. Not a
        #: SessionConfig field: it is resolved here and never travels further.
        skill: str = ""
        skill_arguments: str = ""

    class SendInput(BaseModel):  # type: ignore[misc]
        text: str
        #: True = deliver as a COMPLETE turn (a pty adds the Enter). False = raw
        #: keystrokes, which is what a terminal attach forwards.
        submit: bool = False

    class ResizeInput(BaseModel):  # type: ignore[misc]
        rows: int = 30
        cols: int = 100

    class ProvisionSandbox(BaseModel):  # type: ignore[misc]
        workspace_slug: str
        repos: list[str] = Field(default_factory=list)

    class AnswerDecision(BaseModel):  # type: ignore[misc]
        """Body for POST /decisions/{id}/answer.

        Module level for the reason documented on ``CreateSession`` above: with
        ``from __future__ import annotations`` a model defined inside
        ``create_app`` is invisible to FastAPI's resolver, which silently
        downgrades it to a query parameter and answers every POST with a 422 that
        reads like a client bug.
        """

        choice: str
        note: str = ""
        via: str = "api"
        #: Identity CLAIM a chat bridge attaches, same shape and same
        #: no-registry no-op semantics as ``WakeCreate.origin`` /
        #: ``WakeUpdate.origin`` (see ``_wake_reauthorize_origin``). Added so
        #: ANSWERING a spawning recipe card (wakes-add / wakes-set-command)
        #: gets the identical re-check RAISING one already gets --
        #: previously this model carried no origin field at all, so the
        #: "propose, then confirm" split had origin-narrowing on the propose
        #: half only. A caller (or an agent tool holding the same bearer,
        #: e.g. the awrise toolpack's ``awrise_confirm``) could raise a card
        #: through a narrowed/scoped principal and then answer it with no
        #: origin claim whatsoever, and nothing here would have noticed
        #: (security finding 2026-09-19). Optional and origin-narrowing only
        #: NARROWS what the bearer already allows, so a caller holding
        #: ``wakes:create``/``*`` outright (the default, no-registry
        #: deployment) is unaffected either way -- this closes the
        #: asymmetry for a hardened deployment (a principals registry +
        #: ``channels.json``), it does not by itself change what the shared,
        #: unscoped daemon bearer can do.
        origin: Optional["WakeOrigin"] = None

    class CancelDecision(BaseModel):  # type: ignore[misc]
        note: str = ""

    class RaiseOption(BaseModel):  # type: ignore[misc]
        key: str
        label: str
        consequence: str = ""

    class RaiseDecision(BaseModel):  # type: ignore[misc]
        """Body for POST /decisions — raising a card from off-box.

        Module level for the same reason as ``AnswerDecision``: with
        ``from __future__ import annotations`` a model defined inside ``create_app``
        is invisible to FastAPI's resolver, which downgrades it to a query parameter
        and answers every POST with a 422 that reads like a client bug.

        No credential VALUE field exists here, and none may ever be added. A
        credential card names WHICH secret is wanted and WHY; the value goes
        owner -> masked field -> vault and is verified by read-back length only.
        """

        title: str
        kind: str = "decision"
        urgency: str = "normal"
        summary: str = ""
        facts: list[str] = Field(default_factory=list)
        options: list[RaiseOption] = Field(default_factory=list)
        default: str = ""
        secret_name: str = ""
        credential_format: str = "password"
        credential_scope: str = "platform"
        credential_description: str = ""
        # ---- dedupe / card recipes ------------------------------------------
        # A producer that raises on EVERY failing pass (awrise's report policy is
        # the first) needs "one failing streak = one card, ever". The store has
        # that guard and keys it on ``dedupe_key``; until these three fields
        # existed here, every card raised through this route carried
        # ``dedupe_key=""`` and the guard at store.create() was skipped outright.
        # So the property held on the awask CLI path and nowhere else — and this
        # route is the one Discord, awdesk and AitherDesktop all reach through.
        #
        # ``card_recipe`` + ``recipe_vars`` are the other half: they name the
        # TEMPLATE, and the store's transition turns the owner's answer into the
        # action the recipe promised. A raise that names a recipe is built BY the
        # recipe (title, options, consequences, deadline, dedupe key and all), so
        # an off-box caller cannot invent an option key that maps to no action.
        dedupe_key: str = ""
        card_recipe: str = ""
        recipe_vars: dict[str, str] = Field(default_factory=dict)
        # provenance — set by the caller that authenticated the raiser
        raised_by: str = ""
        agent: str = ""
        session_id: str = ""
        cwd: str = ""
        via: str = "api"

    class SteerDecision(BaseModel):  # type: ignore[misc]
        text: str
        via: str = "api"

    class ChatReply(BaseModel):  # type: ignore[misc]
        """Body for POST /decisions/chat-reply — a raw chat message from a DM bridge.

        The caller (the fleet's Discord service, a Telegram bot, ...) forwards the
        owner's message VERBATIM plus the sender identity it observed; this daemon
        re-authorizes against ~/.aither/decisions/channels.json via the tested
        fail-closed bridge, so a compromised or miswired forwarder cannot answer
        cards for a sender the owner never bound. ``is_direct_message`` has no
        default for the same reason ``DecisionChannelBridge.on_message`` gives it
        none: a forwarder that cannot prove DM-ness must say False and be denied.
        """

        platform: str
        user_id: str
        text: str
        is_direct_message: bool
        # The card the FORWARDER last showed the owner, so a bare "2" from the
        # phone resolves to the card they are looking at, not a guess.
        last_sent_card: str = ""

    class WakeOrigin(BaseModel):  # type: ignore[misc]
        """Identity CLAIM attached to a /wakes mutation by a chat bridge.

        The daemon re-checks it against the owner-bound ``channels.json`` through
        the same fail-closed ``authorize`` that ``/decisions/chat-reply`` uses. It
        can only NARROW what the bearer allows, never widen it. ``is_direct_message``
        has no default for the reason ``ChatReply`` gives it none.
        """

        model_config = {"extra": "forbid"}

        platform: str
        user_id: str
        is_direct_message: bool

    class WakeMutate(BaseModel):  # type: ignore[misc]
        """Optional body for POST /wakes/{name}/{enable|disable|run}.

        Module level for the reason documented on ``CreateSession``. ``extra`` is
        forbidden on purpose: scope (home, binary, argv, cwd, env) comes from the
        daemon's own process environment and never from the payload.
        """

        model_config = {"extra": "forbid"}

        note: str = ""
        origin: Optional[WakeOrigin] = None

    class WakeCreate(BaseModel):  # type: ignore[misc]
        """Body for POST /wakes — register a NEW wake.

        Module level for the reason ``CreateSession`` documents. ``command``,
        ``every`` and (when given) ``cwd`` are each payload-checked — non-empty,
        bounded, no control bytes — before any argv is built; the CLI's own
        grammar for ``every``/``timeout`` still runs inside the spawned process,
        so a payload-safe but malformed interval is a 502 (the CLI's exit code),
        never a silent daemon accept. ``extra`` is forbidden for the same reason
        ``WakeMutate`` forbids it: scope (home, binary, argv) never comes from
        the payload.
        """

        model_config = {"extra": "forbid"}

        name: str
        command: str
        every: str
        timeout: Optional[int] = None
        cwd: Optional[str] = None
        note: str = ""
        origin: Optional[WakeOrigin] = None

    class WakeUpdate(BaseModel):  # type: ignore[misc]
        """Body for PATCH /wakes/{name} — CHANGE an existing wake.

        At least one of ``command``/``every``/``timeout``/``cwd`` is required
        (400 "no fields to update" otherwise); each supplied field is validated
        exactly like ``WakeCreate`` validates it.
        """

        model_config = {"extra": "forbid"}

        command: Optional[str] = None
        every: Optional[str] = None
        timeout: Optional[int] = None
        cwd: Optional[str] = None
        note: str = ""
        origin: Optional[WakeOrigin] = None

    class CreateRoom(BaseModel):  # type: ignore[misc]
        id: str = DEFAULT_ROOM
        title: str = ""

    class PublishEvent(BaseModel):  # type: ignore[misc]
        """One AitherEvent from any producer. Module level for the reason above.

        Everything except ``type`` and ``actor`` is optional on the wire: ``seq`` is
        stamped by the room, and ``pillar`` is DERIVED from the event type when
        absent. That is what makes adding a producer a one-line change rather than an
        integration — it emits the event names it already has and lands in the right
        lane.

        ``to``/``hops`` (U15) are the addressing fields ``Room._normalise`` already
        validates via ``_normalise_addressing`` — but a pydantic model only forwards
        the keys it DECLARES, so before these two existed here every HTTP-posted
        addressed event was silently stripped to an unaddressed one before
        ``room.publish()`` ever saw ``to``/``hops``, and the room-side validation and
        the steer dispatcher (see ``steer_dispatch.py``) never ran for this producer.
        Defaults match ``_normalise_addressing``'s own "absent" case (``[]``/``0``)
        so an unaddressed POST is byte-identical to today's behaviour.
        """

        type: str
        actor: dict[str, Any]
        room: str = DEFAULT_ROOM
        pillar: Optional[str] = None
        tier: str = "host"
        session: str = ""
        stage: str = ""
        payload: dict[str, Any] = Field(default_factory=dict)
        correlation_id: str = ""
        causation_id: str = ""
        ts: float = 0.0
        v: int = 0
        id: str = ""
        to: list[str] = Field(default_factory=list)
        hops: int = 0

    class LinkDeviceCodeResponse(BaseModel):  # type: ignore[misc]
        """Response from POST /auth/link — initiates device flow.

        The daemon does NOT mint credentials; it only relays portal's device-flow
        endpoints and tracks approval status. The browser must navigate to
        verification_uri for the user to enter the code and approve.
        """

        user_code: str
        device_code: str
        verification_uri: str
        expires_in: int

    class LinkStatusResponse(BaseModel):  # type: ignore[misc]
        """Response from GET /auth/link/status/{device_code}."""

        status: str  # "pending" | "approved" | "denied" | "expired"
        expires_at: float  # Unix timestamp

    class SubmitRun(BaseModel):  # type: ignore[misc]
        """Body for POST /awrun/submit — see adk.builtin_tools.queue_submit for
        the field meaning per kind ("agent" needs task+agent, "ci" needs
        workflow, "comet-deploy" needs service_name and is trust-plane gated
        inside awrun itself, not here). Module level for the same
        ``from __future__ import annotations`` reason as every model above.
        """

        kind: str
        priority: int = 0
        paths: list[str] = Field(default_factory=list)
        task: str = ""
        agent: str = ""
        adk_args: list[str] = Field(default_factory=list)
        workflow: str = ""
        ref: str = ""
        inputs: dict[str, Any] = Field(default_factory=dict)
        service_name: str = ""
        target: str = ""
        spec: dict[str, Any] = Field(default_factory=dict)

    class BumpRun(BaseModel):  # type: ignore[misc]
        priority: int


def create_app(manager: Optional[SessionManager] = None, token: str = ""):
    """Build the FastAPI app. Raises if no token can be resolved (fail-closed)."""
    if BaseModel is None:
        raise RuntimeError("fastapi and pydantic are required: pip install fastapi uvicorn")
    from fastapi import Depends, FastAPI, Header, HTTPException, Query
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse

    mgr = manager or default_manager()
    bearer = resolve_token(token)
    if not bearer:
        raise RuntimeError("harness daemon refuses to start without a bearer token")
    origins = allowed_origins()

    app = FastAPI(title="AitherShell Harness Daemon", version="1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    def auth(request: Request, authorization: str = Header(default="")) -> Principal:
        """Authenticate, and RETURN who it was.

        Returns a Principal rather than None so a route can ask "who is this"
        without a second lookup. Existing routes use
        ``dependencies=[Depends(auth)]``, which discards the return value, so
        every one of them keeps its current behaviour untouched; a route that
        wants the subject declares ``principal: Principal = Depends(auth)``.

        Status codes are deliberately unchanged: 401 for missing/malformed
        credentials, 403 for a token that is well-formed but not accepted. The
        browser client refreshes on 401 (see the hub token comment below), so
        turning a rejected token into a 401 would put it into a refresh loop
        against a token that will never work.
        """
        if not authorization:
            raise HTTPException(status_code=401, detail="missing bearer token")
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not value:
            raise HTTPException(status_code=401, detail="malformed Authorization header")
        principal = resolve_principal(value, bearer)
        if principal is None:
            raise HTTPException(status_code=403, detail="invalid token")
        # PATH SCOPE (D1). A scoped token -- the per-node one `adk rc` mints for
        # the reverse link -- is refused on anything outside its own list, BEFORE
        # the handler runs. 403, not 404: a caller holding a valid credential for
        # a narrower surface should learn it was refused, not that the route is
        # missing, or it will retry forever against a decision that never changes.
        if not principal.may_reach(request.url.path):
            raise HTTPException(
                status_code=403,
                detail=f"principal {principal.id!r} is not scoped to "
                       f"{request.url.path}",
            )
        return principal

    def require_entitlement(name: str):
        """Dependency factory gating a route on an entitlement.

        Unused by default: with no registry every caller is OWNER_PRINCIPAL and
        holds "*", so adding this to a route changes nothing until an operator
        actually writes harness_tokens.json. That ordering is the point -- the
        gate ships and proves itself before anyone's access depends on it.
        """
        def check(principal: Principal = Depends(auth)) -> Principal:
            if not principal.has(name):
                raise HTTPException(
                    status_code=403,
                    detail=f"principal {principal.id!r} lacks entitlement {name!r}",
                )
            return principal
        return check

    class CreateSession(BaseModel):
        harness: str = "claude"
        cwd: str = ""
        model_profile: str = ""
        model: str = ""
        permission_mode: str = ""
        resume_session_id: str = ""
        system_prompt_append: str = ""
        add_dirs: list[str] = Field(default_factory=list)
        allowed_tools: list[str] = Field(default_factory=list)
        mcp_config: str = ""
        target: str = ""
        extra_args: list[str] = Field(default_factory=list)
        title: str = ""
        owner: str = ""
        allow_peer_input: bool = False

    class SendInput(BaseModel):
        text: str
        #: True = deliver as a COMPLETE turn (a pty adds the Enter). False = raw
        #: keystrokes, which is what a terminal attach forwards.
        submit: bool = False

    # ── unauthenticated liveness ────────────────────────────────────────────

    @app.get("/health")
    def health() -> dict[str, Any]:
        roots = allowed_roots()
        return {
            "ok": True,
            "service": "aithershell-harness",
            "sessions": len(mgr.list_sessions()),
            "harnesses_installed": mgr.available_harness_ids(),
            "cors_origins": origins,
            # Stated explicitly so "this host is trusted" is a visible posture,
            # never an unnoticed default on a tunnel-exposed daemon.
            "cwd_restricted": bool(roots),
            "allowed_roots": [str(r) for r in roots],
            # The spool tailer is the only path Claude Code tabs reach the room by.
            # If it dies the room simply goes quiet, which reads as "no agents are
            # working" — so its liveness is reported in the CHEAPEST probe there is,
            # not left to be discovered by noticing an absence.
            "spool": default_tailer().stats(),
            "transcripts": default_bridge().stats(),
            "well": default_well().stats(),
        }

    # ── the HUB contract (Aither Hub — gobbonet) ───────────────────────────
    # The hub is a browser page on the user's machine. It cannot hold
    # ~/.aither/harness_token — that file stays machine-only — so it mints a
    # short-lived HUB token through a route that is public like /health but
    # triple-gated: the Origin must be an allowed origin (the CORS plane
    # already refuses random sites), the Host must be loopback (DNS-rebinding
    # guard: a random website's fetch carries the browser's cookies but the
    # Host header is ITS host, which fails here), and the token expires in
    # 15 minutes. The browser holds it in sessionStorage and refreshes on 401.
    import secrets as _secrets
    import time as _time
    hub_tokens: dict[str, float] = {}  # token -> expires_at

    def hub_auth(authorization: str = Header(default="")) -> None:
        if not authorization:
            raise HTTPException(status_code=401, detail="missing bearer token")
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not value:
            raise HTTPException(status_code=401, detail="malformed Authorization header")
        expires = hub_tokens.get(value.strip())
        if expires is None or _time.time() > expires:
            raise HTTPException(status_code=401, detail="hub token missing or expired")
        now = _time.time()
        for t in [t for t, e in hub_tokens.items() if now > e]:
            hub_tokens.pop(t, None)

    @app.post("/hub/token")
    def hub_token(request: Request) -> dict[str, Any]:
        origin = request.headers.get("origin", "")
        host = request.headers.get("host", "")
        if not origin or origin not in origins:
            raise HTTPException(status_code=403, detail="origin not allowed")
        hostname = host.split(":")[0].strip("[]").lower()
        if hostname not in ("127.0.0.1", "localhost", "::1"):
            raise HTTPException(status_code=403, detail="loopback host required")
        tok = _secrets.token_urlsafe(32)
        hub_tokens[tok] = _time.time() + 15 * 60
        return {
            "token": tok,
            "expires_at": hub_tokens[tok],
            "scope": ["sessions:read", "sessions:write", "decisions:read",
                      "decisions:write", "rooms:read", "rooms:write"],
        }

    @app.get("/hub/manifest", dependencies=[Depends(hub_auth)])
    def hub_manifest() -> dict[str, Any]:
        # The surfaces this daemon serves the hub. Declared here so the hub
        # auto-registers them while the daemon is present and unregisters on
        # node loss (honest labelling — never claim online when offline).
        return {
            "panels": [
                {"id": "sessions", "title": "Sessions", "icon": "🖥️",
                 "auth": "local", "statusSourceId": "daemon"},
                {"id": "decisions", "title": "Decisions", "icon": "🗳️",
                 "auth": "local", "statusSourceId": "decisions"},
            ],
            "statusSources": [
                {"id": "daemon", "label": "harness daemon",
                 "degrade": "no harness daemon on this machine"},
                {"id": "decisions", "label": "decision cards",
                 "degrade": "daemon unreachable - cards cannot load"},
            ],
            "rooms": [],
        }

    # ── discovery ───────────────────────────────────────────────────────────

    @app.get("/harnesses")
    def harnesses(
        versions: bool = Query(default=False),
        principal: Principal = Depends(auth),
    ) -> dict[str, Any]:
        """List harnesses this CALLER may drive.

        Filtered, not annotated: a harness the caller cannot start should not
        appear in the picker at all, because an entry that 403s on click reads
        as a broken product rather than an unlicensed one. ``install_hint``
        already covers the other kind of absence -- "you do not have it" -- and
        the two are different problems with different fixes.

        With no principals registry every caller is OWNER_PRINCIPAL holding "*",
        and every shipped harness declares no entitlement, so this returns the
        same list it always did.
        """
        rows = mgr.harnesses(with_version=versions)
        return {
            "harnesses": [
                r for r in rows
                if not r.get("requires_entitlement")
                or principal.has(r["requires_entitlement"])
            ]
        }

    @app.get("/profiles", dependencies=[Depends(auth)])
    def profiles() -> dict[str, Any]:
        try:
            raw = list_profiles()
        except ProfileError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "profiles": [
                {
                    "id": name,
                    "description": p.get("description", ""),
                    "transport": p.get("transport", ""),
                    "context_window": p.get("context_window"),
                    "model": p.get("model", ""),
                }
                for name, p in raw.items()
            ]
        }

    # ── sessions ────────────────────────────────────────────────────────────

    @app.post("/sessions")
    def create_session(
        body: CreateSession,
        principal: Principal = Depends(auth),
    ) -> dict[str, Any]:
        # Enforce here as well as in the listing. Filtering /harnesses shapes the
        # PICKER; it is not access control, because the harness id arrives in this
        # body and a caller can name one the list never offered. A gate that only
        # runs where the UI happens to look is decoration.
        try:
            spec = get_harness(body.harness)
        except Exception:
            spec = None
        needed = getattr(spec, "requires_entitlement", "") if spec else ""
        if needed and not principal.has(needed):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"principal {principal.id!r} is not licensed for harness "
                    f"{body.harness!r} (requires entitlement {needed!r})"
                ),
            )
        # A relay session that names an agent the roster does not know used to
        # start anyway and answer as aither. Refuse with the roster instead: a
        # wrong name is a typo the caller can fix, a silent fallback is not.
        if body.harness in ("aither", "awdk", "group"):
            from adk.harnesses.agents import AGENT_ROSTER

            known = [a["id"] for a in AGENT_ROSTER]
            named = ([body.agent] if body.agent else []) + list(body.participants)
            unknown = [a for a in named if a not in known]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown agent(s) {unknown}; roster: {', '.join(known)}",
                )
        fields = body.model_dump(exclude={"skill", "skill_arguments"})
        if body.harness == "awdk" and body.agent:
            # adk serve has no identity store, so the roster's persona line is
            # the only way a named agent answers as itself on the local loop.
            # Measured 2026-09-21: `--agent atlas` on this harness answered as
            # "your helpful, warm test companion". Genesis loads the identity
            # itself, so the `aither` harness gets nothing here.
            entry = next((a for a in AGENT_ROSTER if a["id"] == body.agent), None)
            if entry and entry.get("persona"):
                persona = f"You are {entry['label']} ({entry['role']}). {entry['persona']}"
                fields["system_prompt_append"] = (
                    persona + "\n\n" + (fields.get("system_prompt_append") or "").strip()
                ).strip()
        if body.skill:
            from adk.harnesses import skills_local

            try:
                rendered = skills_local.resolve(
                    body.skill, body.skill_arguments, cwd=body.cwd or os.getcwd(),
                )
            except KeyError as exc:
                raise HTTPException(status_code=400, detail=str(exc.args[0])) from exc
            fields["system_prompt_append"] = (
                (fields.get("system_prompt_append") or "").rstrip() + "\n\n" + rendered
            ).strip()
        try:
            cwd = validate_cwd(body.cwd)
            config = SessionConfig(**{**fields, "cwd": cwd})
            session = mgr.create(config)
        except ManagerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return session.info()

    @app.get("/sessions", dependencies=[Depends(auth)])
    def list_sessions(owner: str = Query(default="")) -> dict[str, Any]:
        return {"sessions": mgr.list_sessions(owner=owner)}

    @app.get("/sessions/unified", dependencies=[Depends(auth)])
    def unified_sessions() -> dict[str, Any]:
        """List all sessions (daemon-owned + discovered interactive tabs).

        Returns a merged directory that includes:
        - Daemon sessions (full steering capability)
        - Discovered interactive Claude Code tabs (turn-boundary steering)

        Each entry includes:
        - id, title, cwd, harness, origin
        - status (idle/working/waiting-input/waiting-permission/exited)
        - last_activity_at (Unix timestamp)
        - last_activity_summary (one line of context)
        - transcript_path (for reading transcript)
        - steer_capability (full/turn-boundary/none)
        """
        from adk.harnesses.session_directory import default_directory

        directory = default_directory()
        daemon_sessions = mgr.list_sessions()
        unified = directory.list_sessions_sync(daemon_sessions)

        return {
            "sessions": [
                {
                    "id": s.id,
                    "title": s.title,
                    "cwd": s.cwd,
                    "harness": s.harness,
                    "harness_label": s.harness_label,
                    "origin": s.origin,
                    "status": s.status,
                    "last_activity_at": s.last_activity_at,
                    "last_activity_summary": s.last_activity_summary,
                    "transcript_path": s.transcript_path,
                    "pid": s.pid,
                    "steer_capability": s.steer_capability,
                    # The id the PROGRAM carries (claude-tty's --session-id). Every other
                    # surface knows a tab by it; without it a client cannot resolve an
                    # awsh-opened tab by the id its own transcript is named after.
                    "harness_session_id": str(
                        (s.extras or {}).get("harness_session_id") or ""
                    ),
                    # Which tabs opted in to PEER input on their pty at spawn. Only a
                    # daemon-owned row can be True; a discovered tab has no config.
                    "allow_peer_input": bool(
                        (s.extras or {}).get("allow_peer_input") or False
                    ),
                }
                for s in unified
            ]
        }

    @app.get("/sessions/{session_id}", dependencies=[Depends(auth)])
    def get_session(session_id: str) -> dict[str, Any]:
        try:
            return mgr.get_session(session_id).info()
        except ManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def _peer_input_text(session, session_id: str, principal: Principal, text: str) -> str:
        """The text a caller may put on this pty, or a 403.

        The steer dispatcher's tier-1 rule -- owner-plan AND (a human actor OR the
        target's opt-in) -- would be theatre if the direct door stayed open to any
        bearer that can reach ``/sessions``: a scoped ``plan="agent"`` token could
        type into any managed pty by POSTing here instead of publishing an event.
        So a non-owner principal is refused unless the target opted in at spawn, and
        its text is framed with the same provenance the dispatcher prepends, from
        the daemon's own principal id -- never a name the caller typed.
        """
        if principal.plan == "owner":
            return text
        if not getattr(session.config, "allow_peer_input", False):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"session {session_id!r} did not opt in to peer input "
                    f"(allow_peer_input at spawn); principal {principal.id!r} may "
                    "address it through its steering mailbox instead"
                ),
            )
        return frame_peer_text(text, principal.id)

    @app.post("/sessions/{session_id}/input")
    def send_input(
        session_id: str, body: SendInput, principal: Principal = Depends(auth),
    ) -> dict[str, Any]:
        try:
            session = mgr.get_session(session_id)
        except ManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        text = _peer_input_text(session, session_id, principal, body.text)
        accepted = session.submit(text) if body.submit else session.send(text)
        if not accepted:
            raise HTTPException(
                status_code=409, detail=f"session {session.state} cannot accept input"
            )
        return {"ok": True, "turn": session.turn, "seq": session.last_seq}

    @app.post("/sessions/{session_id}/submit")
    def submit_input(
        session_id: str, body: SendInput, principal: Principal = Depends(auth),
    ) -> dict[str, Any]:
        """``/input`` with the Enter key: one complete turn, whatever the transport."""
        try:
            session = mgr.get_session(session_id)
        except ManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        text = _peer_input_text(session, session_id, principal, body.text)
        if not session.submit(text):
            raise HTTPException(
                status_code=409, detail=f"session {session.state} cannot accept input"
            )
        return {"ok": True, "turn": session.turn, "seq": session.last_seq}

    def _browse_roots() -> Any:
        from adk.harnesses.fs import browse_roots

        return browse_roots([s.get("cwd", "") for s in mgr.list_sessions()])

    @app.get("/fs/list", dependencies=[Depends(auth)])
    def fs_list(path: str = Query(default="")) -> dict[str, Any]:
        from adk.harnesses.fs import FsDeniedError, list_dir

        try:
            return list_dir(path, _browse_roots())
        except FsDeniedError as exc:
            # 403 with the reason, never an empty listing. An empty listing
            # reads as "that folder is empty" and hides a containment refusal.
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/fs/read", dependencies=[Depends(auth)])
    def fs_read(path: str = Query(...)) -> dict[str, Any]:
        from adk.harnesses.fs import FsDeniedError, read_file

        try:
            return read_file(path, _browse_roots())
        except FsDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/workforce", dependencies=[Depends(auth)])
    def workforce() -> dict[str, Any]:
        """Aitherium Workforce roster, proxied from Genesis.

        Reported with an explicit ``available`` flag rather than an empty list:
        ``workforce.py`` itself notes the runtime is "frequently not" running,
        and an empty roster would read as "you have no agents" instead of
        "the Workforce service did not answer".
        """
        from adk.harnesses.agents import fetch_workforce

        roster, reason = fetch_workforce()
        return {"agents": roster, "available": bool(roster), "reason": reason}

    @app.post("/sandboxes", dependencies=[Depends(auth)])
    def provision_sandbox(body: ProvisionSandbox) -> dict[str, Any]:
        """Provision a dev workspace container and report how to get inside it.

        Returns the descriptor plus the ``container`` a ``sandbox`` session
        should target, so the caller does not have to know the naming
        convention. An empty ``container`` is reported honestly rather than
        guessed — a wrong name fails later as "no such container", which reads
        as a broken terminal instead of an uninterpretable provisioning result.
        """
        from adk.harnesses import sandbox as sandbox_mod

        try:
            descriptor = sandbox_mod.provision(body.workspace_slug, body.repos or None)
        except sandbox_mod.SandboxError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        container, reason = sandbox_mod.container_name(descriptor)
        # `reason` is returned, not swallowed: a provisioned workspace we cannot
        # exec into must say WHY, or the UI shows a sandbox that silently
        # refuses to open a terminal.
        return {"workspace": descriptor, "container": container, "container_reason": reason}

    @app.get("/sandboxes", dependencies=[Depends(auth)])
    def list_sandboxes() -> dict[str, Any]:
        from adk.harnesses import sandbox as sandbox_mod

        try:
            return {"sandboxes": sandbox_mod.mine()}
        except sandbox_mod.SandboxError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.delete("/sandboxes/{workspace_id}", dependencies=[Depends(auth)])
    def teardown_sandbox(workspace_id: str) -> dict[str, Any]:
        from adk.harnesses import sandbox as sandbox_mod

        try:
            return sandbox_mod.teardown(workspace_id)
        except sandbox_mod.SandboxError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/agents", dependencies=[Depends(auth)])
    def agents() -> dict[str, Any]:
        from adk.harnesses.agents import AGENT_ROSTER

        return {"agents": AGENT_ROSTER}

    # ── skills: what a session can be handed from disk ─────────────────────
    # Read from ``<cwd>/.claude`` then ``~/.claude`` on the HOST, which is where
    # the daemon runs -- no fleet, no mount. This is how a skill leaves Claude
    # Code: it is handed to the session as ``system_prompt_append``.
    @app.get("/skills", dependencies=[Depends(auth)])
    def skills(cwd: str = Query(default="")) -> dict[str, Any]:
        from adk.harnesses import skills_local

        base = cwd or os.getcwd()
        found = skills_local.discover(cwd=base)
        return {
            "skills": [s.describe() for s in found.values()],
            "roots": [str(r) for r in skills_local.default_roots(base)],
            "collisions": {
                name: [str(p) for p in paths]
                for name, paths in skills_local.collisions(cwd=base).items()
            },
        }

    @app.get("/skills/{name}", dependencies=[Depends(auth)])
    def skill_text(name: str, cwd: str = Query(default=""),
                   arguments: str = Query(default="")) -> dict[str, Any]:
        from adk.harnesses import skills_local

        base = cwd or os.getcwd()
        found = skills_local.discover(cwd=base)
        skill = found.get(name)
        if skill is None:
            raise HTTPException(
                status_code=404,
                detail=f"unknown skill {name!r}; {len(found)} known",
            )
        return {**skill.describe(), "text": skills_local.render(skill, arguments)}

    # ── awrun job queue (AWS-runner / CI / agent-run priority control) ─────────
    # Reuses adk.builtin_tools' existing queue_submit/queue_list/queue_status/
    # queue_bump/queue_cancel wrappers rather than talking to awrun.store
    # directly — those already carry the awrun[queue]-missing degradation
    # (returns {"error": "awrun not available", ...} instead of raising) and,
    # for kind="comet-deploy", the trust-plane gate (awrun.authz: resolves
    # AITHER_SESSION_BEARER, checks a permission, writes an awdit record).
    # Reimplementing against RunStore here would either drop that gate or
    # duplicate it out of step with the ADK tool version. Errors come back as
    # {"error": ...} in a 200 body — the same convention those wrappers
    # already use — rather than an HTTPException, since "awrun not installed"
    # and "no such run id" are domain answers, not transport failures.

    @app.post("/awrun/submit", dependencies=[Depends(auth)])
    def awrun_submit(body: SubmitRun) -> dict[str, Any]:
        # kind="comet-deploy" is refused HERE, before reaching queue_submit,
        # not merely documented as unsupported. queue_submit's trust-plane
        # gate resolves AITHER_SESSION_BEARER from os.environ — the DAEMON
        # PROCESS's own environment, since it was written for the ADK-tool
        # call path where the caller and the process are the same identity.
        # This HTTP route breaks that assumption: the daemon authenticates
        # ONE shared bearer token per `Depends(auth)`, not a distinct
        # session per caller, so there is no per-request identity here to
        # correctly forward into AITHER_SESSION_BEARER. Passing kind=
        # comet-deploy through unchanged would silently authorize as
        # whatever session (if any) happens to be set in the daemon
        # process's own environment — an identity NOT derived from the
        # actual caller of this route — the payload-derived-identity trap —
        # for the one kind that spends real money. Fail closed until the
        # daemon's auth model carries per-caller identity to forward.
        if body.kind == "comet-deploy":
            return {
                "error": "comet-deploy is not available through the harness daemon's "
                         "/awrun/submit route — its trust-plane gate authorizes against "
                         "the daemon PROCESS's own AITHER_SESSION_BEARER, not the caller "
                         "of this HTTP request, and the daemon has no per-caller session "
                         "identity to forward correctly. Use `awrun submit --kind "
                         "comet-deploy` (or the ADK tool) directly on the machine holding "
                         "the session, where the caller and the process are the same.",
            }
        from adk.builtin_tools import queue_submit

        return json.loads(queue_submit(
            body.kind, priority=body.priority, paths=body.paths or None,
            task=body.task, agent=body.agent, adk_args=body.adk_args or None,
            workflow=body.workflow, ref=body.ref, inputs=body.inputs or None,
            service_name=body.service_name, target=body.target, spec=body.spec or None,
        ))

    @app.get("/awrun/queue", dependencies=[Depends(auth)])
    def awrun_queue(kind: str = Query(default=""),
                     include_closed: bool = Query(default=False)) -> dict[str, Any]:
        from adk.builtin_tools import queue_list

        return {"runs": json.loads(queue_list(kind=kind, include_closed=include_closed))}

    @app.get("/awrun/status/{run_id}", dependencies=[Depends(auth)])
    def awrun_status(run_id: str) -> dict[str, Any]:
        from adk.builtin_tools import queue_status

        return json.loads(queue_status(run_id))

    @app.post("/awrun/bump/{run_id}", dependencies=[Depends(auth)])
    def awrun_bump(run_id: str, body: BumpRun) -> dict[str, Any]:
        from adk.builtin_tools import queue_bump

        return json.loads(queue_bump(run_id, body.priority))

    @app.post("/awrun/cancel/{run_id}", dependencies=[Depends(auth)])
    def awrun_cancel(run_id: str) -> dict[str, Any]:
        from adk.builtin_tools import queue_cancel

        return json.loads(queue_cancel(run_id))

    # ── decision cards ────────────────────────────────────────────────────────
    # The card store is on local disk, but the DAEMON is what makes it reachable
    # from anywhere: aitherium.com, AitherConnect, a phone through the tunnel and
    # the CLI all read this one endpoint, so a card raised by a background agent
    # is answerable from whichever surface the owner happens to be looking at.
    #
    # Route order matters — FastAPI matches in registration order, so the static
    # `/decisions/count` MUST precede `/decisions/{card_id}` or the parameterised
    # handler swallows it and returns "no such card: count".

    # ── images ──────────────────────────────────────────────────────────────
    # An agent that can write and run code but cannot draw a picture is missing
    # a sense. These route to whatever image server is ALREADY on loopback and
    # start nothing; see adk/images.py for why the probe asks the generation
    # route rather than /health.
    #
    # The path is the OpenAI shape on purpose: it is what every client already
    # speaks, including GobboNet's awdk lane, so the capability arrives without
    # anyone writing a bespoke client. Before this route existed that lane
    # probed /v1/images/generations, got 404, and correctly reported the daemon
    # as "running, but no image route" -- which is exactly what it was.

    @app.get("/v1/images/backends", dependencies=[Depends(auth)])
    async def image_backends() -> dict[str, Any]:
        from adk import images as _img

        lanes = await _img.discover()
        return {
            "backends": [ln.as_dict() for ln in lanes],
            "usable": [ln.id for ln in lanes if ln.up],
        }

    @app.post("/v1/images/generations", dependencies=[Depends(auth)])
    async def image_generate(body: dict[str, Any]) -> dict[str, Any]:
        from fastapi import HTTPException

        from adk import images as _img

        size = str(body.get("size") or "768x768")
        try:
            w_s, h_s = size.lower().split("x", 1)
            width, height = int(w_s), int(h_s)
        except (ValueError, AttributeError):
            raise HTTPException(400, f"size must look like 768x768, got {size!r}")

        req = _img.ImageRequest(
            prompt=str(body.get("prompt") or ""),
            negative=str(body.get("negative_prompt") or ""),
            width=width, height=height,
            steps=int(body.get("steps") or 20),
            cfg=float(body.get("cfg") or 6.0),
            seed=body.get("seed"),
            model=str(body.get("model") or ""),
            backend=str(body.get("backend") or ""),
        )
        try:
            out = await _img.generate(req)
        except _img.ImageError as e:
            # 503, not 500: every ImageError here means "no local backend can
            # do this right now", which is a service-availability answer and
            # the message is written to be shown to a person. A 500 would read
            # as a bug in the daemon and send them to the wrong logs.
            raise HTTPException(503, str(e))

        return {
            "created": 0,
            "data": [{"b64_json": b} for b in out["images_b64"]],
            "backend": out["backend"],
            "model": out["model"],
        }

    # ── images ──────────────────────────────────────────────────────────────
    # An agent that can write and run code but cannot draw a picture is missing
    # a sense. These route to whatever image server is ALREADY on loopback and
    # start nothing; see adk/images.py for why the probe asks the generation
    # route rather than /health.
    #
    # The path is the OpenAI shape on purpose: it is what every client already
    # speaks, including GobboNet's awdk lane, so the capability arrives without
    # anyone writing a bespoke client. Before this route existed that lane
    # probed /v1/images/generations, got 404, and correctly reported the daemon
    # as "running, but no image route" -- which is exactly what it was.

    @app.get("/v1/images/backends", dependencies=[Depends(auth)])
    async def image_backends() -> dict[str, Any]:
        from adk import images as _img

        lanes = await _img.discover()
        return {
            "backends": [ln.as_dict() for ln in lanes],
            "usable": [ln.id for ln in lanes if ln.up],
        }

    @app.post("/v1/images/generations", dependencies=[Depends(auth)])
    async def image_generate(body: dict[str, Any]) -> dict[str, Any]:
        from fastapi import HTTPException

        from adk import images as _img

        size = str(body.get("size") or "768x768")
        try:
            w_s, h_s = size.lower().split("x", 1)
            width, height = int(w_s), int(h_s)
        except (ValueError, AttributeError):
            raise HTTPException(400, f"size must look like 768x768, got {size!r}")

        req = _img.ImageRequest(
            prompt=str(body.get("prompt") or ""),
            negative=str(body.get("negative_prompt") or ""),
            width=width, height=height,
            steps=int(body.get("steps") or 20),
            cfg=float(body.get("cfg") or 6.0),
            seed=body.get("seed"),
            model=str(body.get("model") or ""),
            backend=str(body.get("backend") or ""),
        )
        try:
            out = await _img.generate(req)
        except _img.ImageError as e:
            # 503, not 500: every ImageError here means "no local backend can
            # do this right now", which is a service-availability answer and
            # the message is written to be shown to a person. A 500 would read
            # as a bug in the daemon and send them to the wrong logs.
            raise HTTPException(503, str(e))

        return {
            "created": 0,
            "data": [{"b64_json": b} for b in out["images_b64"]],
            "backend": out["backend"],
            "model": out["model"],
        }

    @app.get("/decisions", dependencies=[Depends(auth)])
    def list_decisions(
        status: str = Query(default="open"),
        session_id: str = Query(default=""),
    ) -> dict[str, Any]:
        from adk.decisions.store import get_store

        wanted = None if status == "all" else status
        cards = get_store().list(status=wanted, session_id=session_id)
        return {"decisions": [c.to_dict() for c in cards], "count": len(cards)}

    @app.get("/decisions/count", dependencies=[Depends(auth)])
    def count_decisions() -> dict[str, Any]:
        """Cheap enough for a browser badge to poll on a timer."""
        from adk.decisions.store import get_store

        cards = get_store().list()
        urgent = [c for c in cards if c.urgency in ("high", "critical")]
        oldest = max((c.age_seconds for c in cards), default=0.0)
        return {
            "open": len(cards),
            "urgent": len(urgent),
            "oldest_age_seconds": round(oldest, 1),
        }

    @app.get("/decisions/needs-human", dependencies=[Depends(auth)])
    def needs_human_decisions() -> dict[str, Any]:
        """Filter: only cards that need an active human decision.

        Uses triage classification: a DECISION has options, a deadline, or is a
        credential/blocked card. CONTEXT cards are info-only (hourly digests,
        status updates, etc.) and are auto-acknowledged after 24 hours.

        Returns the same shape as /decisions but filtered to decisions only.
        """
        from adk.decisions.store import get_store
        from adk.decisions.triage import triage

        cards = get_store().list()
        decisions = []
        for card in cards:
            classification, reason = triage(card.to_dict())
            if classification == "decision":
                decisions.append(card.to_dict())
        return {
            "decisions": decisions,
            "count": len(decisions),
            "triage_reason": ("filtered to decision-only (options, deadline, "
                              "credential, or blocked)"),
        }

    @app.get("/decisions/triage-patterns", dependencies=[Depends(auth)])
    def get_triage_patterns() -> dict[str, Any]:
        """Export triage patterns for desk-side classification.

        The desk reads this at runtime and classifies cards using the same rules
        as the daemon, without re-running Python. This keeps the two in sync.
        """
        from adk.decisions.triage import is_decision_pattern_json

        return is_decision_pattern_json()

    @app.get("/decisions/{card_id}/wait", dependencies=[Depends(auth)])
    async def wait_for_decision(
        card_id: str,
        timeout: int = Query(default=30, ge=1, le=300),  # 1–300 seconds
    ) -> dict[str, Any]:
        """Long-poll for a decision to be answered.

        Blocks for up to `timeout` seconds waiting for the card to be answered,
        cancelled, or expired. Returns immediately if the card is already closed.

        Useful for headless runs that need to wait on their own card.
        Returns 408 (Request Timeout) if the timeout expires with no answer.
        """
        from adk.decisions.store import DecisionError, get_store

        store = get_store()
        start = time.time()

        while True:
            try:
                card = store.get(card_id)
            except DecisionError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            if card is None:
                raise HTTPException(status_code=404, detail=f"no such card: {card_id}")

            # Card is closed (answered, expired, cancelled).
            if card.status != "open":
                return {
                    "card": card.to_dict(),
                    "waited_seconds": round(time.time() - start, 2),
                }

            # Timeout reached.
            elapsed = time.time() - start
            if elapsed >= timeout:
                raise HTTPException(
                    status_code=408,
                    detail=f"timeout waiting for {card_id} after {timeout}s",
                )

            # Wait a bit and retry. 0.5s polling is cheap and gives sub-second
            # responsiveness without hammering the store.
            await asyncio.sleep(0.5)

    # Shared across requests so the bridge's last-sent disambiguation survives
    # between two messages of one conversation. Lazy: the daemon must come up
    # even when adk.decisions.channels cannot import.
    _chat_bridge: dict[str, Any] = {}

    @app.post("/decisions/chat-reply", dependencies=[Depends(auth)])
    async def chat_reply(body: ChatReply) -> dict[str, Any]:
        """One inbound chat message from a DM bridge (Discord/Telegram/Slack).

        Registered BEFORE the `/decisions/{card_id}` family on purpose — the
        same route-order rule as `/decisions/count`. Authorization is the tested
        fail-closed path in ``adk.decisions.channels`` (bound owner, DM-only),
        driven by ``~/.aither/decisions/channels.json`` — the daemon bearer only
        proves the FORWARDER is ours, never that the SENDER may answer.
        """
        from adk.decisions.channels import DecisionChannelBridge

        bridge = _chat_bridge.get("bridge")
        if bridge is None:
            bridge = DecisionChannelBridge()
            _chat_bridge["bridge"] = bridge
        if body.last_sent_card:
            bridge.note_sent(body.platform, body.last_sent_card.strip().lower())

        text = (body.text or "").strip()
        # `steer` prefix (optionally after a card id) sends free text WITHOUT
        # closing the card — the chat bridge itself only answers.
        steer_match = re.match(
            r"^(?:(d-[a-z0-9]{4,12})\s+)?steer\s+(.+)$", text, re.IGNORECASE | re.DOTALL,
        )
        if steer_match:
            from adk.decisions.store import DecisionError

            card, problem = bridge.resolve_target(
                body.platform, steer_match.group(1) or "",
            )
            if card is None:
                return {"reply": problem or "no card to steer"}
            cfg = bridge.configs.get(body.platform)
            from adk.decisions.channels import authorize

            verdict = authorize(
                cfg, user_id=body.user_id, is_direct_message=body.is_direct_message,
            )
            if not verdict.allowed:
                return {"reply": "Not authorized."}
            try:
                bridge.store.steer(card.id, steer_match.group(2).strip(),
                                   via=body.platform)
            except DecisionError as exc:
                return {"reply": str(exc)}
            return {"reply": f"↪️ steered {card.id} — card stays open."}

        reply = await bridge.on_message(
            body.platform,
            "",  # channel id is not used by the bridge
            body.user_id,
            text,
            is_direct_message=body.is_direct_message,
        )
        return {"reply": reply or ""}

    def _recipe_entitlement_for(recipe_id: str) -> str:
        """What a caller must hold to raise or answer this recipe's cards.

        Fail-closed on EVERY way the lookup can go wrong, and the deny answer is
        the INITIAL value rather than something an except handler returns: an
        absent card_recipes module, a registry that raises, an unknown recipe id
        all leave `want` at the entitlement only "*" carries. A build with no
        card_recipes still gates the door — an answer would spawn nothing in THAT
        build, but a gate that disappears along with a module is not a gate.
        """
        want = UNKNOWN_RECIPE_ENTITLEMENT
        try:
            from adk.decisions.card_recipes import recipe_entitlement

            want = recipe_entitlement(recipe_id)
        except ImportError:
            # No registry in this build: DENY, said out loud rather than by
            # declining to overwrite (the shape security_lint SEC019 exists for).
            want = UNKNOWN_RECIPE_ENTITLEMENT
        except Exception:  # noqa: BLE001 - a broken registry must not open the door
            want = UNKNOWN_RECIPE_ENTITLEMENT
        return want

    def _require_recipe(principal: Principal, recipe_id: str) -> None:
        """THE CARD DOOR MUST NOT BE A WAY AROUND THE ROUTE DOOR.

        Measured 2026-09-18: `POST /wakes/{name}/{enable,disable,run}` were the
        only routes carrying `wakes:mutate`, while `POST /decisions` (the recipe
        door) and `POST /decisions/{id}/answer` carried the bearer alone. The
        store's answer transition calls `card_recipes.apply_answer`, which spawns
        `awrise disable|run --name <job>` -- so a principal refused at
        `/wakes/nightly-sync/run` reached the identical spawn by raising a
        `wake-failed` card and answering it, from anywhere on the fleet network
        (this daemon binds 0.0.0.0 and genesis proxies /api/v1/decisions/*).

        BOTH halves are gated, not just the answer: raising a spawning card is a
        remote OFFER to spawn on the owner's host, and an unentitled caller must
        not be able to put one in front of them either.

        With no token registry every caller is the owner principal holding "*",
        so this changes nothing until an operator writes harness_tokens.json --
        the same ordering `require_entitlement` documents.
        """
        want = _recipe_entitlement_for(recipe_id)
        if want and not principal.has(want):
            raise HTTPException(
                status_code=403,
                detail=(f"principal {principal.id!r} lacks entitlement {want!r} "
                        f"(required by card recipe {recipe_id!r})"),
            )

    def _guard_recipe_answer(principal: Principal, card: Any, choice: str,
                              body: Any = None) -> Optional[str]:
        """The checks `_wake_prepare` applies, on the CARD path.

        The entitlement is checked for every recipe card. The rest apply only to
        an answer that actually spawns: the origin claim is re-checked (the
        literal same helper the RAISE side of every /wakes mutation applies --
        see the docstring below), then the job must still exist (a card
        outlives its job easily, and `awrise run --name <gone>` surfaces hours
        later as a confusing 502), and the same job must not already be
        running -- the per-name in-flight slot `/wakes/{name}/run` holds, plus
        the ledger's own view, which is the only one that sees a run this
        process did not start.

        Returns the resolved `via` (see `_wake_reauthorize_origin`) for a
        spawning answer, or ``None`` when this recipe/choice does not spawn --
        callers use that to record who ACTUALLY authorized the spawn, not just
        who held the bearer.
        """
        recipe_id = (getattr(card, "card_recipe", "") or "").strip()
        if not recipe_id:
            return None
        _require_recipe(principal, recipe_id)
        try:
            from adk.decisions.card_recipes import spawns_on
        except ImportError:
            return None
        if not spawns_on(recipe_id, choice):
            return None
        # RAISING a spawning card (POST /decisions with card_recipe=, and every
        # direct /wakes mutation) already re-authorizes an `origin` claim
        # against the owner-bound `channels.json` via `_wake_reauthorize_origin`.
        # ANSWERING one did not -- `AnswerDecision` carried no `origin` field at
        # all until this fix, so the two-step "propose, then confirm" split had
        # that re-check on the propose half only. A caller (or an agent tool
        # holding the daemon's own bearer, e.g. the awrise toolpack's
        # `awrise_confirm`) could raise a wakes-add/wakes-set-command card
        # through a channel-narrowed principal and then answer it itself with
        # no origin claim, closing the loop the split exists to prevent
        # (security finding 2026-09-19). This call is the identical helper,
        # never a lookalike, so it cannot drift from the raise side; it still
        # only NARROWS what the bearer already allows, so a caller holding
        # `wakes:create`/`*` outright (the default, no-registry deployment)
        # sees no behavioural change from this alone.
        via = _wake_reauthorize_origin(body, principal)
        variables = getattr(card, "recipe_vars", None)
        job = (variables or {}).get("job") if isinstance(variables, dict) else None
        if not isinstance(job, str) or not job:
            return via
        from adk.wakes import get_wake, read_jobs, valid_name

        if not valid_name(job):
            raise HTTPException(status_code=400, detail="invalid wake name on the card")
        jobs = read_jobs()
        if not jobs["installed"]:
            raise HTTPException(status_code=503, detail="awrise not installed")
        if jobs["error"]:
            raise HTTPException(status_code=503, detail=jobs["error"])
        if job not in jobs["jobs"]:
            raise HTTPException(status_code=404, detail=f"no such wake: {job}")
        if (choice or "").strip() in _WAKE_RUN_CHOICES:
            with _RUNNING_LOCK:
                live = _RUNNING.get(job)
            if live is not None:
                raise HTTPException(
                    status_code=409, detail={"error": "already running", "pid": live},
                )
            current = get_wake(job)
            if current is not None and current.get("running"):
                raise HTTPException(
                    status_code=409,
                    detail={"error": "already running",
                            "since": current.get("running_since")},
                )
        return via

    def _store_and_notify(card: Any) -> dict[str, Any]:
        """Persist a built card and tell the owner. The tail BOTH raise doors share.

        Shared rather than duplicated because the notify half is the part that is
        easy to forget: a card that lands in the store and raises no window is
        the silent no-op this whole channel exists to prevent, and the recipe
        door would have been a second place to forget it.
        """
        from adk.decisions.store import DecisionError, get_store

        try:
            created = get_store().create(card)
        except DecisionError as exc:
            # 400, not 500: the store's validation IS the contract (a decision needs a
            # default, a credential card needs a secret_name and no options), and a
            # caller that violates it has sent a bad request, not hit a broken daemon.
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # A dedupe HIT returns the card that ALREADY exists — the store documents
        # this as "the object you get back is not the object you passed in", and
        # identity is the only way to tell, since a fresh card has its id minted
        # inside create(). The owner was already told about that card, so
        # notifying again is exactly the storm the key exists to prevent.
        if created is not card:
            return {**created.to_dict(), "deduped": True}

        # Raise the window/toast exactly as a local `adk decide ask` would. Without
        # this a remotely-raised card lands in the store and the owner is never told —
        # the silent no-op this whole channel exists to prevent.
        try:
            from adk.decisions.notify import notify

            notify(created)
        except Exception as exc:  # noqa: BLE001
            # The card IS stored; failing the request now would make the caller retry
            # and duplicate it. Report the degradation in the response instead of
            # swallowing it, so "raised but nobody was told" is visible.
            return {**created.to_dict(), "notify_error": f"{type(exc).__name__}: {exc}"}
        return created.to_dict()

    @app.post("/decisions", dependencies=[Depends(auth)])
    def raise_decision(body: RaiseDecision,
                       principal: Principal = Depends(auth)) -> dict[str, Any]:
        """RAISE a card from off-box.

        Until this existed the daemon could list, read, answer and cancel cards but not
        create one — the raise path was `adk decide ask` on the owner's own machine.
        Every other surface (AitherShell, the portal, AitherConnect, Relay, Room, and
        genesis's `/api/v1/decisions` proxy) could therefore SHOW a card and none could
        raise one, so "an agent anywhere reaches its owner" was not expressible. The
        genesis proxy was already written against this route; without it that proxy
        returns the daemon's 404 as "Decision not found", which reads as a missing card
        rather than a missing endpoint.
        """
        from adk.decisions.store import DecisionCard, DecisionOption, DecisionSource

        _source = DecisionSource(
            session_id=(body.session_id or "").strip(),
            agent=(body.raised_by or body.agent or "api").strip(),
            cwd=(body.cwd or "").strip(),
        )

        # ---- the RECIPE door -------------------------------------------------
        # Naming a recipe builds the whole card here, from code, so an off-box
        # caller supplies only VARIABLES. It cannot invent an option whose key
        # maps to no action, cannot widen the dedupe key, and cannot lengthen the
        # deadline — the properties the recipe layer exists to hold. A recipe
        # this build does not carry is a 400 and never a silently plain card,
        # because a plain card would be answered and do nothing.
        _recipe_id = (body.card_recipe or "").strip()
        if _recipe_id:
            # Gated BEFORE the recipe is built: an unentitled caller is refused,
            # and learns nothing about which recipes this build carries.
            _require_recipe(principal, _recipe_id)
            try:
                from adk.decisions.card_recipes import CardRecipeError, build_card
            except ImportError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=("card recipes are not available in this build "
                            f"({exc}); raise a plain card instead"),
                ) from exc
            try:
                card = build_card(_recipe_id, dict(body.recipe_vars or {}),
                                  source=_source)
            except CardRecipeError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return _store_and_notify(card)

        kwargs: dict[str, Any] = dict(
            id="",
            title=(body.title or "").strip(),
            summary=(body.summary or "").strip(),
            kind=body.kind,
            urgency=body.urgency,
            options=[DecisionOption(key=o.key, label=o.label,
                                    consequence=o.consequence)
                     for o in (body.options or [])],
            default_key=(body.default or "").strip(),
            facts=[f for f in (body.facts or []) if f.strip()],
            source=_source,
        )

        # DEDUPE IS FEATURE-DETECTED, AND FAIL-CLOSED WHEN ASKED FOR AND ABSENT.
        # Same shape and same reason as the credential block below: this daemon
        # and the store can be two different vintages. Silently dropping a
        # dedupe_key would turn "one failing streak = one card" into a card per
        # failing pass — the storm the key exists to prevent — and it would look
        # like the producer misbehaving rather than a version skew.
        _dedupe_fields = set(getattr(DecisionCard, "__dataclass_fields__", {}) or {})
        _want_dedupe = (body.dedupe_key or "").strip()
        if _want_dedupe and "dedupe_key" not in _dedupe_fields:
            raise HTTPException(
                status_code=400,
                detail=("this store build cannot dedupe cards (DecisionCard has no "
                        "dedupe_key); raising anyway would produce one card per "
                        "failing pass"),
            )
        if "dedupe_key" in _dedupe_fields:
            kwargs["dedupe_key"] = _want_dedupe

        # CREDENTIAL FIELDS ARE OPTIONAL AT THE STORE, AND THIS ROUTE MUST NOT DIE
        # WHEN THEY ARE ABSENT.
        #
        # Measured 2026-08-17: this handler passed secret_name / credential_format /
        # credential_scope / credential_description unconditionally, and the store's
        # DecisionCard in this tree declares NONE of them (zero occurrences of
        # "credential" in store.py). So every POST /decisions died with
        #
        #     TypeError: DecisionCard.__init__() got an unexpected keyword
        #                argument 'secret_name'
        #
        # before reaching the store — a hard 500 on the ONLY route that can create a
        # card. Every other surface could list, read, answer and cancel; none could
        # raise. "An agent anywhere reaches its owner" was not expressible, and the
        # failure looked like a broken daemon rather than a two-file version skew
        # (the credential feature lives in another worktree and never landed here).
        #
        # Feature-detected rather than assumed, and FAIL-CLOSED on the credential
        # path: an ordinary card raises fine, while a card that actually asks for a
        # secret is REFUSED with a clear 400 instead of being quietly stripped of the
        # field that says which secret it wants.
        _card_fields = set(getattr(DecisionCard, "__dataclass_fields__", {}) or {})
        _cred = {
            "secret_name": (body.secret_name or "").strip(),
            "credential_format": body.credential_format,
            "credential_scope": body.credential_scope,
            "credential_description": (body.credential_description or "").strip(),
        }
        _missing = [k for k in _cred if k not in _card_fields]
        if _missing and _cred["secret_name"]:
            raise HTTPException(
                status_code=400,
                detail=(
                    "credential cards are not supported by this store build "
                    f"(DecisionCard is missing {', '.join(sorted(_missing))}). "
                    "Raise a normal card, or land the credential fields in "
                    "adk/decisions/store.py first — silently dropping secret_name "
                    "would produce a card that asks for 'a secret' without saying "
                    "which one."
                ),
            )
        for _k, _v in _cred.items():
            if _k in _card_fields:
                kwargs[_k] = _v

        return _store_and_notify(DecisionCard(**kwargs))

    @app.post("/decisions/{card_id}/steer", dependencies=[Depends(auth)])
    def steer_decision(card_id: str, body: SteerDecision) -> dict[str, Any]:
        """Free text to the raising session. Does NOT close the card."""
        from adk.decisions.store import DecisionError, get_store

        try:
            card = get_store().steer(card_id, body.text, via=body.via or "api")
        except DecisionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if card is None:
            raise HTTPException(status_code=404, detail=f"no such card: {card_id}")
        return card.to_dict()

    @app.get("/decisions/{card_id}", dependencies=[Depends(auth)])
    def get_decision(card_id: str) -> dict[str, Any]:
        from adk.decisions.store import DecisionError, get_store

        try:
            card = get_store().get(card_id)
        except DecisionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if card is None:
            raise HTTPException(status_code=404, detail=f"no such card: {card_id}")
        return card.to_dict()

    @app.post("/decisions/{card_id}/answer", dependencies=[Depends(auth)])
    def answer_decision(card_id: str, body: AnswerDecision,
                        principal: Principal = Depends(auth)) -> dict[str, Any]:
        from adk.decisions.store import DecisionError, get_store

        store = get_store()
        # READ BEFORE ANSWERING. The answer TRANSITION is what spawns the recipe's
        # action, so the capability check has to happen while the card is still
        # open -- afterwards the process is already started and a 403 would be a
        # lie. A card that does not exist falls through to store.answer(), which
        # owns the 404/409 wording.
        try:
            _existing = store.get(card_id)
        except DecisionError:
            _existing = None
        _recipe_via = None
        if _existing is not None:
            _recipe_via = _guard_recipe_answer(principal, _existing, body.choice, body)
        try:
            card = store.answer(
                card_id, body.choice, note=body.note or "",
                # A spawning answer records who the origin re-check actually
                # resolved to (e.g. "owner:discord:12345"), the same provenance
                # `_wake_sync` records for a direct /wakes mutation -- never
                # the bare client-supplied `via` for that case, which would
                # hide exactly the identity this re-check exists to surface.
                via=_recipe_via or body.via or "api",
                # Delivered explicitly below (the response reports the path);
                # letting answer() deliver as well writes the mailbox twice.
                deliver=False,
            )
        except DecisionError as exc:
            # 409, not 400: answering an already-answered card is a LOST RACE, not
            # a malformed request. Two surfaces open at once is the normal case
            # here, and the loser needs to be told which answer won.
            message = str(exc)
            status = 409 if "already" in message else 400
            raise HTTPException(status_code=status, detail=message) from exc
        delivered = store.deliver_answer(card)
        return {
            "decision": card.to_dict(),
            # Explicit, because "recorded" and "reached the blocked session" are
            # different things and only the second one unblocks anybody.
            "delivered_to_session": bool(delivered),
            "mailbox": str(delivered) if delivered else "",
        }

    @app.post("/decisions/{card_id}/cancel", dependencies=[Depends(auth)])
    def cancel_decision(card_id: str, body: CancelDecision) -> dict[str, Any]:
        from adk.decisions.store import DecisionError, get_store

        try:
            card = get_store().cancel(card_id, note=body.note or "")
        except DecisionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return card.to_dict()

    # ── awrise wakes: the ONE read/mutate window for the scheduler ──────────
    #
    # Reads come straight off AWRISE_HOME (jobs.json + ledger) through
    # ``adk.wakes``, imported lazily inside each handler so a broken import never
    # takes the daemon down. Mutations spawn the awrise CLI as an argv LIST and
    # propagate its exit code. Scope (home, binary) is the daemon's own process
    # environment; nothing in a payload can name a path, a binary or an argv.
    #
    # Route order matters: FastAPI matches in registration order, so the static
    # /wakes/count and /wakes/ledger are registered BEFORE /wakes/{name}.

    _wake_tail_bytes = 2048

    def _wake_tail(text: str) -> str:
        return (text or "")[-_wake_tail_bytes:]

    def _wake_file_tail(handle: Any) -> str:
        """Last 2 KiB of a temp file the child wrote to, decoded leniently."""
        try:
            handle.flush()
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _wake_tail_bytes))
            data = handle.read()
        except (OSError, ValueError):
            return ""
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data)

    def _wake_reauthorize_origin(body: Any, principal: Principal) -> str:
        """The origin re-authorization every ``/wakes`` mutation applies.

        Extracted so create/update run the LITERAL SAME check ``_wake_prepare``
        always has, rather than a lookalike that could drift from it. Returns
        the ``via`` string: the bare principal id, or ``principal:platform:user``
        once an origin claim has passed re-authorization against the owner-bound
        ``channels.json``. An origin claim can only NARROW what the bearer
        already allows, never widen it.
        """
        via = principal.id
        origin = getattr(body, "origin", None) if body is not None else None
        if origin is None:
            return via
        from adk.decisions.channels import ChannelConfigError, authorize, load_config

        try:
            configs = load_config()
        except ChannelConfigError as exc:
            raise HTTPException(
                status_code=403,
                detail={"error": "origin not authorized", "reason": str(exc)},
            ) from exc
        verdict = authorize(
            configs.get(origin.platform),
            user_id=origin.user_id,
            is_direct_message=origin.is_direct_message,
        )
        if not verdict.allowed:
            raise HTTPException(
                status_code=403,
                detail={"error": "origin not authorized", "reason": verdict.reason},
            )
        return f"{principal.id}:{origin.platform}:{origin.user_id}"

    def _wake_prepare(name: str, verb: str, principal: Principal,
                      body: Any) -> tuple[list[str], str]:
        """Everything that must hold BEFORE a spawn: name, origin, job, binary.

        Order: the name gate first (nothing else may touch a path or an argv
        with an unchecked name), then the origin re-authorization (an identity
        claim is refused before the daemon reveals whether the job exists), then
        job existence (a typo never reaches the CLI), then the binary.
        """
        from adk.wakes import build_argv, read_jobs, resolve_bin, valid_name

        if not valid_name(name):
            raise HTTPException(status_code=400, detail="invalid wake name")
        via = _wake_reauthorize_origin(body, principal)
        jobs = read_jobs()
        if not jobs["installed"]:
            raise HTTPException(status_code=503, detail="awrise not installed")
        if jobs["error"]:
            raise HTTPException(status_code=503, detail=jobs["error"])
        if name not in jobs["jobs"]:
            raise HTTPException(status_code=404, detail=f"no such wake: {name}")
        binary = resolve_bin()
        if binary is None:
            raise HTTPException(status_code=503, detail="awrise not installed")
        return build_argv(binary, verb, name), via

    def _wake_exec(argv: list[str], verb: str, *, timeout_s: float = 30.0
                   ) -> tuple[int, str, str, float]:
        """Run *argv* synchronously; a nonzero exit or a timeout becomes the
        HTTPException every ``/wakes`` mutation answers with. Returns
        ``(exit_code, stdout_tail, stderr_tail, duration_s)`` on success.
        """
        import subprocess

        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout_s, cwd=None, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(
                status_code=408, detail=f"awrise {verb} timed out after {int(timeout_s)}s",
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail=f"awrise not installed: {exc}",
            ) from exc
        duration = round(time.monotonic() - started, 3)
        stderr_tail = _wake_tail(proc.stderr)
        if proc.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail={"error": f"awrise {verb} exited {proc.returncode}",
                        "exit_code": proc.returncode, "stderr_tail": stderr_tail},
            )
        return proc.returncode, _wake_tail(proc.stdout), stderr_tail, duration

    def _wake_sync(name: str, verb: str, principal: Principal, body: Any) -> dict[str, Any]:
        """enable/disable: a bounded synchronous spawn; the exit code is the answer."""
        argv, via = _wake_prepare(name, verb, principal, body)
        exit_code, stdout_tail, stderr_tail, duration = _wake_exec(argv, verb)
        return {
            "ok": True, "name": name, "action": verb, "exit_code": exit_code,
            "via": via, "argv": argv, "note": getattr(body, "note", "") or "",
            "stdout_tail": stdout_tail, "stderr_tail": stderr_tail,
            "duration_s": duration,
        }

    def _require_wakes_create(principal: Principal) -> None:
        """The STRICTER tier a command-carrying wake mutation spends.

        ``wakes:mutate`` is provisioned for turning an already owner-vetted job
        on/off (enable/disable/run) or reshaping WHEN/WHERE it runs
        (every/timeout/cwd on an existing job). It was never meant to cover
        WHAT a job runs — that is an arbitrary host command — so a principal
        holding only ``wakes:mutate`` may still raise/answer neither
        ``wakes-add`` nor ``wakes-set-command``. Same shape as
        ``require_entitlement``, called inline rather than as a route
        dependency because whether it applies to a PATCH depends on the BODY
        (a command change), not the route alone.
        """
        if not principal.has("wakes:create"):
            raise HTTPException(
                status_code=403,
                detail=f"principal {principal.id!r} lacks entitlement 'wakes:create'",
            )

    def _wake_validate_create(body: "WakeCreate", principal: Principal) -> str:
        """Everything that must hold before a CREATE is even OFFERED as a card.

        Same checks and the same order a synchronous ``add`` spawn used to
        apply immediately: name, then every payload field, then the
        network-touching origin re-authorization, then existence (a CREATE
        refuses a name that already exists, 409), then the binary. Moved in
        front of the card raise rather than a spawn — measured 2026-09-19: the
        prior version ran these checks and then spawned ``awrise add``
        SYNCHRONOUSLY on the same call, gated only by ``wakes:mutate`` (the
        same flat entitlement enable/disable/run use), with no owner
        confirmation before a caller's arbitrary command started running on a
        schedule. A malformed or already-taken name must still never reach the
        owner as something to decide about.
        """
        from adk.wakes import (
            MAX_COMMAND_LEN,
            MAX_CWD_LEN,
            MAX_EVERY_LEN,
            read_jobs,
            resolve_bin,
            valid_name,
            valid_payload,
        )

        if not valid_name(body.name):
            raise HTTPException(status_code=400, detail="invalid wake name")
        if not valid_payload(body.command, MAX_COMMAND_LEN):
            raise HTTPException(status_code=400, detail="invalid or oversized command")
        if not valid_payload(body.every, MAX_EVERY_LEN):
            raise HTTPException(status_code=400, detail="invalid or oversized every")
        if body.cwd is not None and not valid_payload(body.cwd, MAX_CWD_LEN):
            raise HTTPException(status_code=400, detail="invalid or oversized cwd")
        if body.timeout is not None and (
            isinstance(body.timeout, bool) or not isinstance(body.timeout, int)
            or body.timeout <= 0
        ):
            raise HTTPException(status_code=400, detail="timeout must be a positive integer")
        via = _wake_reauthorize_origin(body, principal)
        jobs = read_jobs()
        if not jobs["installed"]:
            raise HTTPException(status_code=503, detail="awrise not installed")
        if jobs["error"]:
            raise HTTPException(status_code=503, detail=jobs["error"])
        if body.name in jobs["jobs"]:
            raise HTTPException(status_code=409, detail=f"wake already exists: {body.name}")
        if resolve_bin() is None:
            raise HTTPException(status_code=503, detail="awrise not installed")
        return via

    def _wake_prepare_update(name: str, body: "WakeUpdate", principal: Principal
                             ) -> tuple[list[str], str]:
        """Everything that must hold BEFORE a synchronous ``set`` spawn — the
        every/timeout/cwd path ONLY. ``body.command`` is never read or built
        into an argv here: a command change is routed to
        ``_wake_validate_update_command`` + a card before this function is
        ever called, so a caller cannot reach a direct command spawn through
        this path even if it changed to accept one by accident — there is
        nothing here that would build it."""
        from adk.wakes import (
            MAX_CWD_LEN,
            MAX_EVERY_LEN,
            build_set_argv,
            read_jobs,
            resolve_bin,
            valid_name,
            valid_payload,
        )

        if not valid_name(name):
            raise HTTPException(status_code=400, detail="invalid wake name")
        if body.every is None and body.timeout is None and body.cwd is None:
            raise HTTPException(status_code=400, detail="no fields to update")
        if body.every is not None and not valid_payload(body.every, MAX_EVERY_LEN):
            raise HTTPException(status_code=400, detail="invalid or oversized every")
        if body.cwd is not None and not valid_payload(body.cwd, MAX_CWD_LEN):
            raise HTTPException(status_code=400, detail="invalid or oversized cwd")
        if body.timeout is not None and (
            isinstance(body.timeout, bool) or not isinstance(body.timeout, int)
            or body.timeout <= 0
        ):
            raise HTTPException(status_code=400, detail="timeout must be a positive integer")
        via = _wake_reauthorize_origin(body, principal)
        jobs = read_jobs()
        if not jobs["installed"]:
            raise HTTPException(status_code=503, detail="awrise not installed")
        if jobs["error"]:
            raise HTTPException(status_code=503, detail=jobs["error"])
        if name not in jobs["jobs"]:
            raise HTTPException(status_code=404, detail=f"no such wake: {name}")
        binary = resolve_bin()
        if binary is None:
            raise HTTPException(status_code=503, detail="awrise not installed")
        argv = build_set_argv(binary, name, command=None, every=body.every,
                              timeout=body.timeout, cwd=body.cwd)
        return argv, via

    def _wake_validate_update_command(name: str, body: "WakeUpdate", principal: Principal
                                      ) -> str:
        """Everything that must hold before a COMMAND-changing PATCH is even
        OFFERED as a card. Same shape as ``_wake_validate_create`` — existence
        is inverted (404, not 409) to match every other ``/wakes/{name}``
        route. Any every/timeout/cwd supplied ALONGSIDE the command in the
        same request is validated too and carried onto the same card, so one
        PATCH becomes one card and one eventual ``awrise set``, never two.
        """
        from adk.wakes import (
            MAX_COMMAND_LEN,
            MAX_CWD_LEN,
            MAX_EVERY_LEN,
            read_jobs,
            resolve_bin,
            valid_name,
            valid_payload,
        )

        if not valid_name(name):
            raise HTTPException(status_code=400, detail="invalid wake name")
        if not valid_payload(body.command, MAX_COMMAND_LEN):
            raise HTTPException(status_code=400, detail="invalid or oversized command")
        if body.every is not None and not valid_payload(body.every, MAX_EVERY_LEN):
            raise HTTPException(status_code=400, detail="invalid or oversized every")
        if body.cwd is not None and not valid_payload(body.cwd, MAX_CWD_LEN):
            raise HTTPException(status_code=400, detail="invalid or oversized cwd")
        if body.timeout is not None and (
            isinstance(body.timeout, bool) or not isinstance(body.timeout, int)
            or body.timeout <= 0
        ):
            raise HTTPException(status_code=400, detail="timeout must be a positive integer")
        via = _wake_reauthorize_origin(body, principal)
        jobs = read_jobs()
        if not jobs["installed"]:
            raise HTTPException(status_code=503, detail="awrise not installed")
        if jobs["error"]:
            raise HTTPException(status_code=503, detail=jobs["error"])
        if name not in jobs["jobs"]:
            raise HTTPException(status_code=404, detail=f"no such wake: {name}")
        if resolve_bin() is None:
            raise HTTPException(status_code=503, detail="awrise not installed")
        return via

    def _wake_raise_card(recipe_id: str, variables: dict[str, Any], *, action: str,
                         name: str) -> Any:
        """Build, store and notify a ``wakes-add``/``wakes-set-command`` card,
        and answer the HTTP call with 202 pending — the shape every ``/wakes``
        route that can no longer spawn directly shares. Nothing has been
        registered or changed yet: the job exists (or changes) only once the
        card raised here is ANSWERED, which runs through
        ``card_recipes.apply_answer`` behind the same ``wakes:create``
        entitlement (``_guard_recipe_answer`` on the answer route)."""
        from fastapi.responses import JSONResponse

        from adk.decisions.card_recipes import build_card

        card = build_card(recipe_id, variables)
        result = _store_and_notify(card)
        return JSONResponse(status_code=202, content={
            "ok": True, "pending": True, "action": action, "name": name, "card": result,
        })

    @app.get("/wakes", dependencies=[Depends(auth)])
    def list_wakes(state: str = Query(default="")) -> dict[str, Any]:
        """Every job with its last state, plus clock liveness. Never 5xx.

        ``installed: false`` when awrise has no jobs.json; ``clock_stale: true``
        when no ``tick`` row has landed in five minutes — a green job list with
        a dead clock is the failure this window exists to show.
        """
        from adk.wakes import snapshot

        if state and state not in ("failing", "running", "disabled"):
            raise HTTPException(status_code=400, detail="state must be failing|running|disabled")
        return snapshot(state=state or None)

    @app.post("/wakes")
    def create_wake(
        body: WakeCreate,
        principal: Principal = Depends(require_entitlement("wakes:create")),
    ) -> Any:
        """PROPOSE a new wake. Never spawns directly: this raises a
        ``wakes-add`` card and answers 202 pending. The job is not registered
        until the card is answered ``create`` — by this principal or anyone
        else holding ``wakes:create`` — which runs ``awrise add`` from
        ``card_recipes.apply_answer``, never from this route.

        Gated on ``wakes:create``, stricter than the ``wakes:mutate`` that
        gates enable/disable/run: this call PROPOSES an arbitrary host
        command on a schedule, not merely toggling a job the owner already
        vetted.
        """
        via = _wake_validate_create(body, principal)
        return _wake_raise_card("wakes-add", {
            "name": body.name, "command": body.command, "every": body.every,
            "cwd": body.cwd or "", "timeout": str(body.timeout) if body.timeout else "",
            "requested_by": via,
        }, action="add", name=body.name)

    @app.get("/wakes/count", dependencies=[Depends(auth)])
    def count_wakes() -> dict[str, Any]:
        from adk.wakes import snapshot

        snap = snapshot()
        return {k: snap[k] for k in ("count", "failing", "disabled", "running", "installed",
                                     "schema", "last_tick_at", "clock_stale")}

    @app.get("/wakes/ledger", dependencies=[Depends(auth)])
    def wakes_ledger(
        job: str = Query(default=""),
        limit: int = Query(default=50),
        since: str = Query(default=""),
        event: str = Query(default=""),
    ) -> dict[str, Any]:
        from adk.wakes import read_ledger, valid_name

        if job and not valid_name(job):
            raise HTTPException(status_code=400, detail="invalid wake name")
        return read_ledger(job=job or None, limit=limit, since=since or None,
                           event=event or None)

    @app.get("/wakes/{name}", dependencies=[Depends(auth)])
    def get_wake_detail(name: str) -> dict[str, Any]:
        """One job plus its last 10 ledger rows: state -> reason -> output tail.

        503 when awrise is not installed so a consumer can tell "no such job"
        from "no awrise"; 404 for a job that is not in jobs.json.
        """
        from adk.wakes import get_wake, read_jobs, valid_name

        if not valid_name(name):
            raise HTTPException(status_code=400, detail="invalid wake name")
        jobs = read_jobs()
        if not jobs["installed"]:
            raise HTTPException(status_code=503, detail="awrise not installed")
        if jobs["error"]:
            raise HTTPException(status_code=503, detail=jobs["error"])
        job = get_wake(name)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no such wake: {name}")
        return job

    @app.patch("/wakes/{name}")
    def update_wake(
        name: str,
        body: WakeUpdate,
        principal: Principal = Depends(require_entitlement("wakes:mutate")),
    ) -> Any:
        """Change an existing wake.

        A ``command`` change is a DIFFERENT capability from every/timeout/cwd:
        it replaces WHAT the job runs, the same thing ``POST /wakes`` grants,
        so it never spawns directly either — it raises a ``wakes-set-command``
        card (202 pending), gated on ``wakes:create`` (checked here, in
        addition to the route's own ``wakes:mutate``, since whether the
        stricter tier applies depends on the body). every/timeout/cwd ALONE
        stay the existing synchronous ``awrise set`` under ``wakes:mutate`` —
        those change WHEN/WHERE a job runs, not what it runs, the same
        capability enable/disable/run already spend.
        """
        if body.command is not None:
            _require_wakes_create(principal)
            via = _wake_validate_update_command(name, body, principal)
            return _wake_raise_card("wakes-set-command", {
                "name": name, "command": body.command,
                "every": body.every or "", "cwd": body.cwd or "",
                "timeout": str(body.timeout) if body.timeout else "",
                "requested_by": via,
            }, action="set", name=name)

        argv, via = _wake_prepare_update(name, body, principal)
        exit_code, stdout_tail, stderr_tail, duration = _wake_exec(argv, "set")
        updated_fields = [field for field, value in (
            ("every", body.every), ("timeout", body.timeout), ("cwd", body.cwd),
        ) if value is not None]
        return {
            "ok": True, "name": name, "action": "set", "exit_code": exit_code,
            "via": via, "argv": argv, "note": body.note or "",
            "updated_fields": updated_fields,
            "stdout_tail": stdout_tail, "stderr_tail": stderr_tail,
            "duration_s": duration,
        }

    @app.post("/wakes/{name}/enable", dependencies=[Depends(auth)])
    def enable_wake(
        name: str,
        body: Optional[WakeMutate] = None,
        principal: Principal = Depends(require_entitlement("wakes:mutate")),
    ) -> dict[str, Any]:
        return _wake_sync(name, "enable", principal, body)

    @app.post("/wakes/{name}/disable", dependencies=[Depends(auth)])
    def disable_wake(
        name: str,
        body: Optional[WakeMutate] = None,
        principal: Principal = Depends(require_entitlement("wakes:mutate")),
    ) -> dict[str, Any]:
        return _wake_sync(name, "disable", principal, body)

    def _wake_spawn(name: str, argv: list[str]) -> dict[str, Any]:
        """Start ``awrise run`` detached from the request, reaped by a thread.

        stdout/stderr go to temp FILES (never PIPE — an unattended child must
        not block on a full pipe). The reaper waits for the child, collects the
        tails, unlinks the files and frees the per-name slot; the slot is held
        until the CHILD exits, not until the request returns.
        """
        import subprocess
        import tempfile

        out_f = tempfile.NamedTemporaryFile(prefix="awrise-out-", suffix=".log", delete=False)
        err_f = tempfile.NamedTemporaryFile(prefix="awrise-err-", suffix=".log", delete=False)
        holder: dict[str, Any] = {
            "pid": None, "exit_code": None, "stdout_tail": "", "stderr_tail": "",
            "done": threading.Event(),
        }

        def _cleanup() -> None:
            for handle in (out_f, err_f):
                try:
                    handle.close()
                except OSError:
                    continue
                try:
                    os.unlink(handle.name)
                except OSError:
                    continue

        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=out_f, stderr=err_f, cwd=None,
            )
        except OSError:
            _cleanup()
            raise
        holder["pid"] = proc.pid
        with _RUNNING_LOCK:
            _RUNNING[name] = proc.pid

        def _reap() -> None:
            try:
                holder["exit_code"] = proc.wait()
                holder["stdout_tail"] = _wake_file_tail(out_f)
                holder["stderr_tail"] = _wake_file_tail(err_f)
            finally:
                _cleanup()
                with _RUNNING_LOCK:
                    if _RUNNING.get(name) == proc.pid:
                        _RUNNING.pop(name, None)
                holder["done"].set()

        threading.Thread(target=_reap, name=f"awrise-reap-{name}", daemon=True).start()
        return holder

    @app.post("/wakes/{name}/run", dependencies=[Depends(auth)])
    async def run_wake(
        name: str,
        body: Optional[WakeMutate] = None,
        wait_s: float = Query(default=WAKE_RUN_WAIT_DEFAULT_S),
        principal: Principal = Depends(require_entitlement("wakes:mutate")),
    ) -> dict[str, Any]:
        """Fire one wake now. Waits up to ``wait_s`` (0..120) WITHOUT killing.

        200/502 with the exit code when the child finished inside the window;
        202 with the pid when it is still running (awrise writes the ``finished``
        row itself — read the outcome from ``GET /wakes/{name}``); 409 when this
        daemon already holds a live child for that name.
        """
        from fastapi.responses import JSONResponse

        argv, via = _wake_prepare(name, "run", principal, body)
        wait = min(max(float(wait_s), 0.0), WAKE_RUN_WAIT_MAX_S)
        with _RUNNING_LOCK:
            live = _RUNNING.get(name)
            if live is not None:
                raise HTTPException(
                    status_code=409, detail={"error": "already running", "pid": live},
                )
            _RUNNING[name] = 0  # reserve the slot until Popen hands back a pid
        try:
            holder = _wake_spawn(name, argv)
        except OSError as exc:
            with _RUNNING_LOCK:
                if _RUNNING.get(name) == 0:
                    _RUNNING.pop(name, None)
            raise HTTPException(
                status_code=502,
                detail={"error": f"awrise run could not start: {exc}", "exit_code": None,
                        "stderr_tail": ""},
            ) from exc
        except Exception:
            with _RUNNING_LOCK:
                if _RUNNING.get(name) == 0:
                    _RUNNING.pop(name, None)
            raise
        started = time.monotonic()
        deadline = started + wait
        done: threading.Event = holder["done"]
        while not done.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        if not done.is_set():
            return JSONResponse(status_code=202, content={
                "ok": True, "name": name, "action": "run", "running": True,
                "pid": holder["pid"], "exit_code": None, "via": via, "argv": argv,
                "note": getattr(body, "note", "") or "",
                "outcome": f"GET /wakes/{name} .recent",
            })
        exit_code = holder["exit_code"]
        if exit_code != 0:
            raise HTTPException(
                status_code=502,
                detail={"error": f"awrise run exited {exit_code}", "exit_code": exit_code,
                        "stderr_tail": holder["stderr_tail"]},
            )
        return {
            "ok": True, "name": name, "action": "run", "running": False,
            "pid": holder["pid"], "exit_code": exit_code, "via": via, "argv": argv,
            "note": getattr(body, "note", "") or "",
            "stdout_tail": holder["stdout_tail"], "stderr_tail": holder["stderr_tail"],
            "duration_s": round(time.monotonic() - started, 3),
        }

    @app.post("/sessions/{session_id}/resize", dependencies=[Depends(auth)])
    def resize(session_id: str, body: ResizeInput) -> dict[str, Any]:
        try:
            return {"resized": mgr.resize(session_id, body.rows, body.cols)}
        except ManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/sessions/{session_id}/interrupt", dependencies=[Depends(auth)])
    def interrupt(session_id: str) -> dict[str, Any]:
        try:
            return {"interrupted": mgr.interrupt(session_id)}
        except ManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/sessions/{session_id}", dependencies=[Depends(auth)])
    def stop_session(session_id: str) -> dict[str, Any]:
        try:
            return mgr.stop(session_id)
        except ManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/sessions/{session_id}/events", dependencies=[Depends(auth)])
    def events(session_id: str, since: int = Query(default=0)) -> dict[str, Any]:
        try:
            session = mgr.get_session(session_id)
        except ManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        evs = session.events_since(since)
        return {"events": evs, "last_seq": session.last_seq, "state": session.state}

    @app.get("/sessions/{session_id}/stream", dependencies=[Depends(auth)])
    async def stream(session_id: str, since: int = Query(default=0)):
        """Server-sent events. Resumable via ``?since=`` after a reconnect."""
        try:
            session = mgr.get_session(session_id)
        except ManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        async def gen():
            cursor = since
            idle_ticks = 0
            while True:
                batch = session.events_since(cursor)
                if batch:
                    idle_ticks = 0
                    for event in batch:
                        cursor = event["seq"]
                        yield f"event: {event['kind']}\ndata: {json.dumps(event)}\n\n"
                        if event["kind"] == "session.exited":
                            return
                else:
                    idle_ticks += 1
                    # A comment frame keeps proxies (and Cloudflare, on the
                    # tunnel path) from closing an idle stream. Without it a
                    # long model turn looks like a dead connection.
                    if idle_ticks % 20 == 0:
                        yield ": keepalive\n\n"
                await asyncio.sleep(0.15)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── rooms: the AitherAeon spine ─────────────────────────────────────────
    #
    # A session is one agent doing one thing; a room is where several of them are
    # visible together, every event filed under one of the six pillars. Producers
    # POST here from anywhere on the host -- Claude Code hooks, adk agent loops,
    # the kernel tick, a genesis SSE shim -- and every client reads one stream.

    rooms = default_registry()
    # Every spoken room event becomes its actor's memory (character_recall.py). Constructs
    # in microseconds; the lib import and the embedder run on its own worker thread, so
    # this line costs the boot path nothing.
    character_recall = register_character_recall(rooms)

    # Producers that must not block (Claude Code hooks run synchronously inside the
    # owner's session) append to a spool file instead of POSTing. Tailing it is
    # blocking file I/O, so it lives on its own thread — a blocking call on
    # the event loop is not "slow", it is an outage for every concurrent request.
    tailer = default_tailer()
    tailer.start()

    # Claude Code tabs reach the room by TAILING THE TRANSCRIPTS THEY ALREADY WRITE —
    # measured at zero cost to the session, versus ~224ms of interpreter startup per
    # tool call for the hook equivalent. It also covers sessions that were already
    # running before any of this existed, which a hook can never do.
    bridge = default_bridge()
    bridge.start()

    # SteerDispatcher (U15): the one listener that turns an ADDRESSED event (``to``)
    # into a delivered mailbox. Registered here, beside the tailer and the bridge, so
    # it covers every room created LATER too (``RoomRegistry.add_listener`` applies to
    # rooms not yet constructed) and the two producers above that never touch HTTP at
    # all — a dispatcher wired only into the ``/events`` route would miss both of them.
    # Constructed exactly ONCE per process: ``register()`` is idempotent per-callable
    # via ``Room.add_listener``, but two SEPARATE ``SteerDispatcher`` instances do not
    # share their delivered-event LRU, so registering a second one would double-deliver
    # every addressed event despite that guard.
    def _list_unified_sessions_for_dispatch() -> list[dict[str, Any]]:
        """Real backing for the dispatcher's target resolver -- same source
        ``GET /sessions/unified`` reads. A resolver that raises must not crash dispatch
        of every OTHER target in the same event; ``steer_dispatch`` already wraps this
        call in try/except for that reason, so this stays a thin, honest passthrough.
        """
        from adk.harnesses.session_directory import default_directory

        unified = default_directory().list_sessions_sync(mgr.list_sessions())
        rows = [{"id": s.id, "title": s.title} for s in unified]
        # A daemon-owned claude-tty is known to every OTHER surface by the id the
        # PROGRAM carries -- its --session-id, which is the transcript's name, the desk
        # stage's slot key and the room actor its bridge emits under -- and the
        # directory folds that discovered row INTO the daemon row. So the program id
        # must resolve here as well, or a steer from the desk is refused as "unknown
        # actor" for the very pty this tier was built to reach (measured 2026-09-19).
        for s in unified:
            alias = str((s.extras or {}).get("harness_session_id") or "")
            if alias and alias != s.id:
                rows.append({"id": alias, "title": s.title})
        return rows

    def _send_managed_input_for_dispatch(session_id: str, text: str) -> bool:
        """Real backing for the dispatcher's tier-1 (managed pty) delivery.

        Measured to miss almost always (``steer_dispatch``'s own docstring: every
        interactive Claude Code tab on this box today is ``origin=discovered``, never
        daemon-spawned) -- kept as a real tier so a caller that DID spawn its target
        through THIS daemon still gets immediate delivery instead of a queued one.
        """
        try:
            session = mgr.get_session(session_id)
        except ManagerError:
            session = _managed_session_by_harness_id(session_id)
            if session is None:
                return False
        # submit(), never send(): on a pty, send() leaves the text unsubmitted in the
        # input box while the receipt claims "the agent has it now" (2026-09-19).
        return bool(session.submit(text))

    def _managed_session_by_harness_id(harness_session_id: str):
        """A managed session addressed by the id the PROGRAM knows (claude-tty's
        --session-id, which is also the room actor id its transcript bridge emits under),
        not the daemon's own row id. Both must resolve, or a steer addressed to a tab by
        the id every other surface shows for it misses the pty it is sitting in."""
        wanted = str(harness_session_id or "")
        if not wanted:
            return None
        for info in mgr.list_sessions():
            if str(info.get("harness_session_id") or "") == wanted:
                try:
                    return mgr.get_session(str(info.get("id") or ""))
                except ManagerError:
                    return None
        return None

    def _tier1_opt_in_for_dispatch(session_id: str) -> bool:
        """Did this managed session opt in to PEER input on its pty at spawn?

        Resolves the same two ways ``_send_managed_input_for_dispatch`` does (the
        daemon's row id AND the program's ``harness_session_id``), because a target the
        pty tier can reach by one id must answer the opt-in question by that same id.
        Unknown id or a ``ManagerError`` is False: an unresolvable target did not opt in.
        """
        try:
            session = mgr.get_session(session_id)
        except ManagerError:
            session = _managed_session_by_harness_id(session_id)
            if session is None:
                return False
        return bool(getattr(session.config, "allow_peer_input", False))

    steer_dispatcher = register_steer_dispatcher(
        rooms,
        list_unified_sessions=_list_unified_sessions_for_dispatch,
        send_managed_input=_send_managed_input_for_dispatch,
        tier1_opt_in=_tier1_opt_in_for_dispatch,
    )

    # The ambient context well. Background-computed so a draw is O(1) — an agent that
    # pays 2s of discovery before its first useful thought pays it on every turn.
    well = default_well(session_lister=mgr.list_sessions)
    well.start()

    @app.get("/character-recall/status", dependencies=[Depends(auth)])
    def character_recall_status() -> dict[str, Any]:
        """Read-only status for the character-recall listener: available/booting/off,
        counts (seen, utterances, indexed, unembedded, dropped), the party manifest it
        resolves personas through. A quiet room and a dead hook must not look alike."""
        return character_recall.status()

    @app.get("/well", dependencies=[Depends(auth)])
    def draw_well(
        cwd: str = Query(default=""),
        actor: str = Query(default=""),
        render: bool = Query(default=False),
    ) -> dict[str, Any]:
        """Draw the ambient snapshot. Never rebuilds inline.

        ``?render=1`` also returns the tagged-section form that
        ``AitherGraph.context_for`` emits, so a caller can paste it straight into a
        system prompt without knowing this endpoint's JSON shape.
        """
        snapshot = well.draw(cwd=cwd, actor=actor)
        if render:
            snapshot["rendered"] = well.render_context(cwd=cwd, actor=actor)
        return snapshot


    # ── desk: the owner's Desk bridge, reachable from inside the fleet ───────
    #
    # The desk bridge binds 127.0.0.1 on the Windows host and must keep doing so
    # (an agent command channel on the LAN is a worse trade than the gap it
    # closes). This daemon runs on that same host, already listens where every
    # container can reach it, and already checks the same bearer -- so it is the
    # natural bridge, and the desk grows no new exposure.
    #
    # Measured 2026-09-08, from the aitheros-mcpgateway container:
    #   host.docker.internal:8362   OPEN        <- this daemon
    #   host.docker.internal:47931  REFUSED     <- the desk, loopback-only
    #
    # ALLOWLISTED, never generic. An open forwarder on an authenticated daemon
    # would convert one bearer into reach over every service on the desk host's
    # loopback, which is a larger hole than the one being closed.

    desk_base = os.environ.get("AWDESK_URL", "http://127.0.0.1:47931").rstrip("/")
    desk_surfaces = ("overlay", "app")

    def _desk_call(method: str, path: str, body: Any = None) -> dict[str, Any]:
        """One hop to the desk bridge, carrying this daemon's own bearer.

        Never raises: a desk that is not running is a NORMAL state (the owner
        closed it), and a 502 traceback would read as the daemon being broken.
        """
        import urllib.error  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        url = f"{desk_base}{path}"
        data = None
        headers = {"accept": "application/json"}
        tok = resolve_token()
        if tok:
            headers["authorization"] = f"Bearer {tok}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["content-type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            status = exc.code
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "status": 0,
                "error": (
                    f"awdesk unreachable at {desk_base} ({type(exc).__name__}). The "
                    f"Desk app runs on the owner's Windows session; this daemon "
                    f"reaches it over loopback, so a failure here means Desk is not "
                    f"running -- not that the fleet lost a route."
                ),
            }
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            payload = {"raw": raw[:2000]}
        if isinstance(payload, dict):
            payload.setdefault("ok", 200 <= status < 300)
            payload["status"] = status
            return payload
        return {"ok": 200 <= status < 300, "status": status, "result": payload}

    @app.get("/desk/fleet/status", dependencies=[Depends(auth)])
    def desk_fleet_status() -> dict[str, Any]:
        """The desk's fleet verdict -- containers, masks, VRAM holders, doors."""
        return _desk_call("GET", "/fleet/status")

    @app.post("/desk/command", dependencies=[Depends(auth)])
    def desk_command(body: dict[str, Any]) -> dict[str, Any]:
        """Hand the desk a sentence, the way the Command window does."""
        text = str((body or {}).get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        return _desk_call("POST", "/command", {"text": text})

    @app.get("/desk/desktop/status", dependencies=[Depends(auth)])
    def desk_desktop_status() -> dict[str, Any]:
        """Which desktop surfaces are open (overlay, AitherDesktop app)."""
        return _desk_call("GET", "/desktop/status")

    @app.post("/desk/desktop/{surface}", dependencies=[Depends(auth)])
    def desk_desktop_open(surface: str) -> dict[str, Any]:
        """Raise one desktop surface. The segment is VALIDATED, not forwarded."""
        if surface not in desk_surfaces:
            raise HTTPException(
                status_code=404,
                detail=f"unknown desktop surface {surface!r}; "
                       f"expected one of {list(desk_surfaces)}",
            )
        return _desk_call("POST", f"/desktop/{surface}")

    @app.get("/rooms", dependencies=[Depends(auth)])
    def list_rooms() -> dict[str, Any]:
        return {
            "rooms": rooms.list_rooms(),
            "producers": {"spool": tailer.stats(), "transcripts": bridge.stats()},
        }

    @app.post("/rooms", dependencies=[Depends(auth)])
    def create_room(body: CreateRoom) -> dict[str, Any]:
        try:
            return rooms.get_or_create(body.id, title=body.title).info()
        except RoomError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/rooms/{room_id}", dependencies=[Depends(auth)])
    def room_info(room_id: str) -> dict[str, Any]:
        try:
            room = rooms.get(room_id)
        except RoomError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if room is None:
            raise HTTPException(status_code=404, detail=f"no room {room_id!r}")
        return room.info()

    @app.post("/events")
    def publish_event(
        body: PublishEvent,
        principal: Principal = Depends(auth),
    ) -> dict[str, Any]:
        """Ingest one event. The room is created on first use, so a producer never
        has to know whether it is first -- a 404 here would make startup ordering a
        thing every producer had to get right.

        The ``Principal`` ``auth`` resolved is BOUND here and handed to the room as
        the event's ``auth`` stamp (out-of-band: ``Room._normalise`` builds it from
        this argument only and drops any ``auth`` the payload carried). This covers
        the HTTP producers only, and that is the correct scope: the spool tailer and
        the transcript bridge started above feed this same room via ``Room.publish``
        directly, get NO stamp, and therefore fail closed out of the steer
        dispatcher's tier 1 -- they are agent producers, and the mailbox is where an
        unvouched producer's words belong.
        """
        try:
            room = rooms.get_or_create(body.room)
            stamped = room.publish(
                body.model_dump(), auth=(principal.id, principal.plan)
            )
        except RoomError as exc:
            # 400 with the reason, never a silent accept. A producer sending a bad
            # envelope must learn it now, not by noticing an empty lane next week.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # ``to`` echoed back is the NORMALISED list ``Room._normalise`` produced (deduped,
        # self-address and empty-list both already refused/collapsed) — never the raw
        # ``body.to``, which could still contain the producer's pre-dedup order. ``dispatch``
        # tells the caller, from the envelope alone, whether this event was addressed at
        # all (the dispatcher registered above ran synchronously inside ``room.publish()``
        # by the time this line executes; its aggregate outcome is ``GET /steer/dispatch``,
        # not repeated here per-event).
        to = stamped.get("to") or []
        return {
            "ok": True,
            "seq": stamped["seq"],
            "pillar": stamped["pillar"],
            "to": to,
            "dispatch": {"addressed": bool(to), "targets": len(to)},
        }

    @app.get("/steer/dispatch", dependencies=[Depends(auth)])
    def steer_dispatch_status() -> dict[str, Any]:
        """Read-only status for the addressed-event dispatcher registered at startup.

        Same auth convention as every other substantive GET in this file (``/harnesses``,
        ``/profiles``, ``/agents``) -- there is no existing PUBLIC read-route precedent
        for something this operationally sensitive (delivery targets, recent addressees),
        so this stays behind the bearer rather than joining ``/health``.
        """
        return steer_dispatcher.status()

    # ── local auth: browser sign-in via device flow ──────────────────────────────
    # The daemon is a loopback service reachable ONLY from the local machine.
    # Browser can initiate device-code flow here, and daemon relays to portal.
    # Portal mints the session after the user approves; daemon never mints anything.
    #
    # 🚨 The PEER ADDRESS is the gate here, not the Host header.
    #
    # This daemon binds 0.0.0.0 on purpose (DEFAULT_BIND_HOST above: genesis reaches
    # the harness across the WSL2 podman network), so these routes are exposed to
    # every host on that network, over plaintext HTTP. A Host-header check does NOT
    # contain them: `curl -H "Host: 127.0.0.1:8362" http://<lan-ip>:8362/auth/link`
    # satisfies it trivially. Only a BROWSER is bound by Host/Origin; an attacker
    # with a socket is not. That left the bearer -- sent in cleartext on that same
    # network -- as the sole protection, which is one sniffed request away from an
    # attacker initiating a device flow the user then unknowingly approves.
    #
    # A TCP source address cannot be forged the way a header can (a spoofed SYN
    # never completes the handshake), so the connection's peer is the real signal.
    # Host stays as a second, cheaper check: defence-in-depth, never the decision.
    _loopback_peers = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

    def _validate_localhost_origin(request: Request, host: str = Header(default="")) -> None:
        """Reject anything whose TCP peer is not this machine's loopback."""
        peer = getattr(getattr(request, "client", None), "host", None)
        # Fail CLOSED: an unknown peer (no client info) is refused, never allowed.
        if peer not in _loopback_peers:
            raise HTTPException(
                status_code=403,
                detail="this endpoint is reachable from loopback only",
            )
        if not host:
            raise HTTPException(status_code=400, detail="missing Host header")
        # Accept both 127.0.0.1:PORT and localhost:PORT
        allowed_hosts = (f"127.0.0.1:{DEFAULT_PORT}", f"localhost:{DEFAULT_PORT}")
        if host not in allowed_hosts:
            raise HTTPException(
                status_code=403,
                detail=f"Host header {host} not allowed. "
                f"This endpoint is only reachable from localhost.",
            )

    @app.post("/auth/link", dependencies=[Depends(auth), Depends(_validate_localhost_origin)])
    async def link_device_code(host: str = Header(default="")) -> LinkDeviceCodeResponse:
        """Initiate device-code flow by calling portal's /auth/device/code.

        The daemon does NOT mint credentials. It only relays portal's response
        and tracks the device_code in memory for the /auth/link/status polling endpoint.
        """
        try:
            import httpx
        except ImportError:
            raise HTTPException(
                status_code=503,
                detail="httpx required for device flow (pip install httpx)",
            ) from None

        portal_url = "https://api.aitherium.com"
        portal_endpoint = f"{portal_url}/auth/device/code"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(portal_endpoint)
                if not res.is_success:
                    raise HTTPException(
                        status_code=503,
                        detail=f"portal device flow failed: {res.status_code}",
                    )
                data = res.json()
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=503, detail=f"portal unreachable: {exc}"
            ) from exc

        device_code = data.get("device_code", "")
        expires_in = data.get("expires_in", 600)

        if not device_code:
            raise HTTPException(
                status_code=503, detail="portal returned no device_code"
            )

        # Track this device code in memory with expiration.
        expires_at = time.time() + expires_in
        _device_flow_state.set(device_code, "pending", expires_at)

        return LinkDeviceCodeResponse(
            user_code=data.get("user_code", ""),
            device_code=device_code,
            verification_uri=data.get("verification_uri", ""),
            expires_in=expires_in,
        )

    @app.get(
        "/auth/link/status/{device_code}",
        dependencies=[Depends(auth), Depends(_validate_localhost_origin)],
    )
    async def link_status(
        device_code: str, host: str = Header(default="")
    ) -> LinkStatusResponse:
        """Poll device-code approval status.

        Returns current state from memory (updated asynchronously by background polling).
        If status is 'pending', browser should retry after a short delay.
        """
        try:
            import httpx
        except ImportError:
            raise HTTPException(
                status_code=503,
                detail="httpx required for device flow (pip install httpx)",
            ) from None

        # Check cached state first
        cached = _device_flow_state.get(device_code)
        if cached and cached["status"] != "pending":
            return LinkStatusResponse(
                status=cached["status"], expires_at=cached["expires_at"]
            )

        # Poll portal for current status (may have changed since last check)
        portal_url = "https://api.aitherium.com"
        portal_endpoint = f"{portal_url}/auth/device/token"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    portal_endpoint,
                    json={"device_code": device_code},
                )
                if res.status_code == 200:
                    # Approved
                    _device_flow_state.set(device_code, "approved", time.time() + 600)
                    return LinkStatusResponse(
                        status="approved", expires_at=time.time() + 600
                    )
                elif res.status_code == 400:
                    # Check the error to determine if expired/denied
                    error_data = res.json() if res.text else {}
                    error_code = error_data.get("error", "")
                    if error_code == "expired_token":
                        _device_flow_state.set(device_code, "expired", time.time())
                        return LinkStatusResponse(status="expired", expires_at=time.time())
                    elif error_code == "access_denied":
                        _device_flow_state.set(device_code, "denied", time.time())
                        return LinkStatusResponse(status="denied", expires_at=time.time())
                    else:
                        # Still pending
                        return LinkStatusResponse(
                            status="pending", expires_at=time.time() + 600
                        )
                else:
                    # Unexpected response; return as pending
                    return LinkStatusResponse(
                        status="pending", expires_at=time.time() + 600
                    )
        except httpx.RequestError:
            # Portal unreachable; return current cached state or pending
            if cached:
                return LinkStatusResponse(
                    status=cached["status"], expires_at=cached["expires_at"]
                )
            return LinkStatusResponse(status="pending", expires_at=time.time() + 600)

    @app.get("/rooms/{room_id}/events", dependencies=[Depends(auth)])
    def room_events(
        room_id: str,
        since: int = Query(default=0),
        limit: int = Query(default=0),
    ) -> dict[str, Any]:
        try:
            room = rooms.get(room_id)
        except RoomError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if room is None:
            raise HTTPException(status_code=404, detail=f"no room {room_id!r}")
        return {
            "events": room.events_since(since, limit),
            "last_seq": room.last_seq,
            "pillars": room.pillar_counts(),
        }

    @app.get("/rooms/{room_id}/stream", dependencies=[Depends(auth)])
    async def room_stream(
        room_id: str,
        since: int = Query(default=0),
        pillar: str = Query(default=""),
    ):
        """Server-sent events for a room. Resumable via ``?since=``.

        ``?pillar=reasoning`` filters to one lane. The filter is applied to the
        FAN-OUT only -- ``seq`` still advances over every event, so a filtered client
        that reconnects with its last seq does not silently replay the whole room.
        """
        try:
            room = rooms.get_or_create(room_id)
        except RoomError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        wanted = {p.strip() for p in pillar.split(",") if p.strip()}

        async def gen():
            cursor = since
            idle_ticks = 0
            while True:
                batch = room.events_since(cursor)
                if batch:
                    idle_ticks = 0
                    for event in batch:
                        cursor = event["seq"]
                        if wanted and event.get("pillar") not in wanted:
                            continue
                        yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                else:
                    idle_ticks += 1
                    # Same reason as the session stream: an idle room must not look
                    # like a dead connection to a proxy on the tunnel path.
                    if idle_ticks % 20 == 0:
                        yield ": keepalive\n\n"
                await asyncio.sleep(0.15)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def serve(host: str = "", port: int = 0, token: str = "") -> int:
    """Run the daemon. Prints the token location, never the token itself."""
    try:
        import uvicorn
    except ImportError:
        sys.stderr.write("uvicorn is required: pip install uvicorn fastapi\n")
        return 2

    bind_host = host or DEFAULT_BIND_HOST
    bind_port = port or DEFAULT_PORT
    app = create_app(token=token)
    sys.stderr.write(
        f"AitherShell harness daemon on http://{bind_host}:{bind_port}\n"
        f"  token: {TOKEN_PATH} (or $AITHER_HARNESS_TOKEN)\n"
        f"  cors : {', '.join(allowed_origins())}\n"
    )
    uvicorn.run(app, host=bind_host, port=bind_port, log_level="warning")
    return 0


if __name__ == "__main__":
    # Without this, `python -m adk.harnesses.daemon` EXITS SILENTLY -- it
    # imports the module, defines serve(), and stops. Every recorded start of
    # this daemon has therefore been a `python -c "from ... import serve"`
    # incantation, which is why nothing supervises it and why it was found down
    # today. Bind 0.0.0.0 by default: the WSL socat bridge that publishes this
    # to containers dials the Windows host address, not loopback.
    import argparse as _argparse

    _ap = _argparse.ArgumentParser(prog="adk.harnesses.daemon",
                                   description="Run the AitherShell harness daemon.")
    _ap.add_argument("--host", default="0.0.0.0")
    _ap.add_argument("--port", type=int, default=0)
    _args = _ap.parse_args()
    raise SystemExit(serve(host=_args.host, port=_args.port))

