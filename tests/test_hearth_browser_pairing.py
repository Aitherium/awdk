"""Browser pairing on the ``local`` Hearth channel (:mod:`adk.home.transports.browser`).

A web page on an allowlisted origin exchanges a one-time code for a browser
bearer and then chats with the user's own ``adk home serve``. These tests pin the
safety properties: single-use codes that expire, origin refusal on every path,
Chrome's private-network preflight, the loopback Host check, and that the file
token is neither accepted on /browser/* nor ever returned to a page.
"""

from __future__ import annotations

import asyncio
import json
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
from adk.home.transports import browser as br  # noqa: E402
from adk.home.transports import local as lt  # noqa: E402
from adk.tools import ToolRegistry  # noqa: E402

OS_USER = "localowner"
PAGE = "https://hearth.aitherium.com"
OS_PAGE = "https://aitherium.com"
EVIL = "https://evil.example"


class GateAgent:
    """Runs a scripted list of tool calls; a gated call with no decision pauses."""

    name = "hearth-browser-test"

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
        self.llm = SimpleNamespace(model="bonsai-selfhost")

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
    monkeypatch.delenv(br.ORIGINS_ENV, raising=False)
    monkeypatch.delenv(receipts.KEY_ENV, raising=False)
    monkeypatch.setattr(receipts, "_home_dir", lambda: root)
    monkeypatch.setattr(receipts, "_awseal_private_key", lambda: None)
    monkeypatch.setattr(approval, "_STORE", approval.ApprovalStore(tmp_path / "paused.json"))
    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "")
    serve.apply_approval_policy()
    return root


def _setup(home, script=lambda m, r: [], **kw):
    store = FollowupStore(home / "followups.json")
    agent = GateAgent(store, script)
    t = lt.LocalTransport(root=home, port=0, user_id=OS_USER, **kw)
    core = hearth.HearthCore(agent, store, home / "actions.jsonl", t, root=home)
    t.core = core
    return core, agent, t


def _client(t, host="127.0.0.1"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=t.app),
                             base_url=f"http://{host}")


def _file_auth(t):
    return {"Authorization": f"Bearer {t._token}"}


def _page(origin=PAGE, token=""):
    h = {"Origin": origin}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _paired(c, t, origin=PAGE):
    r = await c.post("/browser-code", headers=_file_auth(t))
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    r = await c.post("/browser/pair", json={"code": code}, headers=_page(origin))
    assert r.status_code == 200, r.text
    return r.json()["token"], code


# ── codes ────────────────────────────────────────────────────────────────────

def test_code_shape_and_normalization():
    p = br.BrowserPairing(br.DEFAULT_ORIGINS)
    code, ttl = p.new_code()
    assert ttl == br.CODE_TTL_S == 300
    assert len(code) == 9 and code[4] == "-"
    assert br.normalize_code(code.lower().replace("-", " ")) == code.replace("-", "")
    for bad in ("", "ABC", "ABCD-EFG0", "ABCD-EFGHI", None, 12345678):
        assert br.normalize_code(bad) == ""


def test_a_code_is_single_use():
    p = br.BrowserPairing(br.DEFAULT_ORIGINS)
    code, _ = p.new_code()
    token, why = p.pair(code, PAGE)
    assert token and why == ""
    again, why = p.pair(code, PAGE)
    assert again is None and "wrong or expired" in why


def test_a_code_expires_after_five_minutes():
    now = [1000.0]
    p = br.BrowserPairing(br.DEFAULT_ORIGINS, clock=lambda: now[0])
    code, _ = p.new_code()
    now[0] += br.CODE_TTL_S + 1
    token, why = p.pair(code, PAGE)
    assert token is None and "expired" in why
    assert p.open_codes() == 0


def test_too_many_wrong_codes_burn_every_open_code():
    p = br.BrowserPairing(br.DEFAULT_ORIGINS)
    code, _ = p.new_code()
    for _ in range(br.MAX_PAIR_FAILS - 1):
        assert p.pair("ZZZZ-ZZZZ", PAGE)[0] is None
    assert p.open_codes() == 1
    token, why = p.pair("ZZZZ-ZZZZ", PAGE)
    assert token is None and "too many" in why
    assert p.open_codes() == 0
    assert p.pair(code, PAGE)[0] is None          # the real code died too


