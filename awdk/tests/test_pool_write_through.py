"""test_pool_write_through — the SDK agent's knowledge-pool write-through.

The five arms of the privacy/opt-in contract (adk.pool_write_through):

1. switch unset  -> nothing happens, no network
2. configured    -> the /external/ingest payload + headers are correct
                    (tenant in BOTH the payload and X-Tenant-ID, Bearer
                    token, task+answer in content, agent in metadata)
3. failed task   -> never sent (a failed outcome is not worth pooling)
4. unreachable   -> never raises (pooling must not fail the work)
5. incomplete    -> missing token/url/tenant -> never sent

All arms monkeypatch the module's ``_urlopen`` alias (deliberately a
module-level alias so the shared urllib module is never touched).
"""

from __future__ import annotations

import json
import os

import pytest

from adk.pool_write_through import (
    report_task_to_pool,
    write_through_enabled,
)

_UNSET_ENVS = (
    "AITHER_POOL_WRITE_THROUGH",
    "AITHER_POOL_INGEST_URL",
    "AITHER_POOL_INGEST_TOKEN",
    "AITHER_TENANT_ID",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _UNSET_ENVS:
        monkeypatch.delenv(name, raising=False)


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_switch_unset_is_a_noop(monkeypatch):
    """Unset switch: nothing happens, no network, no exception."""
    called = []

    def fail(*args, **kwargs):
        called.append(1)

    monkeypatch.setattr("adk.pool_write_through._urlopen", fail)
    report_task_to_pool("t", "a", agent_name="x")
    assert not write_through_enabled()
    assert not called


def test_payload_and_headers_are_tenant_scoped(monkeypatch):
    """The payload tenant equals the header tenant equals the agent's own."""
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setenv("AITHER_POOL_WRITE_THROUGH", "true")
    monkeypatch.setenv("AITHER_POOL_INGEST_URL", "http://stub/external/ingest")
    monkeypatch.setenv("AITHER_POOL_INGEST_TOKEN", "tok123")
    monkeypatch.setenv("AITHER_TENANT_ID", "tenant-a")
    monkeypatch.setattr("adk.pool_write_through._urlopen", fake_urlopen)

    report_task_to_pool("Solve X", "Answer: 42", agent_name="worker-1")

    body = captured["body"]
    assert captured["url"] == "http://stub/external/ingest"
    assert captured["headers"]["authorization"] == "Bearer tok123"
    assert captured["headers"]["x-tenant-id"] == "tenant-a"
    assert captured["timeout"] == 15.0
    assert body["tenant_id"] == "tenant-a"
    assert body["content_type"] == "notes"
    assert body["source_name"] == "sdk_agent:worker-1"
    assert body["metadata"]["agent"] == "worker-1"
    assert body["metadata"]["kind"] == "sdk_agent_task"
    assert body["content"].startswith("TASK: Solve X")
    assert "Answer: 42" in body["content"]
    # The system prompt must NEVER be part of the payload.
    assert "system_prompt" not in body


def test_failed_task_is_never_sent(monkeypatch):
    called = []

    def fail(*args, **kwargs):
        called.append(1)

    monkeypatch.setenv("AITHER_POOL_WRITE_THROUGH", "true")
    monkeypatch.setenv("AITHER_POOL_INGEST_URL", "http://stub")
    monkeypatch.setenv("AITHER_POOL_INGEST_TOKEN", "tok")
    monkeypatch.setenv("AITHER_TENANT_ID", "tenant-a")
    monkeypatch.setattr("adk.pool_write_through._urlopen", fail)
    report_task_to_pool("t", "a", agent_name="x", success=False)
    assert not called


def test_unreachable_endpoint_never_raises(monkeypatch):
    monkeypatch.setenv("AITHER_POOL_WRITE_THROUGH", "true")
    monkeypatch.setenv("AITHER_POOL_INGEST_URL", "http://127.0.0.1:1/nope")
    monkeypatch.setenv("AITHER_POOL_INGEST_TOKEN", "tok")
    monkeypatch.setenv("AITHER_TENANT_ID", "tenant-a")
    # No monkeypatch: the REAL urllib against a closed port — the point.
    report_task_to_pool("t", "a", agent_name="x")


def test_incomplete_config_is_never_sent(monkeypatch):
    called = []

    def fail(*args, **kwargs):
        called.append(1)

    monkeypatch.setenv("AITHER_POOL_WRITE_THROUGH", "true")
    monkeypatch.setenv("AITHER_POOL_INGEST_URL", "http://stub2")
    # NO token.
    monkeypatch.setattr("adk.pool_write_through._urlopen", fail)
    report_task_to_pool("t", "a", agent_name="x")
    assert not called
