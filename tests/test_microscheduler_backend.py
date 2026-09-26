"""MicroSchedulerBackend (sync chat over the scheduler's /v1) and the ARC llm policy.

Hermetic: the scheduler is an ``httpx.MockTransport``. The live check (a real
game through a real scheduler) is ``python -m adk.evalharness.arc_agi3.suite
--policy llm``; it is not a unit test.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest
from adk.core.backends.microscheduler import (
    PREFERRED_MODELS,
    MicroSchedulerBackend,
    SchedulerUnavailableError,
)
from adk.core.model import Message
from adk.evalharness.arc_agi3.llm_policy import llm_policy, parse_actions
from adk.evalharness.arc_agi3.rhae import ActionLedger
from adk.evalharness.arc_agi3.suite import run_suite
from adk.reasoning.solve import Action, Obs

MODELS = {"data": [{"id": "gemma4-12b"}, {"id": "bonsai2-27b"}, {"id": "nomic-embed-text"}]}


def _backend(handler: Any, **kw: Any) -> MicroSchedulerBackend:
    b = MicroSchedulerBackend(base_url="https://sched.test:8150", **kw)
    b._client = httpx.Client(base_url=b.base_url, transport=httpx.MockTransport(handler))
    return b


def _ok(text: str, usage: Dict[str, int] | None = None) -> Any:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/models":
            return httpx.Response(200, json=MODELS)
        body = json.loads(req.content)
        assert body["stream"] is False and body["max_tokens"] > 0
        out: Dict[str, Any] = {
            "model": body["model"],
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        }
        if usage is not None:
            out["usage"] = usage
        return httpx.Response(200, json=out)

    return handler


def test_chat_counts_tokens_from_usage() -> None:
    b = _backend(_ok("A1", {"prompt_tokens": 120, "completion_tokens": 7, "total_tokens": 127}))
    r = b.chat([{"role": "user", "content": "go"}], max_tokens=50)
    b.chat([Message(role="user", content="again")], max_tokens=50)
    assert r.content == "A1" and r.usage["prompt_tokens"] == 120
    s = b.stats()
    assert (s["llm_calls"], s["prompt_tokens"], s["completion_tokens"]) == (2, 240, 14)
    assert s["usage_estimated"] == 0


def test_chat_without_usage_is_estimated_and_flagged() -> None:
    b = _backend(_ok("A2 " * 10))
    b.chat([{"role": "user", "content": "x" * 400}])
    assert b.prompt_tokens >= 100 and b.completion_tokens > 0 and b.usage_estimated == 1


def test_auto_model_takes_the_strongest_preferred_the_scheduler_lists() -> None:
    b = _backend(_ok("A1"))
    assert b.model == "auto"
    assert b.resolve_model() == "bonsai2-27b"  # listed, and ahead of gemma4-12b
    assert PREFERRED_MODELS.index("bonsai2-27b") < PREFERRED_MODELS.index("gemma4-12b")
    assert b.model_source.startswith("auto")
    assert _backend(_ok("A1"), model="gemma4-12b").resolve_model() == "gemma4-12b"


def test_scheduler_5xx_and_unreachable_are_backend_dead() -> None:
    b = _backend(lambda req: httpx.Response(500, text="Internal Server Error"), model="m")
    with pytest.raises(SchedulerUnavailableError) as ei:
        b.chat([{"role": "user", "content": "go"}])
    assert ei.value.backend_dead and ei.value.status == 500 and b.errors == 1

    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=req)

    with pytest.raises(SchedulerUnavailableError, match="unreachable"):
        _backend(refuse, model="m").chat([{"role": "user", "content": "go"}])


def test_4xx_is_a_caller_error_not_a_dead_backend() -> None:
    b = _backend(lambda req: httpx.Response(422, text="max_tokens too large"), model="m")
    with pytest.raises(RuntimeError) as ei:
        b.chat([{"role": "user", "content": "go"}])
    assert not getattr(ei.value, "backend_dead", False)


def test_generate_is_the_async_model_backend_surface() -> None:
    b = _backend(_ok("hello", {"prompt_tokens": 3, "completion_tokens": 1}), model="m")
    resp = asyncio.run(b.generate([Message(role="user", content="hi")], max_tokens=8))
    assert resp.text == "hello" and resp.usage["completion_tokens"] == 1


def test_tls_is_verified_with_the_shared_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[Any] = []

    class Spy:
        def __init__(self, **kw: Any) -> None:
            seen.append(kw.get("verify"))

    monkeypatch.delenv("AITHER_TLS_VERIFY", raising=False)
    monkeypatch.setattr(httpx, "Client", Spy)
    MicroSchedulerBackend(base_url="https://x:8150")._http()
    assert seen and seen[0] is not False
    src = Path(__import__("adk.core.backends.microscheduler").core.backends.microscheduler.__file__)
    tree = ast.parse(src.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "verify":
            assert not (isinstance(node.value, ast.Constant) and node.value.value is False)


# ----------------------------------------------------------------------------
# the llm policy through run_suite
# ----------------------------------------------------------------------------
class CounterEnv:
    """Three ACTION1 clear the only level."""

    def __init__(self) -> None:
        self.ledger = ActionLedger()
        self.ledger.record(0, 0)
        self.count = 0

    @property
    def actions(self) -> int:
        return self.ledger.actions

    @property
    def level_actions(self) -> List[int]:
        return list(self.ledger.level_actions)

    def observe(self) -> Obs:
        return Obs(state=self.count, level=int(self.done()), done=self.done())

    def act(self, action: Action, source: str = "model") -> Obs:
        self.count += action[0] == 1
        self.ledger.record(action[0], int(self.done()))
        return self.observe()

    def available_actions(self) -> List[int]:
        return [1, 2]

    def done(self) -> bool:
        return self.count >= 3

    def render(self, obs: Obs, last: Any) -> str:
        return "count=%s" % obs.state


def test_parse_actions() -> None:
    assert parse_actions("A1\nACTION 2\nA6 3 4\nA9", [1, 2, 6], 5) == [
        (1, -1, -1),
        (2, -1, -1),
        (6, 3, 4),
    ]
    assert parse_actions("A3", [1, 2], 5) == []


def test_llm_policy_row_carries_tokens() -> None:
    b = _backend(_ok("A1\nA1", {"prompt_tokens": 50, "completion_tokens": 4}), model="m")
    res = run_suite(
        llm_policy(b),
        ["cnt"],
        seeds=[0],
        baselines={"cnt": [3]},
        make_env=lambda g, s: CounterEnv(),
    )
    row = res.rows[0]
    assert row["levels"] == 1 and row["llm_calls"] == 2
    assert row["prompt_tokens"] == 100 and row["completion_tokens"] == 8 and row["model"] == "m"
    assert res.summary["prompt_tokens"] == 100 and res.exit_code in (0, 1)


def test_dead_backend_on_every_row_exits_2() -> None:
    b = _backend(lambda req: httpx.Response(503, text="down"), model="m")
    res = run_suite(
        llm_policy(b),
        ["cnt"],
        seeds=[0, 1],
        baselines={"cnt": [3]},
        make_env=lambda g, s: CounterEnv(),
    )
    assert all(r["backend_dead"] for r in res.rows)
    assert res.rows[0]["abandon_reason"].startswith("backend_dead")
    assert res.summary["backend_dead_rows"] == 2 and res.exit_code == 2


def _routed(served_by: str, cross: bool) -> Any:
    """The scheduler's real reply shape: ``aither_route`` names who answered."""

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "model": served_by,
                "aither_route": {
                    "requested": body["model"],
                    "served_by": served_by,
                    "cross_model": cross,
                },
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            },
        )

    return handler