def test_a_bearer_expires_and_is_bound_to_its_origin():
    now = [1000.0]
    p = br.BrowserPairing(br.DEFAULT_ORIGINS, clock=lambda: now[0])
    token, _ = p.pair(p.new_code()[0], PAGE)
    assert p.session_for(token, PAGE)
    assert p.session_for(token, OS_PAGE) is None      # another allowed origin
    assert p.session_for("x" * 43, PAGE) is None
    now[0] += br.SESSION_TTL_S + 1
    assert p.session_for(token, PAGE) is None


def test_open_codes_and_sessions_are_capped():
    p = br.BrowserPairing(br.DEFAULT_ORIGINS)
    first = p.new_code()[0]
    for _ in range(br.MAX_OPEN_CODES):
        p.new_code()
    assert p.open_codes() == br.MAX_OPEN_CODES
    assert p.pair(first, PAGE)[0] is None             # the oldest was dropped
    tokens = []
    for _ in range(br.MAX_SESSIONS + 1):
        tokens.append(p.pair(p.new_code()[0], PAGE)[0])
    assert p.sessions() == br.MAX_SESSIONS
    assert p.session_for(tokens[0], PAGE) is None


@pytest.mark.parametrize("raw,expect", [
    ("", br.DEFAULT_ORIGINS),
    ("off", ()),
    ("https://a.example, https://b.example/", ("https://a.example", "https://b.example")),
    ("http://localhost:3000", ("http://localhost:3000",)),
])
def test_origin_allowlist_parsing(raw, expect):
    assert br.browser_origins(raw) == expect


@pytest.mark.parametrize("raw", ["http://evil.example", "evil.example", "https://a/b",
                                 "ftp://a.example"])
def test_a_bad_origin_entry_is_refused_loudly(raw):
    with pytest.raises(hc.HomeError):
        br.browser_origins(raw)


def test_env_allowlist_reaches_the_transport(home, monkeypatch):
    monkeypatch.setenv(br.ORIGINS_ENV, "https://only.example")
    t = lt.LocalTransport(root=home, port=0, user_id=OS_USER)
    assert t.browser.origins == ("https://only.example",)


# ── HTTP: origin, host, preflight ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_pna_preflight_from_an_allowed_origin(home):
    core, agent, t = _setup(home)
    async with _client(t) as c:
        r = await c.request("OPTIONS", "/browser/pair", headers={
            "Origin": PAGE, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
            "Access-Control-Request-Private-Network": "true"})
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == PAGE
    assert r.headers["access-control-allow-private-network"] == "true"
    assert "authorization" in r.headers["access-control-allow-headers"]
    assert "access-control-allow-credentials" not in r.headers
    assert "Origin" in r.headers["vary"]


@pytest.mark.asyncio
async def test_preflight_without_the_pna_header_does_not_grant_it(home):
    core, agent, t = _setup(home)
    async with _client(t) as c:
        r = await c.request("OPTIONS", "/browser/say", headers={
            "Origin": OS_PAGE, "Access-Control-Request-Method": "POST"})
    assert r.status_code == 204
    assert "access-control-allow-private-network" not in r.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("OPTIONS", "/browser/pair"), ("POST", "/browser/pair"), ("POST", "/browser/say"),
    ("GET", "/browser/state"), ("POST", "/browser/approve"), ("POST", "/message"),
    ("GET", "/hello"), ("POST", "/browser-code"),
])
async def test_a_disallowed_origin_is_refused_on_every_path(home, method, path):
    core, agent, t = _setup(home)
    async with _client(t) as c:
        code = t.new_browser_code()[0]
        r = await c.request(method, path, headers={**_page(EVIL), **_file_auth(t),
                                                   "Access-Control-Request-Private-Network":
                                                       "true"},
                            content=json.dumps({"code": code, "text": "hi"}))
    assert r.status_code == 403
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-private-network" not in r.headers
    assert agent.chats == [] and t.browser.sessions() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["null", "https://hearth.aitherium.com.evil.example",
                                    "http://hearth.aitherium.com"])
