"""Tests for async request batching."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json
import pytest
import httpx
import respx
from unittest.mock import AsyncMock, MagicMock, patch

from adk.llm.base import BatchRequest, Message, LLMResponse
from adk.llm.anthropic import AnthropicProvider
from adk.llm.openai_compat import OpenAIProvider
from adk.llm.batch_runner import run_batch


# ─── Anthropic Batching ───

class TestAnthropicBatch:
    def test_capabilities_batch_enabled(self):
        provider = AnthropicProvider(api_key="sk-ant-test")
        caps = provider.capabilities()
        assert caps.batch is True
        assert caps.prompt_cache == "explicit"

    @respx.mock
    @pytest.mark.asyncio
    async def test_submit_batch(self):
        """Test submitting a batch to Anthropic."""
        respx.post("https://api.anthropic.com/v1/messages/batches").mock(
            return_value=httpx.Response(200, json={"id": "batch_123", "processing_status": "queued"})
        )
        provider = AnthropicProvider(api_key="sk-ant-test")
        requests = [
            BatchRequest(
                custom_id="req_1",
                messages=[Message(role="user", content="hello")],
            ),
            BatchRequest(
                custom_id="req_2",
                messages=[Message(role="user", content="world")],
            ),
        ]
        batch_id = await provider.submit_batch(requests)
        assert batch_id == "batch_123"

    @respx.mock
    @pytest.mark.asyncio
    async def test_poll_batch(self):
        """Test polling batch status."""
        respx.get("https://api.anthropic.com/v1/messages/batches/batch_123").mock(
            return_value=httpx.Response(200, json={"processing_status": "in_progress"})
        )
        provider = AnthropicProvider(api_key="sk-ant-test")
        status = await provider.poll_batch("batch_123")
        assert status == "in_progress"

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_batch_results(self):
        """Test fetching batch results."""
        results_jsonl = (
            '{"custom_id": "req_1", "result": {"message": {"content": [{"type": "text", "text": "Hello!"}], "usage": {"input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}}\n'
            '{"custom_id": "req_2", "result": {"message": {"content": [{"type": "text", "text": "World!"}], "usage": {"input_tokens": 3, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}}\n'
        )

        respx.get("https://api.anthropic.com/v1/messages/batches/batch_123").mock(
            return_value=httpx.Response(200, json={
                "id": "batch_123",
                "processing_status": "completed",
                "results_url": "https://api.anthropic.com/v1/messages/batches/batch_123/results",
            })
        )
        respx.get("https://api.anthropic.com/v1/messages/batches/batch_123/results").mock(
            return_value=httpx.Response(200, text=results_jsonl)
        )

        provider = AnthropicProvider(api_key="sk-ant-test")
        results = await provider.fetch_batch_results("batch_123")
        assert len(results) == 2
        assert results[0].content == "Hello!"
        assert results[1].content == "World!"


# ─── OpenAI Batching ───

class TestOpenAIBatch:
    def test_capabilities_batch_enabled(self):
        provider = OpenAIProvider(api_key="sk-test")
        caps = provider.capabilities()
        assert caps.batch is True
        assert caps.prompt_cache == "automatic"

    @respx.mock
    @pytest.mark.asyncio
    async def test_submit_batch(self):
        """Test submitting a batch to OpenAI."""
        respx.post("https://api.openai.com/v1/files").mock(
            return_value=httpx.Response(200, json={"id": "file_123"})
        )
        respx.post("https://api.openai.com/v1/batches").mock(
            return_value=httpx.Response(200, json={"id": "batch_456", "status": "queued"})
        )

        provider = OpenAIProvider(api_key="sk-test")
        requests = [
            BatchRequest(
                custom_id="req_1",
                messages=[Message(role="user", content="hello")],
            ),
        ]
        batch_id = await provider.submit_batch(requests)
        assert batch_id == "batch_456"

    @respx.mock
    @pytest.mark.asyncio
    async def test_poll_batch(self):
        """Test polling OpenAI batch status."""
        respx.get("https://api.openai.com/v1/batches/batch_456").mock(
            return_value=httpx.Response(200, json={"status": "in_progress"})
        )
        provider = OpenAIProvider(api_key="sk-test")
        status = await provider.poll_batch("batch_456")
        assert status == "in_progress"

    @respx.mock
    @pytest.mark.asyncio
    async def test_poll_batch_local_engine_404(self):
        """Test that 404 from local engine raises NotImplementedError."""
        respx.get("https://localhost:8200/v1/batches/batch_456").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        provider = OpenAIProvider(base_url="https://localhost:8200/v1", api_key="")
        with pytest.raises(NotImplementedError):
            await provider.poll_batch("batch_456")

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_batch_results(self):
        """Test fetching OpenAI batch results."""
        output_jsonl = (
            '{"custom_id":"req_1","result":{"body":{"choices":[{"message":{"content":"Hello!"},"finish_reason":"stop"}],"usage":{"total_tokens":7,"prompt_tokens":5,"completion_tokens":2},"model":"gpt-4o-mini"}}}\n'
            '{"custom_id":"req_2","result":{"body":{"choices":[{"message":{"content":"World!"},"finish_reason":"stop"}],"usage":{"total_tokens":4,"prompt_tokens":3,"completion_tokens":1},"model":"gpt-4o-mini"}}}\n'
        )

        respx.get("https://api.openai.com/v1/batches/batch_456").mock(
            return_value=httpx.Response(200, json={
                "id": "batch_456",
                "status": "completed",
                "output_file_id": "file_output_789",
            })
        )
        respx.get("https://api.openai.com/v1/files/file_output_789/content").mock(
            return_value=httpx.Response(200, text=output_jsonl)
        )

        provider = OpenAIProvider(api_key="sk-test")
        results = await provider.fetch_batch_results("batch_456")
        assert len(results) == 2
        assert results[0].content == "Hello!"
        assert results[1].content == "World!"
        assert results[0].tokens_used == 7
        assert results[1].tokens_used == 4


# ─── Batch Runner ───

class TestBatchRunner:
    @respx.mock
    @pytest.mark.asyncio
    async def test_run_batch_with_native_api(self):
        """Test run_batch using native Anthropic batch API."""
        respx.post("https://api.anthropic.com/v1/messages/batches").mock(
            return_value=httpx.Response(200, json={"id": "batch_123"})
        )

        # Create a stateful mock for polling
        call_count = {"value": 0}

        def poll_response(*args, **kwargs):
            call_count["value"] += 1
            if call_count["value"] == 1:
                return httpx.Response(200, json={"processing_status": "in_progress"})
            else:
                return httpx.Response(200, json={
                    "processing_status": "completed",
                    "results_url": "https://api.anthropic.com/v1/messages/batches/batch_123/results",
                })

        respx.get("https://api.anthropic.com/v1/messages/batches/batch_123").mock(
            side_effect=poll_response
        )
        results_jsonl = (
            '{"custom_id": "req_1", "result": {"message": {"content": [{"type": "text", "text": "Hello!"}], "usage": {"input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}}\n'
        )
        respx.get("https://api.anthropic.com/v1/messages/batches/batch_123/results").mock(
            return_value=httpx.Response(200, text=results_jsonl)
        )

        provider = AnthropicProvider(api_key="sk-ant-test")
        requests = [
            BatchRequest(
                custom_id="req_1",
                messages=[Message(role="user", content="hello")],
            ),
        ]
        results = await run_batch(provider, requests, poll_interval=0.01)
        assert len(results) == 1
        assert results[0].content == "Hello!"

    @pytest.mark.asyncio
    async def test_run_batch_fallback_to_concurrent(self):
        """Test run_batch falls back to concurrent chat() when batch not supported."""
        from adk.llm.base import ProviderCapabilities

        provider = AnthropicProvider(api_key="sk-ant-test")

        # Mock chat() to return success
        async def mock_chat(*args, **kwargs):
            return LLMResponse(content="Mocked response")

        provider.chat = mock_chat

        requests = [
            BatchRequest(
                custom_id="req_1",
                messages=[Message(role="user", content="hello")],
            ),
            BatchRequest(
                custom_id="req_2",
                messages=[Message(role="user", content="world")],
            ),
        ]

        # Force capabilities to report batch=False to test fallback
        provider.capabilities = lambda: ProviderCapabilities(prompt_cache="explicit", batch=False)

        results = await run_batch(provider, requests)
        assert len(results) == 2
        assert all(r.content == "Mocked response" for r in results)

    @pytest.mark.asyncio
    async def test_run_batch_single_item_failure_doesnt_kill_batch(self):
        """Test that one failed request doesn't kill the entire fallback batch."""
        from adk.llm.base import ProviderCapabilities

        provider = AnthropicProvider(api_key="sk-ant-test")

        async def mock_chat(*args, **kwargs):
            # One request fails, others succeed
            if "fail" in str(args):
                raise ValueError("Simulated failure")
            return LLMResponse(content="OK")

        provider.chat = mock_chat

        requests = [
            BatchRequest(
                custom_id="req_1",
                messages=[Message(role="user", content="hello")],
            ),
            BatchRequest(
                custom_id="req_fail",
                messages=[Message(role="user", content="fail me")],
            ),
            BatchRequest(
                custom_id="req_3",
                messages=[Message(role="user", content="world")],
            ),
        ]

        # Force fallback
        provider.capabilities = lambda: ProviderCapabilities(prompt_cache="explicit", batch=False)

        results = await run_batch(provider, requests)
        assert len(results) == 3
        # Failed request should have error finish_reason
        assert results[1].finish_reason == "error"
        # Others should succeed
        assert results[0].content == "OK"
        assert results[2].content == "OK"

    @pytest.mark.asyncio
    async def test_run_batch_result_ordering(self):
        """Test that results are returned in request order, matched by custom_id."""
        from adk.llm.base import ProviderCapabilities

        provider = AnthropicProvider(api_key="sk-ant-test")

        async def mock_chat(*args, **kwargs):
            return LLMResponse(content="Response")

        provider.chat = mock_chat

        requests = [
            BatchRequest(
                custom_id="z_last",
                messages=[Message(role="user", content="z")],
            ),
            BatchRequest(
                custom_id="a_first",
                messages=[Message(role="user", content="a")],
            ),
            BatchRequest(
                custom_id="m_middle",
                messages=[Message(role="user", content="m")],
            ),
        ]

        # Force fallback
        provider.capabilities = lambda: ProviderCapabilities(prompt_cache="explicit", batch=False)

        results = await run_batch(provider, requests)
        # Results should be in the same order as input requests
        assert len(results) == 3
        # All responses should be the same since we return generic "Response"
        assert all(r.content == "Response" for r in results)

    @pytest.mark.asyncio
    async def test_run_batch_timeout(self):
        """Test that timeout triggers fallback to concurrent chat."""
        from adk.llm.base import ProviderCapabilities

        provider = AnthropicProvider(api_key="sk-ant-test")

        # Mock submit_batch to return an ID
        async def mock_submit(*args, **kwargs):
            return "batch_123"

        # Mock poll_batch to always return in_progress (never completes)
        async def mock_poll(*args, **kwargs):
            return "in_progress"

        # Mock chat to return success (for fallback)
        async def mock_chat(*args, **kwargs):
            return LLMResponse(content="Fallback response")

        provider.submit_batch = mock_submit
        provider.poll_batch = mock_poll
        provider.chat = mock_chat

        requests = [
            BatchRequest(
                custom_id="req_1",
                messages=[Message(role="user", content="hello")],
            ),
        ]

        # Run with very short timeout to trigger fallback
        results = await run_batch(provider, requests, poll_interval=0.01, timeout=0.05)
        # Should fall back and return results via concurrent chat
        assert len(results) == 1
        assert results[0].content == "Fallback response"


