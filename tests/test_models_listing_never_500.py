"""GET /v1/models never answers 500.

It is the ONE route the page's node probe gates on (use-local-node.ts). Measured
2026-10-04 with the fleet quiesced: list_models raised an upstream error type outside
the handler's except tuple, the route answered 500, and a paired, running daemon read
as "no node". An empty listing is the honest answer.
"""

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from adk.agent import AitherAgent
from adk.server import create_app


class UpstreamError(Exception):
    """Not RuntimeError/OSError/ConnectionError -- what the provider actually raised."""


def test_a_provider_error_of_any_type_yields_an_empty_listing(monkeypatch):
    for k in ("AITHER_OFFLINE", "AITHER_SERVER_API_KEY", "AITHER_LOCAL_AUTH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AITHER_LOCAL_AUTH", "off")
    agent = MagicMock(spec=AitherAgent)
    agent.name = "a"
    agent.llm = MagicMock()
    agent.llm.provider_name = "mock"
    agent.llm.get_provider = MagicMock(return_value=None)
    agent.llm.list_models = AsyncMock(side_effect=UpstreamError("backend unreachable"))
    c = TestClient(create_app(agent=agent), client=("127.0.0.1", 50125),
                   raise_server_exceptions=False)
    r = c.get("/v1/models")
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []
