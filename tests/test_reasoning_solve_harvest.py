"""Episode capture (``adk.reasoning.solve.harvest``): every solve() run becomes one
provenance-tagged record, only served==requested episodes are trainable, and a reply
from a different model is FATAL for the episode.

A scripted ``ModelBackend`` (no network) plays the 2-level Counter1D toy through the
real bridge and vendored core, so the record is checked against what the model was
actually sent.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, List

import pytest

np = pytest.importorskip("numpy")

from adk.core.model import ModelResponse  # noqa: E402
from adk.reasoning.solve import LoopConfig, solve  # noqa: E402
from adk.reasoning.solve.envs.toy import Counter1D  # noqa: E402
from adk.reasoning.solve.harvest import (  # noqa: E402
    SCHEMA,
    episode_to_examples,
    export_examples,
    read_episodes,
)

REPLY = """SITUATION: a counter at 0.
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


class Backend:
    """``model`` is what the caller asked for; ``serves`` is what every reply says."""

    def __init__(self, serves: str = "gemma4-12b", model: str = "gemma4-12b") -> None:
        self.name = "scripted"
        self.model = model
        self.serves = serves
        self.base_url = "https://127.0.0.1:8150/v1"
        self.local_only = True
        self.calls: List[List[Any]] = []

    async def generate(self, messages, *, temperature=0.7, max_tokens=None, **opts):
        self.calls.append(list(messages))
        return ModelResponse(text=REPLY, model=self.serves, finish_reason="stop",
                             usage={"prompt_tokens": 50, "completion_tokens": 20})


def _run(tmp_path, backend: Backend, env: Any = None) -> Any:
    path = str(tmp_path / "episodes.jsonl")
    res = asyncio.run(solve(env or Counter1D(target=5, levels=2), backend, goal="count to 5",
                            config=LoopConfig(harvest=path)))
    return res, path


def test_a_won_run_is_one_trainable_record_of_every_call(tmp_path):
    backend = Backend()
    res, path = _run(tmp_path, backend)
    assert res.won and res.llm_calls == 2
    (ep,) = read_episodes(path)
    assert ep["schema"] == SCHEMA and ep["trainable"] is True and ep["reward"] == 1.0
    assert len(ep["calls"]) == res.llm_calls == len(backend.calls)
    # the recorded context is exactly what the model was sent, plus its decision
    first = ep["calls"][0]
    assert [m["content"] for m in first["messages"]] == [m.content for m in backend.calls[0]]
    assert first["messages"][0]["role"] == "system" and first["reply"] == REPLY
    assert first["served_model"] == "gemma4-12b" and first["verified"] is True
    prov = ep["provenance"]
    assert prov["requested_model"] == "gemma4-12b" and prov["served_models"] == {"gemma4-12b": 2}
    assert prov["mismatches"] == 0 and prov["unverified"] == 0 and prov["local_only"] is True
    assert prov["env"] == "Counter1D" and len(prov["core_sha"]) == 40
    assert prov["config"]["harvest"] == path
    assert ep["outcome"]["finish_reason"] == "won" and ep["outcome"]["levels"] == 2
    assert [h["name"] for h in ep["verified_hypotheses"]] == ["step_rule"]
    assert res.stats["harvest"]["trainable"] is True
    with open(path, "rb") as fh:
        assert b"\r\n" not in fh.read()


def test_trainable_episodes_export_as_harvest_examples(tmp_path):
    _res, path = _run(tmp_path, Backend())
    (ep,) = read_episodes(path)
    ex = episode_to_examples(ep)
    assert len(ex) == 2
    assert ex[0]["source"] == "solve_episode" and ex[0]["messages"][-1] == {
        "role": "assistant", "content": REPLY}
    assert ex[0]["metadata"]["model"] == "gemma4-12b" and ex[0]["quality_score"] == 1.0
    assert len(ex[0]["content_hash"]) == 32 and ex[0]["id"].endswith("-0")
    out = str(tmp_path / "sft.jsonl")
    assert export_examples([path], out) == 2
    rows = [json.loads(line) for line in open(out, encoding="utf-8")]
    assert all("messages" in r for r in rows)  # the unsloth trainer's {"messages"} format
    assert episode_to_examples(ep, min_reward=1.01) == []  # rejection sampling on reward


def test_a_reply_from_another_model_is_fatal_and_never_trainable(tmp_path):
    backend = Backend(serves="claude-opus-cloud")
    res, path = _run(tmp_path, backend)
    assert not res.won
    assert "MODEL MISMATCH" in str(res.stats.get("fatal", ""))
    assert len(backend.calls) == 1  # the loop stopped calling the model after the mismatch
    (ep,) = read_episodes(path)
    assert ep["trainable"] is False and ep["provenance"]["mismatches"] == 1
    assert ep["calls"][0]["verified"] is False
    assert episode_to_examples(ep, min_reward=0.0) == []


def test_a_reply_that_names_no_model_is_unverified_and_not_exported(tmp_path):
    res, path = _run(tmp_path, Backend(serves=""))
    assert res.won  # nothing to compare: the run plays on, the record says so
    (ep,) = read_episodes(path)
    assert ep["trainable"] is False and ep["provenance"]["unverified"] == 2
    assert episode_to_examples(ep, min_reward=0.0) == []


def test_the_environment_reward_hook_overrides_won(tmp_path):
    class Graded(Counter1D):
        def reward(self) -> float:
            return 0.25

    _res, path = _run(tmp_path, Backend(), env=Graded(target=5, levels=2))
    (ep,) = read_episodes(path)
    assert ep["reward"] == 0.25 and ep["outcome"]["won"] is True
    assert episode_to_examples(ep) == [] and len(episode_to_examples(ep, min_reward=0.2)) == 2


def test_harvest_is_off_by_default(tmp_path):
    res = asyncio.run(solve(Counter1D(target=5, levels=2), Backend(),
                            config=LoopConfig(run_dir=str(tmp_path))))
    assert res.won and "harvest" not in res.stats
    assert not list(tmp_path.glob("*episodes*"))
