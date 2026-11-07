"""awsh marks its turns for MicroScheduler; cloud APIs never see the field.

2026-10-30: the awsh daemon's calls carried no priority, so MicroScheduler treated the
owner's chat as AGENT background work and it waited 174-241 s behind agents.
"""
from __future__ import annotations

from adk.llm.openai_compat import _scheduler_metadata

SCHED = "https://127.0.0.1:8150/v1"


def test_env_metadata_reaches_the_scheduler(monkeypatch):
    monkeypatch.setenv("ADK_SCHEDULER_METADATA", '{"priority": "user", "source": "desk:awsh"}')
    assert _scheduler_metadata(SCHED) == {"priority": "user", "source": "desk:awsh"}
    assert _scheduler_metadata("https://aitheros-microscheduler:8150/v1")


def test_cloud_apis_never_get_metadata(monkeypatch):
    monkeypatch.setenv("ADK_SCHEDULER_METADATA", '{"priority": "user"}')
    assert _scheduler_metadata("https://api.openai.com/v1") is None
    assert _scheduler_metadata("https://api.deepseek.com/v1", {"priority": "user"}) is None


def test_caller_metadata_wins_and_bad_env_is_ignored(monkeypatch):
    monkeypatch.setenv("ADK_SCHEDULER_METADATA", '{"priority": "user", "source": "desk:awsh"}')
    assert _scheduler_metadata(SCHED, {"priority": "agent"})["priority"] == "agent"
    monkeypatch.setenv("ADK_SCHEDULER_METADATA", "not json")
    assert _scheduler_metadata(SCHED) is None
    monkeypatch.setenv("ADK_SCHEDULER_METADATA", '["a list"]')
    assert _scheduler_metadata(SCHED) is None


def test_off_without_env_or_kwarg(monkeypatch):
    monkeypatch.delenv("ADK_SCHEDULER_METADATA", raising=False)
    assert _scheduler_metadata(SCHED) is None
