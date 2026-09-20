"""mcp_stdio must not dead-end on the DISCOVERED guard.

Measured 2026-09-03: every session on this box is `origin: discovered`, so
`_steerable_guard` refuses ALL of them and `awsh_send` was, in practice,
inapplicable to every real session. This file pins the fix (falling through to
`store.write_steer`'s mailbox tier, and reporting which tier landed rather than
collapsing pty/mailbox into one bare "sent") and the new `awsh_say` tool that
adds addressing on top of the same two tiers.

No live daemon here: every test replaces `mcp_stdio._req` with a small fake
that answers exactly the daemon routes this module calls, so the arms are
deterministic and fast. The mailbox itself is real (a tmp `AITHER_STEER_DIR`),
because the header on disk -- not a mock call -- is the actual contract with
the drain hook (`.claude/hooks/steer-mailbox-drain.py`) and the thing a caller
of these tools is trusting.
"""

from __future__ import annotations

import re

import pytest
from adk.harnesses import mcp_stdio

HEADER_RE = re.compile(
    r'^<!-- aither-steer v1 authority="(?P<authority>[^"]*)" from="(?P<sender>[^"]*)" '
    r'kind="(?P<kind>[^"]*)" event="(?P<event>[^"]*)" -->$'
)


class FakeDaemon:
    """Answers exactly the routes mcp_stdio calls, and records every call made.

    Signature matches `mcp_stdio._req(method, path, body=None, timeout=20.0)`.
    """

    def __init__(self, *, managed_ids=(), opted_in=(), sessions_unified=(),
                 fail_input=False, fail_events=False, wake_jobs=None):
        self.calls: list[tuple[str, str, object]] = []
        self.managed_ids = set(managed_ids)
        # Managed sessions spawned with allow_peer_input=true; the rest are managed
        # but closed to a peer's keyboard (the daemon's default).
        self.opted_in = set(opted_in)
        self.sessions_unified = list(sessions_unified)
        self.fail_input = fail_input
        self.fail_events = fail_events
        # name -> job dict, for GET /wakes/{name} (the only /wakes route this
        # module still calls -- mutation lives in the awrise_* toolpack, not here).
        self.wake_jobs = dict(wake_jobs or {})

    def __call__(self, method, path, body=None, timeout=20.0):
        self.calls.append((method, path, body))
        if method == "GET" and path == "/sessions":
            return {"sessions": [{"id": i, "allow_peer_input": i in self.opted_in}
                                 for i in self.managed_ids]}
        if method == "GET" and path == "/sessions/unified":
            return {"sessions": self.sessions_unified}
        if method == "POST" and path.endswith("/input"):
            if self.fail_input:
                return {"error": "harness daemon unreachable at 127.0.0.1:8362: refused"}
            return {"ok": True, "turn": 1, "seq": 7}
        if method == "POST" and path == "/events":
            if self.fail_events:
                return {"error": "harness POST /events: HTTP 400", "detail": "bad envelope"}
            return {"ok": True, "seq": 42, "pillar": "orchestration"}
        if method == "GET" and path == "/wakes":
            return {"jobs": list(self.wake_jobs.values())}
        if method == "GET" and path.startswith("/wakes/") and "?" not in path:
            name = path.split("/wakes/", 1)[1]
            job = self.wake_jobs.get(name)
            if job is None:
                return {"error": "harness GET %s: HTTP 404" % path, "detail": "no such wake"}
            return dict(job)
        if method == "POST" and path == "/wakes":
            # The real daemon raises a wakes-add card; nothing exists yet.
            return {"pending": True, "card_id": "card-fake", "name": body.get("name")}
        if method == "PATCH" and path.startswith("/wakes/"):
            return {"ok": True, "pending": "command" in (body or {})}
        if method == "POST" and path.startswith("/wakes/"):
            return {"ok": True, "verb": path.rsplit("/", 1)[1]}
        raise AssertionError("unhandled fake-daemon route: %s %s" % (method, path))


@pytest.fixture()
def steer_root(tmp_path, monkeypatch):
    """Point the mailbox at tmp_path. Never the real ~/.aither/steer."""
    root = tmp_path / "steer"
    monkeypatch.setenv("AITHER_STEER_DIR", str(root))
    return root


