"""The ``local`` Hearth channel: a loopback window onto the one ``adk home serve``.

HTTP behaviour is driven in-process through ``httpx.ASGITransport``; the SSE and
thin-client tests run the real uvicorn server on an ephemeral 127.0.0.1 port.
"""

from __future__ import annotations

import asyncio
import json
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("starlette")
httpx = pytest.importorskip("httpx")

from adk import approval, receipts  # noqa: E402
from adk.home import cli as home_cli  # noqa: E402
from adk.home import config as hc  # noqa: E402
from adk.home import hearth, local_client, serve  # noqa: E402
from adk.home.life_tools import FollowupStore, build_life_tools  # noqa: E402
from adk.home.transports import local as lt  # noqa: E402
from adk.tools import ToolRegistry  # noqa: E402

OS_USER = "localowner"


class GateAgent:
    """A gated call with no decision pauses the turn; resume re-runs it."""

    name = "hearth-test"

    def __init__(self, store, script=lambda msg, run: []):
        self._tools = ToolRegistry()
        for fn in build_life_tools(store):
            self._tools.register(fn)
        self.sent_mail: list = []

        def send_email(to: str, body: str = "") -> str:
            """Send an email.

            to: recipient
            body: text
            """
            self.sent_mail.append({"to": to, "body": body})
            return json.dumps({"ok": True})

        self._tools.register(send_email)
        self.script = script
        self.runs: dict = {}
        self.chats: list = []

    async def chat(self, message, session_id=None):
        self.chats.append(message)
        store = approval.get_approval_store()
        run = self.runs.get(message, 0)
        self.runs[message] = run + 1
        pending = []
        for i, (tool, args) in enumerate(self.script(message, run)):
            if approval.needs_approval(self.name, tool):
                decision = store.decision_for(session_id, tool)
                if decision == "deny":
                    continue
                if decision != "allow":
                    pending.append({"tool_use_id": f"call_{i}", "tool": tool, "args": args})
                    break
            await self._tools.execute(tool, args)
        if pending:
            store.put_pending(session_id, user_message=message, agent=self.name,
                              pending=pending)
            return SimpleNamespace(content="Waiting", requires_action=True, pending=pending)
        store.clear(session_id)
        return SimpleNamespace(content=f"reply to: {message}", requires_action=False,
                               pending=[])

    async def resume(self, session_id, decisions):
        store = approval.get_approval_store()
        paused = store.get(session_id)
        if not paused:
            return SimpleNamespace(content="", requires_action=False, pending=[])
        store.record_decisions(session_id, decisions)
        return await self.chat(paused["user_message"], session_id=session_id)


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "agent-home"
    monkeypatch.setenv(hc.HOME_ENV, str(root))
    monkeypatch.delenv("AITHER_RECEIPTS_PATH", raising=False)
    monkeypatch.delenv(lt.PORT_ENV, raising=False)
    monkeypatch.delenv(receipts.KEY_ENV, raising=False)
    monkeypatch.setattr(receipts, "_home_dir", lambda: root)
    monkeypatch.setattr(receipts, "_awseal_private_key", lambda: None)
    monkeypatch.setattr(approval, "_STORE", approval.ApprovalStore(tmp_path / "paused.json"))
    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "")
    serve.apply_approval_policy()
    return root


def _setup(home, script=lambda m, r: [], port=0):
    store = FollowupStore(home / "followups.json")
    agent = GateAgent(store, script)
    t = lt.LocalTransport(root=home, port=port, user_id=OS_USER)
    core = hearth.HearthCore(agent, store, home / "actions.jsonl", t, root=home)
    t.core = core                      # in-process: the ASGI app without the server
    return core, agent, t


def _client(t):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=t.app),
                             base_url="http://127.0.0.1")


def _auth(t, token=None):
    return {"Authorization": f"Bearer {token if token is not None else t._token}"}


