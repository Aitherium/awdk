"""Loop policy: the four measured nudges that turn exploration into a landed, verified edit.

Lifted from the SWE-bench-Live harness (2026-09-21), where each was written against a
measured failure of a small model on a real coding task and proven by self-test:

  read_before_edit  an edit LANDED on a path never read -> demand `file_read` first.
                    Measured: 0.6 reads per edit scored 0/12; 3.4 reads per edit scored
                    3/12. `old_text` must match bytes the model never looked at.
  edit_budget       from step `edit_by` on, while no edit has landed, say how much
                    budget is left and which file was read most, and ask for the
                    smallest edit now. Measured: 90 shell calls, 24 searches, ZERO edits
                    in 16 steps on every instance.
  verify_loop       after the first successful edit, require `run_tests` once. Measured:
                    39 edits / 6 test runs scored 0; the only arm that verified scored.
  regression        once the target test is green, run the whole file. Measured: a
                    correct fix (14/14 target tests) scored unresolved for breaking 4 of
                    3,553 neighbours.

The policy is PURE and OPT-IN: ``AitherAgent(loop_policy=LoopPolicy(...))`` records every
tool result and hands back at most one nudge per step, which the loop appends as a
deferred system message (the same channel the loop guard uses). Nothing here decides
whether the policy is the default -- that is the ruler's promotion rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: A tool result is a failure when the tool answered with the builtin error envelope.
_ERROR_PREFIX = '{"error"'


def result_ok(result: Any) -> bool:
    if isinstance(result, str):
        return not result.lstrip().startswith(_ERROR_PREFIX)
    if isinstance(result, dict):
        return "error" not in result
    return True


@dataclass
class LoopPolicy:
    """Step ledger + the four nudges. One instance per agent turn (or per task)."""

    edit_by: int = 6            # first step at which the budget nudge may fire
    ceiling: int = 40           # the tool-call ceiling the budget line quotes
    every: int = 3              # budget nudge cadence after edit_by
    read_before_edit: bool = True
    verify_loop: bool = True
    regression: bool = True
    max_read_nudges: int = 2
    edit_tool: str = "file_edit"
    read_tool: str = "file_read"
    test_tool: str = "run_tests"

    steps: list[dict] = field(default_factory=list)
    read_nudges: int = 0
    verify_nudged: bool = False
    regression_nudged: bool = False

    # ── ledger ──────────────────────────────────────────────────────────────
    def record(self, tool: str, args: dict | None, result: Any) -> dict:
        head = result if isinstance(result, str) else str(result)
        step = {"tool": tool, "args": dict(args or {}), "ok": result_ok(result),
                "result_head": head[:600]}
        self.steps.append(step)
        return step

    def files_read(self, upto: int | None = None) -> set[str]:
        seen: set[str] = set()
        for st in self.steps[:upto]:
            if st["ok"] and st["tool"] in (self.read_tool, self.edit_tool):
                pth = st["args"].get("path")
                if pth:
                    seen.add(str(pth))
        return seen

    def edit_landed(self) -> bool:
        return any(s["tool"] == self.edit_tool and s["ok"] for s in self.steps)

    # ── the four nudges (each returns "" when quiet) ────────────────────────
    def read_before_edit_nudge(self) -> str:
        if not self.read_before_edit or not self.steps or self.read_nudges >= self.max_read_nudges:
            return ""
        last = self.steps[-1]
        if last["tool"] != self.edit_tool:
            return ""
        path = last["args"].get("path")
        if not path or str(path) in self.files_read(upto=len(self.steps) - 1):
            return ""
        self.read_nudges += 1
        return (f"[Harness] You edited {path} without ever reading it. `old_text` must match "
                f"the file BYTE FOR BYTE, and a search result is a snippet, not the file. Call "
                f"`{self.read_tool}` on that path, copy the exact lines you intend to replace "
                f"(including indentation), then edit.")

    def edit_budget_nudge(self) -> str:
        n = len(self.steps)
        if not self.edit_by or n < self.edit_by or self.edit_landed():
            return ""
        if (n - self.edit_by) % max(1, self.every):
            return ""
        reads: dict[str, int] = {}
        for s in self.steps:
            if s["tool"] == self.read_tool:
                pth = s["args"].get("path")
                if pth:
                    reads[str(pth)] = reads.get(str(pth), 0) + 1
        most = max(reads, key=reads.get) if reads else None
        left = max(0, self.ceiling - n)
        where = (f"The file you have read most is `{most}`." if most else
                 f"You have not read any file with {self.read_tool} yet -- read the file the "
                 f"issue points at ({self.read_tool} on the path) before editing.")
        return (f"[Budget] {n} of {self.ceiling} tool calls used, {left} left, and NO edit has "
                f"landed. Exploration is over. {where} Make the smallest correct change with "
                f"{self.edit_tool} NOW (exact old_text copied from {self.read_tool}), then "
                f"{self.test_tool}. Do not search further.")

    def verify_loop_nudge(self) -> str:
        if not self.verify_loop or self.verify_nudged or not self.steps:
            return ""
        last = self.steps[-1]
        if last["tool"] != self.edit_tool or not last["ok"]:
            return ""
        self.verify_nudged = True
        return (f"[Harness] That edit applied. Now RUN THE TESTS with `{self.test_tool}` before "
                f"doing anything else. An edit you have not verified is a guess: if the test "
                f"still fails, read the failure and correct the edit rather than searching for "
                f"another file.")

    def regression_nudge(self) -> str:
        if not self.regression or self.regression_nudged or not self.steps:
            return ""
        last = self.steps[-1]
        if last["tool"] != self.test_tool or not last["ok"]:
            return ""
        low = str(last["result_head"]).lower()
        if "passed" not in low or "failed" in low or "error" in low:
            return ""
        self.regression_nudged = True
        return ("[Harness] The target test passes. Now run the WHOLE test file it lives in, and "
                "any file covering the code you changed. A passing target test proves the bug "
                "is fixed; it proves nothing about what else your change touched, and a fix "
                "that breaks a neighbouring test is scored as a failure. If something broke, "
                "narrow the change rather than widening it.")

    def nudges(self) -> list[str]:
        """At most ONE nudge for the latest step, in the measured priority order: an edit
        against an unread file is wrong regardless of whether it applied (read first); the
        budget line only while nothing has landed; verify on the first landed edit; the
        regression ask only after a green target test."""
        for fn in (self.read_before_edit_nudge, self.edit_budget_nudge,
                   self.verify_loop_nudge, self.regression_nudge):
            text = fn()
            if text:
                return [text]
        return []

    def budget_line(self) -> str:
        """The one-line contract to put in the task prompt, when the caller wants it."""
        if not self.edit_by:
            return ""
        return (f"Budget: {self.ceiling} tool calls TOTAL. Read the file the issue points at, "
                f"make your edit by call {self.edit_by}, then run the tests. Exploring past "
                f"that budget forfeits the fix.")
