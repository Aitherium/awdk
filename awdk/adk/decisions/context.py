"""Make a card's context EXPLORABLE, live, with no further input from the owner.

The complaint this closes: a card states facts, and a fact is an assertion. "three
files hold peers' unfinished work" is exactly the sentence that makes somebody
say *which three, whose work, how much* — and answering that meant leaving the
card, going to a terminal, and running four commands. At which point the card has
failed at the one job it has.

So the card carries **anchors**, and this module turns anchors into a live view
the owner opens with one click.

**Anchors at raise, harvest at explore.** Deliberate, and the opposite of what a
"snapshot the context into the card" design would do:

* a raise must stay cheap — it happens on a Stop hook, in the owner's session;
* a snapshot is stale by the time anybody reads it, and the interesting facts
  here (who holds a lease, what a peer has staged, what the fleet is doing) can
  change between the raise and the click;
* the store is a durable artifact and stuffing a megabyte of diff into it makes
  every surface that lists cards slower.

**Every source answers three ways, and the third is why this is trustworthy:**
``ok`` with items, ``empty`` (it ran and there is genuinely nothing), or ``dead``
(it could not run — no gateway, no docker, no transcript). A source that cannot
answer is NEVER rendered as "nothing found", because those are different facts
and the whole platform has been bitten by conflating them.

Each harvester is bounded, never raises, and is independent — one dead source
must not empty the panel.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from adk.decisions.store import DecisionCard

#: No single source may hold the panel open longer than this.
SOURCE_TIMEOUT = float(os.getenv("AITHER_DECISIONS_CONTEXT_TIMEOUT", "6"))

#: Nothing renders more than this per source — the panel is for orientation, not
#: for reading a 4,000-line diff. Truncation IS reported (see `more`), because a
#: silently shortened list is the defect this whole feature was raised over.
MAX_ITEMS = 40

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_DEAD = "dead"


@dataclass
class ContextItem:
    """One explorable row. ``body`` is revealed when the row is expanded."""

    label: str
    body: str = ""
    path: str = ""


@dataclass
class ContextSource:
    """What one harvester found, or why it could not look."""

    name: str
    title: str
    status: str = STATUS_DEAD
    detail: str = ""
    items: list[ContextItem] = field(default_factory=list)
    more: int = 0
    took_ms: int = 0

    @property
    def headline(self) -> str:
        if self.status == STATUS_DEAD:
            return f"could not look — {self.detail or 'no reason given'}"
        if self.status == STATUS_EMPTY:
            return self.detail or "ran, found nothing"
        tail = f"  (+{self.more} more)" if self.more else ""
        return f"{len(self.items)} item(s){tail}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "title": self.title, "status": self.status,
            "detail": self.detail, "more": self.more, "took_ms": self.took_ms,
            "items": [{"label": i.label, "body": i.body, "path": i.path}
                      for i in self.items],
        }


def _run(argv: list[str], cwd: str = "", timeout: float = SOURCE_TIMEOUT) -> tuple[int, str]:
    """A bounded subprocess with NO console window. ``(returncode, output)``.

    CREATE_NO_WINDOW is load-bearing, not cosmetic: the card window is spawned
    DETACHED and therefore has no console of its own, so any console program it
    starts without this flag makes Windows allocate a NEW one and a window
    flashes on the owner's desktop (quality gate 1t / STW001, and asserted by
    check_decision_card_contract DC007).
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, cwd=cwd or None, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return 127, f"{argv[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]} timed out after {timeout}s"
    except OSError as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


def _cap(source: ContextSource, items: list[ContextItem]) -> None:
    """Fill a source, recording what was dropped rather than hiding it."""
    if len(items) > MAX_ITEMS:
        source.more = len(items) - MAX_ITEMS
        items = items[:MAX_ITEMS]
    source.items = items
    source.status = STATUS_OK if items else STATUS_EMPTY


# ── harvesters ──────────────────────────────────────────────────────────────────


