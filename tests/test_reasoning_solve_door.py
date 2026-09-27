"""The decision door on PRISM's strategy pick (``adk.reasoning.solve._door``).

The door is called with the permitted strategies only, its answer becomes the
active strategy, each strategy window's outcome is reported back, and a dead or
unsure door falls back to PRISM's own scorer. Off by default.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

pytest.importorskip("numpy")

from adk.reasoning.solve import LoopConfig  # noqa: E402
from adk.reasoning.solve._door import PrismDoor, install_door, window_reward  # noqa: E402
from adk.reasoning.solve._run import build_core_loop  # noqa: E402
from adk.reasoning.solve._vendor.prism import Diagnosis  # noqa: E402
from adk.reasoning.solve.envs.toy import GridWalk5  # noqa: E402
from adk.reasoning.solve.memory import InMemory  # noqa: E402


class FakeDoor:
    def __init__(self, answer: Any = None, source: str = "engine", fail: bool = False) -> None:
        self.answer = answer
        self.source = source
        self.fail = fail
        self.calls: List[Dict[str, Any]] = []
        self.outcomes: List[tuple] = []

    def decide(self, fork: str, state: str, *, options: List[str], timeout: float = 0) -> Any:
        self.calls.append({"fork": fork, "state": state, "options": list(options)})
        if self.fail:
            raise ConnectionError("door unreachable")
        ans = self.answer(options) if callable(self.answer) else self.answer
        return SimpleNamespace(
            decision_id="d%d" % len(self.calls), answer=ans, source=self.source, confidence=0.9
        )

    def outcome(self, decision_id: str, reward: float) -> Dict[str, Any]:
        self.outcomes.append((decision_id, reward))
        return {"ok": True}


def _loop(**cfg: Any) -> Any:
    return build_core_loop(
        GridWalk5(levels=2), None, LoopConfig(**cfg), memory=InMemory(), episode_id="door"
    )


def _door(fake: FakeDoor, **kw: Any) -> Any:
    loop = _loop()
    door = install_door(loop, decide=fake.decide, outcome=fake.outcome, **kw)
    return loop, door


def test_door_is_off_by_default():
    assert LoopConfig().door is False
    assert not hasattr(_loop(), "prism_door")


def test_the_door_picks_the_first_strategy_from_the_permitted_llm_options():
    fake = FakeDoor(answer="goal_first")
    loop, door = _door(fake)
    assert fake.calls, "the door was never asked"
    first = fake.calls[0]
    assert first["fork"] == "solve.prism"
    # initial pick: the reasoning strategies; analogy is refused (no previous program)
    assert first["options"] == ["rule_first", "goal_first"]
    assert "phase:initial" in first["state"]
    assert loop.prism.active.id == "goal_first"
    assert loop.summary()["door"]["decisions"] == 1


def test_permits_limits_the_options_and_widens_with_context():
    fake = FakeDoor(answer=lambda opts: opts[-1])
    loop, door = _door(fake)
    loop.prism.best(exclude=[])
    assert "analogy" not in fake.calls[-1]["options"]
    assert door.stats["refused_options"].get("analogy", 0) >= 1
    loop.level_programs[0] = {"actions": [(4, -1, -1)]}
    loop.prism.best(exclude=[])
    assert "analogy" in fake.calls[-1]["options"]


def test_a_rotation_goes_through_the_door_and_its_state_is_stable():
    fake = FakeDoor(answer=lambda opts: opts[0])
    loop, door = _door(fake)
    n = len(fake.calls)
    nxt = loop.prism.rotate(Diagnosis(loop.prism.active.id, "stalled", ["no new states reached"]))
    assert len(fake.calls) > n
    assert nxt.id == fake.calls[-1]["options"][0]
    s1 = door.state(False, ["rule_first"])
    assert s1 == door.state(False, ["rule_first"])
    assert "gaps:no new states reached" in s1


def test_the_window_outcome_is_reported_back_with_the_decision_id():
    fake = FakeDoor(answer="goal_first")
    loop, door = _door(fake)
    loop.prism.record("goal_first", 6, 1, 0)
    loop.prism.record("goal_first", 4, 0, 0)
    assert fake.outcomes == []  # the window is still open
    loop.prism.save()  # run end closes it
    assert fake.outcomes == [("d1", window_reward(10, 1, 0))]
    assert fake.outcomes[0][1] == pytest.approx(1.0)
    # the scorer still learned the same numbers (the fallback stays calibrated)
    assert loop.prism.scores["goal_first"]["actions"] == 10


def test_a_window_with_no_progress_is_a_negative_outcome_and_closes_on_a_switch():
    fake = FakeDoor(answer="rule_first")
    loop, door = _door(fake)
    loop.prism.record("rule_first", 12, 0, 0)
    loop.prism.record("handoff", 25, 0, 0)  # another strategy acted: the window is over
    assert fake.outcomes == [("d1", -0.25)]
    assert window_reward(0, 0, 0) == 0.0
    assert window_reward(100, 0, 2) == pytest.approx(0.2)


def test_a_dead_door_falls_back_to_the_scorer_and_is_not_asked_again():
    fake = FakeDoor(fail=True)
    loop, door = _door(fake)
    assert door.down and "door unreachable" in door.down
    assert len(fake.calls) == 1
    expected = door._orig_best(initial=True)
    assert loop.prism.active.id == expected
    loop.prism.rotate(Diagnosis(loop.prism.active.id, "stalled", ["no new states reached"]))
    assert loop.prism.active.kind == "auto"  # two auto options, door down: the scorer picked
    assert len(fake.calls) == 1
    assert door.stats["fallback_why"]["door_down"] == 2
    loop.prism.record(loop.prism.active.id, 5, 1, 0)
    loop.prism.save()
    assert fake.outcomes == []  # a scorer pick teaches the door nothing


def test_the_fallback_never_picks_a_refused_strategy():
    fake = FakeDoor(fail=True)
    loop, door = _door(fake)
    loop.prism.scores = {"analogy": {"uses": 1, "actions": 1, "levels": 5, "verified": 0}}
    assert door._orig_best(exclude=[]) == "analogy"  # the raw scorer would pick it
    assert loop.prism.best(exclude=[]) != "analogy"


@pytest.mark.parametrize(
    "answer,source,why",
    [("rule_first", "none", "door_unsure"), ("jump", "engine", "door_off_list")],
)
def test_an_unsure_or_off_list_answer_is_a_fallback(answer, source, why):
    fake = FakeDoor(answer=answer, source=source)
    loop, door = _door(fake)
    assert door.stats["fallback_why"] == {why: 1}
    assert loop.prism.active.id == door._orig_best(initial=True)
    assert door.down is None  # the door answered; it is asked again next time


def test_loopconfig_door_installs_it_through_adk_choose(monkeypatch):
    fake = FakeDoor(answer="goal_first")
    import adk.choose as choose

    monkeypatch.setattr(choose, "decide", fake.decide)
    monkeypatch.setattr(choose, "outcome", fake.outcome)
    loop = _loop(door=True, door_fork="solve.prism.test")
    assert isinstance(loop.prism_door, PrismDoor)
    assert fake.calls[0]["fork"] == "solve.prism.test"
    assert loop.prism.active.id == "goal_first"


def test_no_prism_no_door():
    loop = _loop(prism=False, door=True)
    assert loop.prism is None and not hasattr(loop, "prism_door")


def test_a_window_the_game_ended_inside_is_read_off_the_loop_counters():
    """The core skips record() when the game ends mid-turn; the win is still taught."""
    fake = FakeDoor(answer="goal_first")
    loop, door = _door(fake)
    loop.level += 1
    loop.actions += 8
    loop.prism.save()
    assert fake.outcomes == [("d1", window_reward(8, 1, 0))]