def _receipt_rows(home):
    path = home / "actions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ── construction ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "localhost", "::1"])
def test_non_loopback_bind_is_refused(home, host):
    with pytest.raises(hc.HomeError, match="refusing to listen"):
        lt.LocalTransport(root=home, host=host, port=0)


def test_port_comes_from_env_and_bad_values_are_refused(home, monkeypatch):
    assert lt.LocalTransport(root=home).port == lt.DEFAULT_PORT
    monkeypatch.setenv(lt.PORT_ENV, "9444")
    assert lt.LocalTransport(root=home).port == 9444
    monkeypatch.setenv(lt.PORT_ENV, "nope")
    with pytest.raises(hc.HomeError, match=lt.PORT_ENV):
        lt.LocalTransport(root=home)


def test_every_transport_mints_a_fresh_token_and_publishes_nothing_until_up(home):
    a, b = lt.LocalTransport(root=home, port=0), lt.LocalTransport(root=home, port=0)
    assert len(a._token) >= 32 and a._token != b._token
    assert a._token not in repr(a)
    assert not lt.token_path(home).exists()               # written only once listening
    with pytest.raises(hc.HomeError, match="32-128"):
        lt.LocalTransport(root=home, port=0, token="short")


def test_endpoint_round_trip_and_malformed_files(home):
    tok = lt.new_token()
    lt.write_endpoint(tok, 9444, home)
    assert lt.read_endpoint(home) == (tok, 9444) and lt.read_token(home) == tok
    rec = json.loads(lt.token_path(home).read_text(encoding="utf-8"))
    assert rec["pid"] > 0
    assert not list(home.glob("*.tmp"))
    for bad in ("short", tok, json.dumps({"token": tok}),
                json.dumps({"token": tok, "port": 0}),
                json.dumps({"token": tok, "port": True}),
                json.dumps({"token": "x", "port": 9444})):
        lt.token_path(home).write_text(bad, encoding="utf-8")
        with pytest.raises(hc.HomeError, match="malformed"):
            lt.read_endpoint(home)


def test_remove_endpoint_keeps_a_newer_serves_file(home):
    old, new = lt.new_token(), lt.new_token()
    lt.write_endpoint(new, 9444, home)
    lt.remove_endpoint(old, home)                  # an older serve stopping ...
    assert lt.read_token(home) == new              # ... leaves the newer one alone
    lt.remove_endpoint(new, home)
    assert not lt.token_path(home).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_token_file_is_0600(home):
    lt.write_endpoint(lt.new_token(), 9444, home)
    assert stat.S_IMODE(lt.token_path(home).stat().st_mode) == 0o600


def _fake_icacls(monkeypatch, result):
    seen = []

    def run(argv, **kw):
        seen.append((argv, Path(argv[1]).read_bytes()))
        if isinstance(result, BaseException):
            raise result
        return SimpleNamespace(returncode=result, stdout="", stderr="")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", run)
    return seen


def test_windows_acl_is_applied_to_the_empty_file_before_the_token(home, monkeypatch):
    seen = _fake_icacls(monkeypatch, 0)
    tok = lt.new_token()
    lt.write_endpoint(tok, 9444, home)
    (argv, content), = seen
    assert argv[0] == "icacls" and "/inheritance:r" in argv and "/grant:r" in argv
    assert content == b""                           # restricted BEFORE the token lands
    assert lt.read_token(home) == tok


@pytest.mark.parametrize("result", [5, OSError("no icacls"),
                                    subprocess.TimeoutExpired("icacls", 15)])
def test_windows_acl_failure_fails_closed(home, monkeypatch, result):
    _fake_icacls(monkeypatch, result)
    with pytest.raises(hc.HomeError, match="refusing to write the local token"):
        lt.write_endpoint(lt.new_token(), 9444, home)
    assert not lt.token_path(home).exists()
    assert not list(home.glob("*.tmp"))


