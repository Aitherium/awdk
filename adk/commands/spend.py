"""``adk spend`` -- cloud LLM spend (DeepSeek and kin) from the terminal.

    adk spend                  # last 24h: total, per provider/model, top callers, balance
    adk spend --hours 168      # last 7 days
    adk spend --json           # the raw contract, for scripts

Asks the gateway MCP tool ``cloud_spend {hours}`` (MicroScheduler ``/cloud/spend``
behind it). Gateway: ``--gateway`` > ``AITHER_SPEND_MCP_URL`` > the local
``http://127.0.0.1:8182/mcp``; bearer: ``AITHER_MCP_KEY`` > ``~/.aither/session-bearer``.

Exit codes: 0 report printed; 2 could not judge (gateway down, no bearer, tool
missing, an answer that is not the contract). A failure prints the reason and
NEVER a zero-dollar report nobody measured.
"""

from __future__ import annotations

import http.client
import json
import os
import ssl
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

DEFAULT_MCP_URL = "http://127.0.0.1:8182/mcp"
BEARER_PATH = Path.home() / ".aither" / "session-bearer"
TIMEOUT_S = 20.0


class SpendUnavailableError(RuntimeError):
    """The spend report could not be produced; the message says why."""


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def validate(data: Any) -> Optional[Dict[str, Any]]:
    """*data* if it carries the contract's load-bearing fields, else None."""
    if not isinstance(data, dict) or data.get("error"):
        return None
    if _num(data.get("total_usd")) is None or not isinstance(data.get("providers"), list):
        return None
    return data


def tool_payload(result: Any) -> Any:
    """The JSON a ``tools/call`` result carries (structuredContent, else text content)."""
    if not isinstance(result, dict):
        return None
    if result.get("isError"):
        texts = [c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict)]
        return {"error": (" ".join(texts) or "tool error")[:300]}
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured:
        return structured.get("result", structured) if len(structured) == 1 else structured
    for part in result.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            try:
                return json.loads(part.get("text") or "")
            except ValueError:
                return {"error": str(part.get("text"))[:300]}
    return None


def _read_bearer() -> str:
    env = os.environ.get("AITHER_MCP_KEY", "").strip()
    if env:
        return env
    try:
        return BEARER_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _rpc_reply(ctype: str, body: str, want_id: int) -> Dict[str, Any]:
    if "text/event-stream" not in ctype:
        msg = json.loads(body)
        return msg if isinstance(msg, dict) else {}
    for line in body.replace("\r\n", "\n").split("\n"):
        if not line.startswith("data:"):
            continue
        try:
            msg = json.loads(line[5:].strip())
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("id") == want_id:
            return msg
    raise SpendUnavailableError("gateway event stream carried no reply")


def fetch_spend(hours: int, url: str, bearer: str, timeout: float = TIMEOUT_S) -> Dict[str, Any]:
    """``initialize`` + ``tools/call cloud_spend`` over MCP StreamableHTTP."""
    if not bearer:
        raise SpendUnavailableError(
            "no gateway bearer (~/.aither/session-bearer); "
            "sign in again with: adk login")
    parts = urlparse(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    session = ""

    def post(payload: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal session
        if parts.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                parts.hostname, port, timeout=timeout, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(parts.hostname, port, timeout=timeout)
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "Authorization": "Bearer " + bearer, "User-Agent": "adk-spend/1"}
        if session:
            headers["Mcp-Session-Id"] = session
        try:
            conn.request("POST", parts.path or "/mcp", body=json.dumps(payload).encode(),
                         headers=headers)
            resp = conn.getresponse()
            session = resp.getheader("Mcp-Session-Id") or session
            body = resp.read().decode("utf-8", "replace")
            if "id" not in payload:
                return {}
            if resp.status in (401, 403):
                raise SpendUnavailableError(
                    "gateway refused the bearer (HTTP {}); sign in again with: "
                    "adk login".format(resp.status))
            if resp.status != 200:
                raise SpendUnavailableError("gateway HTTP {}: {}".format(resp.status, body[:160]))
            return _rpc_reply(resp.getheader("Content-Type") or "", body, payload["id"])
        except OSError as exc:
            raise SpendUnavailableError("gateway unreachable at {}: {}".format(url, exc)) from exc
        finally:
            conn.close()

    post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                     "clientInfo": {"name": "adk-spend", "version": "1"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    msg = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "cloud_spend", "arguments": {"hours": int(hours)}}})
    if msg.get("error"):
        err = msg["error"]
        raise SpendUnavailableError(str(err.get("message") if isinstance(err, dict) else err))
    payload = tool_payload(msg.get("result"))
    data = validate(payload)
    if data is None:
        why = payload.get("error") if isinstance(payload, dict) else payload
        raise SpendUnavailableError("cloud_spend gave no spend report: {}".format(why)[:300])
    return data


