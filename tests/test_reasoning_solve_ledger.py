"""The calibrated prediction ledger (``adk.reasoning.solve._ledger``): every scored
prediction and the identity baseline land in an awdecide Brier ledger, and the run
summary says which prediction sources beat "nothing changes".

A scripted ``ModelBackend`` (no network) plays the Counter1D toy through the real
bridge and vendored core.
"""

from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("awdecide")

from adk.core.model import ModelResponse  # noqa: E402
from adk.reasoning.solve import Budget, LoopConfig, solve  # noqa: E402
from adk.reasoning.solve.envs.toy import Counter1D  # noqa: E402

GOOD = """SITUATION: a counter at 0.
ANALYSIS: none
SYNTHESIS:
```python
def step_rule(state, action):
    out = state.copy()
    if action[0] == 1:
        out[0, 0] = state[0, 0] + 1
    elif action[0] == 2:
        out[0, 0] = state[0, 0] - 1
    return out
hypothesize(step_rule, "predict")
```
EXECUTION:
```python
for _ in range(5):
    act(1, expect={"changed": True})
```"""

# action 3 does nothing on Counter1D: an inline "it changes" claim is wrong there and
# "nothing changes" is right -- the model's claims carry LESS than the baseline
WRONG = """SITUATION: a counter.
ANALYSIS: none
SYNTHESIS:
```python
pass
```
EXECUTION:
```python
for _ in range(4):
    act(3, expect={"changed": True})
act(1, expect={"changed": False})
```"""


class Backend:
    def __init__(self, replies: List[str]) -> None:
        self.name = self.model = "scripted"
        self.replies = replies
        self.n = 0

    async def generate(self, messages, *, temperature=0.7, max_tokens=None, **opts):
        text = self.replies[min(self.n, len(self.replies) - 1)]
        self.n += 1
        return ModelResponse(text=text, model=self.model, finish_reason="stop",
                             usage={"prompt_tokens": 50, "completion_tokens": 20})


def _solve(tmp_path, replies: List[str], **cfg: Any) -> Any:
    db = str(tmp_path / "ledger.db")
    res = asyncio.run(solve(Counter1D(target=5, levels=2), Backend(replies),
                            config=LoopConfig(ledger=db, **cfg)))
    return res, db


def test_a_verified_rule_beats_the_identity_baseline(tmp_path):
    res, db = _solve(tmp_path, [GOOD])
    assert res.won
    led = res.stats["ledger"]
    by = led["by_backend"]
    assert set(by) == {"hyp:step_rule", "inline", "identity"}
    assert by["identity"]["hit_rate"] == 0.0  # every action on this toy changes the counter
    assert by["hyp:step_rule"]["hit_rate"] == 1.0 and by["inline"]["hit_rate"] == 1.0
    assert led["beats_identity"] == {"hyp:step_rule": True, "inline": True}
    assert led["paired"]["hyp:step_rule"]["identity_hits"] == 0
    # the core's own ledger is untouched: made counts the same predictions
    assert res.stats["predictions"]["made"] == by["hyp:step_rule"]["n"] + by["inline"]["n"]
    # every booked row is resolved in the awdecide database, with its Brier
    from awdecide.ledger import Ledger

    rel = Ledger(tmp_path / "ledger.db").reliability()
    assert rel["resolved"] == led["rows"] == sum(v["n"] for v in by.values())
    assert rel["pending"] == 0 and set(rel["by_backend"]) == set(by)


def test_claims_worse_than_nothing_changes_do_not_beat_identity(tmp_path):
    res, _db = _solve(tmp_path, [WRONG], budget=Budget(max_llm_calls=3))
    by = res.stats["ledger"]["by_backend"]
    paired = res.stats["ledger"]["paired"]["inline"]
    assert by["inline"]["n"] >= 5 and paired["hits"] < paired["identity_hits"]
    assert res.stats["ledger"]["beats_identity"]["inline"] is False


def test_no_ledger_by_default(tmp_path):
    res = asyncio.run(solve(Counter1D(target=5, levels=2), Backend([GOOD]),
                            config=LoopConfig()))
    assert res.won and "ledger" not in res.stats
    assert not list(tmp_path.iterdir())


def test_a_requested_ledger_without_awdecide_fails_loudly(tmp_path, monkeypatch):
    from adk.reasoning.solve import _ledger

    def missing() -> Any:
        raise ImportError("LoopConfig(ledger=...) needs awdecide")

    monkeypatch.setattr(_ledger, "_import_ledger", missing)
    res, _db = _solve(tmp_path, [GOOD])
    assert res.finish_reason == "error" and "needs awdecide" in (res.error or "")
