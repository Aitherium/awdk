"""``adk devices command`` -- the MDM channel from the CLI (Identity signed commands).

A fake ``_send`` stands in for Identity. Pinned: the POST carries exactly {verb, args};
--wait reads the result back from the command history (the audit trail) and the exit
code follows the device's ``ok``; a timeout is exit 3, never a silent success.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from adk import devices


def _args(**kw):
    base = dict(devices_command="command", node_id="node-opt", verb="collect-diagnostics",
                arg=[], wait=False, timeout=300.0, json=False)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def server(monkeypatch):
    st = SimpleNamespace(calls=[], history=[])

    def send(method, url, headers, timeout, body=None):
        st.calls.append((method, url, body))
        if method == "POST":
            rec = {"id": "cmd_1", "verb": body["verb"], "args": body["args"], "status": "queued"}
            st.history.append(rec)
            return 200, json.dumps({"status": "queued", "command": rec})
        return 200, json.dumps({"commands": st.history})

    monkeypatch.setattr(devices, "_send", send)
    monkeypatch.setattr(devices, "resolve_bearer", lambda: "tok")
    return st


def test_post_carries_exactly_verb_and_args(server):
    rc = devices.cmd_devices(_args(verb="upgrade", arg=["version=3.8.62"]))
    assert rc == 0
    method, url, body = server.calls[0]
    assert method == "POST" and url.endswith("/v1/nodes/node-opt/commands")
    assert body == {"verb": "upgrade", "args": {"version": "3.8.62"}}


def test_wait_reads_the_signed_result_from_the_history(server):
    def sleep(_s):
        server.history[0].update(status="done", ok=True, output='{"adk": "3.8.62"}')
    rc = devices._command(_args(wait=True), sleep=sleep, clock=iter(range(100)).__next__)
    assert rc == 0
    assert [c[0] for c in server.calls] == ["POST", "GET", "GET"]


def test_a_failed_result_is_a_failed_exit(server):
    def sleep(_s):
        server.history[0].update(status="failed", ok=False, output="no")
    assert devices._command(_args(wait=True), sleep=sleep,
                            clock=iter(range(100)).__next__) == 1


def test_no_result_inside_the_timeout_is_exit_3(server):
    t = iter([0, 0, 1000, 1000, 1000])
    assert devices._command(_args(wait=True, timeout=5), sleep=lambda s: None,
                            clock=t.__next__) == 3


def test_a_refused_verb_is_reported_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(devices, "resolve_bearer", lambda: "tok")
    monkeypatch.setattr(devices, "_send",
                        lambda m, u, h, t, body=None: (calls.append(m), (422, '{"detail":"unknown verb"}'))[1])
    assert devices.cmd_devices(_args(verb="shell")) == 1
    assert calls == ["POST"]


def test_bad_arg_shape_is_refused_before_sending(server):
    assert devices.cmd_devices(_args(arg=["novalue"])) == 1
    assert server.calls == []
