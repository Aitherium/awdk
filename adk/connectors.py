"""Connector resolution for adk agents and harness children.

The agent half of the connect-once plane: a person connects GitHub / Google
Drive ONCE in the Connections window (the OAuth dance is AitherIdentity's; the
store and resolution plane are Genesis /connectors/*). Agents reach DATA
through server-side tools; the one token that is ever env-injected is GitHub
for git/gh, and only into a harness child that opted in
(``SessionConfig.connectors=["github"]``), through :func:`resolve_git_env`.

    CONNECTOR_GITHUB_TOKEN  + GH_TOKEN + a git credential helper (git/gh)

A harness child never inherits a ``CONNECTOR_*`` / ``GH_TOKEN`` /
``GITHUB_TOKEN`` from the daemon's own environment
(:func:`strip_inherited_connector_env`): a stale token in the daemon env would
otherwise reach every child whatever the grants say (connector slice S5).

Off-fleet (a laptop that ran ``adk login``) the bearer comes from
``~/.aither/config.json`` and the base is the public portal API; in-fleet the
environment overrides both. Scope is decided server-side from the bearer,
never from an X-Tenant-ID.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

from adk._tls import tls_verify

log = logging.getLogger("adk.connectors")

_ENV_PREFIX = "CONNECTOR_"

#: Connectors holding the owner's personal data (mail, calendar, to-do) --
#: mirrors ``PERSONAL_CONNECTORS`` in AitherOS lib/connectors/catalog.py. A
#: resolve-all (``connectors`` is None) never carries their tokens. Genesis
#: already leaves them out; this strips them again so an older Genesis cannot
#: hand mailbox access to a caller that did not name them. Only a caller that
#: NAMES them (the gated Hearth tools) receives them, and a harness child never
#: does: :data:`ENV_INJECTABLE` is GitHub alone.
PERSONAL_CONNECTORS = ("google_calendar", "gmail", "microsoft_graph")
_PERSONAL_ENV = frozenset(f"{_ENV_PREFIX}{c.upper()}_TOKEN" for c in PERSONAL_CONNECTORS)


#: The public resolve path: the portal BFF forwards the caller's own bearer to
#: Genesis ``/connectors/resolve`` (Veil ``/api/connectors/resolve``). The old
#: default was the in-fleet ``https://aitheros-genesis:8001``, which resolves on
#: no laptop -- every off-fleet resolve failed before it was sent.
DEFAULT_RESOLVE_BASE = "https://api.aitherium.com/api"
#: Where a person connects an account: the customer Connections window. Agents,
#: adk, awsh and Hearth only ever deep-link here.
CONNECTIONS_URL = "https://aitherium.com/?app=connections"

#: Bearer env names, most specific first (a harness or an in-fleet worker).
_BEARER_ENV = ("AITHER_API_KEY", "AITHER_IDENTITY_BEARER", "AITHER_SESSION_BEARER")

#: Inherited variables that carry a GitHub credential to git / gh. A harness
#: child gets none of them from the daemon -- only what an opted-in resolve hands.
GITHUB_TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                    "GITHUB_ENTERPRISE_TOKEN")
#: The only connectors a harness child may have env-injected (the target design:
#: GitHub for git/gh; every other provider is reached through server-side tools).
ENV_INJECTABLE = ("github",)
_GITHUB_ENV = f"{_ENV_PREFIX}GITHUB_TOKEN"


class ConnectorEnvError(RuntimeError):
    """An opted-in connector env could not be built. ``str(exc)`` names the
    connector and the reason, never a token; the session refuses to start."""


def resolve_base() -> str:
    """Where ``/connectors/resolve`` lives: env override, else the public portal."""
    for name in ("AITHER_CONNECTORS_URL", "AITHER_GENESIS_URL", "GENESIS_URL"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value.rstrip("/")
    return DEFAULT_RESOLVE_BASE


#: Back-compat name (the in-fleet callers imported it).
_genesis_base = resolve_base


def _env_bearer() -> str:
    for name in _BEARER_ENV:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _saved_bearer() -> str:
    """The sign-in ``adk login`` / ``adk home signin`` saved (``api_key``)."""
    try:
        from adk.config import load_saved_config

        cfg = load_saved_config() or {}
    except (OSError, ValueError, ImportError) as exc:
        log.warning("saved sign-in unreadable: %s", type(exc).__name__)
        return ""
    return str(cfg.get("api_key") or cfg.get("access_token") or "").strip()


#: Set in every harness child's env by ``HarnessSession._child_env``.
HARNESS_CHILD_MARKER = "AITHER_HARNESS_SESSION"


def _bearer() -> str:
    """The caller's bearer: the environment (in-fleet), else the saved sign-in.

    Inside a harness child (:data:`HARNESS_CHILD_MARKER` set) there is NO saved
    sign-in fallback: the daemon resolved whatever the child opted in to and
    stripped its own bearers from the child's env, so a child that never opted
    in must not mint connector tokens from the owner's ``adk login`` on disk.
    """
    return _env_bearer() or ("" if os.environ.get(HARNESS_CHILD_MARKER)
                             else _saved_bearer())


def daemon_bearer() -> str:
    """The harness DAEMON's bearer for an opted-in resolve: env, else saved login.

    Not :func:`_bearer`: a daemon started from inside another harness session
    carries the child marker, and its own resolve must still work.
    """
    return _env_bearer() or _saved_bearer()


def strip_inherited_connector_env(env: Dict[str, str]) -> Dict[str, str]:
    """Drop every ``CONNECTOR_*``, GitHub token and Aitherium bearer from ``env``
    in place.

    A harness child starts from the daemon's ``os.environ``; without this a
    token sitting in the daemon's environment reaches every child no matter
    what was granted. The bearers (:data:`_BEARER_ENV`) go too: any of them can
    call ``/connectors/resolve`` and mint every connector token, so a child that
    kept one would be opted in to everything.
    """
    for key in list(env):
        if key.startswith(_ENV_PREFIX) or key in GITHUB_TOKEN_ENV or key in _BEARER_ENV:
            env.pop(key, None)
    return env


def _clean_env(payload: Dict, named: bool = False) -> Dict[str, str]:
    """Keep only CONNECTOR_* values from a resolve response (never trust the
    server to hand back anything else — this env reaches every child).

    ``named`` is False for a resolve-all: personal-data tokens are dropped.
    """
    if not isinstance(payload, dict):
        payload = {}
    return {
        k: str(v)
        for k, v in payload.items()
        if k.startswith(_ENV_PREFIX) and v and (named or k not in _PERSONAL_ENV)
    }


async def resolve_connectors(
    connectors: Optional[List[str]] = None,
    genesis_base: Optional[str] = None,
    bearer: Optional[str] = None,
    timeout: float = 8.0,
) -> Dict[str, Any]:
    """Resolve connector tokens, saying WHY when none came back.

    Returns ``{"env": {CONNECTOR_*: token}, "error": reason}`` where ``error``
    is "" on a clean answer (``env`` may still be empty: nothing connected),
    else ``unreachable`` / ``denied`` / ``http_<code>`` / ``bad_response``. A
    caller that must tell a person what to do (the Hearth tools) needs that
    difference; :func:`resolve_connector_env` keeps the fail-soft ``{}``.
    ``connectors`` narrows the resolve to the named providers (fewer tokens
    minted, fewer audit rows); None resolves every connected one EXCEPT the
    :data:`PERSONAL_CONNECTORS`, which come back only when named.
    """
    import httpx

    base = (genesis_base or resolve_base()).rstrip("/")
    headers = {"Content-Type": "application/json"}
    token = bearer if bearer is not None else _bearer()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload: Dict[str, Any] = {} if connectors is None else {"connectors": list(connectors)}
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=tls_verify()) as client:
            resp = await client.post(
                f"{base}/connectors/resolve", headers=headers, json=payload
            )
    except (httpx.HTTPError, OSError) as exc:
        log.warning(f"connector env resolution skipped (unreachable): {exc}")
        return {"env": {}, "error": "unreachable"}
    if resp.status_code in (401, 403):
        log.warning(
            "connector env resolution DENIED (%s) — this caller is not entitled "
            "to connectors; an admin must fix the tenant context", resp.status_code,
        )
        return {"env": {}, "error": "denied"}
    if resp.status_code != 200:
        log.warning(f"connector env resolution skipped (HTTP {resp.status_code})")
        return {"env": {}, "error": f"http_{resp.status_code}"}
    try:
        named = connectors is not None
        return {"env": _clean_env(resp.json().get("env"), named=named), "error": ""}
    except (ValueError, AttributeError) as exc:
        log.warning(f"connector env resolution returned an unreadable body: {exc}")
        return {"env": {}, "error": "bad_response"}


async def resolve_connector_env(
    genesis_base: Optional[str] = None,
    bearer: Optional[str] = None,
    timeout: float = 8.0,
    connectors: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Resolve the caller's connector env vars from Genesis /connectors/resolve.

    Returns {} when nothing is connected, genesis is unreachable, or the
    caller is denied — the agent still spawns; the vars are simply absent.
    """
    result = await resolve_connectors(
        connectors, genesis_base=genesis_base, bearer=bearer, timeout=timeout,
    )
    return result["env"]