@pytest.mark.asyncio
async def test_a_channel_that_cannot_restrict_its_token_does_not_start(home, monkeypatch):
    pytest.importorskip("uvicorn")
    core, agent, t = _setup(home)

    def refuse(path):
        raise hc.HomeError("local: icacls could not restrict it")

    monkeypatch.setattr(lt, "_restrict", refuse)
    with pytest.raises(hc.HomeError):
        await t.start(core)
    assert t.bound_hosts == []                      # the listener was torn down again
    assert not lt.token_path(home).exists()


def test_read_token_without_serve_says_how_to_start(home):
    with pytest.raises(hc.HomeError, match="adk home serve"):
        lt.read_token(home)


# ── auth ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer wrong-token-wrong-token-wrong-token"},
    {"Authorization": "Basic abc"}, {"Authorization": "Bearer "},
])
async def test_no_or_wrong_token_is_401_and_the_core_is_untouched(home, headers):
    core, agent, t = _setup(home)
    async with _client(t) as c:
        r = await c.post("/message", json={"text": "hello"}, headers=headers)
        assert r.status_code == 401
        assert (await c.get("/receipts", headers=headers)).status_code == 401
        assert (await c.get("/events", headers=headers)).status_code == 401
    assert agent.chats == []
    assert core.registry.owner("local") == ""
    assert _receipt_rows(home) == []


@pytest.mark.asyncio
async def test_hello_proves_the_token_without_a_credential(home):
    core, agent, t = _setup(home, port=9444)
    nonce = "ab" * 16
    async with _client(t) as c:
        r = await c.get("/hello", params={"nonce": nonce})
        assert r.status_code == 200
        assert r.json() == {"proof": lt.hello_proof(t._token, nonce, 9444), "port": 9444}
        assert t._token not in r.text
        for bad in ("", "xyz", "AB" * 16, "a" * 31):
            assert (await c.get("/hello", params={"nonce": bad})).status_code == 400
    assert lt.hello_proof(t._token, nonce, 9444) != lt.hello_proof(t._token, nonce, 8363)
    assert agent.chats == [] and core.registry.owner("local") == ""


# ── routing ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_right_token_binds_the_os_user_and_returns_the_reply(home):
    core, agent, t = _setup(home)
    async with _client(t) as c:
        r = await c.post("/message", json={"text": "hello"}, headers=_auth(t))
    assert r.status_code == 200
    data = r.json()
    assert data["handled"] is True
    assert [m["text"] for m in data["replies"]] == ["reply to: hello"]
    assert data["replies"][0]["card"] is None
    assert agent.chats == ["hello"]
    # Bound through the registry, persisted, and receipted.
    assert hearth.OwnerRegistry(hearth.owner_path(home)).owner("local") == OS_USER
    rows = _receipt_rows(home)
    pair = [r for r in rows if r["kind"] == "pair"]
    assert len(pair) == 1 and pair[0]["approval"] == "local-token"
    assert pair[0]["result_preview"] == "bound"
    assert any(r["kind"] == "dm_out" and r["approval"] == "sent" for r in rows)


@pytest.mark.asyncio
async def test_a_second_message_does_not_rebind(home):
    core, agent, t = _setup(home)
    async with _client(t) as c:
        for text in ("one", "two"):
            assert (await c.post("/message", json={"text": text},
                                 headers=_auth(t))).status_code == 200
    assert [r["kind"] for r in _receipt_rows(home)].count("pair") == 1


