"""POST /chat is the chat ENDPOINT, not the page that shares its path.

Regression (2026-11-08): with AITHER_SERVER_API_KEY set and local auth not
required, a POST /chat through a public tunnel (Cf-Connecting-Ip present)
answered 200 with no bearer, while POST /stream answered 401. The skip list
names PAGES; only safe methods may pass on it.
"""

import os
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


def _app(api_key: str):
    env = {"AITHER_SERVER_API_KEY": api_key, "AITHER_LOCAL_AUTH": "off"}
    with patch.dict(os.environ, env, clear=False):
        from adk.config import Config
        from adk.server import create_app

        config = Config()
        config.gateway_url = ""
        config.aither_api_key = ""
        agent = MagicMock()
        agent.name = "test"
        agent.llm = MagicMock()
        agent.llm.provider_name = "test"
        agent._identity = MagicMock()
        agent._identity.name = "test"
        agent._identity.description = "Test"
        agent._identity.skills = []
        agent._tools = MagicMock()
        agent._tools.list_tools = MagicMock(return_value=[])
        agent._safety = None
        return create_app(agent=agent, identity="test", config=config)


_PROXIED = {"Cf-Connecting-Ip": "203.0.113.9", "X-Forwarded-For": "203.0.113.9"}


def test_proxied_post_chat_without_bearer_is_refused():
    client = TestClient(_app("k" * 32))
    resp = client.post("/chat", json={"message": "hi"}, headers=_PROXIED)
    assert resp.status_code == 401


def test_proxied_get_chat_page_stays_open():
    client = TestClient(_app("k" * 32))
    resp = client.get("/chat", headers=_PROXIED)
    assert resp.status_code != 401


def test_proxied_post_chat_with_bearer_passes_auth():
    key = "k" * 32
    client = TestClient(_app(key))
    # An empty message is rejected by the handler (400) before the agent runs,
    # which proves the request got PAST the auth middleware.
    resp = client.post("/chat", json={"message": ""},
                       headers={**_PROXIED, "Authorization": f"Bearer {key}"})
    assert resp.status_code == 400
