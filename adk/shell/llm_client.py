"""
Direct LLM Client — standalone mode (no Genesis required)
==========================================================

Lightweight OpenAI-compatible HTTP client for direct LLM inference.
Works with llama.cpp (llama-server), vLLM, LM Studio, OpenAI, or any OpenAI-compatible endpoint.

Used as fallback when Genesis is unreachable, or when configured explicitly
via ``llm_backend`` in ~/.aither/config.yaml.
"""

import asyncio
import json
import logging
import os
from typing import AsyncIterator, Optional

import httpx

logger = logging.getLogger(__name__)


class DirectLLMClient:
    """Async client for OpenAI-compatible chat completions."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        api_key: str = "",
        model: str = "",
        backend: str = "llamacpp",
    ):
        from adk.legacy_backend import migrate_backend_name, migrate_base_url

        self.backend = migrate_backend_name(backend)
        self.api_key = api_key
        self.model = model

        # Every backend speaks the OpenAI shape (llama-server, vLLM, OpenAI, LM Studio).
        self.base_url = migrate_base_url(base_url).rstrip("/")
        if not self.base_url.endswith("/v1"):
            self.base_url += "/v1"
        self._chat_endpoint = "/chat/completions"
        self._models_endpoint = "/models"

        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=30.0),
                headers=headers,
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            resp = await client.get(
                f"{self.base_url}{self._models_endpoint}", timeout=5.0
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def detect_model(self) -> str:
        """Auto-detect the first available model."""
        try:
            client = await self._get_client()
            resp = await client.get(
                f"{self.base_url}{self._models_endpoint}", timeout=10.0
            )
            if resp.status_code == 200:
                data = resp.json()
                models = data.get("data", [])
                if models:
                    return models[0].get("id", "")
        except Exception:
            pass
        return ""

    async def chat_stream(
        self,
        message: str,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        """Stream a chat completion. Yields text chunks."""
        use_model = model or self.model
        if not use_model:
            use_model = await self.detect_model()
            if not use_model:
                raise RuntimeError(
                    "No model available. Specify --model, or start llama-server with a GGUF."
                )
            self.model = use_model

        client = await self._get_client()

        # OpenAI-compatible streaming
        payload = {
            "model": use_model,
            "messages": [{"role": "user", "content": message}],
            "stream": True,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        async with client.stream(
            "POST",
            f"{self.base_url}{self._chat_endpoint}",
            json=payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    return
                try:
                    data = json.loads(data_str)
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        yield content
                except (json.JSONDecodeError, IndexError):
                    continue


async def auto_detect_backend() -> Optional[DirectLLMClient]:
    """Try to find a working LLM backend. Returns client or None."""
    # Priority: env vars → vLLM → llama.cpp (llama-server)

    vllm_url = os.environ.get("AITHER_VLLM_URL") or os.environ.get("VLLM_URL")
    if vllm_url:
        client = DirectLLMClient(base_url=vllm_url, backend="openai")
        if await client.health_check():
            logger.info("Using vLLM at %s", vllm_url)
            return client
        await client.close()

    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        client = DirectLLMClient(
            base_url="https://api.openai.com/v1",
            api_key=openai_key,
            model="gpt-4o-mini",
            backend="openai",
        )
        logger.info("Using OpenAI API")
        return client

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        # Anthropic isn't OpenAI-compatible, skip for now
        pass

    # Try vLLM on common ports
    for port in (8199, 8120, 8200, 8000):
        client = DirectLLMClient(
            base_url=f"http://localhost:{port}", backend="openai"
        )
        if await client.health_check():
            logger.info("Found vLLM at localhost:%d", port)
            return client
        await client.close()

    # A local llama-server (AITHER_LLAMACPP_URL, else its default :8080).
    llamacpp_url = os.environ.get("AITHER_LLAMACPP_URL", "") or "http://127.0.0.1:8080"
    client = DirectLLMClient(base_url=llamacpp_url, backend="llamacpp")
    if await client.health_check():
        logger.info("Found llama-server at %s", llamacpp_url)
        return client
    await client.close()

    return None
