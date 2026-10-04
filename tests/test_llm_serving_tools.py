"""Tests for llm_serving tools: the registration payload (mocked, no network)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from adk.toolpacks.llm_serving import tools


def _ok():
    m = MagicMock(status_code=200, text="")
    m.json = lambda: {"ok": True}
    return m


class TestLlmRegisterBackend:
    @patch("adk.toolpacks.llm_serving.tools.httpx.post")
    def test_payload_carries_stable_name(self, mock_post):
        mock_post.return_value = _ok()
        kw = dict(
            model="nemotron-orchestrator-8b",
            base_url="http://gpu-box:8000",
            genesis_url="https://cp.example",
            token="t",
        )
        r1 = tools.llm_register_backend(**kw)
        payload1 = mock_post.call_args.kwargs["json"]
        tools.llm_register_backend(**kw)
        payload2 = mock_post.call_args.kwargs["json"]

        assert r1["registered"] is True
        assert isinstance(payload1["name"], str) and payload1["name"]
        assert payload1["name"] == payload2["name"] == r1["name"]
        served = r1["served_name"]
        assert payload1["name"] == f"{served}@gpu-box-8000"
        # the other fields are kept
        assert payload1["base_url"] == "http://gpu-box:8000"
        assert payload1["backend_type"] == "vllm"
        assert payload1["models"] == [served]
        assert "category" in payload1

    @patch("adk.toolpacks.llm_serving.tools.httpx.post")
    def test_unknown_model_still_named(self, mock_post):
        mock_post.return_value = _ok()
        tools.llm_register_backend(
            model="something-unlisted", base_url="http://h:9000",
            genesis_url="https://cp.example", token="t",
        )
        assert mock_post.call_args.kwargs["json"]["name"] == "something-unlisted@h-9000"

    @patch("adk.toolpacks.llm_serving.tools.httpx.post")
    def test_fail_closed_without_token(self, mock_post, monkeypatch):
        monkeypatch.delenv("AITHER_AUTH_TOKEN", raising=False)
        r = tools.llm_register_backend(
            model="gemma4-12b", base_url="http://h:9000", genesis_url="https://cp.example"
        )
        assert "error" in r
        mock_post.assert_not_called()
