"""The device runs only what its control plane signed for it, and says what it did.

The control plane queues a command; the heartbeat collects it. These pin the
device half (``adk.node_commands`` + the beat loop): a command runs only when its
signature, device id, expiry and verb all check out and it has not run before;
a refused command runs nothing and answers nothing; a result goes back signed;
a device enrolled before the channel existed fetches its key once. No network:
the HTTP client is a recording stand-in.
"""

import asyncio
import json
import time

import pytest

from adk import enrollment, node_commands

KEY = "ab" * 32
NODE = "deck-1"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    yield tmp_path


def _cmd(**over):
    cmd = {"id": "cmd_1", "tenant_id": "tnt", "node_id": NODE, "verb": "lend-on",
           "args": {"via": "lan"}, "issued_by": "u1", "issued_at": int(time.time()),
           "expires_at": int(time.time()) + 600}
    cmd.update(over)
    cmd["sig"] = node_commands.sign(KEY, node_commands.canonical(cmd))
    return cmd


def _handlers(ran):
    return {v: (lambda a, v=v: ran.append((v, a)) or {"done": v}) for v in node_commands.VERBS}


def test_a_signed_command_for_this_device_runs_once_and_answers_signed():
    ran = []
    (res,) = node_commands.run_commands([_cmd()], NODE, KEY, handlers=_handlers(ran))
    assert ran == [("lend-on", {"via": "lan"})]
    assert res["ok"] is True and json.loads(res["output"]) == {"done": "lend-on"}
    assert res["sig"] == node_commands.sign(
        KEY, node_commands.canonical(res, node_commands.RESULT_FIELDS))
    assert node_commands.run_commands([_cmd()], NODE, KEY, handlers=_handlers(ran)) == []
    assert len(ran) == 1


@pytest.mark.parametrize("cmd,why", [
    (lambda: {**_cmd(), "args": {"via": "tunnel"}}, "bad signature"),
    (lambda: _cmd(node_id="other"), "addressed to another device"),
    (lambda: _cmd(expires_at=int(time.time()) - 1), "expired"),
    (lambda: _cmd(verb="shell", args={}), "not allowed on this device"),
    (lambda: _cmd(args={"via": "http://x"}), "not allowed for lend-on"),
    (lambda: _cmd(args={"cmd": "lan"}), "not allowed for lend-on"),
    (lambda: {k: v for k, v in _cmd().items() if k != "sig"}, "bad signature"),
])
def test_anything_else_runs_nothing_and_answers_nothing(cmd, why):
    c = cmd()
    assert why in node_commands.verify(c, KEY, NODE, seen=[])
    ran = []
    assert node_commands.run_commands([c], NODE, KEY, handlers=_handlers(ran)) == []
    assert ran == []


def test_no_key_on_the_device_runs_nothing():
    assert node_commands.verify(_cmd(), "", NODE, seen=[]) == "no command key on this device"


def test_a_failing_handler_reports_not_raises():
    def boom(_a):
        raise RuntimeError("relay binary missing")
    (res,) = node_commands.run_commands([_cmd()], NODE, KEY, handlers={"lend-on": boom})
    assert res["ok"] is False and "relay binary missing" in res["output"]


def test_the_key_is_kept_per_device():
    assert node_commands.save_key(NODE, KEY)
    assert node_commands.load_key(NODE) == KEY
    assert node_commands.load_key("another") == ""


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class _Client:
    """Answers the beat with commands, the register with a key, results with 200."""

    def __init__(self, commands, key=KEY):
        self.commands, self.key, self.posts = commands, key, []

    async def post(self, url, json=None, headers=None):
        self.posts.append((url, json))
        if url.endswith("/heartbeat"):
            return _Resp(200, {"status": "ok", "commands": self.commands})
        if url.endswith("/v1/nodes/register"):
            return _Resp(200, {"status": "registered", "command_key": self.key})
        return _Resp(200, {"status": "done"})


def _beat_once(client, monkeypatch, ran):
    monkeypatch.setattr(enrollment, "build_registration", lambda node_id, **kw: {
        "node_id": node_id, "inference_ready": False, "available_models": [],
        "gpu_vram_mb": 0, "inference_url": "", "inference_kind": "none"})
    monkeypatch.setattr(node_commands, "_HANDLERS", _handlers(ran))
    asyncio.run(enrollment._heartbeat_beats(
        client, "https://idp.test", {"Authorization": "Bearer t"}, NODE, interval=0,
        inference_url=None, node_class="deck", max_beats=1, reach_provider=None,
        harness_provider=None, beat_immediately=True))


def test_a_device_without_a_key_fetches_it_once_then_runs_and_reports(monkeypatch):
    client, ran = _Client([_cmd(verb="update", args={})]), []
    _beat_once(client, monkeypatch, ran)
    urls = [u for u, _ in client.posts]
    assert urls == ["https://idp.test/v1/nodes/heartbeat", "https://idp.test/v1/nodes/register",
                    f"https://idp.test/v1/nodes/{NODE}/commands/cmd_1/result"]
    assert ran == [("update", {})]
    assert node_commands.load_key(NODE) == KEY
    result = client.posts[-1][1]
    assert result["id"] == "cmd_1" and result["ok"] is True and result["sig"]


