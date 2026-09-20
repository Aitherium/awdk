"""A session's row names the SESSION and its work, not the checkout.

Owner, 2026-09-19: the room said "AitherOS-Fresh says:" and /sessions/unified
listed ten rows all titled "AitherOS-Fresh", because both read Path(cwd).name.
"""
from __future__ import annotations

import json

import pytest

from adk.harnesses.session_directory import (
    _PROMPT_CACHE,
    _last_human_prompt,
    session_display_title,
)
from adk.harnesses.transcript_bridge import session_topic


def _write(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(text):
    return {"type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def test_name_distinguishes_sessions_in_one_checkout():
    a = session_display_title("6313fc71-06fc-4c44", r"C:\AitherOS-Fresh")
    b = session_display_title("77db6255-4db2-4b40", r"C:\AitherOS-Fresh")
    assert a != b, "two tabs of one checkout must not share a name"
    assert a.startswith("AitherOS-Fresh#")


def test_topic_comes_from_the_last_human_prompt(tmp_path):
    t = tmp_path / "s.jsonl"
    _write(t, [_user("fix the door plane"), _assistant("ok"),
               _user("now rebuild the gateway image"), _assistant("done")])
    assert session_display_title("abcd1234", r"C:\AitherOS-Fresh", str(t)) == \
        "AitherOS-Fresh#abcd1234 - now rebuild the gateway image"


@pytest.mark.parametrize("prompt", [
    "<system-reminder>machine</system-reminder>",
    "[SYSTEM] something",
    "Stop hook feedback: Self-heal loop [1/4]",
    "continue",
    "go",
    "status",
])
def test_machine_turns_and_filler_never_become_the_topic(tmp_path, prompt):
    t = tmp_path / "s.jsonl"
    _write(t, [_user("build the relay door"), _assistant("ok"), _user(prompt)])
    title = session_display_title("abcd1234", r"C:\AitherOS-Fresh", str(t))
    assert title == "AitherOS-Fresh#abcd1234 - build the relay door", title


def test_pasted_content_is_unwrapped_not_rejected(tmp_path):
    # 3 of 4 live sessions lost their topic to this: the owner's words follow
    # the block, and rejecting every "<" prompt threw the whole turn away.
    t = tmp_path / "s.jsonl"
    _write(t, [_user('<pasted_content id="1">a path</pasted_content>\n\nfix the avatar overlap')])
    assert session_display_title("abcd1234", r"C:\AitherOS-Fresh", str(t)).endswith(
        "- fix the avatar overlap")


def test_a_tool_result_is_not_a_human_prompt(tmp_path):
    # Tool RESULTS are user entries too; the discriminator is the content
    # SHAPE (a plain string), never the role.
    t = tmp_path / "s.jsonl"
    _write(t, [_user("deploy the gateway"),
               {"type": "user", "message": {"role": "user", "content": [
                   {"type": "tool_result", "tool_use_id": "x", "content": "rebuild the world"}]}}])
    assert session_display_title("abcd1234", r"C:\AitherOS-Fresh", str(t)).endswith(
        "- deploy the gateway")


def test_no_topic_degrades_to_the_bare_name(tmp_path):
    t = tmp_path / "s.jsonl"
    _write(t, [_assistant("thinking")])
    assert session_display_title("abcd1234", r"C:\AitherOS-Fresh", str(t)) == \
        "AitherOS-Fresh#abcd1234"


def test_an_unchanged_transcript_is_not_re_read(tmp_path):
    # The directory re-derives every session on a 2 s TTL; a deep scan per poll
    # would be tens of MB/s of disk for a string that changes once a turn. The
    # first version of this cache was INERT (warm measured 1x cold) because an
    # unchanged SIZE fell through to the deep bound.
    #
    # The proof is a same-SIZE content swap: if the answer changed, the file was
    # read again. (A deleted transcript is a different case on purpose -- the
    # session is gone and the reader says so rather than serving a memory.)
    t = tmp_path / "s.jsonl"
    _write(t, [_user("carry the door gate")])
    _PROMPT_CACHE.pop(str(t), None)
    assert _last_human_prompt(str(t), session_topic) == "carry the door gate"
    before = t.stat().st_size
    _write(t, [_user("CARRY THE DOOR GATF")])  # same byte count, different text
    assert t.stat().st_size == before, "the swap must not change the size"
    assert _last_human_prompt(str(t), session_topic) == "carry the door gate",         "an unchanged size re-read the file: the cache is inert"


def test_appended_turns_update_the_topic(tmp_path):
    t = tmp_path / "s.jsonl"
    _write(t, [_user("first task")])
    _PROMPT_CACHE.pop(str(t), None)
    assert session_display_title("abcd1234", "/x/repo", str(t)).endswith("- first task")
    with open(t, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_assistant("working")) + "\n")
        fh.write(json.dumps(_user("second task")) + "\n")
    assert session_display_title("abcd1234", "/x/repo", str(t)).endswith("- second task")


def test_claude_own_name_wins_over_the_derived_one(tmp_path):
    # Claude Code writes `<repo> <branch> <HH:MM>` itself: it carries the BRANCH
    # (peers work on their own) and a start time the owner can match to a tab,
    # so nothing derived here should override it.
    t = tmp_path / "s.jsonl"
    _write(t, [_user("carry the door gate")])
    _PROMPT_CACHE.pop(str(t), None)
    title = session_display_title("abcd1234", r"C:\AitherOS-Fresh", str(t),
                                  claude_name="AitherOS-Fresh develop 19:16")
    assert title == "AitherOS-Fresh develop 19:16 - carry the door gate"


def test_a_session_waiting_on_a_human_says_so(tmp_path):
    t = tmp_path / "s.jsonl"
    _write(t, [_user("deploy the gateway")])
    _PROMPT_CACHE.pop(str(t), None)
    waiting = session_display_title("abcd1234", "/x/repo", str(t), status="waiting-input")
    assert " - waiting for you - deploy the gateway" in waiting, waiting
    blocked = session_display_title("abcd1234", "/x/repo", str(t), status="blocked?")
    assert " - maybe blocked - " in blocked, blocked


def test_ordinary_statuses_add_no_noise(tmp_path):
    # "working" and "idle" are the ordinary states; saying them on every row
    # would drown the two that mean a human is needed.
    t = tmp_path / "s.jsonl"
    _write(t, [_user("deploy the gateway")])
    _PROMPT_CACHE.pop(str(t), None)
    for status in ("working", "idle", "exited", ""):
        title = session_display_title("abcd1234", "/x/repo", str(t), status=status)
        assert title.endswith("- deploy the gateway"), title
        assert "waiting for you" not in title


def test_the_room_actor_uses_the_same_name_as_the_pane():
    # Two surfaces naming one session differently is how "which tab is that?"
    # stays unanswerable. Both must resolve to Claude Code's own name.
    from adk.harnesses.transcript_bridge import events_from_entry

    claude_name = "AitherOS-Fresh develop 21:54"
    events = events_from_entry(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        "6313fc71", r"C:\AitherOS-Fresh", "rebuild the gateway", claude_name)
    assert events and events[0]["actor"]["name"] == claude_name
    assert events[0]["actor"]["title"] == "rebuild the gateway"
    assert session_display_title("6313fc71", r"C:\AitherOS-Fresh",
                                 claude_name=claude_name).startswith(claude_name)


def test_the_actor_falls_back_when_claude_names_nothing():
    from adk.harnesses.transcript_bridge import events_from_entry

    events = events_from_entry(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        "6313fc71", r"C:\AitherOS-Fresh")
    assert events[0]["actor"]["name"] == "AitherOS-Fresh#6313fc71"


def test_a_rediscovered_session_already_has_its_topic(tmp_path):
    # Learning the topic only from a prompt seen AFTER start meant every
    # session published untitled lines until its owner typed again -- which on
    # a long-running tab is never (measured 2026-09-19: ten live actors in the
    # room, every title empty after a daemon restart).
    from adk.harnesses.transcript_bridge import TranscriptBridge

    t = tmp_path / "s.jsonl"
    _write(t, [_user("carry the door gate"), _assistant("ok")])
    _PROMPT_CACHE.pop(str(t), None)

    class _Sess:
        id = "abcd1234"
        cwd = r"C:\AitherOS-Fresh"
        name = "AitherOS-Fresh develop 19:16"
        transcript_path = str(t)

    bridge = TranscriptBridge(discover_fn=lambda: [_Sess()])
    bridge.refresh_sessions()
    assert bridge._topics["abcd1234"] == "carry the door gate"
    assert bridge._names["abcd1234"] == "AitherOS-Fresh develop 19:16"


def test_the_room_keeps_what_the_session_is_working_on(tmp_path):
    # The room rebuilt every actor from three literal keys, so the bridge's
    # `title` was dropped in normalisation and every stored event served
    # `title: None` -- producer and consumer both looking correct.
    from adk.harnesses.rooms import _normalise_actor

    kept = _normalise_actor("claude_code", "6313fc71",
                            {"name": "AitherOS-Fresh develop 21:54", "title": "rebuild the gateway"})
    assert kept == {"kind": "claude_code", "id": "6313fc71",
                    "name": "AitherOS-Fresh develop 21:54", "title": "rebuild the gateway"}
    bare = _normalise_actor("claude_code", "6313fc71", {"name": "x"})
    assert "title" not in bare, "an absent title must leave the stored shape unchanged"
    assert "title" not in _normalise_actor("claude_code", "x", {"name": "x", "title": "   "})
    assert len(_normalise_actor("claude_code", "x", {"title": "t" * 500})["title"]) == 120