@pytest.mark.asyncio
async def test_approval_card_round_trip_over_local(home):
    core, agent, t = _setup(home, script=lambda m, r: [("send_email", {"to": "a@x"})])
    async with _client(t) as c:
        r = await c.post("/message", json={"text": "email a"}, headers=_auth(t))
        card = r.json()["replies"][-1]
        nonce = core.awaiting["nonce"]
        assert card["card"] == nonce and f"yes {nonce}" in card["text"]
        assert agent.sent_mail == []
        r = await c.post("/message", json={"text": f"yes {nonce}"}, headers=_auth(t))
    assert agent.sent_mail == [{"to": "a@x", "body": ""}]
    assert [m["text"] for m in r.json()["replies"]] == ["reply to: email a"]
    assert core.awaiting is None


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [
    (json.dumps({"text": "x" * (lt.MAX_BODY + 1)}), 413),
    ("{not json", 400),
    (json.dumps({"text": "   "}), 400),
    (json.dumps(["hello"]), 400),
])
async def test_bad_or_oversized_bodies_never_reach_the_core(home, body, status):
    core, agent, t = _setup(home)
    async with _client(t) as c:
        r = await c.post("/message", content=body.encode("utf-8"),
                         headers={**_auth(t), "Content-Type": "application/json"})
    assert r.status_code == status
    assert agent.chats == [] and core.registry.owner("local") == ""


@pytest.mark.asyncio
async def test_a_chunked_body_over_the_cap_is_413(home):
    """No Content-Length: only the streamed size check can stop it."""
    core, agent, t = _setup(home)
    chunk = b"x" * 1024
    total = lt.MAX_BODY + 1

    async def body():
        sent = 0
        while sent < total:
            part = chunk[:total - sent]
            sent += len(part)
            yield part

    async with _client(t) as c:
        r = await c.post("/message", content=body(), headers=_auth(t))
    assert r.request.headers.get("content-length") is None
    assert r.status_code == 413
    assert agent.chats == [] and core.registry.owner("local") == ""


class _FakeRequest:
    def __init__(self, chunks, declared=None):
        self.headers = {} if declared is None else {"content-length": str(declared)}
        self._chunks = chunks

    async def stream(self):
        for c in self._chunks:
            yield c


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks,declared,expect", [
    ([b"12345", b"67890"], None, b"1234567890"),        # exactly at the limit
    ([b"12345", b"678901"], None, None),                 # streamed past it
    ([b"1"], 11, None),                                  # declared past it
    ([b"12345", b"678901"], 3, None),                    # a lying Content-Length
    ([], None, b""),
])
async def test_read_capped_both_paths(chunks, declared, expect):
    from adk.home.transports._webhook import read_capped

    assert await read_capped(_FakeRequest(chunks, declared), limit=10) == expect


@pytest.mark.asyncio
async def test_receipts_returns_the_tail_and_the_verify_verdict(home):
    core, agent, t = _setup(home)
    async with _client(t) as c:
        await c.post("/message", json={"text": "hello"}, headers=_auth(t))
        r = await c.get("/receipts", params={"n": 2}, headers=_auth(t))
        data = r.json()
        code, reason = receipts.check(home / "actions.jsonl")
        assert r.status_code == 200 and len(data["rows"]) == 2
        assert data["verify"] == {"code": code, "reason": reason,
                                  "verdict": {0: "intact", 1: "TAMPERED"}.get(
                                      code, "cannot judge")}
        # A doctored row is reported as tampering.
        path = home / "actions.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[0] = lines[0].replace('"kind":"pair"', '"kind":"pear"')
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        data = (await c.get("/receipts", headers=_auth(t))).json()
    assert data["verify"]["code"] == 1 and data["verify"]["verdict"] == "TAMPERED"


@pytest.mark.asyncio
async def test_a_push_with_no_listener_is_not_claimed_as_delivered(home):
    core, agent, t = _setup(home)
    assert await t.send(OS_USER, "Reminder: water the plants") is False
    assert t._backlog and t._backlog[-1]["missed"] is True
    assert await t.send("someone-else", "hi") is False


# ── the real server: SSE + the thin client ───────────────────────────────────

