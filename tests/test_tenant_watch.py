"""``adk tenant watch``: a file dropped into the Inbox is uploaded once, as the login.

The transport is httpx.MockTransport, so the REAL multipart request the watcher
builds is inspected (path, bearer, filename, bytes) -- not a stubbed helper.
"""

from __future__ import annotations

import json
import os
import time

import httpx
import pytest

from adk import tenant_watch as tw

API = "https://acme-api.example"


class _App:
    """Records uploads; answers like the host app's /api/documents/upload."""

    def __init__(self):
        self.calls = []
        self.names = {}  # filename -> doc id
        self.force = None
        self.delete_status = None
        self.seq = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        self.calls.append({"method": request.method, "path": request.url.path,
                           "auth": request.headers.get("authorization"),
                           "body": body})
        if self.force is not None:
            return httpx.Response(self.force, json={"detail": "forced"})
        if request.method == "DELETE":
            if self.delete_status is not None:
                return httpx.Response(self.delete_status, json={"detail": "admin only"})
            doc_id = request.url.path.rsplit("/", 1)[1]
            for n, i in list(self.names.items()):
                if i == doc_id:
                    del self.names[n]
                    return httpx.Response(200, json={"ok": True})
            return httpx.Response(404, json={"detail": "Document not found"})
        name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
        if name in self.names:
            return httpx.Response(409, json={"detail": f"Document '{name}' already exists."})
        self.seq += 1
        self.names[name] = f"d{self.seq}"
        # The shape of the host app's upload response (doc_id, not id).
        return httpx.Response(200, json={"doc_id": f"d{self.seq}", "filename": name,
                                         "doc_type": "other", "chunks_created": 1})


@pytest.fixture(autouse=True)
def _no_recheck_wait(monkeypatch):
    # The in-pass re-look is exercised explicitly below; elsewhere it would only add a
    # real one-second sleep per pass.
    monkeypatch.setattr(tw, "DEFAULT_RECHECK_S", 0.0)


def _old(p, age=60):
    t = time.time() - age
    os.utime(p, (t, t))


@pytest.fixture
def setup(tmp_path):
    inbox = tmp_path / "ACME Inbox"
    inbox.mkdir()
    app = _App()
    client = httpx.Client(transport=httpx.MockTransport(app))
    yield inbox, app, client, tmp_path / "state.json"
    client.close()


def test_dropped_file_is_uploaded_once_with_the_login_bearer(setup):
    inbox, app, client, state = setup
    f = inbox / "storyboard notes.txt"
    f.write_text("Episode 3 storyboard is due Friday.", encoding="utf-8")
    _old(f)

    res = tw.watch_once(inbox, API, "tok-123", state, client=client)

    assert [r["status"] for r in res] == ["learned"]
    assert len(app.calls) == 1
    call = app.calls[0]
    assert call["path"] == "/api/documents/upload"
    assert call["auth"] == "Bearer tok-123"
    assert b'filename="storyboard notes.txt"' in call["body"]
    assert b"Episode 3 storyboard is due Friday." in call["body"]

    # Second pass: nothing new, nothing sent.
    assert tw.watch_once(inbox, API, "tok-123", state, client=client) == []
    assert len(app.calls) == 1


def test_edited_file_the_watcher_sent_replaces_its_earlier_copy(setup):
    """The app refuses a second upload under one name (409). An edit to a file the
    watcher itself sent must still reach the app: delete OUR copy, send the new one."""
    inbox, app, client, state = setup
    f = inbox / "budget.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    _old(f, 120)
    tw.watch_once(inbox, API, "t", state, client=client)
    assert app.names == {"budget.csv": "d1"}

    f.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    _old(f, 60)
    res = tw.watch_once(inbox, API, "t", state, client=client)
    assert [r["status"] for r in res] == ["replaced"]
    assert [(c["method"], c["path"]) for c in app.calls[1:]] == [
        ("POST", "/api/documents/upload"), ("DELETE", "/api/documents/d1"),
        ("POST", "/api/documents/upload")]
    assert b"3,4" in app.calls[-1]["body"]
    assert app.names == {"budget.csv": "d2"}
    # Settled: nothing more is sent, and the NEW id is the one a later edit replaces.
    assert tw.watch_once(inbox, API, "t", state, client=client) == []
    assert len(app.calls) == 4