async def test_lookalike_and_null_origins_are_refused(home, origin):
    core, agent, t = _setup(home)
    code = t.new_browser_code()[0]
    async with _client(t) as c:
        r = await c.post("/browser/pair", json={"code": code}, headers=_page(origin))
    assert r.status_code == 403 and t.browser.sessions() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/message", "/receipts", "/events", "/hello?nonce=" + "a" * 32,
                                  "/browser-code"])
async def test_an_allowed_origin_still_cannot_reach_the_file_token_endpoints(home, path):
    core, agent, t = _setup(home)
    async with _client(t) as c:
        r = await c.request("POST" if path in ("/message", "/browser-code") else "GET", path,
                            headers={**_page(PAGE), **_file_auth(t)},
                            content=json.dumps({"text": "hi"}))
    assert r.status_code == 403 and "browser/" in r.json()["error"]
    assert agent.chats == []


@pytest.mark.asyncio
async def test_a_rebound_host_name_is_refused(home):
    """DNS rebinding: evil.example resolved to 127.0.0.1 still sends Host: evil.example."""
    core, agent, t = _setup(home)
    async with _client(t, host="evil.example:8363") as c:
        r = await c.post("/message", json={"text": "hi"}, headers=_file_auth(t))
        assert r.status_code == 403
        r = await c.post("/browser/pair", json={"code": "x"}, headers=_page(PAGE))
        assert r.status_code == 403
    async with _client(t, host="localhost:8363") as c:
        assert (await c.post("/message", json={"text": "hi"},
                             headers=_file_auth(t))).status_code == 200


@pytest.mark.asyncio
async def test_pairing_needs_an_origin(home):
    core, agent, t = _setup(home)
    code = t.new_browser_code()[0]
    async with _client(t) as c:
        r = await c.post("/browser/pair", json={"code": code})
    assert r.status_code == 403
    assert t.browser.open_codes() == 1                  # not consumed


# ── HTTP: tokens never cross ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_code_endpoint_needs_the_file_token(home):
    core, agent, t = _setup(home)
    async with _client(t) as c:
        assert (await c.post("/browser-code")).status_code == 401
        assert (await c.post("/browser-code", headers={
            "Authorization": "Bearer " + "x" * 43})).status_code == 401
        r = await c.post("/browser-code", headers=_file_auth(t))
    assert r.status_code == 200
    data = r.json()
    assert br.normalize_code(data["code"]) and data["expires_in"] == 300
    assert data["origins"] == list(br.DEFAULT_ORIGINS)


@pytest.mark.asyncio
async def test_no_file_token_leaks_and_the_tokens_never_cross(home):
    core, agent, t = _setup(home)
    async with _client(t) as c:
        r_code = await c.post("/browser-code", headers=_file_auth(t))
        r_pair = await c.post("/browser/pair", json={"code": r_code.json()["code"]},
                              headers=_page())
        token = r_pair.json()["token"]
        r_say = await c.post("/browser/say", json={"text": "hello"},
                             headers=_page(token=token))
        r_state = await c.get("/browser/state", headers=_page(token=token))
        # The file token is not a browser credential ...
        r_file = await c.post("/browser/say", json={"text": "x"},
                              headers={**_page(), **_file_auth(t)})
        r_file_state = await c.get("/browser/state", headers={**_page(), **_file_auth(t)})
        # ... and the browser bearer is not a local-client credential.
        r_msg = await c.post("/message", json={"text": "x"},
                             headers={"Authorization": f"Bearer {token}"})
        r_rcpt = await c.get("/receipts", headers={"Authorization": f"Bearer {token}"})
    assert token != t._token and len(token) >= 32
    assert r_file.status_code == r_file_state.status_code == 401
    assert r_msg.status_code == r_rcpt.status_code == 401
    for r in (r_code, r_pair, r_say, r_state, r_file, r_msg):
        assert t._token not in r.text
    assert agent.chats == ["hello"]
    assert not any(t._token in json.dumps(row) for row in
                   receipts.tail(200, path=home / "actions.jsonl"))


