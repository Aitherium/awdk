"""Tests for `/agent <name> [--expedition <id>] <task>` dispatch (abb6d539b8).

Two behaviours the fix introduced, neither previously covered by a test:
  * the dispatch payload keys the agent on ``agent_type`` (the old ``agent`` key
    was silently dropped by pydantic → every named dispatch ran auto-routing);
  * ``--expedition <id>`` is parsed out of the task args and mapped to
    ``context.expedition_id`` so the run charges an expedition budget envelope.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from adk.shell.plugins.builtins import agents as agents_mod
from adk.shell.plugins.builtins.agents import AgentPlugin


def _mock_httpx_capture(captured: dict):
    """Return a patched httpx.AsyncClient whose .post records the JSON payload
    and returns a fake 200 so _dispatch takes its success path."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"result": "ok", "agent": "atlas"})
    resp.text = "ok"

    async def _post(url, json=None, headers=None, **kw):
        captured["url"] = url
        captured["payload"] = json
        return resp

    client = MagicMock()
    client.post = AsyncMock(side_effect=_post)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=client)


@pytest.fixture
def plugin():
    return AgentPlugin()


@pytest.fixture(autouse=True)
def _no_network():
    with patch.object(agents_mod, "_genesis_url", return_value="https://genesis.test"), \
         patch.object(agents_mod, "_api_headers", return_value={}), \
         patch.object(agents_mod, "tls_verify", return_value=False):
        yield


@pytest.mark.asyncio
async def test_named_dispatch_uses_agent_type_not_agent(plugin):
    captured = {}
    with patch("httpx.AsyncClient", _mock_httpx_capture(captured)):
        await plugin.run(["atlas", "find", "all", "infra", "services"], {})
    payload = captured["payload"]
    assert payload["agent_type"] == "atlas", "named agent must ride agent_type, not the dropped 'agent' key"
    assert "agent" not in payload
    assert payload["task"] == "find all infra services"


@pytest.mark.asyncio
async def test_expedition_flag_maps_to_context(plugin):
    captured = {}
    with patch("httpx.AsyncClient", _mock_httpx_capture(captured)):
        await plugin.run(["atlas", "--expedition", "exp-42", "audit", "the", "tunnel"], {})
    payload = captured["payload"]
    assert payload["agent_type"] == "atlas"
    assert payload["task"] == "audit the tunnel", "the --expedition <id> pair must be stripped from the task"
    assert payload["context"] == {"expedition_id": "exp-42"}


@pytest.mark.asyncio
async def test_no_expedition_means_no_context_key(plugin):
    captured = {}
    with patch("httpx.AsyncClient", _mock_httpx_capture(captured)):
        await plugin.run(["atlas", "audit", "the", "tunnel"], {})
    assert "context" not in captured["payload"]


@pytest.mark.asyncio
async def test_expedition_without_id_is_a_usage_error_not_a_dispatch(plugin):
    captured = {}
    with patch("httpx.AsyncClient", _mock_httpx_capture(captured)):
        out = await plugin.run(["atlas", "--expedition"], {})
    assert "Usage:" in out
    assert captured == {}, "a malformed --expedition must not dispatch anything"