def test_409_for_a_name_the_watcher_never_sent_deletes_nothing(setup):
    inbox, app, client, state = setup
    app.names["contract.pdf"] = "someone-elses"
    f = inbox / "contract.pdf"
    f.write_bytes(b"%PDF-1.4 x")
    _old(f)
    res = tw.watch_once(inbox, API, "t", state, client=client)
    assert [r["status"] for r in res] == ["exists"]
    assert [c["method"] for c in app.calls] == ["POST"]
    assert app.names == {"contract.pdf": "someone-elses"}
    assert tw.watch_once(inbox, API, "t", state, client=client) == []  # pinned, not retried


def test_a_member_who_cannot_delete_keeps_the_watch_running(setup):
    """DELETE is admin-only in the app: a 403 there is NOT a refused login."""
    inbox, app, client, state = setup
    f = inbox / "a.txt"
    f.write_text("v1", encoding="utf-8")
    _old(f, 120)
    tw.watch_once(inbox, API, "t", state, client=client)
    app.delete_status = 403
    f.write_text("v2 longer", encoding="utf-8")
    _old(f, 60)
    res = tw.watch_once(inbox, API, "t", state, client=client)
    assert [r["status"] for r in res] == ["exists"]
    assert app.names == {"a.txt": "d1"}


def test_a_file_still_growing_between_the_two_looks_waits_a_pass(setup):
    """An Explorer copy keeps the source's OLD mtime, so the settle check passes while
    the bytes are still arriving; the in-pass re-look must catch the size moving."""
    inbox, app, client, state = setup
    f = inbox / "big.pdf"
    f.write_bytes(b"%PDF part")
    _old(f, 3600)

    def _copy_continues(_s):
        with f.open("ab") as fh:
            fh.write(b" more bytes")
        _old(f, 3600)  # the copy keeps the source mtime

    assert tw.watch_once(inbox, API, "t", state, client=client, recheck_s=0.5,
                         sleep=_copy_continues) == []
    assert app.calls == []
    res = tw.watch_once(inbox, API, "t", state, client=client, recheck_s=0.5,
                        sleep=lambda _s: None)
    assert [r["status"] for r in res] == ["learned"]
    assert b"part more bytes" in app.calls[0]["body"]


def test_a_file_removed_mid_pass_does_not_kill_the_watcher(setup, monkeypatch):
    """Reproduced: the user moved a file away while an earlier one uploaded, and the
    unguarded stat() ended run() with FileNotFoundError -- a hidden logon watcher that
    then stops syncing until the next logon."""
    inbox, app, client, state = setup
    for n in ("a.txt", "b.txt"):
        (inbox / n).write_text(n, encoding="utf-8")
        _old(inbox / n)
    real = tw.pending_files

    def _then_vanish(*a, **k):
        out = real(*a, **k)
        for p in out:
            if p.name == "a.txt" and p.exists():
                p.unlink()
        return out

    monkeypatch.setattr(tw, "pending_files", _then_vanish)
    rc = tw.run("acme", inbox, API, lambda: "t", once=False, interval=1, as_json=True,
                client_factory=_CtxOf(client), state_path=state,
                sleep=lambda _s: None, max_passes=2)
    assert rc == 0
    assert [c["body"].count(b'filename="b.txt"') for c in app.calls] == [1]


