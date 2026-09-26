"""The Environment seam of adk.reasoning.solve and its conformance checker."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

import pytest
from adk.reasoning.solve import Action, Environment, Obs
from adk.reasoning.solve.conformance import assert_environment, check_environment


class Counter:
    """Minimal conformant environment: reach 3 with action 1."""

    def __init__(self) -> None:
        self.s = 0

    def observe(self) -> Obs:
        return Obs(state=self.s, done=self.done())

    def act(self, action: Action, source: str = "model") -> Obs:
        self.s += 1 if action[0] == 1 else -1
        return Obs(state=self.s, done=self.done())

    def available_actions(self) -> List[int]:
        return [1, 2]

    def done(self) -> bool:
        return self.s >= 3

    def primer(self) -> str:
        return "count to 3"


@dataclass
class ForeignObs:
    """An observation class that is NOT adk's Obs (the h30 prototype shape)."""

    state: Any
    level: int = 0
    level_up: bool = False
    died: bool = False
    done: bool = False
    win_state: Any = None
    info: Dict[str, Any] = field(default_factory=dict)


class ForeignEnv(Counter):
    def observe(self) -> ForeignObs:  # type: ignore[override]
        return ForeignObs(state=self.s)


def test_conformant_env_passes_isinstance_and_probe() -> None:
    env = Counter()
    assert isinstance(env, Environment)
    assert check_environment(env, probe=True) == []
    assert_environment(env, probe=True)


def test_probe_never_acts() -> None:
    env = Counter()
    check_environment(env, probe=True)
    assert env.s == 0


def test_foreign_obs_shape_is_accepted() -> None:
    assert check_environment(ForeignEnv(), probe=True) == []


def test_missing_method_is_reported() -> None:
    class NoDone:
        def observe(self) -> Obs:
            return Obs(state=0)

        def act(self, action: Action, source: str = "model") -> Obs:
            return Obs(state=0)

        def available_actions(self) -> List[int]:
            return []

    env = NoDone()
    assert not isinstance(env, Environment)
    problems = check_environment(env)
    assert problems == ["missing required method done()"]
    with pytest.raises(TypeError, match="done"):
        assert_environment(env)


def test_act_without_source_is_reported() -> None:
    class NoSource(Counter):
        def act(self, action: Action) -> Obs:  # type: ignore[override]
            return Obs(state=0)

    assert any("source" in p for p in check_environment(NoSource()))


def test_non_callable_hook_is_reported() -> None:
    env = Counter()
    env.state_key = "not callable"  # type: ignore[assignment]
    assert any("state_key" in p for p in check_environment(env))


def test_probe_catches_bad_return_shapes() -> None:
    class Bad(Counter):
        def available_actions(self) -> List[int]:
            return ["1"]  # type: ignore[list-item]

        def done(self) -> bool:
            return 0  # type: ignore[return-value]

        def observe(self) -> Obs:
            return {"state": 0}  # type: ignore[return-value]

    env = Bad()
    assert check_environment(env) == []  # structurally fine...
    problems = check_environment(env, probe=True)  # ...but the shapes are wrong
    assert any("observe()" in p for p in problems)
    assert any("available_actions()" in p for p in problems)
    assert any("done()" in p for p in problems)


def test_probe_reports_a_raising_observe() -> None:
    class Raises(Counter):
        def observe(self) -> Obs:
            raise RuntimeError("boom")

    assert any("raised RuntimeError" in p for p in check_environment(Raises(), probe=True))


def test_solve_package_does_not_import_numpy_or_arc() -> None:
    import ast
    from pathlib import Path

    import adk.reasoning.solve as solve

    root = Path(solve.__file__).parent
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            names: List[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for n in names:
                assert not n.startswith(("numpy", "arc", "adk.evalharness")), (path.name, n)
