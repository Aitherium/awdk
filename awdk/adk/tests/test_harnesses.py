"""Contract tests for the AitherShell harness layer.

These assert the invariants whose violation is SILENT — the ones where the
system keeps returning 200s and rendering happily while being wrong:

1. An unclassifiable harness line is preserved as RAW, never dropped. A dropped
   line turns "this harness changed its protocol" into "the model stopped
   responding", which is the most expensive misdiagnosis in this design.
2. A model binding CLEARS every managed variable before overlaying. An overlay
   that only adds leaves the previous provider's value on any var the new
   profile does not set, and exactly one scenario (subagents, summarisation)
   then silently talks to the old API.
3. A bound session drops the ``user`` settings source. ``env`` in
   ``~/.claude/settings.json`` overrides process env, so without this the
   session runs the GLOBAL model while the UI label says otherwise — a whole
   task billed to the wrong provider with no error anywhere.

Every test carries a mutation guard: it states the broken shape it would catch,
so a checker nobody has watched fail does not accumulate here.
"""

from __future__ import annotations

import pytest
from adk.harnesses.adapters import translate_claude, translate_gemini
from adk.harnesses.events import EventKind
from adk.harnesses.models import MANAGED_VARS, ModelBinding, apply_binding
from adk.harnesses.registry import LaunchSpec, Transport, detect, get

# ── 1. adapters never drop a line ───────────────────────────────────────────

def test_claude_init_becomes_session_ready_with_model():
    events = translate_claude(
        {
            "type": "system",
            "subtype": "init",
            "model": "deepseek-v4-flash[1m]",
            "session_id": "abc123",
            "cwd": "/repo",
            "tools": ["Read"],
        }
    )
    assert len(events) == 1
    assert events[0].kind == EventKind.SESSION_READY
    # The model field is what the per-session binding assertion compares
    # against. If this stops being carried, the mismatch warning goes quiet.
    assert events[0].data["model"] == "deepseek-v4-flash[1m]"
    assert events[0].data["harness_session_id"] == "abc123"


def test_claude_assistant_text_and_tool_use():
    events = translate_claude(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file": "a"}},
                ],
            },
        }
    )
    kinds = [e.kind for e in events]
    assert kinds == [EventKind.TEXT_DELTA, EventKind.THINKING_DELTA, EventKind.TOOL_CALL]
    assert events[0].text == "hello"
    assert events[2].tool == "Read"
    assert events[2].tool_use_id == "t1"
    assert events[2].data["input"] == {"file": "a"}


def test_claude_result_emits_usage_then_turn_completed():
    events = translate_claude(
        {
            "type": "result",
            "subtype": "success",
            "result": "done",
            "usage": {"input_tokens": 5},
            "total_cost_usd": 0.01,
            "session_id": "s1",
        }
    )
    assert [e.kind for e in events] == [EventKind.USAGE, EventKind.TURN_COMPLETED]
    assert events[1].data["is_error"] is False
    assert events[1].data["harness_session_id"] == "s1"


def test_claude_error_result_leads_with_an_error_event():
    events = translate_claude(
        {"type": "result", "subtype": "error_max_turns", "result": "hit the cap"}
    )
    # Mutation guard: if the adapter ever classifies a non-success result as a
    # clean completion, this flips to [USAGE, TURN_COMPLETED] and a failed turn
    # renders as a successful empty one.
    assert events[0].kind == EventKind.ERROR
    assert any(e.kind == EventKind.TURN_COMPLETED and e.data["is_error"] for e in events)


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "totally_new_event_type", "whatever": 1},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "wat"}]}},
    ],
)
def test_unknown_shapes_are_preserved_as_raw(payload):
    events = translate_claude(payload)
    # Mutation guard: `return []` for unknown types would make this list empty.
    # That is the exact change that turns a protocol drift into silence.
    assert events, "an unclassifiable line must never vanish"
    assert any(e.kind == EventKind.RAW for e in events)


def test_gemini_falls_back_to_raw_rather_than_guessing():
    events = translate_gemini({"type": "some_gemini_only_event", "x": 1})
    assert events and events[0].kind == EventKind.RAW


def test_gemini_reuses_claude_shapes_when_they_match():
    events = translate_gemini(
        {"type": "assistant", "message": {"role": "assistant", "content": "hi"}}
    )
    assert [e.kind for e in events] == [EventKind.TEXT_DELTA]


# ── 2. model binding clears before it overlays ──────────────────────────────

