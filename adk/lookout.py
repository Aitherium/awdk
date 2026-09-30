"""Lookout — watch team channels and step in without being @mentioned.

``adk gateway`` answers when someone talks TO the agent. Lookout is the
other half: it reads the streams a team already works in (Slack channels, the
Linear triage queue, any JSONL event stream), decides per item whether the
agent's help is wanted, and — only when it is — queues a real agent run through
awrun. Every verdict, engage or skip, lands in an append-only ledger so the
owner can see what it did and, just as important, what it chose to leave alone.

Knowing when to leave people alone is the product, so the judge is layered:

1. **Hard skips** that no score overrides: our own/bot messages, an explicit
   opt-out ("no bot", "@lookout stop"), a thread we already engaged, a
   Linear issue someone already owns.
2. **Signals**: an error or traceback, a help request with nobody answering,
   a broken build, a bug-shaped triage issue. Social chatter scores negative.
3. **Budgets**: at most one engagement per thread, and a per-channel hourly cap,
   so a noisy incident does not become a noisy bot.

An optional ``llm_judge`` callable can confirm or veto a borderline heuristic
verdict; the heuristic stays the floor so the loop works with no model at all.

Stdlib only (urllib + json) so it installs everywhere awdk does. Credentials are
read from the environment (``SLACK_BOT_TOKEN``, ``LINEAR_API_KEY``), never from
flags, so they do not land in shell history.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol

STATE_DIR = Path(os.environ.get("AITHER_LOOKOUT_HOME", Path.home() / ".aither" / "lookout"))
HTTP_TIMEOUT = 15

# ── event model ─────────────────────────────────────────────────────────────


@dataclass
class Event:
    """One item from a watched stream, normalised across sources."""

    source: str  # "slack" | "linear" | "jsonl"
    id: str  # unique within the source
    channel: str  # slack channel id, linear team key, stream name
    text: str
    author: str = ""
    thread: str = ""  # the conversation this belongs to; engagement is per thread
    url: str = ""
    ts: float = 0.0
    is_bot: bool = False
    replies: int = 0  # human replies already in the thread
    assignee: str = ""  # linear: someone already owns it
    age_s: float = 0.0  # how long it has sat unanswered

    @property
    def thread_key(self) -> str:
        return f"{self.source}:{self.channel}:{self.thread or self.id}"


@dataclass
class Verdict:
    engage: bool
    score: float
    reasons: List[str] = field(default_factory=list)


# ── the judge ───────────────────────────────────────────────────────────────

_OPT_OUT = re.compile(r"\b(no bots?|no ai|bot,? stop|lookout,? (stop|off|leave))\b", re.I)
_ERROR = re.compile(
    r"(traceback \(most recent call last\)|\bexception\b|\berror:|\bstack ?trace\b|"
    r"\bsegfault\b|\bpanic:|\bexit (code|status) [1-9]|\bfailed\b|\b5\d\d\b)",
    re.I,
)
_HELP = re.compile(
    r"(\bcan (someone|anyone|somebody)\b|\bdoes anyone know\b|\bany ?one (seen|know)\b|"
    r"\bhow do (i|we)\b|\bwhy (is|does|would)\b|\bhelp\b|\bstuck\b|\bblocked\b|\?\s*$)",
    re.I,
)
_BROKEN = re.compile(
    r"\b(broken|red build|ci (is )?(red|failing)|flaky|regress(ed|ion)|outage|down)\b", re.I
)
_BUG = re.compile(
    r"\b(bugs?|crash(es|ed|ing)?|fail(s|ed|ing|ure)?|incorrect|wrong|unexpected|repro)\b", re.I
)
_SOCIAL = re.compile(
    r"^\s*(thanks?( you)?|ty|thx|lol|haha|nice|congrats|welcome|good (morning|night)|gm|"
    r"\+1|:[a-z_+-]+:|ok(ay)?|sounds good|lgtm)[\s!.]*$",
    re.I,
)

ENGAGE_THRESHOLD = 2.0
UNANSWERED_S = 10 * 60  # a question nobody touched for 10 minutes


def judge(
    ev: Event,
    *,
    engaged_threads: Iterable[str] = (),
    llm_judge: Optional[Callable[[Event, "Verdict"], Optional[bool]]] = None,
) -> Verdict:
    """Decide whether the agent should step into ``ev``. Pure; no I/O."""
    reasons: List[str] = []
    text = ev.text or ""
    if ev.is_bot:
        return Verdict(False, 0.0, ["skip: bot/own message"])
    if _OPT_OUT.search(text):
        return Verdict(False, 0.0, ["skip: explicit opt-out"])
    if ev.thread_key in set(engaged_threads):
        return Verdict(False, 0.0, ["skip: already engaged this thread"])
    if ev.assignee:
        return Verdict(False, 0.0, [f"skip: owned by {ev.assignee}"])
    if not text.strip() or _SOCIAL.match(text):
        return Verdict(False, -1.0, ["skip: social/empty"])

    score = 0.0
    if _ERROR.search(text):
        score += 2.0
        reasons.append("+2 error/traceback")
    if _BROKEN.search(text):
        score += 1.5
        reasons.append("+1.5 something is broken")
    if _HELP.search(text):
        score += 1.0
        reasons.append("+1 asks for help")
        if ev.replies == 0 and ev.age_s >= UNANSWERED_S:
            score += 1.0
            reasons.append("+1 unanswered for 10m+")
    if ev.source == "linear":
        score += 1.0
        reasons.append("+1 sits in triage unowned")
        if _BUG.search(text):
            score += 1.0
            reasons.append("+1 bug-shaped")
    if ev.replies >= 3:
        score -= 1.5
        reasons.append("-1.5 humans are already on it")
    if len(text) < 12:
        score -= 1.0
        reasons.append("-1 too short to act on")

    verdict = Verdict(score >= ENGAGE_THRESHOLD, score, reasons)
    # Borderline band only: the model may confirm or veto, never override a clear call.
    if llm_judge is not None and ENGAGE_THRESHOLD - 1.0 <= score < ENGAGE_THRESHOLD + 1.0:
        try:
            opinion = llm_judge(ev, verdict)
        except Exception as e:  # a dead model must not stop the loop
            verdict.reasons.append(f"llm judge failed: {type(e).__name__}")
        else:
            if opinion is not None and opinion != verdict.engage:
                verdict.engage = opinion
                verdict.reasons.append(f"llm judge -> {'engage' if opinion else 'skip'}")
    return verdict


# ── sources ────────────────────────────────────────────────────────────────


class Source(Protocol):
    name: str

    def poll(self, cursor: Optional[str]) -> "tuple[List[Event], Optional[str]]": ...


def _http_json(url: str, *, headers: Dict[str, str], body: Optional[dict] = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", **headers}
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


class SlackSource:
    """Poll ``conversations.history`` for each channel. Needs ``channels:history``."""

    name = "slack"

    def __init__(
        self,
        channels: List[str],
        token: Optional[str] = None,
        fetch: Callable[..., dict] = _http_json,
    ) -> None:
        self.channels = channels
        self.token = token or os.environ.get("SLACK_BOT_TOKEN", "")
        self._fetch = fetch
        if not self.token:
            raise ValueError("SLACK_BOT_TOKEN is not set")

    def poll(self, cursor: Optional[str]) -> "tuple[List[Event], Optional[str]]":
        oldest = json.loads(cursor) if cursor else {}
        events: List[Event] = []
        now = time.time()
        for ch in self.channels:
            q = {"channel": ch, "limit": "100"}
            if oldest.get(ch):
                q["oldest"] = oldest[ch]
            data = self._fetch(
                "https://slack.com/api/conversations.history?" + urllib.parse.urlencode(q),
                headers={"Authorization": f"Bearer {self.token}"},
            )
            if not data.get("ok"):
                raise RuntimeError(f"slack {ch}: {data.get('error', 'unknown error')}")
            for m in data.get("messages", []):
                ts = float(m.get("ts", 0))
                events.append(
                    Event(
                        source="slack",
                        id=m.get("ts", ""),
                        channel=ch,
                        text=m.get("text", ""),
                        author=m.get("user", ""),
                        thread=m.get("thread_ts", "") or m.get("ts", ""),
                        ts=ts,
                        is_bot=bool(m.get("bot_id") or m.get("subtype") == "bot_message"),
                        replies=int(m.get("reply_count", 0)),
                        age_s=max(0.0, now - ts),
                    )
                )
                oldest[ch] = max(oldest.get(ch, "0"), m.get("ts", "0"), key=float)
        events.sort(key=lambda e: e.ts)
        return events, json.dumps(oldest)


_LINEAR_QUERY = """
query Triage($team: String!, $after: DateTimeOrDuration) {
  issues(first: 50, orderBy: updatedAt, filter: {
    team: { key: { eq: $team } }, state: { type: { eq: "triage" } },
    updatedAt: { gt: $after } }) {
    nodes { id identifier title description url updatedAt createdAt
            assignee { name } comments { nodes { id } } }
  }
}"""


class LinearSource:
    """Poll a team's triage queue via the Linear GraphQL API."""

    name = "linear"

    def __init__(
        self, team: str, api_key: Optional[str] = None, fetch: Callable[..., dict] = _http_json
    ) -> None:
        self.team = team
        self.api_key = api_key or os.environ.get("LINEAR_API_KEY", "")
        self._fetch = fetch
        if not self.api_key:
            raise ValueError("LINEAR_API_KEY is not set")

    def poll(self, cursor: Optional[str]) -> "tuple[List[Event], Optional[str]]":
        after = cursor or "-P1D"  # first run: the last day of triage
        data = self._fetch(
            "https://api.linear.app/graphql",
            headers={"Authorization": self.api_key},
            body={"query": _LINEAR_QUERY, "variables": {"team": self.team, "after": after}},
        )
        if data.get("errors"):
            raise RuntimeError(f"linear: {data['errors'][0].get('message', data['errors'])}")
        nodes = (((data.get("data") or {}).get("issues") or {}).get("nodes")) or []
        events, newest = [], cursor
        now = time.time()
        for n in nodes:
            created = _iso_ts(n.get("createdAt", ""))
            events.append(
                Event(
                    source="linear",
                    id=n["id"],
                    channel=self.team,
                    text=(
                        f"{n.get('identifier', '')} {n.get('title', '')}\n\n"
                        f"{n.get('description') or ''}"
                    ).strip(),
                    thread=n["id"],
                    url=n.get("url", ""),
                    ts=created,
                    assignee=((n.get("assignee") or {}).get("name") or ""),
                    replies=len(((n.get("comments") or {}).get("nodes")) or []),
                    age_s=max(0.0, now - created) if created else 0.0,
                )
            )
            if not newest or n.get("updatedAt", "") > newest:
                newest = n.get("updatedAt")
        return events, newest


