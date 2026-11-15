"""SystemEnv: explore ANY system defined by a spec -- prompt, parameters, tools, resources,
skills and context -- with the same reasoning loop that plays games.

The solver does not need to know the domain. A spec declares the world:

.. code-block:: yaml

    name: python-package
    prompt: "Which function in {package} parses the config file? Answer with its name."
    parameters: {package: json}
    context: "A Python standard-library package."
    skills: ["Search before you read: grep the package, then open only matching files."]
    resources: {readme: "Optional text the agent may read with read_resource()"}
    tools:
      - {name: list_files, kind: shell, argv: [python, -c, "import {package},os; ..."],
         description: "list the package's files"}
      - {name: grep, kind: shell, argv: [python, -m, ...], params: [pattern]}
      - {name: lookup, kind: python, ref: "mymod:lookup", params: [key]}
      - {name: status, kind: http, url: "https://example.org/status/{service}"}
    goal: {kind: answer_contains, value: ["loads"]}       # or answer_regex / command / python
    budget: {steps: 30}

* **Look before you act.** REPL tools (free, not scored): ``list_tools()``,
  ``read_resource(name)``, ``skills()``, ``context()``, ``last_result()``,
  ``stage_call(tool, **args) -> call_id``, ``stage_answer(text) -> answer_id`` and
  ``predict(tool, **args)`` -- the env's WORLD MODEL: what the same call returned before
  (this episode, and across episodes when the ``awm`` package is installed).
* **Actions** (scored by the ledger): ``act(1, call_id)`` runs a staged tool call;
  ``act(2, answer_id)`` submits an answer to the goal; ``act(3)`` runs the goal check
  against the system's state (``command`` / ``python`` goals).
* **Winning** = the goal scores 1.0; a partial score is visible in the state.
* **State** (numpy ``(1, 6)``): ``[calls, last_ok, distinct_results, answers,
  goal_score_pct, result_digest]``.

Guards: only tools the spec declares run; shell tools take an argv list (never a shell
string) with ``{param}`` substitution per element, a timeout, and a SCRUBBED environment
(``PATH`` and OS basics plus the spec's ``env_passthrough``); HTTP tools are GET-only to
the URL template's fixed host. A guard, not an isolation boundary: run untrusted specs in
a sandbox.

Needs numpy (``adk[reason]``); PyYAML only to load ``.yaml`` specs.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import numpy as np

from .._types import Action, Obs

__all__ = ["SystemEnv", "SystemSpec", "ToolSpec", "load_spec"]

CALL, ANSWER, CHECK = 1, 2, 3
PASSTHROUGH: Tuple[str, ...] = ("PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP",
                                "HOME", "USERPROFILE", "LANG", "PYTHONIOENCODING")
_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class ToolSpec:
    name: str
    kind: str  # shell | python | http
    description: str = ""
    argv: List[str] = field(default_factory=list)
    ref: str = ""
    url: str = ""
    params: List[str] = field(default_factory=list)
    timeout_s: float = 30.0


@dataclass
class SystemSpec:
    name: str
    prompt: str
    tools: List[ToolSpec]
    goal: Dict[str, Any]
    parameters: Dict[str, Any] = field(default_factory=dict)
    context: str = ""
    skills: List[str] = field(default_factory=list)
    resources: Dict[str, str] = field(default_factory=dict)
    budget: Dict[str, Any] = field(default_factory=dict)
    env_passthrough: List[str] = field(default_factory=list)
    workdir: str = ""
    pythonpath: List[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, d: Mapping[str, Any]) -> "SystemSpec":
        tools = [ToolSpec(**{k: v for k, v in t.items() if k in ToolSpec.__dataclass_fields__})
                 for t in d.get("tools") or []]
        for t in tools:
            if t.kind not in ("shell", "python", "http"):
                raise ValueError("tool %r: kind must be shell, python or http" % t.name)
        goal = dict(d.get("goal") or {})
        if goal.get("kind") not in ("answer_contains", "answer_regex", "command", "python"):
            raise ValueError("goal.kind must be answer_contains/answer_regex/command/python")
        return cls(name=str(d.get("name") or "system"), prompt=str(d.get("prompt") or ""),
                   tools=tools, goal=goal, parameters=dict(d.get("parameters") or {}),
                   context=str(d.get("context") or ""),
                   skills=[str(s) for s in d.get("skills") or []],
                   resources={str(k): str(v) for k, v in (d.get("resources") or {}).items()},
                   budget=dict(d.get("budget") or {}),
                   env_passthrough=[str(x) for x in d.get("env_passthrough") or []],
                   workdir=str(d.get("workdir") or ""),
                   pythonpath=[str(x) for x in d.get("pythonpath") or []])


def load_spec(path: str) -> SystemSpec:
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith((".yaml", ".yml")):
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return SystemSpec.from_mapping(data)


def _fill(template: str, values: Mapping[str, Any]) -> str:
    return _PARAM.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)


def _digest(text: str) -> int:
    return int(hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:7], 16)


class SystemEnv:
    """A spec-defined system as a solver Environment (see the module docstring)."""

    output_cap = 4000

    def __init__(self, spec: SystemSpec, *, world: Optional[Any] = None) -> None:
        self.spec = spec
        self.replay_args: Optional[List[str]] = None
        self._toolspecs: Dict[str, ToolSpec] = {t.name: t for t in spec.tools}
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.answers: List[str] = []
        self.results: List[Dict[str, Any]] = []
        self.seen: Dict[str, str] = {}
        self.last_text = ""
        self.last_ok = 0
        self.goal_score = 0.0
        self.over = False
        self.level = 0
        #: world-model failures, visible instead of swallowed
        self.world_errors: List[str] = []
        import sys as _sys

        for p in spec.pythonpath:  # where the spec's python tools live
            if p and p not in _sys.path:
                _sys.path.insert(0, p)
        self._world = world if world is not None else _awm_world(spec.name)
        self._state = self._snapshot()

    @classmethod
    def from_file(cls, path: str) -> "SystemEnv":
        env = cls(load_spec(path))
        # How to rebuild THIS env from the CLI. The harvest records it, so a win can be
        # replayed later (verified rewrite) -- a class name alone cannot name the spec.
        env.replay_args = ["--env", "system", "--spec", str(path)]
        return env

    # ------------------------------------------------------------- world model
    def _key(self, tool: str, args: Mapping[str, Any]) -> str:
        return tool + json.dumps(dict(args), sort_keys=True, default=str)

    def predict(self, tool: str, **args: Any) -> str:
        """What this call did before: this episode first, then the persistent world."""
        k = self._key(tool, args)
        if k in self.seen:
            return "SEEN this episode: " + self.seen[k][:800]
        if self._world is not None:
            try:
                p = self._world.predict(k)
                if p:
                    return "PREDICTED from earlier episodes: " + str(p)[:800]
            except Exception as exc:  # noqa: BLE001 - the world model is advisory
                self.world_errors.append("predict: %s" % type(exc).__name__)
        return "UNKNOWN: never observed; calling it will teach the world model"

    # --------------------------------------------------------------- tools run
    def _env(self) -> Dict[str, str]:
        keep = set(PASSTHROUGH) | set(self.spec.env_passthrough)
        return {k: v for k, v in os.environ.items() if k in keep}

    def _run_tool(self, t: ToolSpec, args: Dict[str, Any]) -> Tuple[bool, str]:
        values = dict(self.spec.parameters, **args)
        missing = [p for p in t.params if p not in values]
        if missing:
            return False, "missing argument(s) %s for %s" % (", ".join(missing), t.name)
        try:
            if t.kind == "shell":
                argv = [_fill(a, values) for a in t.argv]
                cp = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=t.timeout_s, check=False,
                                    env=self._env(), cwd=self.spec.workdir or None)
                return cp.returncode == 0, ((cp.stdout or "") + (cp.stderr or "")).strip()
            if t.kind == "python":
                mod, _, fn = t.ref.partition(":")
                kwargs = {k: values[k] for k in t.params}  # only what the tool declares
                out = getattr(importlib.import_module(mod), fn)(**kwargs)
                return True, out if isinstance(out, str) else json.dumps(out, default=str)
            base = urllib.parse.urlsplit(t.url)
            url = _fill(t.url, {k: urllib.parse.quote(str(v), safe="") for k, v in values.items()})
            if urllib.parse.urlsplit(url).netloc != base.netloc:
                return False, "refused: the URL host must be the one the spec declares"
            with urllib.request.urlopen(url, timeout=t.timeout_s) as r:  # noqa: S310 - spec URL
                return 200 <= r.status < 300, r.read(self.output_cap).decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            return False, "TIMEOUT after %.0fs" % t.timeout_s
        except Exception as exc:  # noqa: BLE001 - a failing tool is an observation
            return False, "%s: %s" % (type(exc).__name__, exc)

    # ----------------------------------------------------------------- goal
    def _score_answer(self, answer: str) -> float:
        g = self.spec.goal
        if g["kind"] == "answer_contains":
            want = [str(w).lower() for w in (g.get("value") or [])]
            got = sum(1 for w in want if w in answer.lower())
            return got / len(want) if want else 0.0
        if g["kind"] == "answer_regex":
            return 1.0 if re.search(str(g.get("value") or "$^"), answer, re.I | re.S) else 0.0
        return self._check()

    def _check(self) -> float:
        g = self.spec.goal
        try:
            if g["kind"] == "command":
                argv = [_fill(a, self.spec.parameters) for a in g.get("argv") or []]
                cp = subprocess.run(argv, capture_output=True,
                                    timeout=float(g.get("timeout_s", 60)), check=False,
                                    env=self._env(), cwd=self.spec.workdir or None)
                return 1.0 if cp.returncode == 0 else 0.0
            if g["kind"] == "python":
                mod, _, fn = str(g.get("ref")).partition(":")
                answer = self.answers[-1] if self.answers else ""
                v = getattr(importlib.import_module(mod), fn)(answer, self)
                return max(0.0, min(1.0, float(v)))
        except Exception as exc:  # noqa: BLE001 - an unrunnable check scores 0, visibly
            self.last_text = "goal check failed: %s: %s" % (type(exc).__name__, exc)
        return 0.0

    # ---------------------------------------------------------- Environment
    def _snapshot(self) -> Any:
        distinct = len({r["digest"] for r in self.results})
        return np.array([[len(self.calls), self.last_ok, distinct, len(self.answers),
                          int(round(self.goal_score * 100)), _digest(self.last_text)]],
                        dtype=np.int64)

    def observe(self) -> Obs:
        return Obs(self._state.copy(), level=self.level, done=self.over)

    def act(self, action: Action, source: str = "model") -> Obs:
        if self.over:
            return Obs(self._state.copy(), level=self.level, done=True)
        a, x = int(action[0]), int(action[1])
        if a == CALL:
            if not 0 <= x < len(self.calls):
                self.last_ok, self.last_text = 0, "no staged call %d (use stage_call)" % x
            else:
                name, args = self.calls[x]
                ok, text = self._run_tool(self._toolspecs[name], args)
                before = int(self._state[0, 5])
                self.last_ok, self.last_text = int(ok), text[-self.output_cap:]
                k = self._key(name, args)
                self.seen[k] = self.last_text
                self.results.append({"tool": name, "args": args, "ok": ok,
                                     "digest": _digest(self.last_text)})
                if self._world is not None:
                    try:
                        self._world.record(k, before, self.last_text[:800])
                    except Exception as exc:  # noqa: BLE001 - recording is best-effort
                        self.world_errors.append("record: %s" % type(exc).__name__)
        elif a in (ANSWER, CHECK):
            if a == ANSWER and not 0 <= x < len(self.answers):
                self.last_text = "no staged answer %d (use stage_answer)" % x
            else:
                ans = self.answers[x] if a == ANSWER else ""
                self.goal_score = self._score_answer(ans) if a == ANSWER else self._check()
                self.last_text = "goal score %.2f" % self.goal_score
        else:
            self.last_text = "unknown action %d" % a
        self._state = self._snapshot()
        if self.goal_score >= 1.0:
            self.level, self.over = 1, True
            return Obs(self._state.copy(), level=1, level_up=True, done=True,
                       win_state=self._state.copy(), info={"won": True})
        return Obs(self._state.copy(), level=self.level, info={"note": self.last_text[:200]})

    def available_actions(self) -> List[int]:
        return [CALL, ANSWER, CHECK]

    def done(self) -> bool:
        return self.over

    # ---------------------------------------------------------------- hooks
    def needs_xy(self, action_id: int) -> bool:
        return int(action_id) in (CALL, ANSWER)

    def stage_call(self, tool: str, **args: Any) -> int:
        if tool not in self._toolspecs:
            raise ValueError("unknown tool %r; list_tools() shows the ones this system has" % tool)
        self.calls.append((tool, args))
        return len(self.calls) - 1

    def stage_answer(self, text: str) -> int:
        self.answers.append(str(text))
        return len(self.answers) - 1

    def list_tools(self) -> List[str]:
        return ["%s(%s): %s" % (t.name, ", ".join(t.params), t.description)
                for t in self.spec.tools]

    def read_resource(self, name: str) -> str:
        if name not in self.spec.resources:
            return "no resource %r; have: %s" % (name, ", ".join(self.spec.resources))
        v = self.spec.resources[name]
        p = Path(v)
        if len(v) < 400 and p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")[: self.output_cap]
        return v[: self.output_cap]

    def primer(self) -> str:
        task = _fill(self.spec.prompt, self.spec.parameters)
        return (
            "DOMAIN: system exploration (%s). Explore the system with its tools, learn how it "
            "behaves, then answer.\nTASK: %s\n%s"
            "Tools (free, unscored): list_tools(); read_resource(name); skills(); context(); "
            "last_result(); predict(tool, **args) -> what that call did before (the world "
            "model); stage_call(tool, **args) -> call_id; stage_answer(text) -> answer_id.\n"
            "Actions (scored): act(1, call_id, 0) runs a staged tool call; act(2, answer_id, 0) "
            "submits an answer; act(3) runs the goal check on the system's state.\n"
            "State (1x6): [calls, last_ok, distinct_results, answers, goal_score_pct, "
            "result_digest]. Winning = goal score 100.\n"
            "System tools: %s\nResources: %s\nSkills:\n- %s"
            % (self.spec.name, task,
               ("CONTEXT: " + self.spec.context + "\n") if self.spec.context else "",
               "; ".join(self.list_tools()) or "none",
               ", ".join(self.spec.resources) or "none",
               "\n- ".join(self.spec.skills) or "none")
        )

    def render(self, obs: Obs, last: Any) -> str:
        return "calls %d, answers %d, goal score %.2f\nlast result:\n%s" % (
            len(self.calls), len(self.answers), self.goal_score, self.last_text[-1500:])

    def describe(self, t: Any) -> str:
        return self.last_text[:200] or "no change"

    def tools(self, loop: Any) -> Dict[str, Tuple[Callable[..., Any], str]]:
        return {
            "list_tools": (self.list_tools, "list_tools() -> the system's tools"),
            "read_resource": (self.read_resource, "read_resource(name) -> text"),
            "skills": (lambda: list(self.spec.skills), "skills() -> procedures to follow"),
            "context": (lambda: self.spec.context, "context() -> background"),
            "last_result": (lambda: self.last_text, "last_result() -> last tool output"),
            "predict": (self.predict, "predict(tool, **args) -> what that call did before"),
            "stage_call": (self.stage_call, "stage_call(tool, **args) -> call_id"),
            "stage_answer": (self.stage_answer, "stage_answer(text) -> answer_id"),
        }


class _AwmWorld:
    """Tool-effect world model in ``awm`` (scope ``local:solver:<system>``), when installed.

    Each call is a transition ``(system, call) -> {result}``; ``predict`` asks awm's
    world model, which answers RECALLED (the majority observed outcome of that exact
    call), PREDICTED or GENERALIZED -- with its source, so a guess never reads as fact.
    """

    def __init__(self, name: str) -> None:
        import time

        import awm  # type: ignore[import-not-found]
        from awm.cli import DEFAULT_DB  # type: ignore[import-not-found]

        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", name)[:60] or "system"
        self._awm, self._time = awm, time
        self.store = awm.MemoryStore(DEFAULT_DB)
        self.scope = awm.Scope.parse("local:solver:" + safe)
        self._before = self._state({"system": safe})

    def _state(self, slots: Dict[str, str]) -> Any:
        digest = hashlib.sha1(json.dumps(slots, sort_keys=True).encode("utf-8")).hexdigest()
        return self._awm.WorldState(digest, dict(slots), str(self.scope), None, None,
                                    self._time.time())

    def record(self, key: str, before: int, outcome: str) -> None:
        after = self._state(dict(self._before.slots, result=outcome[:400]))
        self.store.observe_transition(self.scope, self._before, key[:300], after)

    def predict(self, key: str) -> Optional[str]:
        p = self.store.predict_outcome(self.scope, self._before, key[:300])
        if getattr(p, "source", "NONE") == "NONE" or not getattr(p, "delta", None):
            return None
        return "%s (%s, confidence %.2f)" % (p.delta.get("result"), p.source, p.confidence)


def _awm_world(name: str) -> Optional[Any]:
    try:
        return _AwmWorld(name)
    except Exception:  # noqa: BLE001 - awm is optional
        return None