def test_a_vanished_file_between_scan_and_stat_is_skipped(tmp_path, monkeypatch):
    inbox = tmp_path / "in"
    inbox.mkdir()
    f = inbox / "gone.txt"
    f.write_text("x", encoding="utf-8")
    _old(f)
    real_stat = type(f).stat
    looks = {"n": 0}

    def _stat(self, *a, **k):
        # is_file() still sees it; it is gone by the time its signature is read.
        if self.name == "gone.txt":
            looks["n"] += 1
            if looks["n"] > 1:
                raise FileNotFoundError(2, "The system cannot find the file", str(self))
        return real_stat(self, *a, **k)

    monkeypatch.setattr(type(f), "stat", _stat)
    assert tw.pending_files(inbox, {}, now=time.time()) == []


def test_a_locked_state_file_does_not_kill_the_watcher(setup, monkeypatch, capsys):
    """os.replace fails on Windows while an indexer or antivirus holds the file."""
    inbox, app, client, state = setup
    f = inbox / "a.txt"
    f.write_text("hello", encoding="utf-8")
    _old(f)

    def _locked(*_a, **_k):
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(tw.os, "replace", _locked)
    rc = tw.run("acme", inbox, API, lambda: "t", once=False, interval=1, as_json=True,
                client_factory=_CtxOf(client), state_path=state,
                sleep=lambda _s: None, max_passes=2)
    assert rc == 0
    assert app.calls[0]["method"] == "POST"
    out = capsys.readouterr().out
    assert '"ok": false' not in out, out  # the pass itself completed, it was not rescued


def test_an_unexpected_error_in_one_pass_is_retried_not_fatal(setup, monkeypatch):
    inbox, app, client, state = setup
    boom = {"n": 0}
    real = tw.watch_once

    def _flaky(*a, **k):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("disk hiccup")
        return real(*a, **k)

    monkeypatch.setattr(tw, "watch_once", _flaky)
    rc = tw.run("acme", inbox, API, lambda: "t", once=False, interval=1, as_json=True,
                client_factory=_CtxOf(client), state_path=state,
                sleep=lambda _s: None, max_passes=2)
    assert rc == 0 and boom["n"] == 2
    monkeypatch.setattr(tw, "watch_once", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("again")))
    assert tw.run("acme", inbox, API, "t", once=True, interval=1, as_json=True,
                  client_factory=_CtxOf(client), state_path=state) == 1


def test_unsupported_hidden_and_still_copying_files_are_not_sent(setup):
    inbox, app, client, state = setup
    for name in ("movie.zip", ".hidden.txt", "~$lock.docx"):
        (inbox / name).write_bytes(b"x")
        _old(inbox / name)
    (inbox / "fresh.txt").write_text("still copying", encoding="utf-8")  # mtime = now

    assert tw.watch_once(inbox, API, "t", state, client=client) == []
    assert app.calls == []


def test_auth_refusal_stops_the_watch_with_exit_3(setup, capsys):
    inbox, app, client, state = setup
    f = inbox / "a.txt"
    f.write_text("hello", encoding="utf-8")
    _old(f)
    app.force = 401

    class _Ctx:
        def __enter__(self):
            return client

        def __exit__(self, *exc):
            return False

    rc = tw.run("acme", inbox, API, "bad", once=False, interval=1, as_json=True,
                client_factory=_Ctx, state_path=state)
    assert rc == 3
    assert "adk login" in capsys.readouterr().out


class _CtxOf:
    def __init__(self, client):
        self.client = client

    def __call__(self):
        return self

    def __enter__(self):
        return self.client

    def __exit__(self, *exc):
        return False