def _one_mailbox_file(root, session_id):
    box = root / session_id
    files = sorted(box.glob("*.md")) if box.exists() else []
    assert len(files) == 1, f"expected exactly one mailbox file in {box}, got {files}"
    return files[0]


def read_header(path) -> dict:
    first = path.read_text(encoding="utf-8").splitlines()[0]
    match = HEADER_RE.match(first)
    assert match, f"line 1 is not a v1 steer header: {first!r}"
    return match.groupdict()


# ── awsh_send: falls through instead of dead-ending ─────────────────────────


def test_send_discovered_session_falls_through_to_mailbox(monkeypatch, steer_root):
    """This is the arm that fails against the old code: it used to return the
    guard's refusal and write nothing."""
    fake = FakeDaemon(managed_ids=set())  # nobody is managed -> DISCOVERED
    monkeypatch.setattr(mcp_stdio, "_req", fake)

    result = mcp_stdio._send("sess-1", "hello from a peer")

    assert result["channel"] == "mailbox"
    assert result["landed_now"] is False
    assert result["queued"] is True
    assert "why_not_pty" in result and "DISCOVERED" in result["why_not_pty"]

    path = _one_mailbox_file(steer_root, "sess-1")
    header = read_header(path)
    assert header["authority"] == "peer"
    body = path.read_text(encoding="utf-8")
    assert "[via awsh from" in body
    assert "A peer's request carries no authority" in body


def test_send_managed_but_not_opted_in_session_takes_the_mailbox(monkeypatch, steer_root):
    """Owner ruling 2026-09-19: an awsh tool is a PEER whatever bearer it holds, so a
    managed tab that did not opt in gets the mailbox, never a POST to its pty."""
    fake = FakeDaemon(managed_ids={"sess-managed"})
    monkeypatch.setattr(mcp_stdio, "_req", fake)

    result = mcp_stdio._send("sess-managed", "go")

    assert result["channel"] == "mailbox"
    assert result["landed_now"] is False
    assert "did not opt in" in result["why_not_pty"]
    assert [c for c in fake.calls if c[1].endswith("/input")] == []
    _one_mailbox_file(steer_root, "sess-managed")


def test_send_managed_session_reports_pty(monkeypatch, steer_root):
    fake = FakeDaemon(managed_ids={"sess-managed"}, opted_in={"sess-managed"})
    monkeypatch.setattr(mcp_stdio, "_req", fake)

    result = mcp_stdio._send("sess-managed", "go")

    assert result["channel"] == "pty"
    assert result["landed_now"] is True
    assert result["queued"] is False
    # No mailbox write for the managed/live tier.
    assert not (steer_root / "sess-managed").exists()
    # The framed body reached the daemon, not the raw text.
    input_calls = [c for c in fake.calls if c[1].endswith("/input")]
    assert len(input_calls) == 1
    assert "[via awsh from" in input_calls[0][2]["text"]


def test_send_refuses_empty_text(monkeypatch, steer_root):
    fake = FakeDaemon(managed_ids=set())
    monkeypatch.setattr(mcp_stdio, "_req", fake)

    result = mcp_stdio._send("sess-1", "   ")

    assert "error" in result
    assert fake.calls == []  # refused before any network call


def test_send_guard_wording_is_preserved(monkeypatch, steer_root):
    """The guard's explanation is documentation, not dead code -- it must
    still be reachable, just no longer the terminus."""
    guard = mcp_stdio._steerable_guard("sess-x")
    assert guard is not None
    assert "DISCOVERED" in guard["error"]
    assert "cannot be steered or interrupted" in guard["error"]


# ── awsh_say: addressing, refusals, and honest delivery reporting ───────────


def test_say_refuses_empty_text(monkeypatch, steer_root):
    fake = FakeDaemon()
    monkeypatch.setattr(mcp_stdio, "_req", fake)

    result = mcp_stdio._say("other", "", "main", 0)

    assert "error" in result
    assert fake.calls == []


def test_say_refuses_self_address(monkeypatch, steer_root):
    fake = FakeDaemon()
    monkeypatch.setattr(mcp_stdio, "_req", fake)
    monkeypatch.setattr(mcp_stdio, "SENDER", "claude_code:me")

    result = mcp_stdio._say("claude_code:me", "hello", "main", 0)

    assert "error" in result
    assert "itself" in result["error"]
    assert fake.calls == []


