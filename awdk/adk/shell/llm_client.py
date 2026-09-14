"""
Direct LLM Client — standalone mode (no Genesis required)
==========================================================

Lightweight OpenAI-compatible HTTP client for direct LLM inference.
Works with Ollama, vLLM, LM Studio, OpenAI, or any OpenAI-compatible endpoint.

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
        base_url: str = "http://localhost:11434",
        api_key: str = "",
        model: str = "",
        backend: str = "ollama",
    ):
        self.backend = backend
        self.api_key = api_key
        self.model = model

        if backend == "ollama":
            self.base_url = base_url.rstrip("/")
            self._chat_endpoint = "/api/chat"
            self._models_endpoint = "/api/tags"
        else:
            # OpenAI-compatible (vLLM, OpenAI, LM Studio, etc.)
            self.base_url = base_url.rstrip("/")
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
            if self.backend == "ollama":
                resp = await client.get(self.base_url, timeout=5.0)
            else:
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
                if self.backend == "ollama":
                    models = data.get("models", [])
                    if models:
                        return models[0].get("name", "")
                else:
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
                raise RuntimeError("No model available. Specify --model or pull one with 'ollama pull'.")
            self.model = use_model

        client = await self._get_client()

        if self.backend == "ollama":
            payload = {
                "model": use_model,
                "messages": [{"role": "user", "content": message}],
                "stream": True,
                "options": {"temperature": temperature},
            }
            if max_tokens:
                payload["options"]["num_predict"] = max_tokens

            async with client.stream(
                "POST", f"{self.base_url}{self._chat_endpoint}", json=payload
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                        content = data.get("message", {}).get("content", "")
                        if content:
                            yield content
                        if data.get("done"):
                            return
                    except json.JSONDecodeError:
                        continue
        else:
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
    # Priority: env vars → vLLM → Ollama

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

    # Try Ollama
    ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    if "0.0.0.0" in ollama_host:
        ollama_host = ollama_host.replace("0.0.0.0", "localhost")
    if not ollama_host.startswith("http"):
        ollama_host = "http://" + ollama_host
    client = DirectLLMClient(base_url=ollama_host, backend="ollama")
    if await client.health_check():
        logger.info("Found Ollama at %s", ollama_host)
        return client
    await client.close()

    return None