def test_expired_login_waits_with_a_notice_and_resumes_after_adk_login(setup):
    """A logon-started watcher must not die when its login expires.

    Pass 1: the saved login is refused -> a notice appears in the Inbox, the watcher
    keeps running and the file is NOT recorded as sent. Pass 2: same token -> no
    request (no hammering). Pass 3: `adk login` wrote a new token -> the file is sent
    and the notice disappears."""
    inbox, app, _client, state = setup
    f = inbox / "a.txt"
    f.write_text("hello", encoding="utf-8")
    _old(f)
    saved = {"tok": "expired"}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer fresh":
            app.calls.append({"auth": request.headers.get("authorization")})
            return httpx.Response(401, json={"detail": "Authentication required"})
        return app(request)

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    notice = inbox / tw.notice_name("acme")
    seen = []

    def _sleep(_s):
        seen.append(notice.exists())
        if len(seen) == 2:
            saved["tok"] = "fresh"  # the user ran `adk login` in another window

    rc = tw.run("acme", inbox, API, lambda: saved["tok"], once=False, interval=1,
                as_json=True, client_factory=_CtxOf(client), state_path=state,
                sleep=_sleep, max_passes=3)

    assert rc == 0
    assert seen == [True, True], "the signed-out watcher left no notice in the Inbox"
    refused = [c for c in app.calls if c.get("auth") == "Bearer expired"]
    assert len(refused) == 1, "an unchanged refused token was retried every pass"
    sent = [c for c in app.calls if c.get("auth") == "Bearer fresh"]
    assert len(sent) == 1 and b'filename="a.txt"' in sent[0]["body"]
    assert not notice.exists(), "the notice outlived the new login"
    client.close()


def test_the_notice_tells_the_user_to_run_adk_login(setup):
    inbox, app, client, state = setup
    seen = []
    tw.run("acme", inbox, API, lambda: "", once=False, interval=1, as_json=True,
           client_factory=_CtxOf(client), state_path=state,
           sleep=lambda _s: seen.append(1), max_passes=2)
    notice = inbox / tw.notice_name("acme")
    assert notice.exists()
    assert "adk login" in notice.read_text(encoding="utf-8")
    assert app.calls == []


def test_no_login_at_start_waits_instead_of_exiting(setup):
    inbox, app, client, state = setup
    f = inbox / "a.txt"
    f.write_text("hello", encoding="utf-8")
    _old(f)
    saved = {"tok": ""}
    passes = []

    def _sleep(_s):
        passes.append(1)
        saved["tok"] = "t"

    rc = tw.run("acme", inbox, API, lambda: saved["tok"], once=False, interval=1,
                as_json=True, client_factory=_CtxOf(client), state_path=state,
                sleep=_sleep, max_passes=2)
    assert rc == 0 and passes == [1]
    assert [c["auth"] for c in app.calls] == ["Bearer t"]
    assert not (inbox / tw.notice_name("acme")).exists()


def test_the_notice_itself_is_never_uploaded(setup):
    inbox, app, client, state = setup
    n = inbox / tw.notice_name("acme")
    n.write_text("sign in", encoding="utf-8")
    _old(n)
    assert tw.watch_once(inbox, API, "t", state, client=client, skip=(n.name,)) == []
    assert app.calls == []


def test_cli_once_uploads_from_the_inbox(tmp_path, monkeypatch, capsys):
    """`adk tenant watch --once --json` end to end through adk.cli.main()."""
    import json
    import sys

    from adk import cli

    inbox = tmp_path / "in"
    inbox.mkdir()
    f = inbox / "note.md"
    f.write_text("# hi", encoding="utf-8")
    _old(f)
    app = _App()
    monkeypatch.setattr(tw, "default_state_path", lambda a: tmp_path / "s.json")
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **k: real_client(transport=httpx.MockTransport(app)))
    monkeypatch.setattr(sys, "argv", ["adk", "tenant", "watch", "--app", "acme",
                                      "--dir", str(inbox),
                                      "--url", API, "--token", "tk", "--once", "--json"])
    with pytest.raises(SystemExit) as ex:
        cli.main()
    assert ex.value.code == 0
    line = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")][-1]
    assert json.loads(line)["results"][0] == {"file": "note.md", "status": "learned",
                                              "detail": ""}
    assert app.calls[0]["auth"] == "Bearer tk"