def harvest_git(card: DecisionCard) -> ContextSource:
    """What is actually uncommitted here, and what landed recently."""
    src = ContextSource(name="git", title="Working tree")
    cwd = card.source.cwd or os.getcwd()
    code, out = _run(["git", "status", "--porcelain"], cwd)
    if code != 0:
        src.detail = out[:200]
        return src
    # ONE numstat for the whole tree, indexed in memory. The first version ran
    # `git diff --numstat -- <path>` PER FILE, which is fine on a tidy checkout
    # and catastrophic here: this working tree carries ~2,180 modified files, so
    # the panel fired ~2,180 subprocesses and hung for minutes. A context panel
    # that takes longer than walking to the terminal has defeated itself.
    stats: dict[str, str] = {}
    _code, numstat = _run(["git", "diff", "--numstat"], cwd)
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            stats[parts[2].strip()] = f"+{parts[0]} / -{parts[1]}"

    haystack = "\n".join(card.facts + [card.title, card.summary, card.detail])

    def rank(state: str, path: str) -> tuple:
        """Relevant first, then real edits, then the untracked long tail.

        Ordering is load-bearing on THIS tree, which carries ~2,180 changed
        files: an unranked list showed forty arbitrary untracked scratch paths
        and none of the files the card was actually about, which is a context
        panel that answers a question nobody asked. Relevance is decided by
        whether the card's own text names the file or its directory.
        """
        stem = Path(path).name
        parent = Path(path).parent.name
        relevant = 0 if (stem and stem in haystack) else (
            1 if (parent and parent in haystack) else 2)
        untracked = 1 if state == "??" else 0
        return (relevant, untracked, path)

    rows: list[tuple[tuple, ContextItem]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        state, path = line[:2].strip(), line[3:].strip()
        # git quotes paths containing non-ASCII/odd bytes; the quoted form is
        # unreadable noise in a panel meant for orientation.
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        rows.append((rank(state, path),
                     ContextItem(label=f"[{state or '??'}] {path}",
                                 body=stats.get(path, ""), path=path)))
    rows.sort(key=lambda r: r[0])
    _cap(src, [item for _key, item in rows])
    if src.status == STATUS_EMPTY:
        src.detail = "the working tree is clean"
    return src


def harvest_commits(card: DecisionCard) -> ContextSource:
    """Recent history — who has been landing what, right now."""
    src = ContextSource(name="commits", title="Recent commits")
    cwd = card.source.cwd or os.getcwd()
    code, out = _run(
        ["git", "log", "-12", "--pretty=%h\x1f%an\x1f%ar\x1f%s"], cwd)
    if code != 0:
        src.detail = out[:200]
        return src
    items = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        sha, who, when, subject = parts
        items.append(ContextItem(label=f"{sha}  {subject}",
                                 body=f"{who} · {when}", path=sha))
    _cap(src, items)
    return src


def harvest_leases(card: DecisionCard) -> ContextSource:
    """Who else is IN these files right now — the answer to "whose work?".

    This is the source that most directly closes the complaint. A card that says
    "peers hold unfinished work in three files" is unanswerable until you can see
    the lease table, and until now that meant leaving the card to run `awgit
    lease list` in a terminal.
    """
    src = ContextSource(name="leases", title="Who else is in these files")
    code, out = _run(["awgit", "lease", "list"], card.source.cwd)
    if code != 0:
        src.detail = out[:200] or "awgit is not available"
        return src
    items = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        lease_id, status, actor, kind, target = parts[0], parts[1], parts[2], parts[3], parts[4]
        items.append(ContextItem(
            label=f"{target}",
            body=f"{status} · held by {actor} ({kind}) · lease {lease_id[:8]}",
            path=target,
        ))
    _cap(src, items)
    if src.status == STATUS_EMPTY:
        src.detail = "no active leases — nobody else has claimed a file"
    return src


def harvest_session(card: DecisionCard) -> ContextSource:
    """What this session was DOING — the last files and commands it touched.

    Read from the transcript the raiser anchored, not from a summary written at
    raise time: a summary is one more thing that can be wrong, and the transcript
    is the record.
    """
    src = ContextSource(name="session", title="What this session was doing")
    path = (card.source.transcript or "").strip()
    if not path:
        src.detail = "no transcript anchored on this card"
        return src
    target = Path(path)
    if not target.is_file():
        src.detail = f"transcript is gone: {path}"
        return src
    try:
        size = target.stat().st_size
        with target.open("rb") as handle:
            if size > 256 * 1024:
                handle.seek(size - 256 * 1024)
                handle.readline()
            raw = handle.read().decode("utf-8", "replace")
    except OSError as exc:
        src.detail = f"unreadable: {exc}"
        return src

    items: list[ContextItem] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "tool")
            args = block.get("input") or {}
            hint = ""
            if isinstance(args, dict):
                for key in ("file_path", "path", "command", "pattern", "prompt"):
                    if args.get(key):
                        hint = str(args[key])
                        break
            items.append(ContextItem(label=name, body=hint[:400],
                                     path=hint if "file_path" in str(args) else ""))
    # Newest last in the transcript; the owner wants the newest first.
    items.reverse()
    _cap(src, items)
    if src.status == STATUS_EMPTY:
        src.detail = "the transcript tail holds no tool calls"
    return src


