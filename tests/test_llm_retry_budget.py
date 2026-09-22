"""The OpenAI-compatible client's retry budget is tunable per run (ADK_LLM_MAX_RETRIES,
ADK_LLM_RETRY_WAIT_S) so a benchmark can WAIT for a busy lane instead of dying on a 502
after 6 seconds -- and the defaults are unchanged for everyone else."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from adk.llm import openai_compat as oc


class _Resp:
    def __init__(self, status: int):
        self.status_code = status
        self.headers: dict[str, str] = {}
        self.text = f"status {status}"

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://x")
            raise httpx.HTTPStatusError("bad", request=req,
                                        response=httpx.Response(self.status_code, request=req))


class _Client:
    def __init__(self, statuses: list[int]):
        self.statuses = list(statuses)
        self.calls = 0

    async def post(self, url, json=None):
        self.calls += 1
        return _Resp(self.statuses.pop(0) if self.statuses else 200)


def _provider():
    return oc.OpenAIProvider(base_url="http://x/v1", api_key="k", default_model="m")


def _run(prov, client, sleeps, monkeypatch):
    async def fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(oc.asyncio, "sleep", fake_sleep)
    return asyncio.run(prov._post_with_retry(client, "http://x/v1/chat/completions", {}))


def test_default_budget_is_three_attempts_exponential(monkeypatch):
    monkeypatch.delenv("ADK_LLM_MAX_RETRIES", raising=False)
    monkeypatch.delenv("ADK_LLM_RETRY_WAIT_S", raising=False)
    assert oc._retry_budget() == (3, 0.0)
    sleeps: list[float] = []
    client = _Client([502, 502, 502, 200])
    with pytest.raises(Exception):
        _run(_provider(), client, sleeps, monkeypatch)
    assert client.calls == 3 and sleeps == [2, 4]


def test_env_budget_waits_for_a_busy_lane(monkeypatch):
    monkeypatch.setenv("ADK_LLM_MAX_RETRIES", "6")
    monkeypatch.setenv("ADK_LLM_RETRY_WAIT_S", "20")
    assert oc._retry_budget() == (6, 20.0)
    sleeps: list[float] = []
    client = _Client([502, 502, 502, 502, 200])
    resp = _run(_provider(), client, sleeps, monkeypatch)
    assert resp.status_code == 200 and client.calls == 5
    assert sleeps == [20, 20, 20, 20]  # the floor beats 2^n until 2^n exceeds it


def test_budget_is_clamped_and_typo_safe(monkeypatch):
    monkeypatch.setenv("ADK_LLM_MAX_RETRIES", "9999")
    monkeypatch.setenv("ADK_LLM_RETRY_WAIT_S", "not-a-number")
    assert oc._retry_budget() == (30, 0.0)
    monkeypatch.setenv("ADK_LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("ADK_LLM_RETRY_WAIT_S", "999")
    assert oc._retry_budget() == (1, 120.0)


class _FlakyClient:
    """Raises a transport error N times, then answers 200."""
    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    async def post(self, url, json=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise httpx.ConnectError("All connection attempts failed")
        return _Resp(200)


def test_transport_errors_share_the_retry_budget(monkeypatch):
    monkeypatch.setenv("ADK_LLM_MAX_RETRIES", "5")
    monkeypatch.setenv("ADK_LLM_RETRY_WAIT_S", "10")
    sleeps: list[float] = []
    client = _FlakyClient(3)
    resp = _run(_provider(), client, sleeps, monkeypatch)
    assert resp.status_code == 200 and client.calls == 4 and sleeps == [10, 10, 10]


def test_transport_error_on_the_last_attempt_is_raised(monkeypatch):
    monkeypatch.delenv("ADK_LLM_MAX_RETRIES", raising=False)
    monkeypatch.delenv("ADK_LLM_RETRY_WAIT_S", raising=False)
    sleeps: list[float] = []
    client = _FlakyClient(99)
    with pytest.raises(httpx.ConnectError):
        _run(_provider(), client, sleeps, monkeypatch)
    assert client.calls == 3 and sleeps == [2, 4]