def test_server_error_is_retried_after_its_backoff(setup):
    inbox, app, client, state = setup
    f = inbox / "a.txt"
    f.write_text("hello", encoding="utf-8")
    _old(f)
    app.force = 503
    t0 = time.time()
    assert [r["status"] for r in tw.watch_once(inbox, API, "t", state, client=client,
                                               now=t0)] == ["error"]
    app.force = None
    # Inside the backoff: nothing is sent.
    assert tw.watch_once(inbox, API, "t", state, client=client, now=t0 + 5) == []
    assert len(app.calls) == 1
    later = t0 + tw.retry_delay(1) + 1
    assert [r["status"] for r in tw.watch_once(inbox, API, "t", state, client=client,
                                               now=later)] == ["learned"]


def test_a_file_the_server_keeps_failing_is_not_resent_every_pass(setup):
    """A 500 for one file, a pass every minute. Before the backoff every pass re-uploaded
    it (17k POSTs a day per bad file). Now it is retried at most hourly, given up only
    after the app has answered 5xx for GIVE_UP_AFTER_S, and then not resent until the
    file changes."""
    inbox, app, client, state = setup
    f = inbox / "corrupt.pdf"
    f.write_bytes(b"%PDF-broken")
    _old(f, age=600)
    app.force = 500
    t0 = time.time()
    days = 4
    statuses = []
    for i in range(0, days * 86400, 60):
        statuses += [r["status"] for r in tw.watch_once(inbox, API, "t", state,
                                                        client=client, now=t0 + i)]
    posts = [c for c in app.calls if c["method"] == "POST"]
    # Bounded: the first hour's backoff, then at most one try an hour.
    assert len(posts) <= tw.MAX_TRIES + 24 * days, f"{len(posts)} uploads in {days} days"
    assert statuses[-1] == "failed" and statuses.count("failed") == 1
    rec = json.loads(state.read_text(encoding="utf-8"))["corrupt.pdf"]
    assert rec["status"] == "failed"
    assert rec["at"] - t0 >= tw.GIVE_UP_AFTER_S - 1
    # Saving the file again (new bytes) earns a fresh round.
    app.force = None
    f.write_bytes(b"%PDF-fixed-and-longer")
    _old(f, age=600)
    res = tw.watch_once(inbox, API, "t", state, client=client, now=t0 + days * 86400 + 5)
    assert [r["status"] for r in res] == ["learned"]


def test_an_app_outage_of_several_hours_loses_no_file(setup):
    """A proxy answering 502 for six hours is an outage, not a bad file: once the app
    answers again the file is sent, never pinned "failed"."""
    inbox, app, client, state = setup
    f = inbox / "minutes.docx"
    f.write_bytes(b"PK minutes")
    _old(f, age=600)
    app.force = 502
    t0 = time.time()
    for i in range(0, 6 * 3600, 60):
        tw.watch_once(inbox, API, "t", state, client=client, now=t0 + i)
    app.force = None
    learned = []
    for i in range(6 * 3600, 8 * 3600, 60):
        learned += [r["status"] for r in tw.watch_once(inbox, API, "t", state,
                                                       client=client, now=t0 + i)]
    assert learned == ["learned"]
    assert app.names == {"minutes.docx": "d1"}


class _Offline:
    """A transport that raises (no HTTP answer) while ``down`` is true."""

    def __init__(self, app):
        self.app = app
        self.down = True
        self.attempts = 0

    def __call__(self, request):
        self.attempts += 1
        if self.down:
            raise httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=request)
        return self.app(request)


def test_a_file_dropped_while_offline_is_sent_once_back_online(setup):
    """The reviewer's repro: a connection error for 90 minutes, then 22.5 h of a healthy
    app. With every no-answer send counted toward MAX_TRIES the file was pinned "failed"
    after ~64 min and never uploaded. A send that got no answer must never count."""
    inbox, app, _client, state = setup
    net = _Offline(app)
    client = httpx.Client(transport=httpx.MockTransport(net))
    f = inbox / "invoice.pdf"
    f.write_bytes(b"%PDF-invoice")
    _old(f, age=600)
    t0 = time.time()
    statuses = []
    for i in range(0, 90 * 60, 5):
        statuses += [r["status"] for r in tw.watch_once(inbox, API, "t", state,
                                                        client=client, now=t0 + i)]
    assert set(statuses) == {"error"}
    net.down = False
    for i in range(90 * 60, 24 * 3600, 30):
        statuses += [r["status"] for r in tw.watch_once(inbox, API, "t", state,
                                                        client=client, now=t0 + i)]
    assert "failed" not in statuses
    assert statuses[-1] == "learned" and statuses.count("learned") == 1
    assert app.names == {"invoice.pdf": "d1"}
    # Bounded while offline: the backoff, then at most one try an hour.
    assert net.attempts <= 10, net.attempts
    client.close()


