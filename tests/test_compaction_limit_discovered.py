"""The compaction window follows the endpoint, not the model-name table.

Measured 2026-09-22: the name table pins bonsai at 16,384, so when llama-server was
relaunched with ``-c 131072 --kv-unified`` the agent still compacted at 16k and the
larger window was never used. The agent now records the window the endpoint advertised
(llama.cpp /props, vLLM max_model_len) and budgets against it, below an explicit
``ADK_CONTEXT_LIMIT`` and below a size a provider stated in a refusal.
"""

from adk.agent import AitherAgent


def _agent(**attrs):
    agent = object.__new__(AitherAgent)
    for key, value in attrs.items():
        setattr(agent, key, value)
    return agent


def test_discovered_window_is_used(monkeypatch):
    monkeypatch.delenv("ADK_CONTEXT_LIMIT", raising=False)
    assert _agent(_context_limit_discovered=131072)._compaction_limit() == 131072


def test_refusal_beats_discovery(monkeypatch):
    monkeypatch.delenv("ADK_CONTEXT_LIMIT", raising=False)
    agent = _agent(_context_limit_discovered=131072, _context_limit_observed=16384)
    assert agent._compaction_limit() == 16384


def test_explicit_env_wins(monkeypatch):
    # None hands the decision to context_limit_for, which reads the env override.
    monkeypatch.setenv("ADK_CONTEXT_LIMIT", "65536")
    agent = _agent(_context_limit_discovered=131072, _context_limit_observed=16384)
    assert agent._compaction_limit() is None


def test_nothing_known_falls_to_the_name_table(monkeypatch):
    monkeypatch.delenv("ADK_CONTEXT_LIMIT", raising=False)
    assert _agent()._compaction_limit() is None