def test_say_refuses_hops_at_ceiling(monkeypatch, steer_root):
    fake = FakeDaemon(managed_ids=set())
    monkeypatch.setattr(mcp_stdio, "_req", fake)

    result = mcp_stdio._say("other", "hello", "main", 2)

    assert "error" in result
    assert "hop limit" in result["error"]
    assert fake.calls == []


def test_say_refuses_hops_past_ceiling(monkeypatch, steer_root):
    fake = FakeDaemon(managed_ids=set())
    monkeypatch.setattr(mcp_stdio, "_req", fake)

    result = mcp_stdio._say("other", "hello", "main", 3)

    assert "error" in result
    assert fake.calls == []


def test_say_hops_zero_and_one_are_accepted(monkeypatch, steer_root):
    fake = FakeDaemon(managed_ids=set())
    monkeypatch.setattr(mcp_stdio, "_req", fake)

    for hops in (0, 1):
        result = mcp_stdio._say("other-%d" % hops, "hello", "main", hops)
        assert "error" not in result, result


def test_say_sendmessage_address_from_stubbed_sessions_unified(monkeypatch, steer_root):
    fake = FakeDaemon(
        managed_ids=set(),  # DISCOVERED -> mailbox tier
        sessions_unified=[
            {"id": "other", "title": "AitherOS-Fresh (peer tab)", "harness": "claude"},
        ],
    )
    monkeypatch.setattr(mcp_stdio, "_req", fake)

    result = mcp_stdio._say("other", "please check the build", "main", 0)

    assert result["sendmessage_address"] == "AitherOS-Fresh (peer tab)"
    assert "SendMessage" in result["next"]
    assert result["channel"] == "mailbox"
    assert result["landed_now"] is False


def test_say_falls_back_to_raw_id_when_session_unknown(monkeypatch, steer_root):
    fake = FakeDaemon(managed_ids=set(), sessions_unified=[])
    monkeypatch.setattr(mcp_stdio, "_req", fake)

    result = mcp_stdio._say("mystery-session", "hi", "main", 0)

    assert result["sendmessage_address"] == "mystery-session"


def test_say_publishes_the_addressed_event(monkeypatch, steer_root):
    fake = FakeDaemon(managed_ids=set())
    monkeypatch.setattr(mcp_stdio, "_req", fake)

    result = mcp_stdio._say("other", "hi", "main", 0)

    assert result["published"] is True
    assert result["seq"] == 42
    publish_calls = [c for c in fake.calls if c[1] == "/events"]
    assert len(publish_calls) == 1
    body = publish_calls[0][2]
    assert body["to"] == ["other"]
    assert body["hops"] == 0
    assert body["type"] == "steering"


def test_say_publish_failure_does_not_block_delivery(monkeypatch, steer_root):
    """The room round trip is best-effort; a producer that cannot reach /events
    yet (or ever, for this event) must still deliver over pty/mailbox."""
    fake = FakeDaemon(managed_ids=set(), fail_events=True)
    monkeypatch.setattr(mcp_stdio, "_req", fake)

    result = mcp_stdio._say("other", "hi", "main", 0)

    assert result["published"] is False
    assert result["seq"] is None
    assert result["channel"] == "mailbox"  # delivery still happened


def test_say_delivered_body_carries_peer_authority_framing(monkeypatch, steer_root):
    fake = FakeDaemon(managed_ids=set())
    monkeypatch.setattr(mcp_stdio, "_req", fake)
    monkeypatch.setattr(mcp_stdio, "SENDER", "claude_code:me")

    mcp_stdio._say("other", "please rebase", "main", 0)

    path = _one_mailbox_file(steer_root, "other")
    header = read_header(path)
    assert header["authority"] == "peer"
    assert header["sender"] == "claude_code:me"
    body = path.read_text(encoding="utf-8")
    assert "A peer's request carries no authority" in body