def test_five_days_offline_never_gives_a_file_up(setup):
    inbox, app, _client, state = setup
    net = _Offline(app)
    client = httpx.Client(transport=httpx.MockTransport(net))
    f = inbox / "plan.md"
    f.write_text("# plan", encoding="utf-8")
    _old(f, age=600)
    t0 = time.time()
    for i in range(0, 5 * 86400, 300):
        tw.watch_once(inbox, API, "t", state, client=client, now=t0 + i)
    rec = json.loads(state.read_text(encoding="utf-8"))["plan.md"]
    assert rec["status"] == "error" and rec["tries"] == 0
    assert net.attempts <= 24 * 5 + 8
    net.down = False
    res = tw.watch_once(inbox, API, "t", state, client=client, now=rec["retry_at"] + 1)
    assert [r["status"] for r in res] == ["learned"]
    client.close()


class _FailsFor:
    """Answers 503 for the named files only; every other file goes to the app."""

    def __init__(self, app, bad):
        self.app = app
        self.bad = set(bad)

    def __call__(self, request):
        body = request.read()
        if request.method == "POST":
            name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
            if name in self.bad:
                self.app.calls.append({"method": "POST", "path": request.url.path,
                                       "auth": None, "body": body})
                return httpx.Response(503, json={"detail": "busy"})
        return self.app(request)


def test_one_failing_file_does_not_block_the_files_after_it(setup):
    """The reviewer's repro: a.txt answered 503, b.txt and c.txt pending after it. A name
    clash in watch_once raised TypeError on the second file, so no state was saved,
    b/c were never sent and a.txt was re-POSTed on every pass with no backoff."""
    inbox, app, _client, state = setup
    client = httpx.Client(transport=httpx.MockTransport(_FailsFor(app, {"a.txt"})))
    for n in ("a.txt", "b.txt", "c.txt"):
        (inbox / n).write_text(n, encoding="utf-8")
        _old(inbox / n)
    t0 = time.time()
    res = tw.watch_once(inbox, API, "t", state, client=client, now=t0)
    assert {r["file"]: r["status"] for r in res} == {"a.txt": "error", "b.txt": "learned",
                                                     "c.txt": "learned"}
    rec = json.loads(state.read_text(encoding="utf-8"))
    assert rec["a.txt"]["status"] == "error" and rec["a.txt"]["retry_at"] > t0
    assert rec["b.txt"]["status"] == rec["c.txt"]["status"] == "learned"
    assert set(app.names) == {"b.txt", "c.txt"}
    # The failing file is now in backoff: the next passes send nothing at all.
    before = len(app.calls)
    for i in range(5, int(tw.retry_delay(1)), 5):
        assert tw.watch_once(inbox, API, "t", state, client=client, now=t0 + i) == []
    assert len(app.calls) == before
    # After the backoff only a.txt is retried, and its attempt count advances.
    res = tw.watch_once(inbox, API, "t", state, client=client,
                        now=t0 + tw.retry_delay(1) + 1)
    assert [(r["file"], r["status"]) for r in res] == [("a.txt", "error")]
    assert json.loads(state.read_text(encoding="utf-8"))["a.txt"]["attempts"] == 2
    client.close()