def test_apply_binding_clears_every_managed_var_first():
    stale = {var: "STALE" for var in MANAGED_VARS}
    stale["UNRELATED"] = "keep me"
    binding = ModelBinding(
        profile="p",
        env={"ANTHROPIC_MODEL": "new-model", "ANTHROPIC_BASE_URL": "https://x"},
        expected_model="new-model",
    )
    result = apply_binding(stale, binding)

    assert result["ANTHROPIC_MODEL"] == "new-model"
    assert result["UNRELATED"] == "keep me"
    # Mutation guard: an implementation that only did `env.update(binding.env)`
    # would leave every OTHER managed var at "STALE" — the half-applied switch
    # where the main turn works and subagents silently hit the old provider.
    leftovers = [
        var for var in MANAGED_VARS if var not in binding.env and result.get(var) == "STALE"
    ]
    assert leftovers == [], f"stale provider vars survived the switch: {leftovers}"


def test_stock_binding_is_empty_and_reports_itself_as_stock():
    binding = ModelBinding(profile="anthropic", env={}, expected_model="")
    assert binding.is_stock
    assert binding.claude_setting_sources() == "user,project,local"


def test_bound_binding_drops_the_user_settings_source():
    binding = ModelBinding(
        profile="deepseek-flash",
        env={"ANTHROPIC_MODEL": "deepseek-v4-flash[1m]"},
        expected_model="deepseek-v4-flash[1m]",
    )
    sources = binding.claude_setting_sources()
    # THE load-bearing assertion. `env` in ~/.claude/settings.json overrides
    # process env, so if `user` is ever included here a per-session model
    # silently loses to the global profile and the session runs the wrong model
    # while reporting the right one.
    assert "user" not in sources
    assert sources == "project,local"


def test_binding_redaction_never_echoes_the_credential():
    binding = ModelBinding(
        profile="p",
        env={"ANTHROPIC_AUTH_TOKEN": "sk-super-secret", "ANTHROPIC_MODEL": "m"},
        expected_model="m",
    )
    blob = repr(binding.redacted())
    assert "sk-super-secret" not in blob
    assert binding.redacted()["env"]["ANTHROPIC_AUTH_TOKEN"] == "<set>"


# ── 3. argv construction ────────────────────────────────────────────────────

def test_claude_argv_is_bidirectional_stream_json():
    spec = get("claude")
    argv = spec.argv(LaunchSpec(cwd="/repo"))
    # Bidirectional stream-json is what lets ONE process serve many turns with
    # a warm context. Losing --input-format silently degrades every session to
    # one-shot, which looks like the model forgetting between turns.
    assert "--input-format" in argv and "stream-json" in argv
    assert "--output-format" in argv
    assert "-p" in argv


def test_claude_argv_passes_setting_sources_when_bound():
    spec = get("claude")
    argv = spec.argv(LaunchSpec(cwd="/repo", setting_sources="project,local"))
    assert "--setting-sources" in argv
    assert argv[argv.index("--setting-sources") + 1] == "project,local"


def test_claude_argv_omits_setting_sources_when_unbound():
    spec = get("claude")
    argv = spec.argv(LaunchSpec(cwd="/repo"))
    # An unbound session deliberately wants the machine's own configuration.
    assert "--setting-sources" not in argv


# ── 4. registry honesty ─────────────────────────────────────────────────────

def test_every_spec_declares_a_usable_transport():
    for spec in [get(h["id"]) for h in detect()]:
        assert isinstance(spec.transport, Transport)
        if spec.transport in (Transport.STRUCTURED_BIDI, Transport.ONESHOT_PER_TURN):
            assert spec.build_argv is not None, f"{spec.id} cannot be launched"


def test_detect_reports_install_hints_for_missing_harnesses():
    missing = [h for h in detect() if not h["installed"]]
    for harness in missing:
        # A shell that offers a harness it cannot start, with no way to fix it,
        # is worse than one that says so.
        assert harness.get("install_hint"), f"{harness['id']} is missing without a hint"


def test_no_spec_or_code_path_references_a_removed_transport():
    """Guard against the exact bug this suite failed to catch once already.

    ``Transport.RAW_STREAM`` was renamed to ``PTY_STREAM``; a stale reference
    survived in ``HarnessSession.send`` and every structured turn raised
    AttributeError at runtime — a 500 that no pure-function test could see,
    because the pure functions were all still correct.
    """
    import inspect

    from adk.harnesses import agents, manager, pty_session, session

    valid = {member.name for member in Transport}
    for module in (session, manager, pty_session, agents):
        source = inspect.getsource(module)
        for token in ("RAW_STREAM", "EXEC_STREAM"):
            if token in valid:
                continue
            assert f"Transport.{token}" not in source, (
                f"{module.__name__} references removed Transport.{token}"
            )