@pytest.mark.asyncio
async def test_a_bearer_from_one_origin_is_refused_from_another(home):
    core, agent, t = _setup(home)
    async with _client(t) as c:
        token, _ = await _paired(c, t, PAGE)
        r = await c.post("/browser/say", json={"text": "hi"}, headers=_page(OS_PAGE, token))
        assert r.status_code == 401
        r = await c.post("/browser/say", json={"text": "hi"}, headers=_page(PAGE, token))
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == PAGE


@pytest.mark.asyncio
async def test_pairing_twice_with_one_code_fails_over_http(home):
    core, agent, t = _setup(home)
    async with _client(t) as c:
        token, code = await _paired(c, t)
        r = await c.post("/browser/pair", json={"code": code}, headers=_page())
    assert r.status_code == 401 and "token" not in r.json()


@pytest.mark.asyncio
async def test_forget_revokes_the_bearer(home):
    core, agent, t = _setup(home)
    async with _client(t) as c:
        token, _ = await _paired(c, t)
        assert (await c.post("/browser/forget", headers=_page(token=token))).status_code == 200
        r = await c.get("/browser/state", headers=_page(token=token))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_browser_access_off_refuses_everything(home):
    core, agent, t = _setup(home, origins=())
    async with _client(t) as c:
        r = await c.post("/browser-code", headers=_file_auth(t))
        assert r.status_code == 409
        r = await c.request("OPTIONS", "/browser/pair", headers=_page())
        assert r.status_code == 403
        # The local CLI path is untouched.
        assert (await c.post("/message", json={"text": "hi"},
                             headers=_file_auth(t))).status_code == 200


# ── the page's whole flow ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_say_state_approve_round_trip(home):
    core, agent, t = _setup(home, script=lambda m, r: (
        [("send_email", {"to": "sam@example.com"})] if m.startswith("email") else []))
    async with _client(t) as c:
        token, _ = await _paired(c, t)
        r = await c.post("/browser/say", json={"text": "hello"}, headers=_page(token=token))
        assert [m["text"] for m in r.json()["replies"]] == ["reply to: hello"]
        # The OS user is bound as the local owner, as with the file token.
        assert core.registry.owner("local") == OS_USER

        r = await c.post("/browser/say", json={"text": "email sam"},
                         headers=_page(token=token))
        nonce = r.json()["replies"][-1]["card"]
        assert nonce and agent.sent_mail == []
        state = (await c.get("/browser/state", headers=_page(token=token))).json()
        assert state["pending"]["nonce"] == nonce
        assert state["pending"]["calls"] == [{"tool": "send_email",
                                              "args": {"to": "sam@example.com"}}]
        assert state["agent"] == "hearth-browser-test"
        assert state["model"] == "bonsai-selfhost"
        assert state["receipts"]["verify"]["verdict"] == "intact"

        for bad in ({"nonce": nonce}, {"nonce": "xyz", "allow": True},
                    {"nonce": nonce, "allow": "yes"}):
            assert (await c.post("/browser/approve", json=bad,
                                 headers=_page(token=token))).status_code == 400
        assert (await c.post("/browser/approve", json={"nonce": nonce, "allow": True},
                             headers=_page(OS_PAGE, token))).status_code == 401
        assert agent.sent_mail == []

        r = await c.post("/browser/approve", json={"nonce": nonce, "allow": True},
                         headers=_page(token=token))
        assert r.status_code == 200
        state = (await c.get("/browser/state", headers=_page(token=token))).json()
    assert agent.sent_mail == [{"to": "sam@example.com", "body": ""}]
    assert state["pending"] is None
    rows = receipts.tail(50, path=home / "actions.jsonl")
    assert any(row["kind"] == "approval" and str(row["approval"]).startswith("owner:allow")
               for row in rows)