class JsonlSource:
    """Custom event stream: one JSON object per line, fields as in :class:`Event`.

    The cursor is a byte offset, so a producer can append forever and each poll
    reads only what is new.
    """

    name = "jsonl"

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def poll(self, cursor: Optional[str]) -> "tuple[List[Event], Optional[str]]":
        if not self.path.exists():
            return [], cursor
        offset = int(cursor or 0)
        events: List[Event] = []
        with self.path.open("rb") as f:
            f.seek(offset)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # partial line mid-write; pick it up next poll
                offset += len(raw)
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                known = {k: d[k] for k in Event.__dataclass_fields__ if k in d}
                known.setdefault("source", "jsonl")
                known.setdefault("channel", self.path.stem)
                known.setdefault("id", f"{self.path.stem}:{offset}")
                known.setdefault("text", "")
                events.append(Event(**known))
        return events, str(offset)


def _iso_ts(s: str) -> float:
    if not s:
        return 0.0
    from datetime import datetime

    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# ── dispatch ───────────────────────────────────────────────────────────────


def build_task(ev: Event) -> str:
    where = ev.url or f"{ev.source} {ev.channel} thread {ev.thread or ev.id}"
    return (
        f"Lookout picked this up from {where} without an @mention.\n"
        f"Author: {ev.author or 'unknown'}\n\n---\n{ev.text}\n---\n\n"
        "Investigate. If you can fix it, open a PR and reply in the thread with the link. "
        "If you cannot, reply with what you found and who should look. Keep the reply short; "
        "people did not ask for you, so earn the interruption."
    )


