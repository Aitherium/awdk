"""Protocol drivers for Agent Pack Supervisor.

Provides protocol-agnostic drivers for various agent frameworks (http, langgraph_rest,
a2a, mcp). Each driver exposes a uniform async interface:
- prompt(text: str) -> DriverResult
- close() -> None

Drivers are selected via get_driver(protocol, handle_or_url, **kwargs) and cached.
Tests use httpx.MockTransport for offline execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger("adk.pack_drivers")

# Supported wire protocols for which drivers exist
DRIVER_PROTOCOLS = ("http", "langgraph_rest", "a2a", "mcp")


@dataclass
class ToolCall:
    """Represents a tool invocation from an agent response."""
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str = ""


@dataclass
class DriverResult:
    """Uniform result from any protocol driver."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class ProtocolDriver(ABC):
    """Base class for protocol drivers."""

    @abstractmethod
    async def prompt(self, text: str) -> DriverResult:
        """Send a prompt and return a DriverResult."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close the driver and release resources."""
        ...


class HttpDriver(ProtocolDriver):
    """HTTP protocol driver — POST {"prompt": text} to a base_url.

    Parses response leniently (looks for text|output|response|content keys
    in JSON, or treats the response body as plain text if not JSON).
    """

    def __init__(
        self,
        base_url: str,
        endpoint: str = "/prompt",
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 30.0,
    ):
        """Initialize HttpDriver.

        Args:
            base_url: Base URL for the HTTP endpoint.
            endpoint: Endpoint path (default: /prompt).
            client: Optional httpx.AsyncClient for testing/injection.
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint.lstrip("/")
        self.url = f"{self.base_url}/{self.endpoint}"
        self._client = client
        self._owns_client = client is None
        self.timeout = timeout

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the http client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def prompt(self, text: str) -> DriverResult:
        """Send a prompt via HTTP POST."""
        try:
            client = await self._get_client()
            resp = await client.post(
                self.url,
                json={"prompt": text},
            )
            resp.raise_for_status()

            # Lenient JSON parsing: try common keys
            try:
                data = resp.json()
            except (json.JSONDecodeError, ValueError):
                # Fallback: treat body as plain text
                return DriverResult(text=resp.text, raw={})

            if not isinstance(data, dict):
                return DriverResult(text=str(data), raw=data)

            # Leniently extract text from various key names
            text_value = (
                data.get("text")
                or data.get("output")
                or data.get("response")
                or data.get("content")
                or str(data)
            )

            # Extract tool_calls if present (a list of dicts)
            tool_calls_data = data.get("tool_calls", [])
            tool_calls = []
            if isinstance(tool_calls_data, list):
                for tc in tool_calls_data:
                    if isinstance(tc, dict):
                        tool_calls.append(ToolCall(
                            name=tc.get("name", ""),
                            arguments=tc.get("arguments", {}),
                            tool_call_id=tc.get("id", ""),
                        ))

            return DriverResult(
                text=str(text_value),
                tool_calls=tool_calls,
                raw=data,
            )
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"HTTP error from {self.url}: {e.response.status_code}: {e.response.text}"
            ) from e
        except Exception as e:
            raise RuntimeError(f"HttpDriver.prompt failed: {e}") from e

    async def close(self) -> None:
        """Close the client if owned."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