def test_send_on_a_structured_session_encodes_rather_than_raising(tmp_path):
    """A structured session must reach the encode path, not an attribute error."""
    from adk.harnesses.session import HarnessSession, SessionConfig

    spec = get("claude")
    sess = HarnessSession(spec, SessionConfig(harness="claude"), None, root=tmp_path)
    # No process is running, so send() must fail CLEANLY with an error event
    # rather than raising — the daemon turns a raise into an opaque HTTP 500.
    assert sess.send("hello") is False
    kinds = [e["kind"] for e in sess.events_since(0)]
    assert "error" in kinds


def test_structured_bidi_harnesses_can_encode_input():
    spec = get("claude")
    assert spec.encode_input is not None
    line = spec.encode_input("hello")
    assert '"type": "user"' in line or '"type":"user"' in line
    assert "\n" not in line, "the transport appends the newline; the encoder must not"


# ── 5. sandbox container resolution never guesses ───────────────────────────

def test_container_name_uses_an_explicit_field_when_present():
    from adk.harnesses.sandbox import container_name

    assert container_name({"container": "aitheros-devws-alex-1234abcd"}) == (
        "aitheros-devws-alex-1234abcd",
        "",
    )


def test_container_name_matches_aithertunnels_real_convention():
    from adk.harnesses.sandbox import container_name

    # AitherTunnel._dev_container_name (AitherTunnel.py:7586):
    #   f"{DEV_WORKSPACE_PREFIX}{slug(email)}-{session_id[:8]}"
    got, reason = container_name(
        {"dev_identity": "Alex.Dev@aitherium.com", "session_id": "abcdefgh12345"}
    )
    assert got == "aitheros-devws-alex-dev-abcdefgh"
    assert reason == ""


def test_container_name_refuses_to_guess_from_workspace_id_alone():
    from adk.harnesses.sandbox import container_name

    # Genesis's WorkspaceDescriptor has NO container field and NO session id.
    # An earlier version invented "aitheros-devws-{workspace_id}" — a name that
    # never exists, so `docker exec` failed as "no such container" and read as
    # a broken terminal rather than an unresolvable descriptor.
    got, reason = container_name(
        {"workspace_id": "ws-123", "browser_ssh_target": "host:2222"}
    )
    assert got == ""
    assert "cannot be derived" in reason
    assert "host:2222" in reason


# ── 6. a one-shot turn can never hang silently ──────────────────────────────

def test_oneshot_turn_that_never_exits_is_killed_and_reported(tmp_path):
    """Measured failure: an UNAUTHENTICATED Gemini CLI hangs with no output.

    Before the turn timeout this emitted `turn.started` and then NOTHING — no
    text, no error, no completion — which a UI cannot tell apart from a model
    still thinking. Mutation guard: remove the timeout branch in
    `_send_oneshot` and this test hangs until pytest is killed.
    """
    import sys as _sys
    import time as _time

    from adk.harnesses.registry import HarnessSpec, Transport
    from adk.harnesses.session import HarnessSession, SessionConfig

    hang = HarnessSpec(
        id="hang-probe",
        label="Hang Probe",
        description="a process that never exits on its own",
        transport=Transport.ONESHOT_PER_TURN,
        binary=_sys.executable,
        adapter="text",
        build_argv=lambda spec, launch: [spec.binary, "-c", "import time; time.sleep(120)"],
    )
    config = SessionConfig(harness="hang-probe", turn_timeout=2.0)
    session = HarnessSession(hang, config, None, root=tmp_path)
    session.start()
    assert session.send("go") is True

    deadline = _time.time() + 45
    while _time.time() < deadline:
        kinds = [e["kind"] for e in session.events_since(0)]
        if "turn.completed" in kinds:
            break
        _time.sleep(0.2)

    events = session.events_since(0)
    kinds = [e["kind"] for e in events]
    assert "turn.completed" in kinds, "a hung one-shot turn must still complete"
    assert "error" in kinds, "a killed turn must say so, not complete silently"
    completed = [e for e in events if e["kind"] == "turn.completed"][-1]
    assert completed["data"]["timed_out"] is True
    assert completed["data"]["is_error"] is True
    timeout_error = [e for e in events if e["kind"] == "error"][-1]
    # The message must name the likely cause; "timed out" alone sends the
    # operator to the wrong layer.
    assert "unauthenticated" in timeout_error["text"].lower()
