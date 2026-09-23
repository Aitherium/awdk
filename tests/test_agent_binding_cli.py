"""`adk agent bind|swap-brain|backend|managed ...` must reach the Genesis routes.

Genesis ``routers/agent_binding.py`` serves ``/v1/agent/binding/*`` and
``/v1/agent/managed/*``; before these verbs the CLI had no caller for any of them,
so a CLI-only customer could not store the BYOK Anthropic key ``managed/deploy``
requires. Each case drives the real ``adk.cli.main`` argv path.
"""

from __future__ import annotations

import sys

import httpx
import pytest


class _Resp:
    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload if payload is not None else {"ok": True}
        self.text = str(self._payload)

    def json(self):
        return self._payload


def _run(monkeypatch, argv, resp=None):
    seen: list[dict] = []

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        seen.append({"method": method, "url": url, "json": json, "params": params,
                     "headers": headers})
        return resp or _Resp()

    monkeypatch.setenv("AITHER_API_URL", "https://api.example")
    monkeypatch.setenv("AITHER_API_KEY", "test-bearer")
    monkeypatch.setattr(httpx, "request", fake_request)
    monkeypatch.setattr(sys, "argv", ["adk", *argv])
    from adk import cli

    with pytest.raises(SystemExit) as exc:
        cli.main()
    return exc.value.code, seen


BASE = "https://api.example/v1/agent"


@pytest.mark.parametrize("argv,method,path,body,params", [
    (["agent", "bind", "pack-1", "--agent-id", "a1"], "POST", "/binding/apply-pack",
     {"listing_id": "pack-1", "agent_id": "a1"}, None),
    (["agent", "swap-brain", "acme", "brain-x"], "POST", "/binding/swap-brain",
     {"brain_company": "acme", "brain_pack": "brain-x"}, None),
    (["agent", "backend", "managed"], "POST", "/binding/backend", {"backend": "managed"}, None),
    (["agent", "managed", "status", "--agent-id", "a1"], "GET", "/managed/status", None,
     {"agent_id": "a1"}),
    (["agent", "managed", "chat", "hello"], "POST", "/managed/chat", {"message": "hello"}, None),
    (["agent", "managed", "run", "isolde", "do it"], "POST", "/managed/run",
     {"agent": "isolde", "task": "do it"}, None),
    (["agent", "managed", "resync", "--model", "m1"], "POST", "/managed/resync", {"model": "m1"}, None),
])
def test_verb_hits_genesis_route(monkeypatch, argv, method, path, body, params):
    code, seen = _run(monkeypatch, argv)
    assert code == 0
    assert len(seen) == 1
    assert seen[0]["method"] == method
    assert seen[0]["url"] == BASE + path
    assert seen[0]["json"] == body
    assert seen[0]["params"] == params
    assert seen[0]["headers"] == {"Authorization": "Bearer test-bearer"}


def test_byok_reads_key_from_env_never_argv(monkeypatch):
    monkeypatch.setenv("MY_ANTHROPIC", "fake-anthropic-value")
    code, seen = _run(monkeypatch, ["agent", "managed", "byok", "--from-env", "MY_ANTHROPIC"])
    assert code == 0
    assert seen[0]["url"] == BASE + "/managed/byok"
    assert seen[0]["json"] == {"anthropic_api_key": "fake-anthropic-value"}


def test_byok_without_key_is_a_usage_error(monkeypatch):
    monkeypatch.delenv("EMPTY_VAR", raising=False)
    code, seen = _run(monkeypatch, ["agent", "managed", "byok", "--from-env", "EMPTY_VAR"])
    assert code == 1
    assert seen == []


def test_gateway_error_exits_nonzero(monkeypatch):
    code, _ = _run(monkeypatch, ["agent", "managed", "status"],
                   resp=_Resp(403, {"detail": "Authentication required"}))
    assert code == 1


def test_existing_agent_status_verb_untouched():
    """`adk agent status <name>` (host-tier loop) must still parse as before."""
    from adk.agent_binding_client import BINDING_VERBS

    assert "status" not in BINDING_VERBS