def test_two_failing_files_both_back_off_and_both_give_up(setup):
    """Several files erroring in the same pass: each gets its own backoff, and the
    give-up clock (MAX_TRIES + GIVE_UP_AFTER_S) takes effect for every one of them."""
    inbox, app, _client, state = setup
    client = httpx.Client(transport=httpx.MockTransport(
        _FailsFor(app, {"a.pdf", "b.pdf"})))
    for n in ("a.pdf", "b.pdf", "c.txt"):
        (inbox / n).write_bytes(n.encode())
        _old(inbox / n, age=600)
    t0 = time.time()
    days = 4
    for i in range(0, days * 86400, 300):
        tw.watch_once(inbox, API, "t", state, client=client, now=t0 + i)
    rec = json.loads(state.read_text(encoding="utf-8"))
    assert rec["a.pdf"]["status"] == rec["b.pdf"]["status"] == "failed"
    assert rec["c.txt"]["status"] == "learned"
    posts = [c for c in app.calls if c["method"] == "POST"]
    assert len(posts) <= 2 * (tw.MAX_TRIES + 24 * days) + 1, len(posts)
    client.close()


def test_files_the_app_will_not_take_are_listed_in_the_inbox(setup):
    """The watcher runs hidden: a given-up or unreadable file must be told in the
    folder the user looks at, and the note must go once nothing is left to tell."""
    inbox, app, _client, state = setup
    bad = inbox / "scan.pdf"
    bad.write_bytes(b"%PDF-image-only")
    _old(bad, age=600)
    refusals = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        if b"image-only" in body:
            refusals.append(1)
            return httpx.Response(400, json={"detail": "No text could be extracted"})
        return app(request)

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    note = inbox / tw.problems_name("acme")
    rc = tw.run("acme", inbox, API, lambda: "t", once=False, interval=1, as_json=True,
                client_factory=_CtxOf(client), state_path=state,
                sleep=lambda _s: None, max_passes=3)
    assert rc == 0
    assert note.exists(), "a refused file left no trace the user can see"
    text = note.read_text(encoding="utf-8")
    assert "scan.pdf" in text and "save it again" in text
    assert len(refusals) == 1 and app.calls == [], "the note was uploaded or the file resent"
    # The user saves it again as a readable file: it is sent and the note goes.
    bad.write_bytes(b"%PDF-with-a-text-layer-now")
    _old(bad, age=600)
    tw.run("acme", inbox, API, lambda: "t", once=False, interval=1, as_json=True,
           client_factory=_CtxOf(client), state_path=state,
           sleep=lambda _s: None, max_passes=1)
    assert app.names == {"scan.pdf": "d1"}
    assert not note.exists()
    client.close()


def test_the_watchers_own_notes_are_never_uploaded(setup):
    inbox, app, client, state = setup
    for name in tw.notice_names("acme"):
        (inbox / name).write_text("note", encoding="utf-8")
        _old(inbox / name)
    rc = tw.run("acme", inbox, API, lambda: "t", once=False, interval=1, as_json=True,
                client_factory=_CtxOf(client), state_path=state,
                sleep=lambda _s: None, max_passes=1)
    assert rc == 0 and app.calls == []


def test_a_400_could_not_extract_is_refused_and_never_resent(setup):
    inbox, app, client, state = setup
    f = inbox / "scan.pdf"
    f.write_bytes(b"%PDF-image-only")
    _old(f, age=600)
    app.force = 400
    t0 = time.time()
    first = tw.watch_once(inbox, API, "t", state, client=client, now=t0)
    assert [r["status"] for r in first] == ["refused"]
    for i in range(1, 50):
        assert tw.watch_once(inbox, API, "t", state, client=client, now=t0 + i * 3600) == []
    assert len(app.calls) == 1