class LangGraphRestDriver(ProtocolDriver):
    """LangGraph REST protocol driver — POST to <base>/runs/stream.

    Parses Server-Sent Events (SSE) response and aggregates messages.
    Final text is extracted from the last message event.
    """

    def __init__(
        self,
        base_url: str,
        endpoint: str = "/runs/stream",
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 60.0,
    ):
        """Initialize LangGraphRestDriver.

        Args:
            base_url: Base URL for the LangGraph endpoint.
            endpoint: Endpoint path (default: /runs/stream).
            client: Optional httpx.AsyncClient for testing/injection.
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint.lstrip("/")
        self.url = f"{self.base_url}/{self.endpoint}"
        self._client = client
        self._owns_client = client is None
        self.timeout = timeout

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the http client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _parse_sse_line(self, line: str) -> Optional[dict]:
        """Parse a single SSE data: line into JSON."""
        if not line.startswith("data:"):
            return None
        data_str = line[5:].strip()
        if not data_str:
            return None
        try:
            return json.loads(data_str)
        except json.JSONDecodeError:
            return None

    async def prompt(self, text: str) -> DriverResult:
        """Send a prompt and consume SSE response."""
        try:
            client = await self._get_client()
            payload = {
                "input": {"messages": [{"role": "user", "content": text}]},
                "stream_mode": "messages",
            }

            resp = await client.post(self.url, json=payload)
            resp.raise_for_status()

            # Parse SSE stream
            final_text = ""
            tool_calls: list[ToolCall] = []
            raw_data: dict[str, Any] = {}

            # Split by lines and parse SSE format
            for line in resp.text.split("\n"):
                line = line.rstrip()
                if not line or line.startswith(":"):
                    # Empty line or comment — skip
                    continue

                parsed = self._parse_sse_line(line)
                if parsed is None:
                    continue

                raw_data = parsed

                # LangGraph emits messages in a specific structure
                if isinstance(parsed.get("messages"), list):
                    messages = parsed["messages"]
                    for msg in messages:
                        if isinstance(msg, (list, tuple)) and len(msg) > 1:
                            # Tuple format: (role, content)
                            if isinstance(msg[1], str):
                                final_text = msg[1]
                            elif isinstance(msg[1], dict):
                                final_text = msg[1].get("content", "")

                # Also check for direct content/text at root
                if "content" in parsed:
                    final_text = str(parsed["content"])
                elif "text" in parsed:
                    final_text = str(parsed["text"])

            return DriverResult(
                text=final_text,
                tool_calls=tool_calls,
                raw=raw_data,
            )
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"HTTP error from {self.url}: {e.response.status_code}: {e.response.text}"
            ) from e
        except Exception as e:
            raise RuntimeError(f"LangGraphRestDriver.prompt failed: {e}") from e

    async def close(self) -> None:
        """Close the client if owned."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


class A2ADriver(ProtocolDriver):
    """A2A (Agent-to-Agent) protocol driver — JSON-RPC message/send.

    Sends a JSON-RPC 2.0 'message/send' request to the agent's /a2a endpoint.
    """

    def __init__(
        self,
        base_url: str,
        endpoint: str = "/a2a",
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 30.0,
    ):
        """Initialize A2ADriver.

        Args:
            base_url: Base URL for the A2A endpoint.
            endpoint: Endpoint path (default: /a2a).
            client: Optional httpx.AsyncClient for testing/injection.
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint.lstrip("/")
        self.url = f"{self.base_url}/{self.endpoint}"
        self._client = client
        self._owns_client = client is None
        self.timeout = timeout
        self._request_id = 0

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the http client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _next_request_id(self) -> int:
        """Get the next JSON-RPC request ID."""
        self._request_id += 1
        return self._request_id

    async def prompt(self, text: str) -> DriverResult:
        """Send a prompt via A2A message/send."""
        try:
            client = await self._get_client()

            payload = {
                "jsonrpc": "2.0",
                "id": self._next_request_id(),
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [{"type": "text", "text": text}],
                    },
                },
            }

            resp = await client.post(self.url, json=payload)
            resp.raise_for_status()

            data = resp.json()
            if not isinstance(data, dict):
                return DriverResult(text=str(data), raw={})

            # Check for JSON-RPC error
            if "error" in data:
                error_info = data.get("error", {})
                error_msg = error_info.get("message", "Unknown error")
                raise RuntimeError(f"A2A error: {error_msg}")

            # Extract result
            result = data.get("result", {})
            if not isinstance(result, dict):
                return DriverResult(text=str(result), raw=data)

            # Extract message text from task/message
            final_text = ""
            task_data = result.get("task", {})
            if isinstance(task_data, dict):
                history = task_data.get("history", [])
                # Last message in history should be the agent response
                if history and isinstance(history[-1], dict):
                    last_msg = history[-1]
                    parts = last_msg.get("parts", [])
                    if parts and isinstance(parts[0], dict):
                        final_text = parts[0].get("text", "")

            message_data = result.get("message", {})
            if isinstance(message_data, dict):
                parts = message_data.get("parts", [])
                if parts and isinstance(parts[0], dict):
                    final_text = parts[0].get("text", "")

            return DriverResult(
                text=final_text,
                tool_calls=[],
                raw=data,
            )
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"HTTP error from {self.url}: {e.response.status_code}: {e.response.text}"
            ) from e
        except Exception as e:
            raise RuntimeError(f"A2ADriver.prompt failed: {e}") from e

    async def close(self) -> None:
        """Close the client if owned."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