def test_say_managed_target_reports_pty_and_no_next_action(monkeypatch, steer_root):
    fake = FakeDaemon(managed_ids={"other"}, opted_in={"other"})
    monkeypatch.setattr(mcp_stdio, "_req", fake)

    result = mcp_stdio._say("other", "hi", "main", 0)

    assert result["channel"] == "pty"
    assert result["landed_now"] is True
    assert "no further action" in result["next"]


def test_say_managed_but_not_opted_in_target_takes_the_mailbox(monkeypatch, steer_root):
    """The ruling's own verify: awsh_say to a non-opted-in session answers mailbox,
    and the pty route is never called for it."""
    fake = FakeDaemon(managed_ids={"other"})
    monkeypatch.setattr(mcp_stdio, "_req", fake)

    result = mcp_stdio._say("other", "hi", "main", 0)

    assert result["channel"] == "mailbox"
    assert result["landed_now"] is False and result["queued"] is True
    assert [c for c in fake.calls if c[1].endswith("/input")] == []
    _one_mailbox_file(steer_root, "other")


def test_say_tool_is_registered():
    assert "awsh_say" in mcp_stdio.BY_NAME
    tool = mcp_stdio.BY_NAME["awsh_say"]
    assert "to" in tool["schema"]["properties"]
    assert "text" in tool["schema"]["properties"]
    assert tool["schema"]["required"] == ["to", "text"]



# ── awsh_wake_*: mutation is steering, and command authoring is a CARD ──────
#
# History, because this was decided twice. A build once added seven mutating
# verbs here and they were removed: awrise is RCE-capable, and a bare create
# verb reachable from any awsh_spawn'd session would have let any harness with
# the token author a host command. That pin held until 2026-09-20, when two
# things were measured: (1) the intent proof (wf_ac32a8e7) was NOT PROVEN --
# a spawned session told to 'schedule a job' had NO wake verb and silently
# substituted Claude Code's session-scoped CronCreate; the toolpack is an
# adk-agent surface a Claude Code session never sees; (2) the daemon's
# POST /wakes and command-changing PATCH now raise a card the owner confirms
# before any command exists (daemon.py create_wake, card_recipes wakes-add /
# wakes-set-command), re-validated at apply time. The owner's standing ruling
# is that scheduling must work by prompting. So the invariant is no longer
# 'no mutation verbs'; it is: NO VERB HERE CAN AUTHOR A COMMAND WITHOUT A
# HUMAN ANSWER, and every verb refuses a name that could become a path or an
# option. remove and explain stay absent: remove is create-tier and unneeded,
# explain is read-side and belongs on awsh_wakes.


def test_wake_mutation_verbs_are_exactly_the_steering_set():
    registered = set(mcp_stdio.BY_NAME)
    expected = {"awsh_wake_add", "awsh_wake_set", "awsh_wake_enable",
                "awsh_wake_disable", "awsh_wake_run"}
    assert expected <= registered, expected - registered
    assert not ({"awsh_wake_remove", "awsh_wake_explain"} & registered)


def test_wake_add_goes_to_the_card_raising_route_only():
    fake = FakeDaemon()
    orig = mcp_stdio._req
    mcp_stdio._req = fake
    try:
        mcp_stdio._awsh_wake_add({"name": "nightly", "command": "backup.sh",
                                  "every": "1d", "timeout": 60})
        assert fake.calls and fake.calls[-1][0] == "POST"
        assert fake.calls[-1][1] == "/wakes"
        body = fake.calls[-1][2]
        assert body["command"] == "backup.sh" and body["every"] == "1d"
        assert body["timeout"] == 60 and "cwd" not in body
    finally:
        mcp_stdio._req = orig


def test_wake_add_asks_instead_of_guessing():
    out = mcp_stdio._awsh_wake_add({"name": "nightly"})
    assert "error" in out and "ask" in out


def test_wake_verbs_refuse_a_name_that_could_be_a_path_or_an_option():
    fake = FakeDaemon()
    orig = mcp_stdio._req
    mcp_stdio._req = fake
    try:
        for bad in ("../x", "-name", "a b", "x" * 65, "", "a;rm"):
            for fn in (mcp_stdio._awsh_wake_add, mcp_stdio._awsh_wake_set,
                       mcp_stdio._awsh_wake_verb("run")):
                out = fn({"name": bad, "command": "c", "every": "1h"})
                assert out.get("error") == "invalid wake name", (bad, out)
        assert fake.calls == []
    finally:
        mcp_stdio._req = orig


