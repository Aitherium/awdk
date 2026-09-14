"""Tests for app proxy tool registration."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from adk.app_proxy_tools import (
    _build_parameters_schema,
    _make_proxy_fn,
    register_app_proxy_tools,
)


class TestBuildParametersSchema:
    def test_empty_params(self):
        schema = _build_parameters_schema({})
        assert schema == {"type": "object", "properties": {}}

    def test_required_params(self):
        params = {
            "query": {"type": "string", "required": True},
            "top_k": {"type": "integer", "default": 5},
        }
        schema = _build_parameters_schema(params)
        assert "query" in schema["properties"]
        assert "top_k" in schema["properties"]
        assert schema["required"] == ["query"]
        assert schema["properties"]["top_k"]["default"] == 5

    def test_enum_params(self):
        params = {
            "doc_type": {"type": "string", "enum": ["resume", "brochure"]},
        }
        schema = _build_parameters_schema(params)
        assert schema["properties"]["doc_type"]["enum"] == ["resume", "brochure"]


class TestMakeProxyFn:
    def test_creates_async_function(self):
        tool_def = {
            "name": "test_tool",
            "endpoint": "/api/test",
            "method": "POST",
            "description": "Test tool",
        }
        fn = _make_proxy_fn("http://app:8900", tool_def)
        assert fn.__name__ == "test_tool"
        assert "Test tool" in fn.__doc__
        import asyncio
        assert asyncio.iscoroutinefunction(fn)


class TestRegisterAppProxyTools:
    def test_no_env_returns_zero(self):
        agent = MagicMock()
        agent.name = "test"
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ADK_APP_PROXY_URL", None)
            count = register_app_proxy_tools(agent)
        assert count == 0

    def test_registers_tools_from_manifest_env(self):
        agent = MagicMock()
        agent.name = "test"
        agent._tools = MagicMock()
        agent._tools._tools = {}

        tools = [
            {
                "name": "rag_search",
                "description": "Search docs",
                "endpoint": "/api/tools/rag_search",
                "method": "POST",
                "parameters": {"query": {"type": "string", "required": True}},
            },
            {
                "name": "staff_search",
                "description": "Search staff",
                "endpoint": "/api/tools/staff_search",
                "method": "POST",
                "parameters": {"query": {"type": "string", "required": True}},
            },
        ]

        env = {
            "ADK_APP_PROXY_URL": "http://gargbot:8900",
            "ADK_APP_MANIFEST": json.dumps(tools),
        }

        with patch.dict(os.environ, env, clear=False):
            count = register_app_proxy_tools(agent)

        assert count == 2
        assert "rag_search" in agent._tools._tools
        assert "staff_search" in agent._tools._tools

    def test_skips_tools_without_name(self):
        agent = MagicMock()
        agent.name = "test"
        agent._tools = MagicMock()
        agent._tools._tools = {}

        tools = [{"description": "No name tool"}]
        env = {
            "ADK_APP_PROXY_URL": "http://app:8900",
            "ADK_APP_MANIFEST": json.dumps(tools),
        }

        with patch.dict(os.environ, env, clear=False):
            count = register_app_proxy_tools(agent)

        assert count == 0