def resolve_git_env(agent_id: str = "", workspace: str = "", *,
                    base: Optional[str] = None, bearer: Optional[str] = None,
                    timeout: float = 8.0) -> Dict[str, str]:
    """GitHub for git/gh, for ONE opted-in harness child. Never a resolve-all.

    Asks ``/connectors/resolve`` for ``{"connectors": ["github"], "purpose":
    "git"}`` with the agent named (``X-Aither-Agent``), so the server can hold
    it to a per-agent ``git`` grant. Returns exactly
    ``{"CONNECTOR_GITHUB_TOKEN": token}``. Every failure RAISES
    :class:`ConnectorEnvError` (loud): a session that asked for GitHub and got
    an empty env would fail later at ``git push`` for a reason nobody can see.
    """
    import httpx

    token = bearer if bearer is not None else _bearer()
    if not token:
        raise ConnectorEnvError(
            "github: no Aitherium sign-in on this machine -- run `adk login`, then "
            "`adk connectors connect github`")
    where = (base or resolve_base()).rstrip("/")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    agent = (agent_id or "").strip()
    body: Dict[str, Any] = {"connectors": ["github"], "purpose": "git"}
    if agent:
        headers["X-Aither-Agent"] = agent
        body["agent_id"] = agent
    if workspace:
        body["workspace"] = workspace
    try:
        with httpx.Client(timeout=timeout, verify=tls_verify()) as client:
            resp = client.post(f"{where}/connectors/resolve", headers=headers, json=body)
    except (httpx.HTTPError, OSError) as exc:
        log.error("github git env: resolve unreachable (%s)", type(exc).__name__)
        raise ConnectorEnvError(
            f"github: could not reach the connector service ({where})") from None
    if resp.status_code in (401, 403):
        reason = _detail(resp)
        log.error("github git env DENIED (%s) for agent %r: %s", resp.status_code,
                  agent or "-", reason)
        raise ConnectorEnvError(
            f"github: refused ({resp.status_code}) -- "
            f"{reason or 'this agent is not granted git'}")
    if resp.status_code != 200:
        log.error("github git env: resolve answered HTTP %s", resp.status_code)
        raise ConnectorEnvError(
            f"github: the connector service answered HTTP {resp.status_code}")
    try:
        env = _clean_env(resp.json().get("env"), named=True)
    except (ValueError, AttributeError):
        raise ConnectorEnvError(
            "github: the connector service sent an unreadable answer") from None
    value = env.get(_GITHUB_ENV, "")
    if not value:
        raise ConnectorEnvError(
            "github: not connected for this account -- run `adk connectors connect github`")
    return {_GITHUB_ENV: value}