# ---------------------------------------------------------------- formatting

def _usd(value: Any) -> str:
    n = _num(value)
    return "$?" if n is None else "${:,.2f}".format(n)


def _int(value: Any) -> str:
    n = _num(value)
    return "?" if n is None else "{:,.0f}".format(n)


def window_label(hours: Any) -> str:
    h = _num(hours) or 24
    return "{:.0f}h".format(h) if h < 48 else "{:.0f}d".format(h / 24)


def format_report(data: Dict[str, Any]) -> List[str]:
    """Human report lines for a contract-shaped spend dict."""
    out: List[str] = []
    gen = str(data.get("generated_at") or "")[:16].replace("T", " ")
    out.append("Cloud LLM spend -- last {}{}".format(
        window_label(data.get("window_hours")), "  (as of {}Z)".format(gen) if gen else ""))
    total = "Total  {}".format(_usd(data.get("total_usd")))
    unpriced = _num(data.get("unpriced_requests")) or 0
    if unpriced:
        total += "   + {} unpriced request(s) NOT in the total".format(_int(unpriced))
    out.append(total)
    providers = [p for p in data.get("providers") or [] if isinstance(p, dict)]
    if not providers:
        out.append("  (no cloud requests in this window)")
    for p in sorted(providers, key=lambda x: -(_num(x.get("usd")) or 0)):
        failed = _num(p.get("failed")) or 0
        out.append("")
        out.append("  {:<26} {:>10}  {:>7} req{}  {} in / {} out tok".format(
            str(p.get("provider") or "?"), _usd(p.get("usd")), _int(p.get("requests")),
            " ({} failed)".format(_int(failed)) if failed else "",
            _int(p.get("prompt_tokens")), _int(p.get("completion_tokens"))))
        models = [m for m in p.get("models") or [] if isinstance(m, dict)]
        for m in sorted(models, key=lambda x: -(_num(x.get("usd")) or 0)):
            out.append("    {:<24} {:>10}  {:>7} req  {} in / {} out tok".format(
                str(m.get("model") or "?"), _usd(m.get("usd")), _int(m.get("requests")),
                _int(m.get("prompt_tokens")), _int(m.get("completion_tokens"))))
    sources = [s for s in data.get("top_sources") or [] if isinstance(s, dict)]
    if sources:
        out.append("")
        out.append("  Top callers")
        for s in sources[:10]:
            out.append("    {:<32} {:>10}  {:>7} req  {} tok".format(
                str(s.get("source") or "?"), _usd(s.get("usd")), _int(s.get("requests")),
                _int(s.get("tokens"))))
    out.append("")
    out.extend(format_balances(data.get("balance")))
    return out


def format_balances(balance: Any) -> List[str]:
    if not isinstance(balance, dict) or not balance:
        return ["  Balance  not reported"]
    lines = []
    for name, b in sorted(balance.items()):
        if not isinstance(b, dict):
            continue
        label = "DeepSeek" if name == "deepseek" else str(name)
        if b.get("available") and _num(b.get("total_balance")) is not None:
            checked = str(b.get("checked_at") or "")[11:16]
            lines.append("  {} balance  {:.2f} {}{}".format(
                label, _num(b.get("total_balance")), b.get("currency") or "",
                "  (checked {}Z)".format(checked) if checked else ""))
        else:
            lines.append("  {} balance  unavailable{}".format(
                label, ": {}".format(str(b.get("error"))[:80]) if b.get("error") else ""))
    return lines or ["  Balance  not reported"]


# ---------------------------------------------------------------- the verb

def register_parser(sub: Any) -> None:
    p = sub.add_parser(
        "spend",
        help="Cloud LLM spend: totals, per provider/model, top callers, DeepSeek balance",
    )
    p.add_argument("--hours", type=int, default=24,
                   help="Window in hours (default 24; 168 = 7d, 720 = 30d)")
    p.add_argument("--json", action="store_true", help="Print the raw spend report JSON")
    p.add_argument("--gateway", default=None,
                   help="Gateway MCP URL (default $AITHER_SPEND_MCP_URL or the local :8182)")


def cmd_spend(args: Any, fetch: Optional[Callable[..., Dict[str, Any]]] = None,
              out: Callable[[str], None] = print) -> int:
    hours = max(1, min(int(getattr(args, "hours", 24) or 24), 24 * 90))
    url = (getattr(args, "gateway", None) or os.environ.get("AITHER_SPEND_MCP_URL")
           or DEFAULT_MCP_URL)
    try:
        data = (fetch or fetch_spend)(hours, url, _read_bearer())
    except SpendUnavailableError as exc:
        print("spend unavailable: {}".format(exc), file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        out(json.dumps(data, indent=2))
        return 0
    for line in format_report(data):
        out(line)
    return 0
