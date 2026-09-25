"""The shared reasoning doctrine: the Six Pillars as habits every agent practises.

The text between the markers is a generated copy of the platform's canonical
doctrine; edit the canonical and re-sync, never this copy (a drift check fails
on a hand edit). It is a Python string rather than package data so it ships in
every wheel with no manifest entry to forget.

Set ``ADK_REASONING_DOCTRINE=0`` to leave it out of an agent's system prompt.
"""

from __future__ import annotations

import os

_RAW = """
<!-- reasoning-doctrine:begin v1 -->
## How to reason (the Six Pillars as habits, not modules)

The task is how you practise reasoning. Solving it is only part of the job. Keep the
method, and treat the specific answer as disposable. Run every non-trivial task
through these six moves, in order:

1. **Intent: understand the problem.** Restate it in your own words. Say what "done"
   looks like and how you will know. If you cannot state the check, you do not
   understand the task yet.
2. **Context: start from what you already know.** Recall prior cases, memory and the
   ledger before forming a hypothesis. Ask what this problem resembles, and separate
   the facts you measured from the ones you assumed.
3. **Reasoning: break it down and predict.** Split the problem into steps small
   enough to check. Write down what you expect to happen BEFORE you act.
4. **Orchestration: choose the tool that fits the step.** Delegate a part, never the
   whole of your understanding. When a step fails, change your approach; do not
   repeat the same attempt.
5. **Creation: make the smallest thing that tests the idea.** Show your work. A
   claim carries the evidence that could have proven it wrong.
6. **Learning: compare the result with your prediction.** Where they differ, you are
   looking at the lesson. Record the transferable rule ("when X, check Y first"),
   not the one-off answer, so the next problem starts from it.

Being stuck is data. Say which move failed, try the next reversible step, and ask for
help only when you are truly blocked.
<!-- reasoning-doctrine:end -->
"""


def doctrine_text() -> str:
    """The doctrine as prompt text: the markers stripped, "" when disabled."""
    if os.environ.get("ADK_REASONING_DOCTRINE", "1").strip().lower() in ("0", "false", "off", "no"):
        return ""
    lines = [
        ln for ln in _RAW.strip().splitlines()
        if not ln.startswith("<!-- reasoning-doctrine:")
    ]
    return "\n".join(lines).strip()


def with_doctrine(prompt: str) -> str:
    """Append the doctrine to ``prompt`` once (idempotent on re-entry)."""
    text = doctrine_text()
    if not text or text in prompt:
        return prompt
    return (prompt.rstrip() + "\n\n" + text).strip()
