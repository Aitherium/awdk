"""B.3 — agent self-introspection tools tests.

Covers:
  - agent._introspection is a bounded deque (maxlen=200)
  - agent._files_touched tracks read/write/edit ops with first/last ts + op list
  - self_recent_tool_calls returns last N (clamped to [1, 200])
  - self_files_touched serialises the tracked dict
  - self_session_summary aggregates counts + errors
  - self_memory_search degrades cleanly when memory backend is absent
"""

import json
from collections import deque

import pytest

from adk.agent import AitherAgent
from adk.builtin_tools import register_self_tools


@pytest.fixture
def agent():
    """Bare agent — no LLM, no memory, builtins disabled. Just data structures."""
    a = AitherAgent("introspect_test", builtin_tools=False)
    return a


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

def test_introspection_is_bounded_deque(agent):
    assert isinstance(agent._introspection, deque)
    assert agent._introspection.maxlen == 200


def test_introspection_evicts_oldest_past_cap(agent):
    """Maxlen=200 ⇒ inserting 250 keeps only the last 200."""
    for i in range(250):
        agent._introspection.append({"ts": i, "tool": f"t{i}"})
    assert len(agent._introspection) == 200
    # Oldest 50 evicted; first remaining must be index 50.
    assert agent._introspection[0]["ts"] == 50
    assert agent._introspection[-1]["ts"] == 249


def test_files_touched_starts_empty(agent):
    assert agent._files_touched == {}


# ---------------------------------------------------------------------------
# self_* tool outputs
# ---------------------------------------------------------------------------

def _get_tool(agent, name):
    register_self_tools(agent)
    td = agent._tools.get(name)
    assert td is not None, f"tool {name!r} not registered"
    return td.fn


def test_self_recent_tool_calls_empty(agent):
    fn = _get_tool(agent, "self_recent_tool_calls")
    out = json.loads(fn(10))
    assert out["calls"] == []
    assert out["count"] == 0


def test_self_recent_tool_calls_returns_last_n(agent):
    for i in range(15):
        agent._introspection.append({"ts": float(i), "tool": f"t{i}", "error": False})
    fn = _get_tool(agent, "self_recent_tool_calls")
    out = json.loads(fn(5))
    assert out["count"] == 5
    assert [c["tool"] for c in out["calls"]] == ["t10", "t11", "t12", "t13", "t14"]
    assert out["total_recorded"] == 15


def test_self_recent_tool_calls_clamps_n(agent):
    """n is clamped to [1, 200] — defends against the LLM passing nonsense."""
    for i in range(10):
        agent._introspection.append({"ts": i, "tool": "x"})
    fn = _get_tool(agent, "self_recent_tool_calls")
    # Negative clamps to 1
    assert json.loads(fn(-5))["count"] == 1
    # Huge clamps to len(buf) (since min(huge, 200) > 10)
    assert json.loads(fn(99999))["count"] == 10


def test_self_files_touched_serialises(agent):
    agent._files_touched["/tmp/a.py"] = {
        "first_ts": 1.0, "last_ts": 2.0, "ops": ["file_read", "file_edit"]
    }
    fn = _get_tool(agent, "self_files_touched")
    out = json.loads(fn())
    assert out["count"] == 1
    assert "/tmp/a.py" in out["files"]
    assert out["files"]["/tmp/a.py"]["ops"] == ["file_read", "file_edit"]


def test_self_session_summary_counts_tools_and_errors(agent):
    agent._introspection.append({"tool": "file_read",  "error": False})
    agent._introspection.append({"tool": "file_read",  "error": False})
    agent._introspection.append({"tool": "file_write", "error": True})
    agent._files_touched["/x"] = {"first_ts": 0, "last_ts": 0, "ops": ["file_write"]}
    fn = _get_tool(agent, "self_session_summary")
    out = json.loads(fn())
    assert out["tool_calls_total"] == 3
    assert out["tool_calls_by_name"] == {"file_read": 2, "file_write": 1}
    assert out["tool_errors"] == 1
    assert out["files_touched"] == 1
    assert out["agent"] == "introspect_test"


def test_self_memory_search_no_backend(agent):
    """Agent has no memory ⇒ returns empty results, not an exception."""
    agent.memory = None
    fn = _get_tool(agent, "self_memory_search")
    out = json.loads(fn("anything"))
    assert out["results"] == []
    assert "note" in out


def test_register_self_tools_count(agent):
    """register_self_tools advertises exactly 4 tools (contract guard)."""
    n = register_self_tools(agent)
    assert n == 4
    for name in (
        "self_recent_tool_calls",
        "self_files_touched",
        "self_session_summary",
        "self_memory_search",
    ):
        assert agent._tools.get(name) is not None