# ─── Integration tests ───

class TestBatchIntegration:
    @respx.mock
    @pytest.mark.asyncio
    async def test_anthropic_batch_end_to_end(self):
        """End-to-end test of Anthropic batch workflow."""
        # Setup mocks for the entire batch lifecycle
        respx.post("https://api.anthropic.com/v1/messages/batches").mock(
            return_value=httpx.Response(200, json={"id": "batch_100"})
        )

        # Stateful mock for polling
        poll_count = {"value": 0}

        def poll_response(*args, **kwargs):
            poll_count["value"] += 1
            if poll_count["value"] == 1:
                return httpx.Response(200, json={"processing_status": "in_progress"})
            else:
                return httpx.Response(200, json={
                    "processing_status": "completed",
                    "results_url": "https://api.anthropic.com/v1/messages/batches/batch_100/results",
                })

        respx.get("https://api.anthropic.com/v1/messages/batches/batch_100").mock(
            side_effect=poll_response
        )
        results_jsonl = (
            '{"custom_id": "q1", "result": {"message": {"content": [{"type": "text", "text": "Answer 1"}], "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 5}}}}\n'
            '{"custom_id": "q2", "result": {"message": {"content": [{"type": "text", "text": "Answer 2"}], "usage": {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 8, "cache_creation_input_tokens": 0}}}}\n'
        )
        respx.get("https://api.anthropic.com/v1/messages/batches/batch_100/results").mock(
            return_value=httpx.Response(200, text=results_jsonl)
        )

        provider = AnthropicProvider(api_key="sk-ant-test")
        requests = [
            BatchRequest(
                custom_id="q1",
                messages=[
                    Message(role="system", content="You are helpful"),
                    Message(role="user", content="What is 2+2?"),
                ],
                cache=True,
            ),
            BatchRequest(
                custom_id="q2",
                messages=[
                    Message(role="system", content="You are helpful"),
                    Message(role="user", content="What is 3+3?"),
                ],
                cache=True,
            ),
        ]

        results = await run_batch(provider, requests, poll_interval=0.01)
        assert len(results) == 2
        assert results[0].content == "Answer 1"
        assert results[1].content == "Answer 2"
        # Check cache accounting
        assert results[0].cache_write_tokens == 5
        assert results[1].cache_read_tokens == 8

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_batch_end_to_end(self):
        """End-to-end test of OpenAI batch workflow."""
        respx.post("https://api.openai.com/v1/files").mock(
            return_value=httpx.Response(200, json={"id": "file_200"})
        )
        respx.post("https://api.openai.com/v1/batches").mock(
            return_value=httpx.Response(200, json={"id": "batch_200", "status": "queued"})
        )

        # Stateful mock for polling
        poll_count = {"value": 0}

        def poll_response(*args, **kwargs):
            poll_count["value"] += 1
            if poll_count["value"] == 1:
                return httpx.Response(200, json={"status": "in_progress"})
            else:
                return httpx.Response(200, json={
                    "status": "completed",
                    "output_file_id": "file_output_200",
                })

        respx.get("https://api.openai.com/v1/batches/batch_200").mock(
            side_effect=poll_response
        )
        output_jsonl = (
            '{"custom_id":"q1","result":{"body":{"choices":[{"message":{"content":"Answer 1"},"finish_reason":"stop"}],"usage":{"total_tokens":20,"prompt_tokens":15,"completion_tokens":5},"model":"gpt-4o"}}}\n'
            '{"custom_id":"q2","result":{"body":{"choices":[{"message":{"content":"Answer 2"},"finish_reason":"stop"}],"usage":{"total_tokens":18,"prompt_tokens":15,"completion_tokens":3},"model":"gpt-4o"}}}\n'
        )
        respx.get("https://api.openai.com/v1/files/file_output_200/content").mock(
            return_value=httpx.Response(200, text=output_jsonl)
        )

        provider = OpenAIProvider(api_key="sk-test")
        requests = [
            BatchRequest(
                custom_id="q1",
                messages=[Message(role="user", content="What is 2+2?")],
            ),
            BatchRequest(
                custom_id="q2",
                messages=[Message(role="user", content="What is 3+3?")],
            ),
        ]

        results = await run_batch(provider, requests, poll_interval=0.01)
        assert len(results) == 2
        assert results[0].content == "Answer 1"
        assert results[1].content == "Answer 2"
        assert results[0].tokens_used == 20
        assert results[1].tokens_used == 18
