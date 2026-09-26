"""The optional-hook contract of ``adk.reasoning.solve`` matches how the loop CALLS hooks.

The reasoning loop being promoted into awdk probes every optional hook with
``getattr(hooks, name)(*args)`` (its ``_hook`` helper). A hook documented with the
wrong arity is not a documentation nit: an environment written to the doc raises
``TypeError`` the first time the loop installs its namespace. These tests pin the
arity of every hook to the loop's call sites:

* a hermetic shim that calls each hook exactly the way the loop does;
* the conformance checker, which must flag a hook with the wrong arity;
* optionally, the prototype loop itself, imported read-only from ``$ADK_H30_DIR``
  in a subprocess (skipped when that checkout or numpy is absent).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest
from adk.reasoning.solve import Action, Obs, _types
from adk.reasoning.solve.conformance import check_environment

AWDK = Path(__file__).resolve().parents[1]
OPTIONAL_HOOKS = _types.OPTIONAL_HOOKS
HOOK_ARGS: Dict[str, Tuple[str, ...]] = getattr(_types, "HOOK_ARGS", {})

#: Every optional hook the prototype loop (or its PredictionLearner) calls, with
#: the positional arguments it passes.
LOOP_CALLS: Dict[str, int] = {
    "primer": 0,
    "render": 2,  # render(obs, last)
    "describe": 1,  # describe(transition)
    "tools": 1,  # tools(loop)
    "candidates": 0,
    "state_key": 1,  # state_key(state)
    "auto_action": 0,
    "needs_xy": 1,  # needs_xy(action_id)
    "significant_change": 2,  # significant_change(before, after)
}
#: Declared by the prototype's DomainHooks but called by domain code, not the loop.
DECLARED_ONLY: Dict[str, int] = {"handoff": 1, "hud": 0}


class _Transition:
    def __init__(self, before: Any, after: Any, action: Action) -> None:
        self.before, self.after, self.action = before, after, action
        self.changed = int(before != after)


class _ShimLoop:
    """Calls hooks exactly like the prototype loop's ``_hook(name, *args)``."""

    def __init__(self, env: Any) -> None:
        self.env = env
        self.hooks = env
        self.ns: Dict[str, Any] = {}
        self.system = str(self._hook("primer", default=""))
        tools = self._hook("tools", self, default={}) or {}
        for name, (fn, _doc) in tools.items():
            self.ns[name] = fn
        self.changed_fn = getattr(self.hooks, "significant_change", None)

    def _hook(self, name: str, *args: Any, default: Any = None) -> Any:
        fn = getattr(self.hooks, name, None)
        if fn is None:
            return default
        return fn(*args)

    def turn(self) -> Tuple[str, str, List[Action]]:
        obs = self.env.observe()
        if self._hook("needs_xy", 1, default=False):
            raise AssertionError("action 1 takes no coordinates")
        a = self._hook("auto_action") or (1, -1, -1)
        before = obs.state
        obs = self.env.act(tuple(a), source="explore")
        t = _Transition(before, obs.state, tuple(a))
        key = self._hook("state_key", obs.state)
        desc = self._hook("describe", t)
        situation = self._hook("render", obs, t)
        if self.changed_fn is not None:
            assert isinstance(self.changed_fn(before, obs.state), bool)
        cands = [tuple(int(v) for v in c) for c in (self._hook("candidates", default=[]) or [])]
        return str(key), "%s | %s" % (desc, situation), cands  # type: ignore[return-value]


class ContractEnv:
    """A counter world written to the documented contract, every hook present."""

    def __init__(self) -> None:
        self.n = 0
        self.loop: Any = None

    def observe(self) -> Obs:
        return Obs(state=self.n, level=0, done=self.done())

    def act(self, action: Action, source: str = "model") -> Obs:
        self.n += 1 if action[0] == 1 else 0
        return Obs(state=self.n, level=0, done=self.done(), info={"source": source})

    def available_actions(self) -> List[int]:
        return [1, 2]

    def done(self) -> bool:
        return self.n >= 5

    # optional hooks, with the arities the loop uses
    def primer(self) -> str:
        return "count to five"

    def render(self, obs: Obs, last: Any) -> str:
        return "n=%s last=%r" % (obs.state, getattr(last, "action", last))

    def describe(self, t: Any) -> str:
        return "%s -> %s" % (t.before, t.after)

    def tools(self, loop: Any) -> Dict[str, Tuple[Callable[..., Any], str]]:
        self.loop = loop
        return {"peek": (lambda: self.n, "the counter")}

    def candidates(self) -> List[Action]:
        return [(1, -1, -1), (2, -1, -1)]

    def state_key(self, state: Any) -> str:
        return "n%s" % state

    def auto_action(self) -> Optional[Action]:
        return (1, -1, -1)

    def needs_xy(self, action_id: int) -> bool:
        return False

    def handoff(self, n: int) -> Dict[str, Any]:
        return {"actions": 0, "requested": n}

    def hud(self) -> Any:
        return None

    def significant_change(self, before: Any, after: Any) -> bool:
        return before != after


class OldContractEnv(ContractEnv):
    """``tools()`` with no argument: what the doc used to say."""

    def tools(self) -> Dict[str, Tuple[Callable[..., Any], str]]:  # type: ignore[override]
        return {}


def test_contract_names_every_hook_the_loop_calls() -> None:
    missing = sorted((set(LOOP_CALLS) | set(DECLARED_ONLY)) - set(OPTIONAL_HOOKS))
    assert not missing, "OPTIONAL_HOOKS omits %s" % missing
    assert set(HOOK_ARGS) == set(OPTIONAL_HOOKS)


