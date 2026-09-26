"""
WebMCP Design Studio plugin for AitherShell
===========================================

The studio is a static browser app (studio.aitherium.com). Its six design tools live
IN THE TAB (WebMCP); a remote agent reaches them through AitherStudioPair: the open
tab mints a 6-char pairing code, and the tab's live tool roster is re-exposed as an
MCP Streamable HTTP server at ``<PAIR_HOST>/api/mcp/<CODE>`` (30-min TTL, one tab).

This verb is the terminal side of that handshake: it turns the code the tab shows
into the exact connector URL / ``claude mcp add`` line, and can probe that the code
opens a real MCP session before the user pastes it anywhere. It holds no state and
mints nothing -- the TAB owns the pairing.

Usage:
    /studio                  -- where the studio is and how pairing works
    /studio open             -- open the studio in the default browser
    /studio connect <CODE>   -- connector URL + `claude mcp add` line for a tab's code
    /studio check <CODE>     -- probe that the code opens an MCP session

Aliases: /webmcp
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from adk.shell.plugins import SlashCommand

STUDIO_URL = "https://studio.aitherium.com/"
#: The pairing relay is served by the studio's preview origin, not by the Pages
#: origin (studio.aitherium.com is static and answers no /api/).
PAIR_HOST = "https://studio-preview.aitherium.com"
_CODE = re.compile(r"^[A-Za-z0-9]{6}$")
#: The edge rejects urllib's default User-Agent, so send a real one.
_UA = "adk-shell-studio/1.0 (+https://aitherium.com)"

Poster = Callable[[str, dict], Tuple[int, bytes]]


def mcp_url(code: str) -> str:
    """Connector URL for a pairing code. Raises ValueError on a malformed code."""
    code = (code or "").strip()
    if not _CODE.match(code):
        raise ValueError(
            f"a pairing code is 6 letters/digits as the studio tab shows it, got {code!r}")
    return f"{PAIR_HOST}/api/mcp/{code}"


def _post(url: str, body: dict) -> Tuple[int, bytes]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "User-Agent": _UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:  # a 4xx is a verdict about the code, not a crash
        return e.code, e.read()


def check_code(code: str, post: Optional[Poster] = None) -> Tuple[bool, str]:
    """(ok, message). ok only when MCP `initialize` against the code returns 200."""
    url = mcp_url(code)
    post = post or _post
    try:
        status, body = post(url, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "adk-shell-studio", "version": "1"}},
        })
    except Exception as exc:  # noqa: BLE001 - unreachable is a message, never a pass
        return False, f"[x] cannot reach {PAIR_HOST}: {type(exc).__name__}: {exc}"
    text = body.decode("utf-8", "replace")
    if status == 200:
        return True, f"[ok] code {code} opens an MCP session at {url}"
    if "jsonrpc" in text:
        return False, (f"[x] the relay answered but refused code {code} (HTTP {status}): "
                       f"{text[:160]} -- the tab may have closed or the 30-min code expired; "
                       f"re-pair from the tab")
    return False, (f"[x] {url} returned HTTP {status} without a JSON-RPC body -- the "
                   f"pairing relay is not reachable behind the edge")


class WebMCPDesignStudioPlugin(SlashCommand):
    name = "studio"
    description = "WebMCP Design Studio -- turn a tab's pairing code into an MCP connector"
    aliases = ["webmcp"]

    def __init__(self, post: Optional[Poster] = None,
                 opener: Optional[Callable[[str], Any]] = None):
        super().__init__(
            name="studio",
            description="WebMCP Design Studio -- turn a tab's pairing code into an MCP connector",
            aliases=["webmcp"],
        )
        self._post = post
        self._opener = opener

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args or args[0].lower() in ("help", "info"):
            return self._help()
        sub, rest = args[0].lower(), args[1:]
        if sub == "open":
            opener = self._opener
            if opener is None:
                import webbrowser
                opener = webbrowser.open
            opener(STUDIO_URL)
            return f"Opened {STUDIO_URL} -- click 'Connect your own agent' to get a pairing code."
        if sub in ("connect", "check"):
            if not rest:
                return f"Usage: /studio {sub} <CODE>   (the 6-char code the studio tab shows)"
            try:
                url = mcp_url(rest[0])
            except ValueError as exc:
                return f"[x] {exc}"
            if sub == "check":
                return check_code(rest[0].strip(), self._post)[1]
            return (
                f"MCP connector URL (Streamable HTTP): {url}\n"
                f"Claude Code:  claude mcp add --transport http aither-studio {url}\n"
                f"ChatGPT / Claude.ai: add a custom connector with that URL.\n"
                f"The code lives 30 minutes and pairs ONE open tab; keep the tab open."
            )
        return f"Unknown sub-command: {sub}\n\n" + self._help()

    def _help(self) -> str:
        return (
            f"WebMCP Design Studio -- {STUDIO_URL}\n"
            "  The design tools run in your browser tab. To let your own agent drive them,\n"
            "  click 'Connect your own agent' in the tab and bring the 6-char code here.\n\n"
            "  /studio open             Open the studio in your browser\n"
            "  /studio connect <CODE>   Connector URL + `claude mcp add` line\n"
            "  /studio check <CODE>     Probe that the code opens an MCP session\n\n"
            "Aliases: /webmcp"
        )