def test_a_viewer_role_403_says_ask_your_admin_not_adk_login(setup):
    """The host app answers a signed-in viewer 403 "role 'viewer' cannot write". `adk login`
    cannot fix a role, so the Inbox notice must not send the user there."""
    inbox, app, _client, state = setup
    f = inbox / "a.txt"
    f.write_text("hello", encoding="utf-8")
    _old(f)

    def _handler(request: httpx.Request) -> httpx.Response:
        app.calls.append({"method": request.method})
        return httpx.Response(403, json={"detail": "role 'viewer' cannot write"})

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    rc = tw.run("acme", inbox, API, lambda: "tok", once=False, interval=1, as_json=True,
                client_factory=_CtxOf(client), state_path=state,
                sleep=lambda _s: None, max_passes=3)
    assert rc == 0
    role = inbox / tw.notice_name("acme", "role")
    text = role.read_text(encoding="utf-8")
    assert "role 'viewer' cannot write" in text
    assert "admin" in text and "adk login" not in text
    # The NAME the user reads in Explorer must not say "sign in again" either.
    assert "SIGN IN" not in role.name.upper() and "ADMIN" in role.name.upper()
    assert not (inbox / tw.notice_name("acme")).exists()
    assert len(app.calls) == 1, "an unchanged role-refused token was retried every pass"
    client.close()


def test_a_role_notice_and_a_login_notice_never_sit_side_by_side(setup):
    """The role is fixed but the login then expires (or the reverse): only the CURRENT
    reason may be in the Inbox, and a good pass clears whichever is there."""
    inbox, app, _client, state = setup
    f = inbox / "a.txt"
    f.write_text("hello", encoding="utf-8")
    _old(f)
    answer = {"code": 403, "detail": "role 'viewer' cannot write"}
    saved = {"tok": "t1"}

    def _handler(request: httpx.Request) -> httpx.Response:
        if answer["code"]:
            return httpx.Response(answer["code"], json={"detail": answer["detail"]})
        return app(request)

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    role = inbox / tw.notice_name("acme", "role")
    login = inbox / tw.notice_name("acme")
    seen = []

    def _sleep(_s):
        seen.append((role.exists(), login.exists()))
        if len(seen) == 1:  # the admin fixed the role; the login expired meanwhile
            answer.update(code=401, detail="Authentication required")
            saved["tok"] = "t2"
        elif len(seen) == 2:  # the user ran adk login
            answer.update(code=0)
            saved["tok"] = "t3"

    rc = tw.run("acme", inbox, API, lambda: saved["tok"], once=False, interval=1,
                as_json=True, client_factory=_CtxOf(client), state_path=state,
                sleep=_sleep, max_passes=3)
    assert rc == 0
    assert seen == [(True, False), (False, True)]
    assert not role.exists() and not login.exists()
    assert app.names == {"a.txt": "d1"}
    client.close()


def test_a_tenant_mismatch_403_still_says_adk_login():
    class _R:
        status_code = 403

        def json(self):
            return {"detail": "token belongs to another tenant"}

    class _C:
        def post(self, *a, **k):
            return _R()

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "a.txt"
        f.write_text("x", encoding="utf-8")
        with pytest.raises(tw.AuthRefusedError) as ex:
            tw.upload_one(_C(), API, "t", f)
    assert ex.value.kind == "login" and "adk login" in str(ex.value)


def test_there_is_no_default_app_or_api_url(monkeypatch):
    """One tenant's watcher must never send its files to another tenant's API: the
    URL comes only from --url or AITHER_<APP>_API_URL, and --app is required."""
    import argparse

    monkeypatch.delenv("AITHER_ACME_API_URL", raising=False)
    assert not hasattr(tw, "APP_API_URLS")
    with pytest.raises(ValueError):
        tw.resolve_api_url("acme")
    with pytest.raises(ValueError):
        tw.resolve_api_url("acme", "https:/")
    monkeypatch.setenv("AITHER_ACME_API_URL", "https://acme-api.example/")
    assert tw.resolve_api_url("acme") == "https://acme-api.example"
    ap = argparse.ArgumentParser()
    tw.register(ap.add_subparsers(dest="command"))
    with pytest.raises(SystemExit):
        ap.parse_args(["tenant", "watch", "--once"])