def _detail(resp: Any) -> str:
    try:
        data = resp.json()
    except ValueError:
        return ""
    if isinstance(data, dict):
        return str(data.get("detail") or data.get("error") or "")[:200]
    return ""


#: git's credential helper for github.com: reads the token FROM THE ENVIRONMENT.
GIT_CREDENTIAL_HELPER = ('!f() { test "$1" = get || exit 0; echo username=x-access-token; '
                         'echo "password=$GH_TOKEN"; }; f')


def git_child_env(token: str, env: Dict[str, str]) -> Dict[str, str]:
    """Wire ``token`` into ``env`` for git and gh, in place; return ``env``.

    gh reads ``GH_TOKEN``. git gets a credential helper for github.com through
    ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_n``: an empty helper first (drops any
    inherited github.com helper), then :data:`GIT_CREDENTIAL_HELPER`, which
    prints the token from the environment. No token on any command line,
    nothing written to disk.
    """
    env[_GITHUB_ENV] = token
    env["GH_TOKEN"] = token
    try:
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        n = 0
    helper_key = "credential.https://github.com.helper"
    for value in ("", GIT_CREDENTIAL_HELPER):
        env[f"GIT_CONFIG_KEY_{n}"] = helper_key
        env[f"GIT_CONFIG_VALUE_{n}"] = value
        n += 1
    env["GIT_CONFIG_COUNT"] = str(n)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def setup_gh_auth(token: str = "") -> dict:
    """Sandbox pairing: materialize the GitHub connector into gh's store.

    Perplexity's headline Connectors pairing, for the AitherOS sandbox: "use
    git and gh with your credentials already in place". The dev-workspace
    image bakes ``gh``; this consumes CONNECTOR_GITHUB_TOKEN (the env var a
    harness session injected) and hands it to gh's own credential store —
    after which ``gh api`` / ``git`` (via ``gh auth setup-git``) work with
    the CONNECTED ACCOUNT's permissions, with no token in any command line,
    prompt, or tool argument. Idempotent.
    """
    token = token or os.environ.get("CONNECTOR_GITHUB_TOKEN", "")
    if not token:
        return {"success": False, "reason": "no CONNECTOR_GITHUB_TOKEN in env"}

    login = subprocess.run(
        ["gh", "auth", "login", "--with-token"],
        input=token + "\n", text=True, capture_output=True,
        encoding="utf-8", errors="replace",
    )
    if login.returncode != 0:
        return {
            "success": False, "reason": "gh auth login failed",
            "error": (login.stderr or login.stdout)[:300],
        }
    setup_git = subprocess.run(["gh", "auth", "setup-git"], capture_output=True)
    return {
        "success": True,
        "git_rewrite": setup_git.returncode == 0,
        "account": _gh_whoami(),
    }


def _gh_whoami() -> str:
    try:
        who = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
        return who.stdout.strip() if who.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def setup_gh_auth_main() -> int:
    result = setup_gh_auth()
    if not result.get("success"):
        sys.stderr.write(f"[connectors] {result.get('reason')}\n")
        return 1
    print(f"[connectors] gh authenticated as {result.get('account', '?')} "
          f"(git rewrite: {result.get('git_rewrite')})")
    return 0
