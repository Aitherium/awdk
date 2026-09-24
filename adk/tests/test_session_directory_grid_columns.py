"""Cockpit grid columns: branch and token spend on every unified session row.

The awsh cockpit rendered name/cwd/origin/status/age/summary only -- no branch
(peers each work on their own) and no token spend. Both come from the
transcript Claude Code already writes: `gitBranch` on every entry and a
`usage` block on every assistant message.
"""

from __future__ import annotations

import json

from adk.harnesses import session_directory as sd
from adk.harnesses.discovery import DiscoveredSession


def _write(path, entries):
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def _assistant(mid, inp, out, cache_create=0, cache_read=0, branch="feat/x"):
    return {
        "type": "assistant",
        "gitBranch": branch,
        "timestamp": "2026-09-24T10:00:00Z",
        "message": {
            "id": mid,
            "role": "assistant",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_creation_input_tokens": cache_create,
                "cache_read_input_tokens": cache_read,
            },
        },
    }


def test_usage_counts_each_message_once_and_excludes_cache_reads(tmp_path):
    t = tmp_path / "s.jsonl"
    # msg_1 spans two content blocks -> two lines carrying the same usage.
    _write(t, [
        _assistant("msg_1", 10, 5, cache_create=100, cache_read=99999),
        _assistant("msg_1", 10, 5, cache_create=100, cache_read=99999),
        _assistant("msg_2", 1, 2, branch="keystone/w4"),
    ])
    sd._USAGE_CACHE.clear()
    branch, tokens = sd._transcript_usage(str(t))
    assert branch == "keystone/w4"
    assert tokens == (10 + 5 + 100) + (1 + 2)


def test_usage_is_incremental_on_append(tmp_path):
    t = tmp_path / "s.jsonl"
    _write(t, [_assistant("m1", 10, 10)])
    sd._USAGE_CACHE.clear()
    assert sd._transcript_usage(str(t))[1] == 20
    with open(t, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_assistant("m2", 5, 5, branch="develop")) + "\n")
    assert sd._transcript_usage(str(t)) == ("develop", 30)


def test_unified_rows_carry_branch_and_tokens(tmp_path):
    t = tmp_path / "abc.jsonl"
    _write(t, [_assistant("m1", 7, 3, branch="feat/cockpit")])
    sd._USAGE_CACHE.clear()
    disc = DiscoveredSession(
        id="abc", cwd=str(tmp_path), name="repo feat 10:00", pid=1,
        entrypoint="cli", kind="interactive", status="", transcript_path=str(t),
    )
    directory = sd.SessionDirectory(discover_fn=lambda: [disc])
    rows = directory.list_sessions_sync([
        {"id": "d1", "transcript": str(t), "cwd": str(tmp_path), "title": "d", "state": "ready"},
    ])
    assert rows and all(r.branch == "feat/cockpit" for r in rows)
    assert all(r.tokens_spent == 10 for r in rows)