def test_a_forged_beat_answer_runs_nothing(monkeypatch):
    node_commands.save_key(NODE, KEY)
    forged = {**_cmd(verb="lend-off", args={}), "sig": "0" * 64}
    client, ran = _Client([forged]), []
    _beat_once(client, monkeypatch, ran)
    assert ran == []
    assert [u for u, _ in client.posts] == ["https://idp.test/v1/nodes/heartbeat"]


def _beat_device(client, monkeypatch, ran):
    monkeypatch.setattr(enrollment, "build_registration", lambda node_id, **kw: {
        "node_id": node_id, "inference_ready": False, "available_models": [],
        "gpu_vram_mb": 0, "inference_url": "", "inference_kind": "none"})
    monkeypatch.setattr(node_commands, "_HANDLERS", _handlers(ran))
    asyncio.run(enrollment._heartbeat_beats(
        client, "https://idp.test", {"Authorization": "Bearer device-tok"}, NODE, interval=0,
        inference_url=None, node_class="spark", max_beats=1, reach_provider=None,
        harness_provider=None, beat_immediately=True, device=True))


def test_a_paired_device_beats_and_reports_as_itself(monkeypatch):
    node_commands.save_key(NODE, KEY)
    client, ran = _Client([_cmd(verb="collect-diagnostics", args={})]), []
    _beat_device(client, monkeypatch, ran)
    assert [u for u, _ in client.posts] == ["https://idp.test/v1/nodes/device/heartbeat",
                                            "https://idp.test/v1/nodes/device/results/cmd_1"]
    assert ran == [("collect-diagnostics", {})]


def test_a_device_with_no_key_never_tries_to_reregister_with_its_token(monkeypatch):
    client, ran = _Client([_cmd()]), []
    _beat_device(client, monkeypatch, ran)
    assert [u for u, _ in client.posts] == ["https://idp.test/v1/nodes/device/heartbeat"]
    assert ran == []


def test_a_removed_device_is_reported_not_reregistered(monkeypatch):
    class _Gone(_Client):
        async def post(self, url, json=None, headers=None):
            self.posts.append((url, json))
            return _Resp(200, {"status": "unknown_node"})

    client = _Gone([])
    monkeypatch.setattr(enrollment, "_heartbeat_state", enrollment._new_heartbeat_state())
    _beat_device(client, monkeypatch, [])
    assert len(client.posts) == 1
    assert "removed" in enrollment._heartbeat_state.get("last_error", "")


def test_a_paired_record_resumes_a_device_token_heartbeat(tmp_path, monkeypatch):
    from adk import fleet_enroll
    rec = {"node_id": NODE, "bearer_token": "device-tok", "enrolled_via": "pairing-code",
           "node_class": "spark"}
    monkeypatch.setattr(fleet_enroll, "_load_node_auth", lambda: rec)
    calls = []
    monkeypatch.setattr(enrollment, "_heartbeat_task", None)
    monkeypatch.setattr(enrollment, "start_heartbeat",
                        lambda base, tok, nid, **kw: calls.append((base, tok, nid, kw)) or True)
    out = enrollment.resume_heartbeat("", default_base_url="https://idp.test")
    assert out == {"started": True, "reason": "", "node_id": NODE}
    ((base, tok, nid, kw),) = calls
    assert (base, tok, nid, kw["device"], kw["node_class"]) == (
        "https://idp.test", "device-tok", NODE, True, "spark")
    # A signed-in person still beats as themselves.
    calls.clear()
    rec["mode"] = "rich"
    enrollment.resume_heartbeat("user-tok", default_base_url="https://idp.test")
    assert calls[0][1] == "user-tok" and "device" not in calls[0][3]


def test_adk_pair_defaults_to_identitys_own_confirm_route(monkeypatch):
    """The portal proxy answered an anonymous confirm with 401; Identity is the door."""
    from types import SimpleNamespace

    from adk import devices, node_pairing

    seen = {}

    async def fake_pair(code, base, node_class="laptop", confirm_path=""):
        seen.update(code=code, base=base, node_class=node_class, path=confirm_path)
        return {"paired": True, "node_id": NODE}

    monkeypatch.delenv("AITHER_PORTAL_URL", raising=False)
    monkeypatch.setattr(devices, "enroll_base", lambda: "https://idp.test")
    monkeypatch.setattr(node_pairing, "pair_with_code", fake_pair)
    assert node_pairing.cmd_pair(SimpleNamespace(code="ABCD2345", portal="",
                                                 node_class="spark")) == 0
    assert seen == {"code": "ABCD2345", "base": "https://idp.test", "node_class": "spark",
                    "path": "/v1/nodes/pairing/confirm"}
