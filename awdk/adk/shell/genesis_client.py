"""
AitherShell Genesis HTTP Client
=================================

High-performance async HTTP client for Genesis (port 8001).
Uses the ``/chat/stream`` SSE endpoint for real-time pipeline
visibility, session steering, and liveness-based timeouts.
"""

import asyncio
import json
import logging
import time
from typing import AsyncIterator, Dict, Any, Optional, Callable, Awaitable
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


class GenesisClient:
    """
    Async HTTP client for Genesis orchestrator.

    Handles:
    - SSE streaming via /chat/stream (pipeline events, answer, heartbeat)
    - Session steering via /chat/steer (inject input into active sessions)
    - Configurable timeouts with generous read timeout for long inference
    - Exponential backoff retries for connection failures
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8001",
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
        enable_logging: bool = True,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")

        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.enable_logging = enable_logging
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Connect timeout is short; read timeout is generous because
            # LLM inference can take minutes. The SSE liveness timeout on
            # the server side handles true hangs.
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=self.timeout,
                    read=600.0,    # 10 min read — server sends heartbeats
                    write=30.0,
                    pool=30.0,
                ),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            response = await client.get(
                f"{self.base_url}/health",
                timeout=self.timeout / 2,
            )
            return response.status_code == 200
        except (httpx.RequestError, asyncio.TimeoutError):
            if self.enable_logging:
                logger.debug(f"Health check failed for {self.base_url}")
            return False

    async def get_status(self) -> Optional[Dict[str, Any]]:
        """Fetch Genesis /status endpoint for detailed system info."""
        try:
            client = await self._get_client()
            response = await client.get(
                f"{self.base_url}/status",
                timeout=self.timeout,
            )
            if response.status_code == 200:
                return response.json()
        except (httpx.RequestError, asyncio.TimeoutError):
            if self.enable_logging:
                logger.debug(f"Status fetch failed for {self.base_url}")
        return None

    async def chat_stream(
        self,
        message: str,
        persona: Optional[str] = None,
        effort: Optional[int] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        safety_level: Optional[str] = None,
        private_mode: bool = False,
        session_id: Optional[str] = None,
        on_event: Optional[Callable[[str, dict], Awaitable[None]]] = None,
        prefer_cloud_model: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """Stream a chat request via the /chat/stream SSE endpoint.

        Args:
            message: User message/query
            persona: Persona name
            effort: Effort level 1-10
            model: Model override
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature
            safety_level: Safety level
            private_mode: Enable privacy mode
            session_id: Session ID for steering/context continuity
            on_event: Optional callback for ALL SSE events (pipeline, thinking, etc.)
            prefer_cloud_model: Route to a specific cloud model (e.g. "deepseek-v4-flash")

        Yields:
            String chunks from the ``answer`` event (the actual response text).
            Pipeline/thinking/tool events are forwarded to ``on_event`` if provided.
        """
        payload: dict = {"message": message}
        if persona:
            payload["persona"] = persona
        if effort is not None:
            payload["effort"] = effort
        if model:
            payload["model"] = model
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if safety_level:
            payload["safety_level"] = safety_level
        if private_mode:
            payload["private_mode"] = True
        if session_id:
            payload["session_id"] = session_id
        if prefer_cloud_model:
            payload["prefer_cloud_model"] = prefer_cloud_model

        if self.enable_logging:
            logger.debug(f"Chat stream request: {json.dumps(payload, default=str)}")

        async for chunk in self._sse_stream("/chat/stream", payload, on_event=on_event):
            yield chunk

    async def _sse_stream(
        self,
        endpoint: str,
        payload: dict,
        on_event: Optional[Callable[[str, dict], Awaitable[None]]] = None,
    ) -> AsyncIterator[str]:
        """POST to an SSE endpoint, parse events, yield answer text.

        SSE format: ``event: <type>\\ndata: <json>\\n\\n``

        - ``answer`` events: yield the ``answer`` field as text
        - ``error`` events: raise GenesisError
        - ``complete`` events: return
        - All other events: forward to on_event callback
        """
        url = f"{self.base_url}{endpoint}"
        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                client = await self._get_client()

                if self.enable_logging:
                    logger.debug(f"POST {endpoint} (attempt {attempt + 1}/{self.max_retries + 1})")

                async with client.stream(
                    "POST", url,
                    json=payload,
                    headers={"Accept": "text/event-stream"},
                ) as response:
                    if response.status_code >= 400:
                        error_text = await response.aread()
                        error_msg = error_text.decode("utf-8", errors="replace")
                        raise GenesisError(
                            f"Genesis returned {response.status_code}: {error_msg}",
                            status_code=response.status_code,
                        )

                    # Parse SSE events from the response stream
                    _event_type = ""
                    _data_lines: list = []

                    async for line in response.aiter_lines():
                        if line.startswith("event:"):
                            _event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            _data_lines.append(line[5:].strip())
                        elif line == "" and (_event_type or _data_lines):
                            # End of event block — process it
                            raw_data = "\n".join(_data_lines)
                            _data_lines = []

                            try:
                                data = json.loads(raw_data) if raw_data else {}
                            except json.JSONDecodeError:
                                data = {"raw": raw_data}

                            evt = _event_type or data.get("type", "unknown")
                            _event_type = ""

                            if evt == "answer":
                                answer_text = data.get("answer", "")
                                if answer_text:
                                    yield answer_text
                                if on_event:
                                    try:
                                        await on_event(evt, data)
                                    except Exception:
                                        pass

                            elif evt == "complete":
                                if on_event:
                                    try:
                                        await on_event(evt, data)
                                    except Exception:
                                        pass
                                return  # Done

                            elif evt == "error":
                                err_msg = data.get("error", "Unknown error")
                                if on_event:
                                    try:
                                        await on_event(evt, data)
                                    except Exception:
                                        pass
                                raise GenesisError(err_msg)

                            else:
                                # pipeline, thinking, tool_call, tool_result,
                                # heartbeat, steering, session_start, etc.
                                if on_event:
                                    try:
                                        await on_event(evt, data)
                                    except Exception:
                                        pass

                return  # Success (stream ended without complete event)

            except asyncio.TimeoutError:
                last_error = GenesisTimeoutError(
                    f"Request timed out after {self.timeout}s",
                    attempt=attempt,
                )
                if self.enable_logging:
                    logger.warning(f"Timeout on attempt {attempt + 1}")

            except httpx.ConnectError as e:
                last_error = GenesisConnectionError(
                    f"Failed to connect to Genesis: {e}",
                    attempt=attempt,
                )
                if self.enable_logging:
                    logger.warning(f"Connection error on attempt {attempt + 1}: {e}")

            except httpx.RequestError as e:
                last_error = GenesisConnectionError(
                    f"Request error: {e}",
                    attempt=attempt,
                )
                if self.enable_logging:
                    logger.warning(f"Request error on attempt {attempt + 1}: {e}")

            except GenesisError:
                raise  # Don't retry application errors

            # Exponential backoff before retry
            if attempt < self.max_retries:
                wait_time = self.backoff_factor ** attempt
                if self.enable_logging:
                    logger.debug(f"Waiting {wait_time:.1f}s before retry")
                await asyncio.sleep(wait_time)

        if last_error:
            raise last_error
        else:
            raise GenesisConnectionError(
                f"Failed to connect to Genesis after {self.max_retries + 1} attempts",
                attempt=self.max_retries,
            )

    async def steer(
        self,
        session_id: str,
        message: str,
        action: str = "append",
    ) -> dict:
        """Send a steering message into an active streaming session.

        Args:
            session_id: Active session ID to steer
            message: Steering text (e.g., "focus on X", "stop")
            action: "append" (inject input) or "cancel" (abort session)

        Returns:
            Server response dict
        """
        client = await self._get_client()
        try:
            response = await client.post(
                f"{self.base_url}/chat/steer",
                json={
                    "session_id": session_id,
                    "message": message,
                    "action": action,
                },
                timeout=5.0,
            )
            if response.status_code >= 400:
                return {"ok": False, "error": f"HTTP {response.status_code}"}
            return response.json()
        except Exception as e:
            logger.warning(f"Steer failed: {e}")
            return {"ok": False, "error": str(e)}

    async def download_file(self, file_path: str, dest: str) -> str:
        """Download a file from Genesis /files/{path} to a local path.

        Args:
            file_path: Remote file path (e.g. Library/Output/images/gen_xxx.png)
            dest: Local destination path

        Returns:
            Absolute path of saved file

        Raises:
            GenesisError: If download fails
        """
        from pathlib import Path as _Path

        client = await self._get_client()
        url = f"{self.base_url}/files/{file_path}"
        try:
            response = await client.get(url, timeout=60.0)
            if response.status_code >= 400:
                raise GenesisError(
                    f"Download failed: HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            dest_path = _Path(dest)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(response.content)
            return str(dest_path.resolve())
        except GenesisError:
            raise
        except Exception as e:
            raise GenesisError(f"Download failed: {e}")

    async def chat(
        self,
        message: str,
        **kwargs,
    ) -> str:
        """Send a chat message and wait for full response.

        Args:
            message: User message/query
            **kwargs: Additional arguments passed to chat_stream

        Returns:
            Full response text
        """
        response_text = ""
        async for chunk in self.chat_stream(message, **kwargs):
            response_text += chunk
        return response_text


# Error classes
class GenesisError(Exception):
    """Base Genesis error."""

    def __init__(self, message: str, status_code: Optional[int] = None, attempt: int = 0):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.attempt = attempt
        self.timestamp = datetime.utcnow().isoformat()


class GenesisConnectionError(GenesisError):
    """Connection or network error."""
    pass


class GenesisTimeoutError(GenesisError):
    """Request timeout error."""
    pass