@pytest.mark.parametrize("hook", sorted(set(LOOP_CALLS) | set(DECLARED_ONLY)))
def test_contract_arity_matches_the_loop_call_site(hook: str) -> None:
    want = {**LOOP_CALLS, **DECLARED_ONLY}[hook]
    assert hook in HOOK_ARGS, "no documented arity for %s" % hook
    assert len(HOOK_ARGS[hook]) == want, (hook, HOOK_ARGS[hook])


def test_contract_env_runs_under_the_loop_call_convention() -> None:
    env = ContractEnv()
    assert check_environment(env, probe=True) == []
    loop = _ShimLoop(env)
    assert env.loop is loop and loop.ns["peek"]() == 0
    key, text, cands = loop.turn()
    assert key == "n1" and "0 -> 1" in text and cands == [(1, -1, -1), (2, -1, -1)]


def test_old_contract_tools_is_flagged_and_breaks_the_loop() -> None:
    env = OldContractEnv()
    problems = check_environment(env)
    assert any("tools" in p and "1 positional" in p for p in problems), problems
    with pytest.raises(TypeError):
        _ShimLoop(env)


def test_wrong_arity_on_any_hook_is_flagged() -> None:
    class BadRender(ContractEnv):
        def render(self, obs: Obs) -> str:  # type: ignore[override]
            return ""

    class BadKey(ContractEnv):
        def state_key(self) -> str:  # type: ignore[override]
            return ""

    assert any("render" in p for p in check_environment(BadRender()))
    assert any("state_key" in p for p in check_environment(BadKey()))


# ----------------------------------------------------------------------------
# the prototype loop itself, read-only, when a checkout is available
# ----------------------------------------------------------------------------
H30_SCRIPT = textwrap.dedent(
    """
    import sys
    sys.path[:0] = [sys.argv[1], sys.argv[2]]
    import numpy as np
    from agent.repl.core.interfaces import InMemoryBackend, Obs as H30Obs
    from agent.repl.core.loop import LoopConfig, ReasoningLoop
    from adk.reasoning.solve.conformance import check_environment

    class Env:
        def __init__(self):
            self.n = 0
            self.loop = None
        def _obs(self):
            s = np.zeros((1, 8), np.int16); s[0, min(self.n, 7)] = 1
            return H30Obs(state=s, level=0, done=self.n >= 6)
        def observe(self): return self._obs()
        def act(self, action, source="model"):
            self.n += 1 if action[0] == 1 else 0
            return self._obs()
        def available_actions(self): return [1, 2]
        def done(self): return self.n >= 6
        def primer(self): return "PRIMER-SEEN"
        def render(self, obs, last): return "n=%d" % self.n
        def describe(self, t): return "moved %d" % t.changed
        def tools(self, loop):
            self.loop = loop
            return {"peek": (lambda: self.n, "counter")}
        def candidates(self): return [(1, -1, -1), (2, -1, -1)]
        def state_key(self, state): return "k%d" % int(state.argmax())
        def auto_action(self): return (1, -1, -1)
        def needs_xy(self, a): return False
        def handoff(self, n): return {"actions": 0}
        def hud(self): return None
        def significant_change(self, before, after): return bool((before != after).any())

    env = Env()
    assert check_environment(env, probe=True) == [], check_environment(env, probe=True)
    loop = ReasoningLoop(env, None, LoopConfig(prism=False), backend=InMemoryBackend())
    assert env.loop is loop and "peek" in loop.sandbox.ns
    assert "PRIMER-SEEN" in loop.system
    loop.obs = env.observe()
    r = loop.auto(2)
    assert r["actions"] == 2, r
    res = loop.sandbox.ns["act"](1)
    assert res.diff == "moved 2", res.diff  # one cell cleared, one set
    assert loop._key(loop.obs.state) == "k3"
    assert loop.seen_keys == {"k1", "k2", "k3"}, loop.seen_keys
    print("H30-LOOP-OK")
    """
)


_H30 = os.environ.get("ADK_H30_DIR", "")


@pytest.mark.skipif(
    not _H30 or not (Path(_H30) / "agent" / "repl" / "core" / "loop.py").is_file(),
    reason="set ADK_H30_DIR to a reasoning-loop prototype checkout to run this",
)
def test_prototype_loop_accepts_a_contract_env(tmp_path: Path) -> None:
    h30 = _H30
    pytest.importorskip("numpy")
    script = tmp_path / "h30_contract.py"
    script.write_text(H30_SCRIPT, encoding="utf-8")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, str(script), h30, str(AWDK)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0 and "H30-LOOP-OK" in proc.stdout, proc.stdout + proc.stderr


def test_import_adk_reasoning_needs_no_numpy_and_solve_is_lazy() -> None:
    code = (
        "import sys\n"
        "sys.modules['numpy'] = None  # any numpy import now raises ImportError\n"
        "import adk.reasoning as r\n"
        "assert 'adk.reasoning.solve' not in sys.modules\n"
        "assert r.solve.Environment is not None\n"
        "import adk.evalharness.arc_agi3\n"
        "print('LAZY-OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(AWDK),
        env=dict(os.environ, PYTHONPATH=str(AWDK), PYTHONDONTWRITEBYTECODE="1"),
    )
    assert proc.returncode == 0 and "LAZY-OK" in proc.stdout, proc.stdout + proc.stderr