def test_a_cross_model_reply_is_refused_as_a_dead_backend() -> None:
    """Measured 2026-09-25: a busy deepseek-v4-flash-pool slot was answered by
    deepseek_api (a cloud model). An eval row scored on that measures the wrong
    model, so it is refused -- and it is backend_dead, never a scored zero."""
    from adk.core.backends.microscheduler import CrossModelRouteError

    b = _backend(_routed("deepseek_api", True), model="deepseek-v4-flash-pool")
    with pytest.raises(CrossModelRouteError) as ei:
        b.chat([{"role": "user", "content": "go"}])
    assert ei.value.backend_dead and ei.value.served_by == "deepseek_api"
    assert b.stats()["cross_model_replies"] == 1 and b.calls == 0


def test_served_by_mismatch_is_refused_even_without_the_flag() -> None:
    from adk.core.backends.microscheduler import CrossModelRouteError

    b = _backend(_routed("kimi-k3", False), model="bonsai2-27b")
    with pytest.raises(CrossModelRouteError):
        b.chat([{"role": "user", "content": "go"}])


def test_a_same_model_reply_passes_and_names_who_served() -> None:
    b = _backend(_routed("gemma4-12b", False), model="gemma4-12b")
    r = b.chat([{"role": "user", "content": "go"}])
    assert r.model == "gemma4-12b" and b.stats()["cross_model_replies"] == 0


def test_allow_cross_model_accepts_but_still_counts() -> None:
    b = _backend(
        _routed("deepseek_api", True), model="deepseek-v4-flash-pool", allow_cross_model=True
    )
    r = b.chat([{"role": "user", "content": "go"}])
    assert r.model == "deepseek_api" and b.stats()["cross_model_replies"] == 1


def _capture(seen: List[Dict[str, Any]]) -> Any:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"content": "A1"}, "finish_reason": "stop"}],
            },
        )

    return handler


def test_every_request_opts_out_of_cloud_failover_by_default() -> None:
    """Owner rule 2026-09-25: eval runs are LOCAL ONLY. Measured the same day, a
    busy pool slot sent eval requests to deepseek_api; refusing the reply after
    the fact is too late, so the opt-out rides on EVERY request."""
    seen: List[Dict[str, Any]] = []
    b = _backend(_capture(seen), model="deepseek-v4-flash-pool")
    b.chat([{"role": "user", "content": "go"}])
    asyncio.run(b.generate([Message(role="user", content="go")]))
    assert len(seen) == 2
    assert all(s["metadata"]["local_only"] is True for s in seen)
    assert b.stats()["local_only"] is True


def test_extra_metadata_cannot_drop_the_opt_out() -> None:
    seen: List[Dict[str, Any]] = []
    b = _backend(_capture(seen), model="deepseek-v4-flash-pool")
    b.chat([{"role": "user", "content": "go"}], extra={"metadata": {"effort": 9}})
    assert seen[0]["metadata"] == {"source": "adk.reasoning", "effort": 9, "local_only": True}


def test_local_only_false_is_an_explicit_choice() -> None:
    seen: List[Dict[str, Any]] = []
    b = _backend(_capture(seen), model="deepseek-v4-flash-pool", local_only=False)
    b.chat([{"role": "user", "content": "go"}])
    assert "local_only" not in seen[0]["metadata"]
