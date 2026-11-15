"""SystemEnv: any system from a spec -- tools, resources, skills, context, goal.

Pinned:
* the spec is validated (tool kinds, goal kinds);
* staged calls run only declared tools; missing args are an observation, not a crash;
* the world model: predict() is UNKNOWN before a call and returns the outcome after it
  (this episode), and the persistent world is consulted and fed;
* answers are scored (answer_contains fraction; regex); a full score wins the episode;
* shell tools get argv substitution, never a shell; HTTP tools refuse a different host;
* the CLI registers ``--env system``.
"""

from __future__ import annotations

import sys

import pytest

np = pytest.importorskip("numpy")

from adk.reasoning.solve.envs.system import SystemEnv, SystemSpec  # noqa: E402

KV = {"alpha": "nothing here", "beta": "the answer is 42", "gamma": "try beta"}


def kv_keys() -> str:
    return ", ".join(sorted(KV))


def kv_get(key: str) -> str:
    return KV.get(key, "no such key")


SPEC = {
    "name": "kv-store",
    "prompt": "A key-value service holds one fact about {topic}. Find it and answer it.",
    "parameters": {"topic": "the answer"},
    "context": "Three keys; one of them says where to look.",
    "skills": ["List keys first, then read the most promising one."],
    "resources": {"notes": "beta is usually right"},
    "tools": [
        {"name": "keys", "kind": "python", "ref": __name__ + ":kv_keys",
         "description": "list keys"},
        {"name": "get", "kind": "python", "ref": __name__ + ":kv_get", "params": ["key"],
         "description": "read a key"},
        {"name": "echo", "kind": "shell", "argv": [sys.executable, "-c",
                                                  "print('hi {who}')"], "params": ["who"]},
        {"name": "web", "kind": "http", "url": "https://example.invalid/{path}",
         "params": ["path"]},
    ],
    "goal": {"kind": "answer_contains", "value": ["42"]},
}


class FakeWorld:
    def __init__(self):
        self.recorded = []

    def record(self, key, before, outcome):
        self.recorded.append((key, outcome))

    def predict(self, key):
        return "from yesterday: 42" if "beta" in key else None


def _env(**kw):
    return SystemEnv(SystemSpec.from_mapping(dict(SPEC, **kw)), world=FakeWorld())


def test_spec_validation():
    with pytest.raises(ValueError):
        SystemSpec.from_mapping(dict(SPEC, goal={"kind": "vibes"}))
    with pytest.raises(ValueError):
        SystemSpec.from_mapping(dict(SPEC, tools=[{"name": "x", "kind": "telnet"}]))


def test_explore_predict_and_win():
    env = _env()
    assert "kv-store" in env.primer() and "List keys first" in env.primer()
    assert env.read_resource("notes") == "beta is usually right"
    assert env.predict("get", key="alpha").startswith("UNKNOWN")
    assert "yesterday" in env.predict("get", key="beta")        # persistent world consulted
    c = env.stage_call("get", key="gamma")
    obs = env.act((1, c, 0))
    assert env.last_text == "try beta" and obs.state[0, 0] == 1 and not obs.done
    assert env.predict("get", key="gamma").startswith("SEEN")      # this episode's model
    assert env._world.recorded and env._world.recorded[0][1] == "try beta"
    a = env.stage_answer("I am not sure")
    assert not env.act((2, a, 0)).done and env.goal_score == 0.0
    a = env.stage_answer("The answer is 42.")
    obs = env.act((2, a, 0))
    assert obs.done and obs.level_up and obs.info.get("won")


def test_tools_are_guarded():
    env = _env()
    with pytest.raises(ValueError):
        env.stage_call("rm_rf")
    c = env.stage_call("get")                                    # missing 'key'
    env.act((1, c, 0))
    assert "missing argument" in env.last_text and env.last_ok == 0
    c = env.stage_call("echo", who="world")
    env.act((1, c, 0))
    assert env.last_text == "hi world" and env.last_ok == 1
    c = env.stage_call("web", path="x@evil.example/")
    env.act((1, c, 0))
    assert env.last_ok == 0


def test_regex_goal_and_cli_registration():
    env = _env(goal={"kind": "answer_regex", "value": r"\b42\b"})
    assert env._score_answer("it is 42") == 1.0 and env._score_answer("420") == 0.0
    from adk.commands.solve import ENVIRONMENTS

    assert "system" in ENVIRONMENTS


def test_tools_hook_is_callable_not_shadowed():
    env = _env()
    hooks = env.tools(None)       # the loop calls env.tools(loop) -- must not be a dict attr
    assert {"stage_call", "predict", "list_tools"} <= set(hooks)
    fn, _doc = hooks["stage_call"]
    assert isinstance(fn("keys"), int)


def test_spec_file_env_records_replay_args(tmp_path):
    """A win is replayable later only if the episode names the spec, not just the class."""
    import json

    from adk.reasoning.solve.harvest import HarvestingBackend, build_episode

    spec = tmp_path / "kv.json"
    spec.write_text(json.dumps(dict(SPEC, tools=SPEC["tools"][:2])), encoding="utf-8")
    env = SystemEnv.from_file(str(spec))
    assert env.replay_args == ["--env", "system", "--spec", str(spec)]
    ep = build_episode(HarvestingBackend(object(), expected_model="m"), object(),
                       episode_id="e1", env=env, goal="g")
    assert ep["provenance"]["replay_args"] == ["--env", "system", "--spec", str(spec)]
    assert ep["provenance"]["env"] == "SystemEnv"
    # an env that declares nothing records None, never a guess
    plain = build_episode(HarvestingBackend(object(), expected_model="m"), object(),
                          episode_id="e2", env=_env(), goal="g")
    assert plain["provenance"]["replay_args"] is None
