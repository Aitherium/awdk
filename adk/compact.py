"""adk.compact -- shrink a long tool output through the decision door.

    from adk.compact import compact, teach
    r = compact(pytest_output, "pytest")
    r.kept              # what the model should see
    r.kept_lines, r.total_lines, r.dropped
    teach(r, wrong_ids=[...])   # a dropped line mattered -> the door learns it

A long tool output (a 1,000-line pytest run, a build log, a deploy trace) is
mostly filler around a handful of lines that decide what happens next. This
asks the door, per distinct LINE SHAPE, whether the model needs to see it, in
ONE `/decide/batch` round trip -- and keeps the line whenever the answer is
"nothing to go on". The lines that must never be dropped (tracebacks, summary
lines, error lines, the head and tail) are kept by RULE and never sent anywhere.

Two paths, tried in this order and reported in `mode`:

  in-process   the world-model service tree is importable here (a fleet
               container, or a dev box with AITHER_WM_SVC pointing at it) --
               no network at all
  remote       POST /decide/batch to AITHER_DECIDE_URL (adk.choose's endpoint
               rules apply: the in-fleet door, or a gateway's .../v1)

With neither, `compact` still returns -- rules only, every shape `no-door`, and
`mode="rules-only"`. A compactor that refuses because a service is away would
be worse than one that compacts a little less.

Teaching is the half a static classifier cannot have: `teach(result, wrong_ids)`
posts the reward for each shape's decision, so the next output of that shape is
judged from evidence rather than by a model.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: Where the world-model service tree lives when it is on this box. The module
#: it holds (`compact.py`) is the implementation; this file is the client.
#:
#: NO developer-box path is listed here on purpose: a shipped package that names
#: somebody's drive letter tells a stranger the shape of a machine they will
#: never have, and it resolves on exactly one box. AITHER_WM_SVC answers for a
#: checkout anywhere; the two directories below are where a fleet container
#: mounts the tree; and `_search_up` finds it beside a sibling checkout without
#: being told.
SVC_ENV = "AITHER_WM_SVC"
SVC_DIRNAME = "arc-world-model-svc"
SVC_CANDIDATES = (
    "/app/wm",
    "/opt/aitheros/" + SVC_DIRNAME,
)

DEFAULT_TOOL = "tool"
DEFAULT_BUDGET = 40


class CompactUnavailableError(RuntimeError):
    """Neither the local implementation nor the door could be reached, AND the
    caller asked for strict mode. Plain `compact()` never raises this."""


@dataclass
class CompactResult:
    kept: str
    kept_lines: int
    total_lines: int
    dropped: int
    mode: str
    tool: str
    source_counts: Dict[str, int] = field(default_factory=dict)
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    latency_ms: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def learned(self) -> bool:
        """True when every shape was answered from evidence (no model call)."""
        srcs = self.source_counts or {}
        return bool(srcs) and set(srcs) <= {"engine", "neighbor"}

    def header(self) -> str:
        """The one line a hook prints above the compacted text."""
        return (
            f"[compacted by the decision door: kept {self.kept_lines} of "
            f"{self.total_lines} lines; teach with /compact teach <id>]"
        )

    def decision_ids(self) -> List[str]:
        return [str(d["decision_id"]) for d in self.decisions if d.get("decision_id")]


def _search_up(start: Optional[Path] = None, levels: int = 6) -> Optional[Path]:
    """The service tree as a sibling of this checkout, found by walking up.
    Costs a handful of `is_file` calls and keeps a machine-specific path out of
    the shipped source."""
    here = (start or Path(__file__).resolve()).parent
    for _ in range(levels):
        for parent in (here, here.parent):
            cand = parent / SVC_DIRNAME / "compact.py"
            if cand.is_file():
                return cand.parent
        if here.parent == here:
            break
        here = here.parent
    return None


def _impl_path() -> Optional[Path]:
    for cand in (os.environ.get(SVC_ENV), *SVC_CANDIDATES):
        if cand and (Path(cand) / "compact.py").is_file():
            return Path(cand)
    return _search_up()


def _impl():
    """The service tree's `compact` module, imported under a private name so it
    never shadows this one in sys.modules."""
    path = _impl_path()
    if path is None:
        return None
    import importlib.util

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    spec = importlib.util.spec_from_file_location("_wm_compact_impl", path / "compact.py")
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_wm_compact_impl", mod)
    spec.loader.exec_module(mod)
    return mod


class _DoorOverChoose:
    """A Decider-shaped adapter over adk.choose, so the implementation's remote
    mode works through the same endpoint, TLS and token rules the rest of adk
    uses. Kept here rather than in choose.py: choose.py is the decision API,
    this is one consumer's adapter."""

    def __init__(self, url: Optional[str] = None, timeout: float = 90.0) -> None:
        self._url, self._timeout = url, timeout

    def _with_url(self, fn, *a, **kw):
        prev = os.environ.get("AITHER_DECIDE_URL")
        if self._url:
            os.environ["AITHER_DECIDE_URL"] = self._url
        try:
            return fn(*a, **kw)
        finally:
            if self._url:
                if prev is None:
                    os.environ.pop("AITHER_DECIDE_URL", None)
                else:
                    os.environ["AITHER_DECIDE_URL"] = prev

    def decide_batch(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        from adk import choose as door

        decisions = self._with_url(
            door.decide_batch,
            "compact",
            [
                {
                    "fork": it["domain"],
                    "state": it["state"],
                    "kind": it.get("kind", "yesno"),
                    "question": it.get("question", ""),
                    "min_confidence": it.get("min_confidence", 0.0),
                }
                for it in items
            ],
            timeout=self._timeout,
        )
        return {"answers": [d.raw for d in decisions]}

    def outcome(self, body: Dict[str, Any]) -> Dict[str, Any]:
        from adk import choose as door

        if body.get("decision_id"):
            return self._with_url(
                door.outcome, str(body["decision_id"]), float(body.get("reward", 0.0))
            )
        return self._with_url(
            door.teach,
            str(body.get("domain", "compact")),
            str(body.get("state", "")),
            body.get("answer"),
            float(body.get("reward", 0.0)),
        )


def _decider(mode: str, url: Optional[str], timeout: float, llm: bool = False):
    """(decider, mode_label). `mode` is 'auto' | 'in-process' | 'remote' | 'rules'.

    `llm` is OFF by default on the IN-PROCESS path, and it is a latency decision
    measured on this box (2026-09-20): a cold shape that falls to the local brain
    costs ~0.5 s per attempt when the brain is unreachable and ~35 s when it
    answers, and this runs on a tool result -- a hook that adds half a minute to
    every long command gets switched off within a day. With the rung off, a cold
    shape reads `source=none` and is KEPT, and one `teach` makes it engine-served
    for good. Pass llm=True where a cold answer is worth the wait (a batch job,
    a bench). The remote path leaves the choice to the service."""
    if mode == "rules":
        return None, "rules-only"
    if mode in ("auto", "in-process"):
        impl = _impl()
        if impl is not None and not url:
            try:
                return impl.in_process_decider(llm_enabled=bool(llm),
                                               embed_enabled=bool(llm)), "in-process"
            except Exception:  # noqa: BLE001 -- no world_model here; fall through
                if mode == "in-process":
                    return None, "rules-only"
        elif mode == "in-process":
            return None, "rules-only"
    return _DoorOverChoose(url, timeout), "remote"


def compact(
    text: str,
    tool_name: str = DEFAULT_TOOL,
    *,
    budget_lines: int = DEFAULT_BUDGET,
    url: Optional[str] = None,
    mode: str = "auto",
    timeout: float = 90.0,
    min_lines: int = 0,
    strict: bool = False,
    llm: bool = False,
) -> CompactResult:
    """Compact `text`. Never raises for a door that is away unless `strict`.
    `llm=True` lets the in-process door ask the local brain about a shape it has
    never seen; OFF by default because this runs on a tool result (see
    `_decider`). Without it a cold shape is kept, not guessed at."""
    impl = _impl()
    if impl is None:
        if strict:
            raise CompactUnavailableError(
                "the world-model service tree is not on this box; set "
                f"{SVC_ENV}=<path to arc-world-model-svc>"
            )
        return _rules_only_fallback(text, tool_name)
    decider, label = _decider(mode, url, timeout, llm=llm)
    res = impl.compact(
        text, tool_name, budget_lines, decider=decider, min_lines=min_lines
    )
    srcs = res.get("source_counts") or {}
    if decider is not None and srcs and set(srcs) <= {"error", "no-door", "short-batch"}:
        if strict:
            raise CompactUnavailableError(
                f"door unreachable: {next(iter(d.get('error', '') for d in res['decisions']), '')}"
            )
        label = f"{label} (door unreachable; rules only)"
    return CompactResult(
        kept=res["kept"],
        kept_lines=res["kept_lines"],
        total_lines=res["total_lines"],
        dropped=res["dropped"],
        mode=label,
        tool=tool_name,
        source_counts=srcs,
        decisions=res.get("decisions") or [],
        latency_ms=float(res.get("latency_ms") or 0.0),
        raw=res,
    )


# A line that REPORTS a count is a summary and is kept; a line that merely
# contains the word is not. `passed` was a bare keep-word until 2026-09-20 and
# that single entry kept every per-test `PASSED` line -- on a passing pytest run
# the fallback kept 308 of 314 lines where the real rules keep 28, i.e. it
# inverted the feature's headline case on exactly the machines that only have
# the fallback. Measured with tools/compact_bench.py's labels.
_FB_SUMMARY = re.compile(
    r"\b\d+\s+(passed|failed|errors?|skipped|warnings?|deselected|xfailed|xpassed)\b|"
    r"exit(?:ed)?(?:\s+with)?(?:\s+code)?\s*[=: ]\s*-?\d+|"
    r"\berror\s*:|^\s*\d+ files? changed|^Found \d+ errors?|All checks passed|"
    r"^(FAILED|ERROR|FAIL)\b|^=+\s.*\s=+$|^curl: \(\d+\)|^npm ERR!"
    # See compact.py: a single-letter F/E pytest verdict has the same skeleton as
    # the PASSED lines around it, so the failing test name was dropped with them.
    r"|::\S+\s+[FE]\b|^\S+\.py\s+[.sx]*[FE][.sxFE]*\s*(?:\[\s*\d+%\])?\s*$",
    re.I,
)
# Words that make a line worth seeing on their own. `passed`/`warning` are NOT
# here: they are only interesting as a COUNT, which _FB_SUMMARY catches.
_FB_WORDS = ("error", "fail", "exception", "traceback", "fatal", "denied",
             "refused", "cannot", "no such", "not found", "unable", "abort",
             "panic", "assert", "invalid", "unauthorized", "forbidden")
_FB_TRACEBACK = re.compile(r'^\s+File ".+", line \d+|^Traceback \(most recent|^\s+at .+:\d+')
#: A RECORD BOUNDARY is kept like a summary line. Without this, every
#: `commit <sha>` in `git log --stat` shares one skeleton, so the frequency rule
#: below kept three of forty and the bench's labels lost 31 commit hashes
#: (recall 68.4%, measured 2026-09-20). A boundary is what makes the surrounding
#: lines readable at all, so it is a rule, not a judgement.
_FB_RECORD = re.compile(
    r"^commit [0-9a-f]{7,40}\b|^(Author|Date|Merge|AuthorDate|CommitDate):\s|"
    r"^(STEP|Step) \d+(/\d+)?[: ]|^COMMIT\b|^Successfully\b|^-{3,}\s*$|^={3,}\s*$"
)


#: Collapse a run of this many consecutive same-skeleton lines or more.
_FB_RUN = 4
_FB_WORD_RUN = re.compile(r"[A-Za-z_]+")
_FB_NUM_RUN = re.compile(r"\d+")
_FB_TOKEN_RUN = re.compile(r"[W#][W#_.\-/]*")
_FB_PLUSMINUS = re.compile(r"[+\-]{2,}")
_FB_SPACE_RUN = re.compile(r"\s+")


def _fb_skeleton(line: str) -> str:
    """A crude STABLE descriptor of a line's shape. Words become `W`, numbers
    `#`, then any run of those joined by `_ . - /` collapses to one `T` and any
    run of `+`/`-` to one `+`. That last collapse is what makes it work on real
    output: without it `dev/tests/test_foo.py::test_bar_baz PASSED` and
    `dev/tests/test_qux.py::test_a PASSED` have DIFFERENT skeletons (the test
    names hold different numbers of words), so 300 identical-looking pytest
    lines never form a run and nothing collapses. Truncated at 48 characters --
    two lines that agree that far are the same kind of line here. It is the
    fallback's one-word version of the real shape grammar."""
    s = _FB_NUM_RUN.sub("#", _FB_WORD_RUN.sub("W", line.strip()))
    s = _FB_TOKEN_RUN.sub("T", s)
    s = _FB_PLUSMINUS.sub("+", s)
    s = _FB_SPACE_RUN.sub(" ", s)
    return (("i" if line[:1].isspace() else "s") + s)[:48]


def _rules_only_fallback(text: str, tool_name: str) -> CompactResult:
    """No implementation on this box. Keeps the head, the tail, every traceback
    frame, error line and summary line by RULE, then collapses the MIDDLE of any
    run of >= `_FB_RUN` consecutive same-skeleton lines (keeping its first two
    and its last). Deliberately crude -- it exists so a hook on a stranger's
    machine degrades instead of failing.

    Dropping by run rather than by word is what makes it safe on both shapes at
    once: a word list aggressive enough to collapse 300 `PASSED` lines also
    threw away commit subjects in `git log --stat` (recall 36.8% on the bench's
    labels, 2026-09-20), and one conservative enough to keep them kept 308 of
    314 lines of a passing pytest run. A run is the thing they actually differ
    in, so gate on that."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    n = len(lines)
    always: List[bool] = []
    for i, ln in enumerate(lines):
        low = ln.lower()
        always.append(bool(
            i < 2
            or i >= n - 5
            or _FB_TRACEBACK.match(ln)
            or _FB_RECORD.match(ln)
            or _FB_SUMMARY.search(ln)
            or any(w in low for w in _FB_WORDS)
        ))
    # The first indented line after a record header is that record's TITLE (a
    # commit subject after `Date:`), and it is the only line of the record a
    # reader actually scans. It is free prose, so enough subjects share a token
    # count to collide under the skeleton and be collapsed as repetition -- one
    # was still lost at this point on the bench's labels.
    for i, ln in enumerate(lines):
        if always[i] or not ln[:1].isspace() or not ln.strip():
            continue
        k = i - 1
        while k >= 0 and not lines[k].strip() and i - k <= 2:
            k -= 1
        if k >= 0 and _FB_RECORD.match(lines[k]):
            always[i] = True
    verdict = ["keep"] * n
    skel = [_fb_skeleton(ln) for ln in lines]
    # Gate on how often a skeleton occurs in the WHOLE output, not on how many
    # times it occurs consecutively. Real output interleaves: a pytest run mixes
    # `[ 45%]` and `[100%]` progress suffixes and the occasional parametrized
    # id, which breaks a strict run every few lines and left 174 of 314 lines
    # standing (measured 2026-09-20). Occurrences of a skeleton seen `_FB_RUN`
    # times or more keep their first two and their last; the rest collapse.
    seen: Dict[str, List[int]] = {}
    for i in range(n):
        if not always[i]:
            seen.setdefault(skel[i], []).append(i)
    for idxs in seen.values():
        if len(idxs) >= _FB_RUN:
            for k in idxs[2:-1]:
                verdict[k] = "drop"
    kept: List[str] = []
    dropped_run = 0
    for i, ln in enumerate(lines):
        if verdict[i] == "keep":
            if dropped_run:
                kept.append(f"... ({dropped_run} similar lines dropped)")
                dropped_run = 0
            kept.append(ln)
        else:
            dropped_run += 1
    if dropped_run:
        kept.append(f"... ({dropped_run} similar lines dropped)")
    body = "\n".join(kept)
    return CompactResult(
        kept=body,
        kept_lines=sum(1 for k in kept if not k.startswith("... (")),
        total_lines=len(lines),
        dropped=len(lines) - sum(1 for k in kept if not k.startswith("... (")),
        mode="rules-only (no service tree)",
        tool=tool_name,
        source_counts={"no-door": 0},
    )


def teach(
    result: CompactResult,
    wrong_ids: Optional[Sequence[str]] = None,
    *,
    url: Optional[str] = None,
    timeout: float = 30.0,
    mode: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Teach the door about THIS compaction. `wrong_ids` are the decision ids
    whose verdict was wrong (a dropped line that mattered, or a kept line that
    was filler); every other decision is confirmed right. With `wrong_ids=None`
    nothing is posted -- silence is not a reward.

    The outcome goes back to the door that ANSWERED: a decision id minted
    in-process means nothing to the remote door and vice versa, so the mode is
    taken from `result.mode` unless the caller overrides it."""
    if wrong_ids is None:
        return []
    impl = _impl()
    wrong = {str(w) for w in wrong_ids}
    grades = {str(d["decision_id"]): (str(d["decision_id"]) not in wrong)
              for d in result.decisions if d.get("decision_id")}
    if not grades:
        return []
    if mode is None:
        mode = "remote" if result.mode.startswith("remote") else (
            "in-process" if result.mode.startswith("in-process") else
            ("auto" if impl else "remote"))
    decider, _ = _decider(mode, url, timeout)
    if decider is None:
        decider = _DoorOverChoose(url, timeout)
    if impl is None:  # post by hand, same contract
        out = []
        for did, right in grades.items():
            try:
                out.append(decider.outcome({"decision_id": did, "reward": 1.0 if right else -1.0}))
            except Exception as exc:  # noqa: BLE001
                out.append({"decision_id": did, "error": str(exc)[:200]})
        return out
    return impl.compact_outcome(result.decisions, grades, decider, tool_name=result.tool)


__all__ = [
    "CompactResult",
    "CompactUnavailableError",
    "compact",
    "teach",
    "DEFAULT_BUDGET",
    "DEFAULT_TOOL",
]


# ------------------------------------------------------------------- CLI
def _main(argv: Optional[List[str]] = None) -> int:
    """`python -m adk.compact <file|-> [--tool X] [--json] [--min-lines N]`.

    Exit 0 compacted (or deliberately passed through), 1 nothing to read,
    2 strict mode could not reach a door. The Claude Code hook
    `.claude/hooks/tool-output-compact.ps1` and the awsh mod both drive this."""
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(prog="adk.compact", description=__doc__.split("\n\n")[0])
    ap.add_argument("file", nargs="?", default="-", help="file to compact, or - for stdin")
    ap.add_argument("--tool", default=DEFAULT_TOOL)
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    ap.add_argument("--min-lines", type=int, default=0,
                    help="shorter than this many lines: pass through untouched")
    ap.add_argument("--url", default=None)
    ap.add_argument("--mode", default="auto", choices=("auto", "in-process", "remote", "rules"))
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--llm", action="store_true",
                    help="let the in-process door ask the local brain about an unseen "
                         "shape (slow: seconds per cold batch); off by default")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--header", action="store_true", help="prefix the one-line header")
    a = ap.parse_args(argv)
    text = sys.stdin.read() if a.file == "-" else Path(a.file).read_text(
        encoding="utf-8", errors="replace"
    )
    if not text.strip():
        return 1
    try:
        r = compact(text, a.tool, budget_lines=a.budget, url=a.url, mode=a.mode,
                    timeout=a.timeout, min_lines=a.min_lines, strict=a.strict, llm=a.llm)
    except CompactUnavailableError as exc:
        print(f"adk.compact: {exc}", file=sys.stderr)
        return 2
    if a.json:
        print(_json.dumps({
            "kept": r.kept, "kept_lines": r.kept_lines, "total_lines": r.total_lines,
            "dropped": r.dropped, "mode": r.mode, "tool": r.tool,
            "source_counts": r.source_counts, "decision_ids": r.decision_ids(),
            "header": r.header(), "latency_ms": r.latency_ms,
        }))
    else:
        if a.header:
            print(r.header())
        print(r.kept)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