@pytest.mark.asyncio
async def test_server_events_and_client_end_to_end(home):
    pytest.importorskip("uvicorn")
    core, agent, t = _setup(home)
    await t.start(core)
    try:
        port = t.bound_port
        assert port
        # Every listening socket is on loopback, never a wider address.
        assert t.bound_hosts and set(t.bound_hosts) == {"127.0.0.1"}
        # The token file names the port that is actually bound.
        assert lt.read_endpoint(home) == (t._token, port)
        client = local_client.LocalClient(root=home, timeout=20)   # dials the record
        assert client.url == f"http://127.0.0.1:{port}"
        # A push nobody heard is replayed to the first subscriber.
        await t.send(OS_USER, "missed one")
        got = asyncio.ensure_future(asyncio.to_thread(lambda: list(client.events(limit=3))))
        for _ in range(250):
            if t._subscribers:
                break
            await asyncio.sleep(0.02)
        assert t._subscribers, "the SSE subscriber never registered"
        reply = await asyncio.to_thread(client.say, "hello")
        assert [m["text"] for m in reply["replies"]] == ["reply to: hello"]
        await core._send_owner("local", "Reminder: stretch")      # a follow-up push
        events = await asyncio.wait_for(got, 20)
        assert [(e["kind"], e["text"]) for e in events] == [
            ("push", "missed one"), ("reply", "reply to: hello"),
            ("push", "Reminder: stretch")]
        assert events[0]["missed"] is True
        tail = await asyncio.to_thread(client.receipts, 5)
        assert "verify" in tail and tail["rows"]
        bad = local_client.LocalClient(url=f"http://127.0.0.1:{port}",
                                       token="x" * 43, timeout=5)
        with pytest.raises(local_client.LocalClientError, match="token was NOT sent"):
            await asyncio.to_thread(bad.say, "hello")
    finally:
        await t.stop()
    assert agent.chats == ["hello"]
    assert not lt.token_path(home).exists()          # a stopped serve leaves no token


def _squatter(monkeypatch, client, proof_for):
    """Point ``client`` at an in-memory listener; record every header it gets."""
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        if request.url.path == "/hello":
            nonce = request.url.params["nonce"]
            return httpx.Response(200, json={"proof": proof_for(nonce), "port": 9000})
        return httpx.Response(200, json={"handled": True, "replies": [
            {"text": "forged", "card": "abcd1234"}]})

    monkeypatch.setattr(client, "_client", lambda timeout=None: httpx.Client(
        transport=httpx.MockTransport(handler)))
    return seen


@pytest.mark.parametrize("proof_for", [
    lambda tok: (lambda nonce: ""),                                   # knows nothing
    lambda tok: (lambda nonce: "0" * 64),                             # guesses
    lambda tok: (lambda nonce: lt.hello_proof(tok, nonce, 9000)),     # relays to 9000
    lambda tok: (lambda nonce: lt.hello_proof(lt.new_token(), nonce, 8363)),  # own token
])
def test_client_never_sends_the_token_to_an_unproven_listener(home, monkeypatch,
                                                               proof_for):
    tok = lt.new_token()
    lt.write_endpoint(tok, 8363, home)
    client = local_client.LocalClient(root=home, timeout=2)
    seen = _squatter(monkeypatch, client, proof_for(tok))
    for call in (lambda: client.say("hello"), lambda: client.receipts(3),
                 lambda: list(client.events(limit=1))):
        with pytest.raises(local_client.LocalClientError, match="token was NOT sent"):
            call()
    assert seen and all("authorization" not in h for h in seen)
    assert all(tok not in json.dumps(h) for h in seen)


def test_client_sends_the_token_only_after_a_valid_proof(home, monkeypatch):
    tok = lt.new_token()
    lt.write_endpoint(tok, 8363, home)
    client = local_client.LocalClient(root=home, timeout=2)
    seen = _squatter(monkeypatch, client, lambda n: lt.hello_proof(tok, n, 8363))
    assert client.say("hello")["handled"] is True
    assert "authorization" not in seen[0]                       # /hello: no credential
    assert seen[1]["authorization"] == f"Bearer {tok}"