def _gateway_url() -> str:
    # 127.0.0.1, never localhost: measured on this box, ::1 refuses after
    # 2120 ms while IPv4 connects in 3 ms.
    return os.getenv("AITHER_MCP_GATEWAY", "http://127.0.0.1:8182").rstrip("/")


def harvest_platform(card: DecisionCard) -> ContextSource:
    """AitherOS itself — the graph, memory and fleet views, when reachable.

    Reports DEAD with the exact reason when the gateway is not up, which on this
    box is common and is NOT the same as "the platform knows nothing about this".
    The distinction is the entire reason this returns a status instead of a list.
    """
    src = ContextSource(name="platform", title="AitherOS (graph · memory · fleet)")
    base = _gateway_url()
    try:
        request = urllib.request.Request(f"{base}/health", method="GET")
        with urllib.request.urlopen(request, timeout=2) as response:
            healthy = 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:
        src.detail = (f"MCP gateway unreachable at {base} ({exc}) — codegraph, "
                      f"memory recall and fleet views are unavailable, NOT empty")
        return src
    if not healthy:
        src.detail = f"MCP gateway at {base} answered but is not healthy"
        return src

    items = [ContextItem(
        label="MCP gateway is up",
        body=f"{base} — codegraph_*, recall and fleet tools are reachable from "
             f"this box. Ask the session to run one; this panel deliberately does "
             f"not fire tool calls on its own.",
    )]
    _cap(src, items)
    return src


def harvest_fleet(card: DecisionCard) -> ContextSource:
    """Container reality, when an engine answers. Unhealthy first.

    Engine ladder (2026-08-29): this box runs rootful podman inside the WSL2
    Debian distro; Docker Desktop answers API 500 / "not found". The first
    version shelled `docker` only, so every card's Fleet panel read "docker
    did not answer" while ~140 containers ran. Try docker, then the podman
    lane, and only report dead when BOTH cannot answer.
    """
    src = ContextSource(name="fleet", title="Fleet")
    code, out = _run(
        ["docker", "ps", "--format", "{{.Names}}\x1f{{.Status}}"], timeout=5)
    if code != 0:
        code, out = _run([
            "wsl", "-d", "Debian", "-u", "root", "--",
            "podman", "ps", "--format", "{{.Names}}\x1f{{.Status}}",
        ], timeout=10)
    if code != 0:
        src.detail = out[:200] or "no container engine answered (docker + podman)"
        return src
    rows = []
    for line in out.splitlines():
        name, _, status = line.partition("\x1f")
        if not name:
            continue
        rows.append((name.strip(), status.strip()))
    # Anything not plainly healthy is what the owner needs to see first.
    rows.sort(key=lambda r: ("healthy" in r[1].lower(), r[0]))
    items = [ContextItem(label=name, body=status) for name, status in rows]
    _cap(src, items)
    if src.status == STATUS_EMPTY:
        src.detail = "no container engine answered with running containers"
    return src


#: Order is the reading order in the panel: the local and certain first, the
#: remote and probably-dead last.
HARVESTERS: tuple[tuple[str, Callable[[DecisionCard], ContextSource]], ...] = (
    ("git", harvest_git),
    ("leases", harvest_leases),
    ("session", harvest_session),
    ("commits", harvest_commits),
    ("platform", harvest_platform),
    ("fleet", harvest_fleet),
)


def explore(card: DecisionCard, only: Optional[str] = None) -> list[ContextSource]:
    """Run every harvester. Never raises; a dead source is reported, not dropped.

    ``only`` runs one by name — used by the panel to refresh a single section
    without paying for the whole sweep again.
    """
    out: list[ContextSource] = []
    for name, harvester in HARVESTERS:
        if only and name != only:
            continue
        started = time.time()
        try:
            source = harvester(card)
        except Exception as exc:  # noqa: BLE001 - one bad source must not empty the panel
            source = ContextSource(name=name, title=name.title(),
                                   status=STATUS_DEAD, detail=f"harvester raised: {exc}")
        source.took_ms = int((time.time() - started) * 1000)
        out.append(source)
    return out


