"""Your PLATFORM Hearth from a terminal -- the gateway's ``hearth_*`` MCP tools.

``adk home serve`` is the Hearth on your own machine. The platform runs one too, per
signed-in account, as MCP tools on the tool gateway (``mcp_hearth.py``): reminders,
approval codes, the taint rule and a signed receipt log, all enforced there. This module
is the thin terminal side of it, used by awsh ``/hearth`` when no local serve is running
(or on ``/hearth cloud ...``):

    status                    your Hearth at a glance, with the codes waiting for you
    reminders                 the reminder list
    due                       deliver reminders that are now due
    receipts [n]              the last n receipts + the verify verdict (0/1/2)
    yes <code> | no <code>    YOUR answer to an approval card
    remind <when> -- <text>   a one-time reminder ("in 10 minutes -- stretch")

Identity is the gateway bearer (``~/.aither/session-bearer``, or ``AITHER_MCP_KEY``) and
nothing else: no argument names a user, so a terminal cannot open another person's home.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional

USAGE = __doc__ or "hearth"

#: The verbs this module answers; awsh falls back here for exactly these.
VERBS = frozenset({"status", "reminders", "due", "receipts", "yes", "no", "remind"})

Caller = Callable[[str, Dict[str, Any]], Dict[str, Any]]


def call_gateway(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """One ``tools/call`` on the gateway. Every failure is a dict with ``error``, never a raise."""
    from adk.client._gateway_mcp import GatewayMCPClient

    async def go() -> Dict[str, Any]:
        client = GatewayMCPClient()
        if not client.api_key:
            return {"error": "no gateway credential: sign in (adk home signin) or mint "
                             "~/.aither/session-bearer"}
        # ping() runs the initialize handshake; call_tool needs its session id.
        if not await client.ping():
            return {"error": f"could not reach the tool gateway at {client.gateway_url}/mcp"}
        res = await client.call_tool(name, arguments)
        if res.get("error"):
            return {"error": f"{res['error']}: {res.get('message', '')}".rstrip(": ")}
        try:
            data = json.loads(res.get("text") or "")
        except ValueError:
            return {"error": f"{name} returned non-JSON"}
        return data if isinstance(data, dict) else {"error": f"{name} returned non-object JSON"}

    try:
        return asyncio.run(go())
    except RuntimeError as exc:           # already inside a running loop, or the loop died
        return {"error": f"call failed: {exc}"}


def _render_status(d: Dict[str, Any]) -> str:
    lines = [f"hearth {d.get('home')} at {d.get('time')}: {d.get('reminders', 0)} reminder(s), "
             f"{d.get('unread_mail', 0)} unread, {d.get('outbox', 0)} in the outbox"]
    if d.get("tainted_by"):
        lines.append(f"untrusted content read ({', '.join(d['tainted_by'])}): "
                     "outbound actions ask first")
    pending = d.get("pending_approvals") or []
    for p in pending:
        lines.append(f"  waiting for you: {p.get('summary')}  -> /hearth yes {p.get('code')} "
                     f"| /hearth no {p.get('code')}")
    if not pending:
        lines.append("nothing is waiting for your OK")
    return "\n".join(lines)


def _render_reminders(d: Dict[str, Any]) -> str:
    rows = d.get("reminders") or []
    if not rows:
        return "(no reminders)"
    return "\n".join(f"{r.get('at')}  {r.get('text')}"
                     f"{' (' + r['every'] + ')' if r.get('every') else ''}"
                     f"{'  [delivered]' if r.get('delivered') else ''}" for r in rows)


def _render_due(d: Dict[str, Any]) -> str:
    rows = d.get("delivered") or []
    if not rows:
        return f"{d.get('time')}: nothing due"
    return "\n".join(f"due {r.get('at')}: {r.get('text')}" for r in rows)


def _render_receipts(d: Dict[str, Any]) -> str:
    v = d.get("verify") or {}
    verdict = {0: "intact", 1: "BROKEN", 2: "cannot judge"}.get(v.get("exit"), "?")
    lines = [f"receipts: {verdict} -- {v.get('reason', '')}".rstrip(" -")]
    for r in d.get("rows") or []:
        lines.append(f"#{r.get('seq')} {r.get('kind')} {r.get('name')} [{r.get('approval')}]")
    if len(lines) == 1:
        lines.append("(no receipts yet)")
    return "\n".join(lines)


def _render_approve(d: Dict[str, Any]) -> str:
    if not d.get("ran"):
        return "declined; nothing ran"
    return f"ran {d.get('tool')}: {json.dumps(d.get('result'))[:400]}"


def _render_remind(d: Dict[str, Any]) -> str:
    return f"reminder {d.get('id')} set for {d.get('at')}"


def run_verb(args: List[str], call: Optional[Caller] = None) -> str:
    """Run one platform-Hearth verb and return what to print (``call`` defaults to the gateway)."""
    if not args or args[0] in ("help", "-h", "--help"):
        return USAGE
    verb, rest = args[0], args[1:]
    if verb == "status" and not rest:
        name, arguments, render = "hearth_status", {}, _render_status
    elif verb == "reminders" and not rest:
        name, arguments, render = "hearth_reminders", {}, _render_reminders
    elif verb == "due" and not rest:
        name, arguments, render = "hearth_due", {}, _render_due
    elif verb == "receipts" and len(rest) <= 1:
        raw = rest[0] if rest else "10"
        if not raw.isdigit():
            return "hearth: usage: /hearth receipts [n]"
        name, render = "hearth_receipts", _render_receipts
        arguments = {"n": int(raw), "verify": True}
    elif verb in ("yes", "no") and len(rest) == 1:
        name, render = "hearth_approve", _render_approve
        arguments = {"code": rest[0], "allow": verb == "yes"}
    elif verb == "remind" and "--" in rest:
        cut = rest.index("--")
        when, text = " ".join(rest[:cut]).strip(), " ".join(rest[cut + 1:]).strip()
        if not when or not text:
            return "hearth: usage: /hearth remind <when> -- <text>"
        name, render = "hearth_remind", _render_remind
        arguments = {"when": when, "text": text}
    else:
        return f"hearth: unknown platform verb {' '.join(args)!r}\n\n{USAGE}"
    data = (call or call_gateway)(name, arguments)
    if data.get("error"):
        return f"hearth: {data['error']}"
    return render(data)