class McpDriver(ProtocolDriver):
    """MCP (Model Context Protocol) driver — JSON-RPC tools/call.

    Sends a JSON-RPC 2.0 'tools/call' request to invoke an MCP tool.
    """

    def __init__(
        self,
        base_url: str,
        endpoint: str = "/mcp",
        tool_name: str = "prompt",
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 30.0,
    ):
        """Initialize McpDriver.

        Args:
            base_url: Base URL for the MCP endpoint.
            endpoint: Endpoint path (default: /mcp).
            tool_name: Name of the tool to invoke (default: prompt).
            client: Optional httpx.AsyncClient for testing/injection.
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint.lstrip("/")
        self.url = f"{self.base_url}/{self.endpoint}"
        self.tool_name = tool_name
        self._client = client
        self._owns_client = client is None
        self.timeout = timeout
        self._request_id = 0

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the http client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _next_request_id(self) -> int:
        """Get the next JSON-RPC request ID."""
        self._request_id += 1
        return self._request_id

    async def prompt(self, text: str) -> DriverResult:
        """Send a prompt via MCP tools/call."""
        try:
            client = await self._get_client()

            payload = {
                "jsonrpc": "2.0",
                "id": self._next_request_id(),
                "method": "tools/call",
                "params": {
                    "name": self.tool_name,
                    "arguments": {"prompt": text},
                },
            }

            resp = await client.post(self.url, json=payload)
            resp.raise_for_status()

            data = resp.json()
            if not isinstance(data, dict):
                return DriverResult(text=str(data), raw={})

            # Check for JSON-RPC error
            if "error" in data:
                error_info = data.get("error", {})
                error_msg = error_info.get("message", "Unknown error")
                raise RuntimeError(f"MCP error: {error_msg}")

            # Extract result
            result = data.get("result", {})
            if not isinstance(result, dict):
                return DriverResult(text=str(result), raw=data)

            # Extract text from result (leniently)
            final_text = (
                result.get("text")
                or result.get("output")
                or result.get("content")
                or str(result)
            )

            return DriverResult(
                text=str(final_text),
                tool_calls=[],
                raw=data,
            )
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"HTTP error from {self.url}: {e.response.status_code}: {e.response.text}"
            ) from e
        except Exception as e:
            raise RuntimeError(f"McpDriver.prompt failed: {e}") from e

    async def close(self) -> None:
        """Close the client if owned."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


def get_driver(
    protocol: str,
    handle_or_url: str,
    client: Optional[httpx.AsyncClient] = None,
    **kwargs: Any,
) -> ProtocolDriver:
    """Get a protocol driver for the given protocol and endpoint.

    Args:
        protocol: Protocol name (http, langgraph_rest, a2a, mcp).
        handle_or_url: URL or handle for the endpoint.
        client: Optional httpx.AsyncClient for injection (testing).
        **kwargs: Protocol-specific options (e.g., endpoint, tool_name).

    Returns:
        A ProtocolDriver instance ready to use.

    Raises:
        NotImplementedError: If the protocol is not supported.
    """
    if protocol == "http":
        return HttpDriver(
            base_url=handle_or_url,
            endpoint=kwargs.get("endpoint", "/prompt"),
            client=client,
            timeout=kwargs.get("timeout", 30.0),
        )
    elif protocol == "langgraph_rest":
        return LangGraphRestDriver(
            base_url=handle_or_url,
            endpoint=kwargs.get("endpoint", "/runs/stream"),
            client=client,
            timeout=kwargs.get("timeout", 60.0),
        )
    elif protocol == "a2a":
        return A2ADriver(
            base_url=handle_or_url,
            endpoint=kwargs.get("endpoint", "/a2a"),
            client=client,
            timeout=kwargs.get("timeout", 30.0),
        )
    elif protocol == "mcp":
        return McpDriver(
            base_url=handle_or_url,
            endpoint=kwargs.get("endpoint", "/mcp"),
            tool_name=kwargs.get("tool_name", "prompt"),
            client=client,
            timeout=kwargs.get("timeout", 30.0),
        )
    else:
        raise NotImplementedError(
            f"Protocol {protocol!r} not supported. "
            f"Supported: {', '.join(DRIVER_PROTOCOLS)}"
        )