def test_client_dials_the_recorded_port_not_the_env(home, monkeypatch):
    lt.write_endpoint(lt.new_token(), 9555, home)
    monkeypatch.setenv(lt.PORT_ENV, "8363")
    assert local_client.LocalClient(root=home).url == "http://127.0.0.1:9555"
    assert local_client.LocalClient(root=home, port=9666).url == "http://127.0.0.1:9666"


def test_client_without_a_running_serve_is_a_client_error(home):
    with pytest.raises(local_client.LocalClientError, match="adk home serve"):
        local_client.LocalClient(root=home)


@pytest.mark.asyncio
async def test_a_restart_kills_the_previous_token(home):
    pytest.importorskip("uvicorn")
    core, agent, t = _setup(home)
    await t.start(core)
    old = t._token
    await t.stop()
    core2, agent2, t2 = _setup(home)
    await t2.start(core2)
    try:
        assert t2._token != old and lt.read_token(home) == t2._token
        stale = local_client.LocalClient(url=f"http://127.0.0.1:{t2.bound_port}",
                                         token=old, timeout=5)
        with pytest.raises(local_client.LocalClientError, match="token was NOT sent"):
            await asyncio.to_thread(stale.say, "hello")
    finally:
        await t2.stop()
    assert agent2.chats == []


def test_client_reports_nothing_serving(home):
    lt.write_endpoint(lt.new_token(), 1, home)           # a serve that died uncleanly
    client = local_client.LocalClient(root=home, timeout=2)
    assert client.url == "http://127.0.0.1:1"
    with pytest.raises(local_client.LocalClientError, match="adk home serve"):
        client.say("hello")


def test_client_refuses_an_oversized_message_before_sending(home):
    client = local_client.LocalClient(url="http://127.0.0.1:1", token="t" * 43)
    with pytest.raises(local_client.LocalClientError, match="too long"):
        client.say("x" * (lt.MAX_BODY + 1))


def test_parse_sse_and_render():
    lines = [": connected", "", "event: message", 'data: {"kind": "push", "text": "a"}',
             "", "data: not json", "", 'data: {"kind": "reply", "text": "b",',
             'data: "card": "abcd1234"}', ""]
    events = list(local_client.parse_sse(iter(lines)))
    assert [e["text"] for e in events] == ["a", "b"]
    assert "yes abcd1234" in local_client.render_message(events[1])
    assert local_client.render_replies({"handled": True, "replies": []}) == "(no reply)"
    out = local_client.render_receipts({"verify": {"verdict": "intact", "reason": "ok"},
                                        "rows": [{"seq": 0, "kind": "tool", "name": "x"}]})
    assert out.startswith("receipts: intact") and "#0" in out


# ── CLI ──────────────────────────────────────────────────────────────────────

class FakeClient:
    calls: list = []

    def __init__(self, *a, **k):
        pass

    def say(self, text):
        FakeClient.calls.append(("say", text))
        return {"handled": True, "replies": [
            {"text": "Your agent wants to:\n1. send_email(to)", "card": "abcd1234"}]}

    def receipts(self, n):
        FakeClient.calls.append(("receipts", n))
        return {"verify": {"verdict": "intact", "reason": "intact: 1 signed rows"},
                "rows": [{"seq": 0, "kind": "tool", "name": "remind_me"}]}

    def events(self, limit=0):
        FakeClient.calls.append(("events", limit))
        yield {"kind": "push", "text": "Reminder: stretch"}