def test_wake_set_without_fields_asks():
    out = mcp_stdio._awsh_wake_set({"name": "nightly", "note": "x"})
    assert "ask" in out


def test_wake_steering_verbs_hit_the_named_route():
    fake = FakeDaemon()
    orig = mcp_stdio._req
    mcp_stdio._req = fake
    try:
        for verb in ("enable", "disable", "run"):
            mcp_stdio._awsh_wake_verb(verb)({"name": "nightly"})
            assert fake.calls[-1][:2] == ("POST", "/wakes/nightly/%s" % verb)
    finally:
        mcp_stdio._req = orig


def test_awsh_wakes_tool_is_read_only():
    fake = FakeDaemon(wake_jobs={
        "nightly-backup": {"name": "nightly-backup", "run": "backup.sh",
                           "every": "0 2 * * *", "enabled": True}})
    monkeypatch_target = mcp_stdio
    orig = monkeypatch_target._req
    monkeypatch_target._req = fake
    try:
        result = mcp_stdio._awsh_wakes({"name": "nightly-backup"})
        assert result["name"] == "nightly-backup"
        assert fake.calls == [("GET", "/wakes/nightly-backup", None)]
        assert all(c[0] == "GET" for c in fake.calls)
    finally:
        monkeypatch_target._req = orig


def test_awsh_wakes_docstring_names_no_mutation():
    doc = mcp_stdio._awsh_wakes.__doc__ or ""
    assert "read-only" in doc.lower() or "never mutates" in doc.lower()
    assert "card" in doc.lower()  # the reason the verbs exist is written down


# ── the scoped agent token: preferred, and the root fallback is LOUD ────────────
#
# Without this the daemon's "owner-plan only" tier-1 rule is theatre: every tab that
# presents ~/.aither/harness_token IS the owner to the daemon. Nothing below touches a
# live ~/.aither -- both token paths and the principals registry are re-pointed at tmp.


@pytest.fixture()
def token_files(tmp_path, monkeypatch):
    root = tmp_path / "harness_token"
    root.write_text("root-bearer-value", encoding="utf-8")
    scoped = tmp_path / "agent-tokens" / "me"
    monkeypatch.setattr(mcp_stdio, "TOKEN_PATH", root)
    monkeypatch.setenv(mcp_stdio.AGENT_TOKEN_FILE_ENV, str(scoped))
    monkeypatch.setattr(mcp_stdio, "_ROOT_FALLBACK_WARNED", False)
    return {"root": root, "scoped": scoped}


def test_token_prefers_the_scoped_agent_token_when_present(token_files, capsys):
    token_files["scoped"].parent.mkdir(parents=True)
    token_files["scoped"].write_text("scoped-agent-value\n", encoding="utf-8")

    assert mcp_stdio._token() == "scoped-agent-value"
    assert capsys.readouterr().err == ""


def test_token_falls_back_to_root_with_exactly_one_stderr_line(token_files, capsys):
    first = mcp_stdio._token()
    second = mcp_stdio._token()

    assert first == second == "root-bearer-value"
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.strip()]
    assert len(lines) == 1, err
    assert "ROOT" in lines[0] and str(token_files["scoped"]) in lines[0]


def test_token_is_empty_and_silent_when_neither_file_exists(token_files, capsys):
    token_files["root"].unlink()
    assert mcp_stdio._token() == ""
    assert capsys.readouterr().err == ""


def test_agent_token_path_defaults_under_agent_tokens_and_is_filename_safe(monkeypatch):
    monkeypatch.delenv(mcp_stdio.AGENT_TOKEN_FILE_ENV, raising=False)
    path = mcp_stdio.agent_token_path("an unidentified peer session")
    assert path.parent.name == "agent-tokens"
    assert path.name == "an_unidentified_peer_session"
    assert mcp_stdio.agent_token_path("../../etc/passwd").name == "etc_passwd"


