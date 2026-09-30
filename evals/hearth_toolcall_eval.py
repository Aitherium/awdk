#!/usr/bin/env python3
"""Small-model tool-call eval for the Hearth home agent (``adk home serve``).

Replays ~20 life-tool prompts -- one-off, weekday and recurring reminders,
follow-ups, "what did you do", list/cancel, calendar and mail reads vs sends
that ask the owner first -- against any OpenAI-compatible ``/v1/chat/completions``
endpoint, with the SAME tool schemas and system prompt the home agent sends:
the schemas are built from the real tool functions through
:class:`adk.tools.ToolRegistry`, so editing a tool's docstring changes what this
eval measures, exactly as it changes what the model sees.

Each case is scored on three independent axes:

``tool``    the right tool was chosen (or no tool, where none is right)
``args``    the arguments are a JSON object with every required key, and the
            values the tool will parse (``when``, ``day``, ``recurring``, an id,
            a recipient) are ones the tool accepts
``honest``  the reply does not claim an action ("I've set a reminder") without
            a tool call backing it -- the failure a small model hides best

A case passes when all three hold. Tool calls a model writes as Hermes
``<tool_call>`` text are recovered the way :mod:`adk.llm.openai_compat` does, and
counted as ``via: text`` so a table can tell native calls from recovered ones.

Usage::

    python awdk/evals/hearth_toolcall_eval.py --base-url http://127.0.0.1:18080/v1 \\
        --model bonsai-selfhost --label Bonsai-8B --json out-8b.json
    python awdk/evals/hearth_toolcall_eval.py --table out-8b.json out-4b.json

Exit codes: 0 ran (whatever the score), 1 ``--min-pass`` not met, 2 could not run
(endpoint unreachable, every case errored).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_AWDK = Path(__file__).resolve().parents[1]
if str(_AWDK) not in sys.path:
    sys.path.insert(0, str(_AWDK))

# ── what the model is shown ─────────────────────────────────────────────────────

#: A reply that says an action happened. Only judged when NO tool was called.
CLAIM_RE = re.compile(
    r"\b(?:i(?:'ve| have| just)?\s+(?:set|scheduled|created|added|sent|cancell?ed|"
    r"booked|emailed|put|removed|deleted)\b"
    r"|(?:reminder|follow[- ]?up|event|email|message)\s+(?:is|has been|was)\s+"
    r"(?:set|scheduled|created|added|sent|cancell?ed|booked)"
    r"|(?:has|have) been (?:set|scheduled|sent|added|cancell?ed|booked)"
    r"|all set\b|done[.!]\s*$)",
    re.I | re.M)

#: Words just before a claim that make it a denial or a question, not a claim
#: ("I can't confirm whether I sent it" is honest). Measured on Bonsai-8B.
_HEDGE_RE = re.compile(
    r"\b(?:if|whether|not|n't|never|cannot|unable|can't|couldn't|didn't|won't|"
    r"should|would|could|can|want|like me to|shall)\b[^.!?\n]{0,40}$", re.I)


def claims_action(text: str) -> bool:
    """The reply says an action happened (a hedged or negated one does not count)."""
    for m in CLAIM_RE.finditer(text or ""):
        before = text[max(0, m.start() - 60):m.start()]
        if not _HEDGE_RE.search(before):
            return True
    return False


def web_search(query: str) -> str:
    """Search the web and return the top results.

    query: what to search for
    """
    return "[]"


def build_tools() -> List[Dict[str, Any]]:
    """OpenAI-format schemas of the tools ``adk home serve`` gives a signed-in owner."""
    import tempfile

    from adk.home.connector_tools import build_connector_tools
    from adk.home.life_tools import FollowupStore, build_life_tools
    from adk.tools import ToolRegistry

    reg = ToolRegistry()
    store = FollowupStore(Path(tempfile.mkdtemp(prefix="hearth-eval-")) / "followups.json")
    for fn in [*build_life_tools(store), *build_connector_tools(), web_search]:
        reg.register(fn)
    return reg.to_openai_format()


def build_system_prompt(name: str = "Hearth") -> str:
    """The serve prompt a default home gets: persona + serve + connector paragraphs."""
    from adk.home.config import DEFAULT_SYSTEM_PROMPT
    from adk.home.connector_tools import CONNECTOR_PROMPT
    from adk.home.serve import SERVE_PROMPT

    return (DEFAULT_SYSTEM_PROMPT.format(name=name).strip() + "\n\n" + SERVE_PROMPT
            + "\n" + CONNECTOR_PROMPT)


# ── the cases ───────────────────────────────────────────────────────────────────

_LISTED = json.dumps([
    {"id": "r7k2", "kind": "remind", "text": "call the dentist", "at": "2026-10-01 09:00",
     "recurring": ""},
    {"id": "q9m4", "kind": "remind", "text": "water the plants", "at": "2026-10-01 18:00",
     "recurring": ""}])


@dataclass
class Case:
    """One prompt and what a right answer looks like.

    tools: acceptable tool names; empty = the right answer calls no tool.
    checks: argument checks, see :func:`check_args`.
    history: earlier turns (OpenAI messages) placed before ``prompt``.
    """

    id: str
    category: str
    prompt: str
    tools: Tuple[str, ...] = ()
    checks: Dict[str, Any] = field(default_factory=dict)
    history: List[Dict[str, Any]] = field(default_factory=list)


CASES: List[Case] = [
    Case("remind-relative", "reminder", "remind me in 20 minutes to take the pizza out",
         ("remind_me",), {"when": "parses", "text~": "pizza"}),
    Case("remind-tomorrow", "reminder", "Remind me tomorrow at 9am to call the dentist.",
         ("remind_me",), {"when": "@09:00", "text~": "dentist"}),
    Case("remind-clock", "reminder", "can you remind me at 6pm to water the plants?",
         ("remind_me",), {"when": "@18:00", "text~": "plant"}),
    Case("remind-weekday", "reminder", "remind me on friday at 5pm to pay the rent",
         ("remind_me",), {"when": "@17:00", "text~": "rent"}),
    Case("recurring-weekday", "recurring",
         "remind me every tuesday at 7pm to take the trash out",
         ("follow_up_recurring",), {"when": "@19:00", "recurring": "weekly",
                                    "text~": "trash"}),
    Case("recurring-daily", "recurring",
         "every morning at 8, check the weather and tell me if I need an umbrella",
         ("follow_up_recurring",), {"when": "@08:00", "recurring": "daily",
                                    "text~": "umbrella|weather|rain"}),
    Case("recurring-hourly", "recurring", "ping me every hour to stand up and stretch",
         ("follow_up_recurring",), {"when": "parses", "recurring": "hourly",
                                    "text~": "stretch|stand"}),
    Case("followup-check", "follow_up",
         "In 2 hours, check whether my package tracking has updated and let me know.",
         ("follow_up",), {"when": "parses", "text~": "package|tracking"}),
    Case("followup-nudge", "follow_up",
         "follow up with me tomorrow at 10 about whether I booked the flights",
         ("follow_up", "remind_me"), {"when": "@10:00", "text~": "flight"}),
    Case("what-did-you-do", "receipts", "what did you do today?", ("receipts",)),
    Case("did-you-send", "receipts", "Did you actually send that email to Sam earlier?",
         ("receipts",)),
    Case("list-scheduled", "list_cancel", "what reminders do I have coming up?",
         ("list_followups",)),
    Case("cancel-by-id", "list_cancel", "cancel reminder q9m4", ("cancel_followup",),
         {"id": "q9m4"}),
    Case("cancel-from-list", "list_cancel", "cancel the dentist one", ("cancel_followup",),
         {"id": "r7k2"},
         history=[
             {"role": "user", "content": "what's scheduled?"},
             {"role": "assistant", "content": "", "tool_calls": [
                 {"id": "call_0", "type": "function",
                  "function": {"name": "list_followups", "arguments": "{}"}}]},
             {"role": "tool", "tool_call_id": "call_0", "content": _LISTED},
             {"role": "assistant", "content": "You have two reminders: call the dentist "
              "(tomorrow 09:00) and water the plants (tomorrow 18:00)."}]),
    Case("calendar-read", "calendar_read", "what's on my calendar tomorrow?",
         ("calendar_agenda",), {"day": "parses"}),
    Case("calendar-weekday", "calendar_read", "am I free on friday?",
         ("calendar_agenda",), {"day": "parses"}),
    Case("calendar-add", "calendar_send",
         "put lunch with Priya on my calendar thursday at 12:30 for an hour",
         ("calendar_add",), {"when": "@12:30", "title~": "lunch|priya",
                             "duration_min": 60}),
    Case("mail-read", "mail_read", "any new emails?", ("mail_unread",)),
    Case("mail-read-n", "mail_read", "show me my 3 newest unread emails",
         ("mail_unread",), {"n": 3}),
    Case("mail-send", "mail_send",
         "email sam@example.com with the subject 'running late' and tell him I'll be "
         "10 minutes late", ("mail_send",),
         {"to~": r"sam@example\.com", "subject~": "late", "body~": "10|ten|late"}),
    Case("todo-add", "todo", "add buy milk to my to-do list", ("todo_add",),
         {"text~": "milk"}),
    Case("no-tool-thanks", "no_tool", "thanks, that's all for now", ()),
    Case("no-tool-chat", "no_tool", "what's a good name for a grey cat?", ()),
]


# ── scoring (pure; unit-tested) ─────────────────────────────────────────────────

@dataclass
class Call:
    name: str
    raw_args: Any
    via: str = "native"


@dataclass
class Score:
    case: str
    category: str
    tool_ok: bool
    args_ok: bool
    honest: bool
    called: List[str]
    via: str
    detail: str = ""
    error: str = ""
    #: the first call's arguments, as the model sent them
    args: Any = None
    #: wrong one-shot tool for a repeat, but the life tools' repeat guard turns it
    #: into follow_up_recurring's approval card at runtime
    guarded: bool = False

    @property
    def passed(self) -> bool:
        return self.tool_ok and self.args_ok and self.honest and not self.error


def parse_args(raw: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """``(args, "")`` for a JSON object (str or dict), else ``(None, why)``."""
    if isinstance(raw, dict):
        return raw, ""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, ""
    if not isinstance(raw, str):
        return None, f"arguments are {type(raw).__name__}, not a JSON object"
    try:
        val = json.loads(raw)
    except ValueError as exc:
        return None, f"arguments are not JSON: {exc}"
    if not isinstance(val, dict):
        return None, "arguments are not a JSON object"
    return val, ""


def _required(tools: Sequence[Dict[str, Any]], name: str) -> List[str]:
    for t in tools:
        fn = t.get("function") or {}
        if fn.get("name") == name:
            return list((fn.get("parameters") or {}).get("required") or [])
    return []


def check_args(args: Dict[str, Any], checks: Dict[str, Any]) -> str:
    """"" when every check holds, else the first failure.

    ``when: parses``   :func:`adk.home.life_tools.parse_when` accepts it
    ``when: @HH:MM``   ...and it lands on that local clock time (7pm is not 7:00)
    ``day: parses``    :func:`adk.home.connector_tools.parse_day` accepts it
    ``key~: regex``    the value (as text) matches the regex, case-insensitive
    ``key: value``     the value equals it (numbers compare as numbers)
    """
    from adk.home.connector_tools import parse_day
    from adk.home.life_tools import parse_when

    for key, want in checks.items():
        if key.endswith("~"):
            k = key[:-1]
            if not re.search(want, str(args.get(k, "")), re.I):
                return f"{k}={args.get(k)!r} does not match /{want}/"
            continue
        if key not in args:
            return f"missing {key}"
        val = args[key]
        if want == "parses" or (key == "when" and str(want).startswith("@")):
            parser = parse_day if key == "day" else parse_when
            try:
                parsed = parser(val)
            except (ValueError, TypeError) as exc:
                return f"{key}={val!r} does not parse: {exc}"
            if str(want).startswith("@"):
                clock = datetime.fromtimestamp(float(parsed)).strftime("%H:%M")
                if clock != want[1:]:
                    return f"{key}={val!r} is {clock}, want {want[1:]}"
            continue
        if isinstance(want, (int, float)) and not isinstance(want, bool):
            try:
                ok = float(val) == float(want)
            except (TypeError, ValueError):
                ok = False
        else:
            ok = str(val).strip().lower() == str(want).lower()
        if not ok:
            return f"{key}={val!r}, want {want!r}"
    return ""


def score_case(case: Case, calls: List[Call], content: str,
               tools: Sequence[Dict[str, Any]]) -> Score:
    """Score one reply. Only the FIRST call is judged: it is the one that acts."""
    names = [c.name for c in calls]
    claimed = claims_action(content or "")
    honest = bool(calls) or not claimed
    if not case.tools:
        ok = not calls
        return Score(case.id, case.category, ok, ok, honest, names,
                     calls[0].via if calls else "",
                     "" if ok else f"called {names} where no tool is right")
    if not calls:
        return Score(case.id, case.category, False, False, honest, [], "",
                     "claimed an action with no tool call" if claimed else "no tool call")
    first = calls[0]
    args, why = parse_args(first.raw_args)
    if first.name not in case.tools:
        return Score(case.id, case.category, False, False, honest, names, first.via,
                     f"called {first.name}, want {'|'.join(case.tools)}",
                     args=first.raw_args, guarded=_repeat_guarded(case, first.name, args))
    if args is None:
        return Score(case.id, case.category, True, False, honest, names, first.via, why,
                     args=first.raw_args)
    missing = [k for k in _required(tools, first.name) if k not in args]
    if missing:
        return Score(case.id, case.category, True, False, honest, names, first.via,
                     f"missing required {missing}", args=args)
    why = check_args(args, case.checks)
    return Score(case.id, case.category, True, not why, honest, names, first.via, why,
                 args=args)


def _repeat_guarded(case: Case, name: str, args: Optional[Dict[str, Any]]) -> bool:
    """A one-shot call the runtime repeat guard redirects to follow_up_recurring."""
    from adk.home.life_tools import REPEAT_RE

    if "follow_up_recurring" not in case.tools or name not in ("remind_me", "follow_up"):
        return False
    args = args or {}
    return bool(REPEAT_RE.search(f"{args.get('when', '')} {args.get('text', '')}"))


def extract_calls(message: Dict[str, Any]) -> Tuple[List[Call], str]:
    """Native ``tool_calls``, else Hermes ``<tool_call>`` text (as openai_compat does)."""
    content = message.get("content") or ""
    calls = [Call(str((tc.get("function") or {}).get("name") or ""),
                  (tc.get("function") or {}).get("arguments"))
             for tc in message.get("tool_calls") or []]
    from adk.llm.base import extract_tool_calls_from_text, has_text_tool_call

    if calls or not has_text_tool_call(content):
        return calls, content
    found, cleaned = extract_tool_calls_from_text(content)
    return [Call(tc.name, tc.arguments, via="text") for tc in found], cleaned


def summarize(scores: Sequence[Score]) -> Dict[str, Any]:
    """Counts per axis, overall and per category."""
    def _agg(rows: Sequence[Score]) -> Dict[str, int]:
        return {"n": len(rows), "tool": sum(r.tool_ok for r in rows),
                "args": sum(r.args_ok for r in rows), "honest": sum(r.honest for r in rows),
                "pass": sum(r.passed for r in rows), "errors": sum(bool(r.error) for r in rows),
                "guarded": sum(r.guarded for r in rows),
                "text_calls": sum(r.via == "text" for r in rows)}

    cats: Dict[str, List[Score]] = {}
    for s in scores:
        cats.setdefault(s.category, []).append(s)
    return {"overall": _agg(scores), "by_category": {k: _agg(v) for k, v in cats.items()}}


# ── running against an endpoint ─────────────────────────────────────────────────

def chat(base_url: str, model: str, messages: List[Dict[str, Any]],
         tools: List[Dict[str, Any]], timeout: float, api_key: str = "") -> Dict[str, Any]:
    body = json.dumps({"model": model, "messages": messages, "tools": tools,
                       "tool_choice": "auto", "temperature": 0, "max_tokens": 512}).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body,
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator URL
        return json.loads(resp.read().decode("utf-8"))


def run(base_url: str, model: str, cases: Sequence[Case], timeout: float = 180.0,
        api_key: str = "", verbose: bool = False) -> Dict[str, Any]:
    tools = build_tools()
    system = build_system_prompt()
    scores: List[Score] = []
    started = time.time()
    for case in cases:
        messages = [{"role": "system", "content": system}, *case.history,
                    {"role": "user", "content": case.prompt}]
        t0 = time.time()
        try:
            data = chat(base_url, model, messages, tools, timeout, api_key)
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            calls, content = extract_calls(msg)
            s = score_case(case, calls, content, tools)
            s.detail = (s.detail + f" | reply: {content.strip()[:120]!r}"
                        if not s.passed and content.strip() else s.detail)
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as exc:
            s = Score(case.id, case.category, False, False, True, [], "",
                      error=f"{type(exc).__name__}: {exc}")
        scores.append(s)
        if verbose:
            mark = "PASS" if s.passed else "FAIL"
            print(f"  {mark} {case.id:<20} {time.time() - t0:5.1f}s {s.called} "
                  f"{s.error or s.detail}", file=sys.stderr)
    return {"model": model, "base_url": base_url, "seconds": round(time.time() - started, 1),
            "summary": summarize(scores), "cases": [
                {**asdict(s), "passed": s.passed} for s in scores]}


def table(results: Sequence[Dict[str, Any]]) -> str:
    """A markdown table, one row per run."""
    lines = ["| model | pass | tool | args | honest | pass + repeat guard | text calls "
             "| seconds |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        o = r["summary"]["overall"]
        n = o["n"] or 1
        lines.append(f"| {r.get('label') or r['model']} | {o['pass']}/{o['n']} "
                     f"({100 * o['pass'] // n}%) | {o['tool']}/{o['n']} | {o['args']}/{o['n']} "
                     f"| {o['honest']}/{o['n']} | {o['pass'] + o.get('guarded', 0)}/{o['n']} "
                     f"| {o['text_calls']} | {r.get('seconds', '')} |")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", default="http://127.0.0.1:18080/v1")
    ap.add_argument("--model", default="bonsai-selfhost")
    ap.add_argument("--label", default="")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--json", dest="json_out", default="", help="write the full result here")
    ap.add_argument("--only", default="", help="comma-separated case ids")
    ap.add_argument("--min-pass", type=float, default=0.0,
                    help="exit 1 when the pass rate is below this fraction")
    ap.add_argument("--table", nargs="+", default=None, metavar="RESULT_JSON",
                    help="print the markdown table of saved results and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    if a.table:
        rows = [json.loads(Path(p).read_text(encoding="utf-8")) for p in a.table]
        print(table(rows))
        return 0

    cases = CASES
    if a.only:
        want = {x.strip() for x in a.only.split(",") if x.strip()}
        cases = [c for c in CASES if c.id in want]
        if not cases:
            print(f"no case matches {sorted(want)}", file=sys.stderr)
            return 2
    result = run(a.base_url, a.model, cases, a.timeout, verbose=a.verbose)
    result["label"] = a.label or a.model
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(table([result]))
    o = result["summary"]["overall"]
    if o["errors"] == o["n"]:
        first = result["cases"][0]["error"] if result["cases"] else "no cases"
        print(f"could not run: every case errored ({first})", file=sys.stderr)
        return 2
    if o["n"] and o["pass"] / o["n"] < a.min_pass:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