def awrun_dispatch(agent: str) -> Callable[[Event], dict]:
    def _dispatch(ev: Event) -> dict:
        from adk.builtin_tools import queue_submit

        return json.loads(queue_submit("agent", task=build_task(ev), agent=agent))

    return _dispatch


# ── the loop ───────────────────────────────────────────────────────────────


class Lookout:
    def __init__(
        self,
        sources: List[Any],
        dispatch: Callable[[Event], dict],
        *,
        state_dir: Path = STATE_DIR,
        hourly_cap: int = 3,
        llm_judge: Optional[Callable[[Event, Verdict], Optional[bool]]] = None,
        dry_run: bool = False,
    ) -> None:
        self.sources = sources
        self.dispatch = dispatch
        self.state_dir = Path(state_dir)
        self.hourly_cap = hourly_cap
        self.llm_judge = llm_judge
        self.dry_run = dry_run
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "state.json"
        self.ledger_path = self.state_dir / "ledger.jsonl"
        self.state = self._load()

    def _load(self) -> Dict[str, Any]:
        try:
            s = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            s = {}
        s.setdefault("cursors", {})
        s.setdefault("engaged", {})  # thread_key -> ts
        return s

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    def _ledger(self, row: Dict[str, Any]) -> None:
        with self.ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def _over_cap(self, ev: Event, now: float) -> bool:
        prefix = f"{ev.source}:{ev.channel}:"
        recent = [
            t for k, t in self.state["engaged"].items() if k.startswith(prefix) and now - t < 3600
        ]
        return len(recent) >= self.hourly_cap

    def tick(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Poll every source once; return the ledger rows written."""
        now = now or time.time()
        rows: List[Dict[str, Any]] = []
        for src in self.sources:
            key = getattr(src, "name", type(src).__name__)
            try:
                events, cursor = src.poll(self.state["cursors"].get(key))
            except (urllib.error.URLError, RuntimeError, OSError, ValueError) as e:
                row = {"ts": now, "source": key, "action": "source-error", "error": str(e)[:300]}
                self._ledger(row)
                rows.append(row)
                continue
            for ev in events:
                v = judge(ev, engaged_threads=self.state["engaged"], llm_judge=self.llm_judge)
                action = "engage" if v.engage else "skip"
                if v.engage and self._over_cap(ev, now):
                    action, v.reasons = (
                        "skip",
                        v.reasons + [f"skip: channel cap {self.hourly_cap}/h"],
                    )
                row: Dict[str, Any] = {
                    "ts": now,
                    "source": ev.source,
                    "channel": ev.channel,
                    "thread": ev.thread_key,
                    "action": action,
                    "score": round(v.score, 2),
                    "reasons": v.reasons,
                    "url": ev.url,
                    "excerpt": ev.text[:160],
                }
                if action == "engage":
                    if self.dry_run:
                        row["dispatch"] = "dry-run"
                    else:
                        try:
                            row["dispatch"] = self.dispatch(ev)
                        except Exception as e:  # keep watching; record the failure
                            row["action"], row["dispatch"] = "dispatch-error", str(e)[:300]
                    if row["action"] == "engage":
                        self.state["engaged"][ev.thread_key] = now
                self._ledger(row)
                rows.append(row)
            if cursor is not None:
                self.state["cursors"][key] = cursor
        # forget engagements older than a week so state stays small
        self.state["engaged"] = {
            k: t for k, t in self.state["engaged"].items() if now - t < 7 * 86400
        }
        self._save()
        return rows


# ── CLI ────────────────────────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="adk lookout",
        description="Watch Slack/Linear/event streams; step in when help is wanted.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="Watch the configured streams")
    run.add_argument(
        "--slack",
        action="append",
        default=[],
        metavar="CHANNEL_ID",
        help="Slack channel id to watch (repeatable; token from SLACK_BOT_TOKEN)",
    )
    run.add_argument(
        "--linear",
        metavar="TEAM_KEY",
        help="Linear team whose triage queue to watch (key from LINEAR_API_KEY)",
    )
    run.add_argument(
        "--events",
        action="append",
        default=[],
        metavar="FILE.jsonl",
        help="Custom JSONL event stream (repeatable)",
    )
    run.add_argument(
        "--agent", default="aither", help="Agent that takes engaged items (default aither)"
    )
    run.add_argument("--interval", type=int, default=60, help="Seconds between polls (default 60)")
    run.add_argument(
        "--hourly-cap", type=int, default=3, help="Max engagements per channel per hour"
    )
    run.add_argument("--once", action="store_true", help="Poll once and exit")
    run.add_argument("--dry-run", action="store_true", help="Judge and log, never dispatch")
    j = sub.add_parser("judge", help="Explain the verdict for one message")
    j.add_argument("text", nargs="+")
    j.add_argument("--source", default="slack", choices=["slack", "linear", "jsonl"])
    j.add_argument("--replies", type=int, default=0)
    j.add_argument("--age", type=float, default=0.0, help="Seconds unanswered")
    lg = sub.add_parser("ledger", help="Show recent verdicts")
    lg.add_argument("-n", type=int, default=20)
    lg.add_argument("--engaged", action="store_true", help="Only engagements")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "judge":
        v = judge(
            Event(
                source=args.source,
                id="cli",
                channel="cli",
                text=" ".join(args.text),
                replies=args.replies,
                age_s=args.age,
            )
        )
        print(f"{'ENGAGE' if v.engage else 'SKIP'}  score={v.score:.1f}")
        for r in v.reasons:
            print(f"  {r}")
        return 0
    if args.cmd == "ledger":
        path = STATE_DIR / "ledger.jsonl"
        if not path.exists():
            print("no ledger yet — run `adk lookout run` first")
            return 0
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        if args.engaged:
            rows = [r for r in rows if r.get("action") == "engage"]
        for r in rows[-args.n :]:
            when = time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0)))
            print(
                f"{when}  {r.get('action', ''):14} {r.get('source', '')}:{r.get('channel', '')}  "
                f"{r.get('score', '')}  {(r.get('excerpt') or r.get('error') or '')[:70]!r}"
            )
        return 0

    sources: List[Any] = []
    try:
        if args.slack:
            sources.append(SlackSource(args.slack))
        if args.linear:
            sources.append(LinearSource(args.linear))
    except ValueError as e:
        print(f"lookout: {e}", file=sys.stderr)
        return 2
    sources.extend(JsonlSource(p) for p in args.events)
    if not sources:
        print("lookout: nothing to watch — pass --slack, --linear or --events", file=sys.stderr)
        return 2
    ship = Lookout(
        sources, awrun_dispatch(args.agent), hourly_cap=args.hourly_cap, dry_run=args.dry_run
    )
    while True:
        for r in ship.tick():
            if r["action"] != "skip":
                where = r.get("thread", r.get("source"))
                what = r.get("excerpt", r.get("error", ""))[:80]
                print(f"{r['action']:14} {where}  {what!r}")
        if args.once:
            return 0
        time.sleep(max(5, args.interval))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "Event",
    "Verdict",
    "judge",
    "SlackSource",
    "LinearSource",
    "JsonlSource",
    "Lookout",
    "awrun_dispatch",
    "build_task",
    "main",
]
