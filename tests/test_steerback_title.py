"""Both continuation titles must say WHICH task continues, and fit a tab.

The owner's screenshot showed a row of identical "resumed: credential stored"
chips: every revived waiter got the same title, so the strip could not be used
to pick one. These arms pin the two titles ``resume_waiter`` sends, without a
daemon: ``_daemon_post`` is replaced and only its call bodies are inspected.
"""

from __future__ import annotations

import pytest

from adk.decisions import steerback


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Record every daemon POST; answer a create with a fresh session id."""
    calls: list[tuple[str, dict]] = []

    def fake_post(path: str, body: dict) -> tuple[bool, dict]:
        calls.append((path, dict(body)))
        if path == "/sessions":
            return True, {"id": "new-session-1"}
        return True, {}

    monkeypatch.setattr(steerback, "_daemon_post", fake_post)
    return calls


def _titles(calls: list[tuple[str, dict]]) -> list[str]:
    return [body.get("title", "") for path, body in calls if path == "/sessions"]


def test_resume_title_names_the_task_and_fits_a_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture(monkeypatch)
    ok, why = steerback.resume_waiter(
        {
            "harness": "claude",
            "cwd": "C:\\repo",
            "session_id": "sess-abcdef123456",
            "task": "Fix Solana address",
        },
        "the credential is stored",
    )
    assert ok, why
    title = _titles(calls)[0]
    assert "credential stored" in title
    assert "Fix Solana address" in title
    assert len(title) <= steerback.TITLE_MAX


def test_spawn_title_names_the_task_when_there_is_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture(monkeypatch)
    ok, why = steerback.resume_waiter(
        {"harness": "claude", "cwd": "C:\\repo", "session_id": "",
         "task": "Rotate the staging API key"},
        "the credential is stored",
    )
    assert ok, why
    title = _titles(calls)[0]
    assert "Rotate the staging API key" in title
    assert not title.startswith("resumed:")
    assert len(title) <= steerback.TITLE_MAX


def test_titles_are_unique_per_waiter(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture(monkeypatch)
    steerback.resume_waiter({"session_id": "sess-1", "task": "task alpha"}, "go")
    steerback.resume_waiter({"session_id": "sess-2", "task": "task beta"}, "go")
    titles = _titles(calls)
    assert len(titles) == 2
    assert len(set(titles)) == 2
    assert all(len(title) <= steerback.TITLE_MAX for title in titles)


def test_a_long_task_is_clamped_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture(monkeypatch)
    ok, _why = steerback.resume_waiter(
        {"session_id": "sess-1", "task": "a" * 200},
        "go",
    )
    assert ok
    title = _titles(calls)[0]
    assert len(title) <= steerback.TITLE_MAX
    assert title.endswith("…")
    assert title.startswith("resumed: credential stored — continue ")
