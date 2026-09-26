"""h31 data-grounded synthesis through the public ``LoopConfig(grounded=True)``.

* the prompt carries the EVIDENCE TABLE (one row per action family) before any rule;
* a rule with no cited row is rejected; a cited rule the rows contradict is refuted
  on the transitions that already exist -- before the turn spends an action -- and
  the refutation evidence is in the next prompt;
* ``grounded=False`` (the default) is the h30 core: no table, no plan()/evidence().
"""

from __future__ import annotations

from typing import Any, List

import pytest

np = pytest.importorskip("numpy")

from adk.reasoning.solve import LoopConfig  # noqa: E402
from adk.reasoning.solve._run import build_core_loop  # noqa: E402
from adk.reasoning.solve._vendor.interfaces import ChatReply  # noqa: E402
from adk.reasoning.solve.envs.toy import GridWalk5  # noqa: E402
from adk.reasoning.solve.memory import InMemory  # noqa: E402


class Script:
    def __init__(self, replies: List[str]) -> None:
        self.replies, self.n, self.users = replies, 0, []

    def chat(
        self, messages: Any, max_tokens: int = 1000, temperature: float = 0.4, extra: Any = None
    ) -> Any:
        self.users.append(messages[-1]["content"])
        r = self.replies[min(self.n, len(self.replies) - 1)]
        self.n += 1
        return ChatReply(r, {"prompt_tokens": 10, "completion_tokens": 5})


PROBE = """SITUATION: new
ANALYSIS: none
SYNTHESIS: none
EXECUTION:
```python
for a in [1, 1, 1, 1, 1, 3, 3, 3, 3, 3]:
    act(a)
```"""

RULES = '''SITUATION: the dot stops at the wall
ANALYSIS: none
SYNTHESIS:
```python
def always_up(state, action):
    """action 1 always moves the dot up"""
    if action != 1:
        return None
    r, c = [int(v) for v in np.argwhere(state == 1)[0]]
    return {"cells": {(r - 1, c): 1}}
def no_cite(state, action):
    return {"changed": True}
print("R1", hypothesize(always_up, note="E1: action 1 moves the dot up")["status"])
print("R2", hypothesize(no_cite)["status"])
```
EXECUTION:
```python
act(2)
```'''


def test_grounded_rules_cite_the_table_and_contradicted_ones_are_refuted_before_acting():
    llm = Script([PROBE, RULES, RULES])
    loop = build_core_loop(
        GridWalk5(levels=3), llm, LoopConfig(grounded=True, planning=False), memory=InMemory()
    )
    assert loop.cfg.grounded and "plan" in loop.sandbox.ns and "evidence" in loop.sandbox.ns
    loop.cfg.max_calls = 3
    loop.run()
    assert "EVIDENCE TABLE" in llm.users[1] and "E1 A1: 5 tried" in llm.users[1]
    ref = [h for h in loop.hyps.refuted if h.name == "always_up"]
    assert ref and ref[0].cites == ("E1",)
    assert (
        ref[0].checked_at == 10 and 0 <= ref[0].refuted_at_transition < 10
    )  # the probe's blocked A1
    assert loop.stats["uncited_rejected"] >= 1 and "no_cite" not in loop.hyps.active
    assert "REFUTATION EVIDENCE" in llm.users[2] and "always_up [cites E1]" in llm.users[2]


def test_grounded_off_is_the_h30_core():
    loop = build_core_loop(GridWalk5(), None, LoopConfig(planning=False), memory=InMemory())
    assert loop.cfg.grounded is False
    assert "plan" not in loop.sandbox.ns and "evidence" not in loop.sandbox.ns
    assert "EVIDENCE TABLE" not in loop.system and "cite" not in loop.system