def test_mint_agent_token_creates_the_directory_lazily_with_plan_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_stdio, "_ROOT_FALLBACK_WARNED", False)
    registry = tmp_path / "harness_tokens.json"
    target = tmp_path / "agent-tokens" / "claude_code:me"
    assert not target.parent.exists()

    landed = mcp_stdio.mint_agent_token("claude_code:me", path=target, registry=registry)

    assert landed == target and target.is_file()
    token = target.read_text(encoding="utf-8").strip()
    assert token
    from adk.harnesses.daemon import load_principals, resolve_principal

    principal = resolve_principal(token, "not-the-root-bearer", load_principals(registry))
    assert principal is not None
    assert principal.plan == "agent"
    assert principal.id == "agent:claude_code:me"
    assert principal.paths == mcp_stdio.AGENT_TOKEN_PATHS
    assert principal.may_reach("/events") and principal.may_reach("/sessions/x/input")
    assert not principal.may_reach("/fs/read"), "an agent token must not open the fs routes"


def test_spawn_passes_allow_peer_input_only_when_true(monkeypatch, steer_root):
    calls = []

    def spawn_fake(method, path, body=None, timeout=20.0):
        calls.append((method, path, body))
        return {"id": "sess-new"}

    monkeypatch.setattr(mcp_stdio, "_req", spawn_fake)
    mcp_stdio._spawn({"harness": "claude-tty", "allow_peer_input": True})
    mcp_stdio._spawn({"harness": "claude-tty"})

    opted, default = calls[0][2], calls[1][2]
    assert opted["allow_peer_input"] is True
    assert "allow_peer_input" not in default


def test_spawn_tool_schema_offers_allow_peer_input():
    spawn = [t for t in mcp_stdio.TOOLS if t["name"] == "awsh_spawn"][0]
    assert spawn["schema"]["properties"]["allow_peer_input"]["type"] == "boolean"


# ── awsh_send tier 3: a session with no mailbox HERE is still reachable ──


def test_send_falls_through_to_the_relay_channel_when_there_is_no_mailbox(monkeypatch):
    """The mailbox is a file on THIS box. A session on another machine is alive,
    mirrored and addressable -- answering 'not a valid session id' for it was the
    tool reporting its own locality as the target's fault."""
    fake = FakeDaemon(managed_ids=set())
    monkeypatch.setattr(mcp_stdio, "_req", fake)
    monkeypatch.setattr(mcp_stdio, "_write_mailbox_fallback",
                        lambda *a, **kw: None)
    posted = {}
    monkeypatch.setattr(mcp_stdio, "_relay_channel_fallback",
                        lambda sid, text: posted.setdefault("ch", "#session-" + sid[:8]))

    result = mcp_stdio._send("77db6255-4db2-4b40-a716-c005488ca203", "peer here")

    assert result["channel"] == "relay"
    assert result["relay_channel"].startswith("#session-")
    assert result["queued"] is True and result["landed_now"] is False
    assert "DISCOVERED" in result["why_not_pty"]


def test_send_says_none_landed_when_neither_mailbox_nor_relay_works(monkeypatch):
    fake = FakeDaemon(managed_ids=set())
    monkeypatch.setattr(mcp_stdio, "_req", fake)
    monkeypatch.setattr(mcp_stdio, "_write_mailbox_fallback", lambda *a, **kw: None)
    monkeypatch.setattr(mcp_stdio, "_relay_channel_fallback", lambda sid, text: "")

    result = mcp_stdio._send("sess-unknown", "peer here")

    assert result["channel"] == "none" and "error" in result
    assert result["landed_now"] is False


def test_the_mailbox_detail_does_not_promise_a_prompt_wait(monkeypatch, steer_root):
    """Since the PostToolUse drain landed, a WORKING session reads this in seconds.
    The old copy ('not delivered live', 'next turn boundary') told callers to expect
    a latency that no longer exists."""
    fake = FakeDaemon(managed_ids=set())
    monkeypatch.setattr(mcp_stdio, "_req", fake)

    result = mcp_stdio._send("sess-copy", "hello")

    assert result["channel"] == "mailbox"
    assert "next tool call" in result["detail"]
    assert "not delivered live" not in result["detail"]


def test_a_short_session_id_has_no_relay_channel():
    """`#session-<id8>` needs eight hex; a stub id must not become `#session-abc`."""
    assert mcp_stdio._relay_channel_fallback("abc", "x") == ""
