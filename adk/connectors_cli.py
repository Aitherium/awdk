"""``adk connectors`` -- your connected accounts, from a terminal.

The CLI door to the connect-once plane. It never starts an OAuth dance and
never prints a token: connecting happens in the Connections window (one
connect UI for every client), and this door lists, deep-links and grants.

    adk connectors list                      every connector and its status
    adk connectors status [github]           one (exit 1 when not connected)
    adk connectors connect github            open the Connections window on it
    adk connectors grant github --agent demiurge --cap git [--revoke]

There is deliberately no ``gh-auth`` verb: ``gh auth login --with-token`` and
``gh auth setup-git`` persist the token in gh's store and the global
``~/.gitconfig``, where it outlives the session, the grant and any revocation
and reaches every later child. An opted-in harness child already has
``GH_TOKEN`` and an env-only git credential helper.

Identity is the sign-in ``adk login`` saved (``~/.aither/config.json``), or a
bearer in the environment. Scope is decided server-side from that bearer.

Exit codes: 0 done, 1 refused / not connected / failed, 2 could not ask (no
sign-in, service unreachable, an answer that cannot be read).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from typing import Any, Callable, Dict, Optional, Tuple

from adk import connectors as _cn

#: The portal API that serves ``/me/connectors`` (Veil BFF over Genesis
#: ``/connectors/mine``). ``AITHER_PORTAL_API_URL`` overrides it.
DEFAULT_PORTAL_API = "https://api.aitherium.com/api"
CAPABILITIES = ("read", "write", "git")
_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_AGENT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def portal_api() -> str:
    return (os.environ.get("AITHER_PORTAL_API_URL") or DEFAULT_PORTAL_API).strip().rstrip("/")


def connect_url(connector: str = "") -> str:
    """The Connections window, opened on ``connector`` when one is named."""
    base = (os.environ.get("AITHER_CONNECT_URL") or _cn.CONNECTIONS_URL).strip()
    if not connector:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}connect={urllib.parse.quote(connector, safe='')}"


def _valid_id(value: str) -> str:
    value = (value or "").strip().lower()
    if not _ID_RE.match(value):
        raise argparse.ArgumentTypeError(f"not a connector id: {value!r}")
    return value


def _valid_agent(value: str) -> str:
    value = (value or "").strip()
    if not _AGENT_RE.match(value):
        raise argparse.ArgumentTypeError(f"not an agent id: {value!r}")
    return value


# ── HTTP (one door so tests swap it) ─────────────────────────────────────────

Transport = Callable[[str, str, Dict[str, str], Optional[dict]], Tuple[int, Any]]


def _http(method: str, url: str, headers: Dict[str, str],
          body: Optional[dict] = None) -> Tuple[int, Any]:
    """``(status, parsed json or None)``; status 0 = unreachable."""
    import httpx

    from adk._tls import tls_verify

    try:
        with httpx.Client(timeout=15.0, verify=tls_verify()) as client:
            resp = client.request(method, url, headers=headers, json=body)
    except (httpx.HTTPError, OSError):
        return 0, None
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


def _ask(method: str, path: str, transport: Optional[Transport],
         body: Optional[dict] = None) -> Tuple[int, Any]:
    bearer = _cn._bearer()
    if not bearer:
        return -1, None
    headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
    return (transport or _http)(method, f"{portal_api()}{path}", headers, body)


def _no_ask(status: int, out: Callable[[str], None]) -> int:
    if status == -1:
        out("Not signed in. Run `adk login` first.")
    elif status == 0:
        out(f"Could not reach {portal_api()}. Nothing was changed.")
    elif status in (401, 403):
        out(f"The portal refused this sign-in ({status}). Run `adk login` again.")
        return 1
    else:
        out(f"The portal answered HTTP {status}.")
    return 2


# ── verbs ────────────────────────────────────────────────────────────────────

def _rows(transport: Optional[Transport], out: Callable[[str], None]):
    status, data = _ask("GET", "/me/connectors", transport)
    if status != 200:
        return None, _no_ask(status, out)
    rows = data.get("connectors") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        out("The portal sent an answer this version cannot read.")
        return None, 2
    # Rebuilt field by field: nothing the server adds later (a masked hint, say)
    # reaches a terminal or a --json pipe through this door.
    return [{k: r.get(k) for k in _FIELDS} for r in rows if isinstance(r, dict)], 0


_FIELDS = ("connector", "display_name", "status", "account", "available", "connected_at")


def _line(row: dict) -> str:
    status = str(row.get("status") or "not_connected")
    account = str(row.get("account") or "")
    avail = row.get("available")
    note = "" if avail is not False or status != "not_connected" else "  (not set up yet)"
    who = f"  {account}" if account else ""
    return f"  {str(row.get('connector', '?')):<18} {status:<16}{who}{note}"


def cmd_list(args: argparse.Namespace, transport: Optional[Transport] = None,
             out: Callable[[str], None] = print) -> int:
    rows, code = _rows(transport, out)
    if rows is None:
        return code
    if getattr(args, "json", False):
        out(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        out(_line(row))
    if not any(r.get("status") == "connected" for r in rows):
        out(f"\n  Nothing connected yet: adk connectors connect github  ({connect_url()})")
    return 0


def cmd_status(args: argparse.Namespace, transport: Optional[Transport] = None,
               out: Callable[[str], None] = print) -> int:
    wanted = getattr(args, "connector", "") or ""
    if not wanted:
        return cmd_list(args, transport, out)
    rows, code = _rows(transport, out)
    if rows is None:
        return code
    row = next((r for r in rows if r.get("connector") == wanted), None)
    if row is None:
        out(f"Unknown connector {wanted!r}.")
        return 1
    if getattr(args, "json", False):
        out(json.dumps(row, indent=2))
    else:
        out(_line(row))
    if row.get("status") == "connected":
        return 0
    out(f"  Connect it: adk connectors connect {wanted}  ({connect_url(wanted)})")
    return 1


def cmd_connect(args: argparse.Namespace, opener: Optional[Callable[[str], Any]] = None,
                out: Callable[[str], None] = print) -> int:
    url = connect_url(args.connector)
    out(f"Connect {args.connector} in the Connections window:\n  {url}")
    if not getattr(args, "no_open", False):
        try:
            if opener is None:
                import webbrowser

                opener = webbrowser.open
            opener(url)
        except Exception as exc:  # noqa: BLE001 -- printing the link is the fallback
            out(f"  (could not open a browser: {type(exc).__name__}; open the link above)")
    out(f"Then check: adk connectors status {args.connector}")
    return 0


def cmd_grant(args: argparse.Namespace, transport: Optional[Transport] = None,
              out: Callable[[str], None] = print) -> int:
    granted = not getattr(args, "revoke", False)
    body = {"agent": args.agent, "capability": args.cap, "granted": granted}
    status, data = _ask("PUT", f"/me/connectors/{args.connector}/grants", transport, body)
    if status in (404, 405, 501):
        # The per-agent grant store is not on this server yet. Say so; never
        # report a grant that was not recorded.
        out(f"Per-agent grants are not available on {portal_api()} yet "
            f"(HTTP {status}). Nothing was changed.")
        return 1
    if status not in (200, 201, 204):
        detail = data.get("detail") if isinstance(data, dict) else ""
        code = _no_ask(status, out)
        if detail:
            out(f"  {str(detail)[:200]}")
        return code
    verb = "granted" if granted else "revoked"
    out(f"{args.agent}: {args.connector}:{args.cap} {verb}.")
    return 0


# ── argparse ─────────────────────────────────────────────────────────────────

def register_parser(sub: Any) -> None:
    p = sub.add_parser(
        "connectors",
        help="Your connected accounts (GitHub, Google, Microsoft): list, connect, grant",
        description="List and check your connected accounts, open the Connections window "
        "to connect one, and grant an agent use of one. Sign in first with 'adk login'.",
    )
    verbs = p.add_subparsers(dest="connectors_command")
    ls = verbs.add_parser("list", help="Every connector and whether it is connected")
    ls.add_argument("--json", action="store_true")
    st = verbs.add_parser("status", help="One connector (exit 1 when not connected)")
    st.add_argument("connector", nargs="?", default="", type=_valid_id)
    st.add_argument("--json", action="store_true")
    cn = verbs.add_parser("connect", help="Open the Connections window on a connector")
    cn.add_argument("connector", type=_valid_id)
    cn.add_argument("--no-open", action="store_true", help="Print the link only")
    gr = verbs.add_parser("grant", help="Let an agent use a connector (read, write or git)")
    gr.add_argument("connector", type=_valid_id)
    gr.add_argument("--agent", required=True, type=_valid_agent)
    gr.add_argument("--cap", required=True, choices=CAPABILITIES)
    gr.add_argument("--revoke", action="store_true", help="Remove the grant instead")


def cmd_connectors(args: argparse.Namespace) -> int:
    verb = getattr(args, "connectors_command", None) or "list"
    if verb == "list":
        return cmd_list(args)
    if verb == "status":
        return cmd_status(args)
    if verb == "connect":
        return cmd_connect(args)
    if verb == "grant":
        return cmd_grant(args)
    sys.stderr.write(f"unknown verb {verb!r}\n")
    return 2
