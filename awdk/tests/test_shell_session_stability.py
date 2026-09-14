"""Session stability — the TUI must keep ONE session_id per shell run.

Pins the 2026-07-02 live failure: _run_generation minted a fresh uuid4 per
MESSAGE when config.session_id was unset, so every turn was a brand-new
server-side session and conversation history never followed
([session_context] history_chars=0 on follow-ups).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from adk.shell.repl import AitherREPL


def _repl_with_capture(captured):
    repl = AitherREPL.__new__(AitherREPL)
    config = MagicMock()
    config.session_id = None
    config.last_session_id = None
    config.persona = "aither"
    config.effort = 0
    config.model = ""
    config.max_tokens = 0
    config.safety_level = "standard"
    repl.config = config
    repl._generating = False
    repl._active_session = None
    repl._thinking_active = False
    repl._tokens_displayed = False

    async def _fake_stream(**kwargs):
        captured.append(kwargs.get("session_id"))
        return
        yield  # pragma: no cover — make it an async generator

    client = MagicMock()
    client.chat_stream = _fake_stream
    repl.genesis_client = client
    repl._on_event = AsyncMock()
    # Attributes touched after the stream loop; keep permissive.
    repl._render_final = AsyncMock()
    return repl


@pytest.mark.asyncio
async def test_session_id_stable_across_turns():
    captured = []
    repl = _repl_with_capture(captured)
    try:
        await repl._run_generation("what time is it?")
    except AttributeError:
        pass  # post-stream rendering internals aren't wired in this harness
    try:
        await repl._run_generation("how do you know that?")
    except AttributeError:
        pass
    assert len(captured) == 2
    assert captured[0] is not None
    assert captured[0] == captured[1], (
        "session_id changed between turns — per-message uuid4 regression")
    # And the stable id is recorded for cross-restart auto-restore.
    assert repl.config.session_id == captured[0]
    assert repl.config.last_session_id == captured[0]


@pytest.mark.asyncio
async def test_explicit_session_id_respected():
    captured = []
    repl = _repl_with_capture(captured)
    repl.config.session_id = "resumed-session-123"
    try:
        await repl._run_generation("continue where we left off")
    except AttributeError:
        pass
    assert captured == ["resumed-session-123"]
