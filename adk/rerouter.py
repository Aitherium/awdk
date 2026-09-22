"""The re-router (orchestration spec section 5, owner 2026-09-22).

After every step, the result and the STATE DELTA go back to a lightweight decision:
``continue | replan | escalate | done``. The orchestrator plans once; this is what keeps
the plan honest for the rest of the turn, at no model cost -- it reads the step ledger
the harness already records.

The definitions are the measured ones, not intuitions:

* **delta** -- a step that changed the world: an edit or write that applied, a shell or
  python call that ran, a test run. A read, a list, a search and a graph query change
  nothing. Measured 2026-09-22 (L3d reflex-4129): 47 steps, 28 of them ``file_read``,
  zero delta, and the old stuck counter -- which counted STEPS -- never fired.
* **escalate** -- ``stuck_threshold`` consecutive no-delta steps. It fires ONCE per stuck
  spell: re-arming needs a delta in between, because a signal that fires on every step
  carries no information and costs a deep-tier call each time (measured on an earlier
  arm: escalation fired on 12/12 instances, every one at step 4).
* **replan** -- the same tool failing on the same target three times running, or three
  quarters of the ceiling spent with nothing landed. The plan, not the effort, is wrong.
* **done** -- a test run passed after an edit landed. Nothing here ENDS the loop; ``done``
  is a signal the caller may act on, because only the caller knows its oracle.

Pure and opt-in, like ``adk.loop_policy``: the caller feeds it steps and decides what to
do with each decision. ``message()`` is the one line to inject when it is not ``continue``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

#: Tools that change the world. Everything else is orientation.
DELTA_TOOLS = ("file_edit", "file_write", "shell_exec", "python_exec", "run_tests",
               "apply_patch", "git_apply")
#: Tools whose OK result still means nothing changed (they report, they do not act).
READ_TOOLS = ("file_read", "file_list", "file_search", "code_search", "code_symbols",
              "git_diff", "git_status", "deep_reasoning")
ACTIONS = ("continue", "replan", "escalate", "done")

_GREEN = re.compile(r"\b(\d+)\s+passed\b", re.IGNORECASE)
_RED = re.compile(r"\b(\d+)\s+(failed|error(?:s|ed)?)\b", re.IGNORECASE)


def tests_green(result_head: str) -> Optional[bool]:
    """True/False when the text is a test result, None when it is not one at all."""
    text = result_head or ""
    red = _RED.search(text)
    green = _GREEN.search(text)
    if red and int(red.group(1)) > 0:
        return False
    if green:
        return True
    return None


def is_delta(step: dict) -> bool:
    """Did this step change the world? ``work_tree_changed`` wins when the harness set it."""
    if not step.get("ok", True):
        return False
    tool = str(step.get("tool") or "")
    if step.get("work_tree_changed") is True:
        return True
    if tool in READ_TOOLS:
        return False
    return tool in DELTA_TOOLS


def _target(step: dict) -> str:
    args = step.get("args") or {}
    for key in ("path", "file", "pattern", "command", "cmd", "code"):
        val = args.get(key)
        if isinstance(val, str) and val:
            return f"{key}={val[:120]}"
    return ""


@dataclass
class Decision:
    action: str
    reason: str
    step: int
    stuck: int

    def to_dict(self) -> dict:
        return {"action": self.action, "reason": self.reason, "step": self.step,
                "stuck": self.stuck}

    def message(self) -> str:
        """What to tell the executor. ``continue`` says nothing -- silence is the signal."""
        if self.action == "escalate":
            return ("[RE-ROUTER] " + self.reason + ". Escalate now: call `deep_reasoning` "
                    "with the concrete question you are stuck on, or make the smallest "
                    "edit you can defend. Another read will not move this.")
        if self.action == "replan":
            return ("[RE-ROUTER] " + self.reason + ". The plan is wrong, not the effort. "
                    "State in one line what you now believe the defect is, then take the "
                    "one action that tests it.")
        if self.action == "done":
            return ("[RE-ROUTER] " + self.reason + ". Stop exploring: confirm the diff is "
                    "the fix you intend and finish.")
        return ""


@dataclass
class ReRouter:
    """Decide after each step. One instance per turn (or per task)."""

    stuck_threshold: int = 8
    ceiling: int = 40
    repeat_threshold: int = 3
    #: internal counters, readable as telemetry
    stuck: int = 0
    edits_landed: int = 0
    escalations: int = 0
    replans: int = 0
    armed: bool = True          # False after an escalation, until the next delta
    counts: dict = field(default_factory=lambda: {a: 0 for a in ACTIONS})
    decisions: list[dict] = field(default_factory=list)
    _fail_streak: dict = field(default_factory=dict)

    def telemetry(self) -> dict:
        return {"counts": dict(self.counts), "escalations": self.escalations,
                "replans": self.replans, "edits_landed": self.edits_landed,
                "decisions": list(self.decisions)}

    def after_step(self, step: dict, *, step_index: Optional[int] = None) -> Decision:
        n = int(step_index if step_index is not None else len(self.decisions) + 1)
        tool = str(step.get("tool") or "")
        head = str(step.get("result_head") or step.get("result") or "")
        delta = is_delta(step)

        if delta:
            self.stuck = 0
            self.armed = True
        else:
            self.stuck += 1

        if tool in ("file_edit", "file_write", "apply_patch") and step.get("ok"):
            if step.get("work_tree_changed") is not False:
                self.edits_landed += 1

        key = f"{tool}|{_target(step)}"
        if step.get("ok"):
            self._fail_streak.pop(key, None)
        else:
            self._fail_streak[key] = self._fail_streak.get(key, 0) + 1

        decision = self._decide(n, tool, head, key)
        self.counts[decision.action] = self.counts.get(decision.action, 0) + 1
        self.decisions.append(decision.to_dict())
        if decision.action == "escalate":
            self.escalations += 1
            self.armed = False
            self.stuck = 0
        elif decision.action == "replan":
            self.replans += 1
            self._fail_streak.pop(key, None)
        return decision

    def _decide(self, n: int, tool: str, head: str, key: str) -> Decision:
        ran_a_test = tool == "run_tests" or (
            tool in ("shell_exec", "python_exec") and tests_green(head) is not None)
        if ran_a_test:
            verdict = tests_green(head)
            if verdict is True and self.edits_landed:
                return Decision("done", f"tests passed after {self.edits_landed} landed edit(s)",
                                n, self.stuck)
        streak = self._fail_streak.get(key, 0)
        if streak >= self.repeat_threshold:
            return Decision("replan", f"{key.split('|')[0]} failed {streak}x on the same target",
                            n, self.stuck)
        if self.edits_landed == 0 and self.ceiling and n >= (self.ceiling * 3) // 4:
            return Decision("replan", f"step {n} of {self.ceiling} and nothing has landed",
                            n, self.stuck)
        if self.armed and self.stuck >= self.stuck_threshold:
            return Decision("escalate", f"{self.stuck} steps with no change to the work tree",
                            n, self.stuck)
        return Decision("continue", "", n, self.stuck)


def make_on_step(router: ReRouter, rec: dict, *, key: str = "reroute") -> Any:
    """Adapter for a harness whose ``on_step(rec) -> str | None`` injects text after a tool.

    Records the router's telemetry on ``rec[key]`` (a live dict, so the row carries it) and
    returns the message for any decision other than ``continue``.
    """
    rec[key] = router.telemetry()

    def on_step(r: dict) -> Optional[str]:
        steps = r.get("steps") or []
        if not steps:
            return None
        decision = router.after_step(steps[-1], step_index=len(steps))
        r[key] = router.telemetry()
        return decision.message() or None

    return on_step
