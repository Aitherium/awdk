"""`adk pair` names a transient platform failure with its own exit code (75).

Measured 2026-10-21/22 on the owner's laptop: Identity's /v1/nodes/register answered 503
while its Directory restarted ("directory get_entry ... Connection refused"), `adk pair`
exited 1 like any refusal, and the installer reported "mesh fail adk exit 1". The retry
cannot live in awdk: Identity leaves the code SPENT on a 5xx, so only a caller that can
mint a fresh code (the installers) can retry -- and it needs to know WHEN to. Pinned:

- a 5xx answer and a transport failure exit 75 (EX_TEMPFAIL) and say the code is spent;
- a 4xx (expired/used code, wrong class) still exits 1: a refusal is never retried.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from adk import devices, enrollment, node_pairing


class _Resp:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code = status
        self.text = text

    def json(self):  # pragma: no cover - only a 200 is parsed
        return {}


def _client(answer):
    class _C:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            if isinstance(answer, BaseException):
                raise answer
            return answer

    return _C


@pytest.fixture
def _offline(monkeypatch):
    from adk import fleet_enroll

    monkeypatch.setattr(fleet_enroll, "_load_node_auth", lambda: {"node_id": "lap-1"})
    monkeypatch.setattr(enrollment, "build_registration",
                        lambda node_id, **kw: {"node_id": node_id, "node_class": "laptop"})
    monkeypatch.setattr(devices, "enroll_base", lambda: "https://idp.test")
    monkeypatch.delenv("AITHER_PORTAL_URL", raising=False)


def _pair(monkeypatch, answer):
    monkeypatch.setattr(httpx, "AsyncClient", _client(answer))
    return asyncio.run(node_pairing.pair_with_code("ABCD2345", "https://idp.test"))


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_5xx_is_transient(monkeypatch, _offline, status):
    out = _pair(monkeypatch, _Resp(status, '{"detail":"Cannot register: directory get_entry"}'))
    assert out["paired"] is False
    assert out["transient"] is True and out["http_status"] == status


def test_a_transport_failure_is_transient(monkeypatch, _offline):
    out = _pair(monkeypatch, httpx.ConnectError("Connection refused"))
    assert out["paired"] is False and out["transient"] is True


@pytest.mark.parametrize("status", [400, 402, 403, 422, 429])
def test_a_refusal_is_not_transient(monkeypatch, _offline, status):
    out = _pair(monkeypatch, _Resp(status, "no"))
    assert out["paired"] is False and out["transient"] is False


def _cmd(monkeypatch, capsys, answer) -> tuple[int, str]:
    monkeypatch.setattr(httpx, "AsyncClient", _client(answer))
    rc = node_pairing.cmd_pair(SimpleNamespace(code="ABCD2345", portal="", node_class="laptop",
                                               no_autostart=True))
    return rc, capsys.readouterr().out


def test_cmd_pair_exits_75_on_the_measured_503(monkeypatch, capsys, _offline):
    rc, out = _cmd(monkeypatch, capsys, _Resp(
        503, '{"detail":"Cannot register node: directory get_entry failed: Connection refused"}'))
    assert rc == node_pairing.EXIT_TRANSIENT == 75
    assert "HTTP 503" in out and "code is spent" in out


def test_cmd_pair_exits_75_when_identity_is_unreachable(monkeypatch, capsys, _offline):
    rc, _ = _cmd(monkeypatch, capsys, httpx.ConnectError("Connection refused"))
    assert rc == 75


def test_cmd_pair_still_exits_1_on_an_expired_code(monkeypatch, capsys, _offline):
    rc, out = _cmd(monkeypatch, capsys, _Resp(400, '{"detail":"Invalid or expired pairing code"}'))
    assert rc == 1 and "codes live 5 minutes" in out