@pytest.fixture
def fake_client(monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr(local_client, "LocalClient", FakeClient)
    return FakeClient


def test_cli_say_and_events_use_the_local_client(home, fake_client, capsys):
    assert home_cli.main(["say", "email a"]) == home_cli.EXIT_OK
    out = capsys.readouterr().out
    assert "send_email" in out and "yes abcd1234" in out
    assert home_cli.main(["events", "-n", "1"]) == home_cli.EXIT_OK
    assert "[push] Reminder: stretch" in capsys.readouterr().out
    assert fake_client.calls == [("say", "email a"), ("events", 1)]


def test_cli_say_when_nothing_serves_is_a_failure(home, capsys):
    assert home_cli.main(["say", "hello"]) == home_cli.EXIT_FAIL       # no token file
    assert "adk home serve" in capsys.readouterr().err
    lt.write_endpoint(lt.new_token(), 1, home)
    assert home_cli.main(["say", "--port", "1", "hello"]) == home_cli.EXIT_FAIL
    assert "adk home serve" in capsys.readouterr().err


def test_local_joins_the_default_selection_only(home, monkeypatch):
    monkeypatch.delenv("AITHER_RELAY_TOKEN", raising=False)
    monkeypatch.setattr("adk.config.load_saved_config", lambda *a, **k: {})
    assert home_cli._select_channels("") == ([], False)       # never by env scan
    names, built, rc = home_cli._prepare_channels(SimpleNamespace(channels=""), local=True)
    assert rc == home_cli.EXIT_OK and names == ["local"]
    assert isinstance(built["local"], lt.LocalTransport)
    assert built["local"].host == "127.0.0.1"
    names, _, _ = home_cli._prepare_channels(SimpleNamespace(channels="local"))
    assert names == ["local"]                                  # or asked for by name
    assert "local" in home_cli.CHANNEL_SPECS


def test_serve_runs_with_only_the_local_channel_and_no_owner(home, monkeypatch, capsys):
    hc.init_home(name="hearth-test")
    monkeypatch.delenv("AITHER_RELAY_TOKEN", raising=False)
    monkeypatch.setattr("adk.config.load_saved_config", lambda *a, **k: {})
    monkeypatch.setattr(serve, "build_serve_agent", lambda *a, **k: SimpleNamespace(
        name="hearth-test"))
    monkeypatch.setattr(serve, "tool_names", lambda agent: [])
    monkeypatch.setattr(hearth.HearthCore, "_wrap_execute", lambda self: None)
    seen = {}

    async def fake_run(core, tick):
        seen["core"] = core
        return home_cli.EXIT_OK

    monkeypatch.setattr(home_cli, "_run_hearth", fake_run)
    assert home_cli.main(["serve"]) == home_cli.EXIT_OK
    assert list(seen["core"].transports) == ["local"]
    out = capsys.readouterr().out
    assert "local:    http://127.0.0.1:8363" in out
    # The token is published only by a started channel (fake_run started none).
    assert not lt.token_path(home).exists()
    assert "local.token" in out
    # --no-local with nothing else configured is the old setup error.
    assert home_cli.main(["serve", "--no-local"]) == home_cli.EXIT_SETUP


# ── awsh /hearth ─────────────────────────────────────────────────────────────

def test_hearth_plugin_is_a_builtin_and_routes_to_the_client(home, fake_client):
    from adk.shell.plugins import PluginRegistry

    reg = PluginRegistry([])
    reg.load_all()
    plugin = reg.get("hearth")
    assert plugin is not None and plugin.name == "hearth"
    out = asyncio.run(plugin.run(["email", "a"], {}))
    assert "yes abcd1234" in out
    out = asyncio.run(plugin.run(["receipts", "3"], {}))
    assert out.startswith("receipts: intact") and "remind_me" in out
    asyncio.run(plugin.run(["say", "receipts"], {}))
    assert fake_client.calls == [("say", "email a"), ("receipts", 3), ("say", "receipts")]
    assert "Usage" in asyncio.run(plugin.run([], {}))


def test_hearth_plugin_reports_an_unreachable_server(home):
    from adk.shell.plugins.builtins import hearth as hearth_plugin

    out = hearth_plugin.hearth_command(["hello"])
    assert out.startswith("hearth: ") and "adk home serve" in out