def _self_test() -> int:
    """Prove a dead source stays DEAD and never masquerades as empty.

    That distinction is the only thing here worth asserting mechanically: the
    items themselves depend on a live machine, but "could not look" being
    reported as "nothing found" is the failure this module exists to avoid, and
    it is invisible once it happens.
    """
    import tempfile

    from adk.decisions.store import DecisionSource

    problems: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if condition else 'FAIL'} {name}{'' if condition else ' ' + detail}")
        if not condition:
            problems.append(name)

    # A card pointing at a directory that is not a repo: git MUST report dead.
    with tempfile.TemporaryDirectory() as tmp:
        nowhere = DecisionCard(id="d-test", title="t",
                               source=DecisionSource(cwd=tmp))
        git = harvest_git(nowhere)
        check("git reports DEAD outside a repo",
              git.status == STATUS_DEAD and bool(git.detail), f"{git.status} {git.detail}")
        check("a dead source says 'could not look'", "could not look" in git.headline,
              git.headline)

        session = harvest_session(nowhere)
        check("no transcript anchored is DEAD, not empty",
              session.status == STATUS_DEAD and "no transcript" in session.detail,
              f"{session.status} {session.detail}")

        missing = DecisionCard(id="d-test", title="t", source=DecisionSource(
            cwd=tmp, transcript=str(Path(tmp) / "gone.jsonl")))
        session = harvest_session(missing)
        check("a vanished transcript is DEAD, not empty",
              session.status == STATUS_DEAD and "gone" in session.detail,
              f"{session.status} {session.detail}")

        # A REAL transcript with one tool call must come back as ok.
        transcript = Path(tmp) / "t.jsonl"
        transcript.write_text(json.dumps({"message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Edit",
             "input": {"file_path": "C:/Projects/Example/thing.py"}}]}}) + "\n",
            encoding="utf-8")
        real = DecisionCard(id="d-test", title="t", source=DecisionSource(
            cwd=tmp, transcript=str(transcript)))
        session = harvest_session(real)
        check("a real transcript yields tool calls",
              session.status == STATUS_OK and session.items[0].label == "Edit",
              f"{session.status} {[i.label for i in session.items]}")

    # In THIS repo git must genuinely find things — a harvester that always
    # reports dead would pass every assertion above.
    here = DecisionCard(id="d-test", title="t",
                        source=DecisionSource(cwd=os.getcwd()))
    git = harvest_git(here)
    check("git works in a real repo", git.status in (STATUS_OK, STATUS_EMPTY),
          f"{git.status} {git.detail}")
    commits = harvest_commits(here)
    check("commits are readable in a real repo", commits.status == STATUS_OK,
          f"{commits.status} {commits.detail}")

    # The capping must REPORT what it dropped.
    src = ContextSource(name="x", title="X")
    _cap(src, [ContextItem(label=str(i)) for i in range(MAX_ITEMS + 7)])
    check("truncation is reported, not silent", src.more == 7 and "+7 more" in src.headline,
          src.headline)

    # explore() must return one entry per harvester even when they all fail.
    broken = DecisionCard(id="d-test", title="t",
                          source=DecisionSource(cwd="Z:/nope"))
    sources = explore(broken)
    check("explore returns every source even when they fail",
          len(sources) == len(HARVESTERS), str(len(sources)))
    check("no source silently reports empty on failure",
          all(s.status != STATUS_EMPTY or s.detail for s in sources))

    print()
    if problems:
        print(f"context self-test FAILED - {', '.join(problems)}")
        return 1
    print("context self-test passed - a source that cannot look never reports empty")
    return 0


if __name__ == "__main__":
    if "--dump" in sys.argv:
        from adk.decisions.store import DecisionSource

        probe = DecisionCard(id="d-dump", title="dump",
                             source=DecisionSource(cwd=os.getcwd()))
        for entry in explore(probe):
            print(f"[{entry.status:5}] {entry.title:38} {entry.headline}  "
                  f"({entry.took_ms}ms)")
        raise SystemExit(0)
    raise SystemExit(_self_test())