@pytest.mark.asyncio
async def test_state_lists_pending_reminders(home):
    core, agent, t = _setup(home)
    core.store.add("remind", "stretch", 9e9)
    done = core.store.add("remind", "old", 1.0)
    core.store.claim_due(now=2.0)
    async with _client(t) as c:
        token, _ = await _paired(c, t)
        state = (await c.get("/browser/state", headers=_page(token=token))).json()
    assert [r["text"] for r in state["reminders"]] == ["stretch"]
    assert done["id"] not in {r["id"] for r in state["reminders"]}


@pytest.mark.asyncio
async def test_unpaired_browser_endpoints_are_401(home):
    core, agent, t = _setup(home)
    async with _client(t) as c:
        for method, path in (("POST", "/browser/say"), ("GET", "/browser/state"),
                             ("POST", "/browser/approve"), ("POST", "/browser/forget")):
            r = await c.request(method, path, headers=_page(token="x" * 43),
                                content=json.dumps({"text": "hi"}))
            assert r.status_code == 401
            assert r.headers["access-control-allow-origin"] == PAGE   # readable error
    assert agent.chats == []


# ── real server + CLI ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_browser_cli_against_a_real_serve(home, capsys):
    pytest.importorskip("uvicorn")
    core, agent, t = _setup(home)
    await t.start(core)
    try:
        port = t.bound_port
        rc = await asyncio.to_thread(home_cli.main, ["connect-browser", "--json"])
        assert rc == home_cli.EXIT_OK
        data = json.loads(capsys.readouterr().out)
        assert t.browser.open_codes() == 1
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                     trust_env=False) as c:
            r = await c.post("/browser/pair", json={"code": data["code"]}, headers=_page())
            assert r.status_code == 200
            token = r.json()["token"]
            r = await c.post("/browser/say", json={"text": "hello"},
                             headers=_page(token=token))
        assert [m["text"] for m in r.json()["replies"]] == ["reply to: hello"]
        rc = await asyncio.to_thread(home_cli.main, ["connect-browser"])
        out = capsys.readouterr().out
        assert rc == 0 and "BROWSER CODE:" in out and PAGE in out
        assert t._token not in out
    finally:
        await t.stop()


def test_connect_browser_without_a_serve_fails(home, capsys):
    assert home_cli.main(["connect-browser"]) == home_cli.EXIT_FAIL
    assert "adk home serve" in capsys.readouterr().err


def test_client_browser_code_is_sent_only_after_the_hello_proof(home, monkeypatch):
    lt.write_endpoint(lt.new_token(), 9000, home)
    client = local_client.LocalClient(root=home, timeout=5)
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"proof": "0" * 64, "port": 9000})

    monkeypatch.setattr(client, "_client", lambda timeout=None: httpx.Client(
        transport=httpx.MockTransport(handler)))
    with pytest.raises(local_client.LocalClientError, match="NOT sent"):
        client.browser_code()
    assert seen == ["/hello"]


def test_serve_browser_flag_prints_a_code(home, monkeypatch, capsys):
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
    assert home_cli.main(["serve", "--browser"]) == home_cli.EXIT_OK
    out = capsys.readouterr().out
    line = next(x for x in out.splitlines() if "BROWSER CODE:" in x)
    code = line.split("BROWSER CODE:")[1].split()[0]
    assert seen["core"].transports["local"].browser.pair(code, PAGE)[0]
    assert home_cli.main(["serve", "--browser", "--no-local"]) == home_cli.EXIT_SETUP


@pytest.mark.parametrize("provider,base_url,this_computer", [
    ("bonsai", "", True),
    ("bonsai", "http://10.1.2.3:8080/v1", False),   # "local" preset pointed at another host
    ("openai", "", False),                          # bring-your-own-key: off this computer
])
def test_browser_state_says_where_the_model_runs(home, provider, base_url, this_computer):
    """The page names where a turn ran. A hosted or remote model must never be reported as
    "this computer" (reverting ``model_ran_on`` drops the field and this fails)."""
    from adk.home import models

    hc.init_home(name="hearth-test")
    cfg = hc.load_config()
    cfg.model = models.choose_model(provider, base_url=base_url)
    hc.save_config(cfg)
    core, agent, t = _setup(home)
    state = t.browser_state()
    assert state["ran_on"]["this_computer"] is this_computer
    assert state["ran_on"]["provider"] == provider
