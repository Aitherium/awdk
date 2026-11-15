"""NVIDIA CUDA docs pack — fail-soft tools over the hosted CUDA MCP server.

The server is ``https://api.copilot.nsight.ngc.nvidia.com/mcp/cuda-docs``: MCP
JSON-RPC over HTTP with SSE-framed replies. Every tool returns a dict and never
raises. A missing credential is a status (``not_authenticated``) with a fix,
not an exception into the agent loop — an agent with no NVIDIA account must
still boot and work exactly as it does today.

Two rules this pack exists to enforce:

* The search tool is discovered from ``tools/list`` by SHAPE, never hardcoded.
  An upstream rename must fail loudly with the available names instead of
  degrading into "no documentation matched", which reads like an honest miss.
* An empty result means the wording did not match the corpus, not that the API
  does not exist. The status says so, and no result is ever reported as absence.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib import parse as urlparse

from . import config

logger = logging.getLogger("cuda_pack")

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "adk-nvidia-cuda", "version": "1.0.0"}
_SEARCH_ARG_HINTS = ("query", "q", "prompt", "text", "search")


def _denied() -> dict | None:
    """The not-authenticated envelope, or None when a bearer is available."""
    if config.access_token():
        return None
    return {
        "status": "not_authenticated",
        "error": "not_authenticated",
        "credential_source": config.credential_source(),
        "fix": config.FIX,
    }


def _parse_body(text: str) -> dict:
    """Parse a JSON body or the last ``data:`` frame of an SSE body."""
    body = (text or "").strip()
    if not body:
        return {}
    if body.startswith("data:") or "\ndata:" in body:
        payload = ""
        for line in body.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
        body = payload
    try:
        parsed = json.loads(body)
    except ValueError:
        return {"_unparsed": body[:500]}
    return parsed if isinstance(parsed, dict) else {"_unparsed": str(parsed)[:500]}


def _post(payload: dict, session_id: str = "", timeout: float = 30.0) -> dict:
    """POST one JSON-RPC message. Returns the transport dict from config."""
    token = config.access_token()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return config.http_request(
        "POST",
        config.MCP_URL,
        body=json.dumps(payload).encode("utf-8"),
        headers=headers,
        timeout=timeout,
    )


def _rpc(
    method: str,
    params: dict | None,
    session_id: str = "",
    request_id: int = 1,
    timeout: float = 30.0,
) -> tuple[str, dict, dict | None]:
    """One request/response. Returns (session_id, result, error_envelope)."""
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    response = _post(message, session_id=session_id, timeout=timeout)
    status = int(response.get("status") or 0)
    if status in (401, 403):
        return session_id, {}, {
            "status": "not_authenticated",
            "error": "not_authenticated",
            "http_status": status,
            "credential_source": config.credential_source(),
            "fix": config.FIX,
        }
    if status == 0:
        return session_id, {}, {
            "status": "unreachable",
            "error": "unreachable",
            "message": config.redact(response.get("error") or "request failed"),
        }
    if status >= 400:
        return session_id, {}, {
            "status": "service_error",
            "error": "service_error",
            "http_status": status,
            "message": config.redact(response.get("text") or "")[:500],
        }
    new_session = ""
    for key, value in (response.get("headers") or {}).items():
        if str(key).lower() == "mcp-session-id":
            new_session = str(value)
    body = _parse_body(response.get("text") or "")
    if body.get("error"):
        err = body["error"]
        return new_session or session_id, {}, {
            "status": "service_error",
            "error": "rpc_error",
            "message": str(err.get("message") if isinstance(err, dict) else err)[:500],
        }
    result = body.get("result")
    return new_session or session_id, result if isinstance(result, dict) else {}, None


def _open_session() -> tuple[str, dict | None]:
    """Initialize an MCP session and send ``notifications/initialized``."""
    session_id, _result, error = _rpc(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        },
    )
    if error:
        return "", error
    if not session_id:
        return "", {
            "status": "service_error",
            "error": "no_session",
            "message": "server did not return an Mcp-Session-Id header",
        }
    _post(
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        session_id=session_id,
        timeout=15.0,
    )
    return session_id, None


def _list_tools() -> tuple[list, dict | None]:
    """List the server's real tools. Returns (tools, error)."""
    session_id, error = _open_session()
    if error:
        return [], error
    _sid, result, error = _rpc("tools/list", {}, session_id=session_id, request_id=2)
    if error:
        return [], error
    tools = result.get("tools")
    if not isinstance(tools, list):
        return [], {
            "status": "service_error",
            "error": "bad_tools_list",
            "message": "tools/list returned no tools array",
        }
    return tools, None


def _search_tool(tools: list) -> dict | None:
    """Pick the search tool by SHAPE: name first, then description."""
    ranked: list[tuple[int, dict]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").lower()
        description = str(tool.get("description") or "").lower()
        score = 0
        if "search" in name:
            score = 100
        elif "search" in description or "retriev" in description:
            score = 60
        elif "query" in name:
            score = 40
        if score:
            ranked.append((score, tool))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def _search_arguments(tool: dict, query: str, limit: int | None) -> dict:
    """Build the call arguments from the tool's own inputSchema."""
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if not properties:
        return {"query": query}
    arguments: dict[str, Any] = {}
    query_key = next((key for key in properties if str(key).lower() in _SEARCH_ARG_HINTS), None)
    if query_key is None:
        string_keys = [
            key
            for key, spec in properties.items()
            if isinstance(spec, dict) and spec.get("type") == "string"
        ]
        query_key = string_keys[0] if string_keys else str(next(iter(properties)))
    arguments[str(query_key)] = query
    if limit is not None and "limit" in properties:
        arguments["limit"] = int(limit)
    return arguments


def _extract_text(result: dict) -> list[str]:
    """Pull the text blocks out of a ``tools/call`` result."""
    content = result.get("content") if isinstance(result.get("content"), list) else []
    texts = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return texts


def cuda_docs_search(query: str, limit: int = 5) -> dict:
    """Search the live CUDA documentation corpus.

    Call this BEFORE writing or reviewing CUDA C++/kernels, nvcc or CMake
    flags, cuBLAS/cuDNN/NCCL calls, occupancy advice, or Nsight metric names,
    then cite the returned URLs. An empty result means the wording did not
    match — re-query with the exact symbol, never conclude the API is absent.
    """
    denied = _denied()
    if denied:
        return denied
    if not str(query or "").strip():
        return {
            "status": "invalid_argument",
            "error": "invalid_argument",
            "fix": "Pass the exact symbol or concept as `query`.",
        }
    tools, error = _list_tools()
    if error:
        return error
    tool = _search_tool(tools)
    if tool is None:
        return {
            "status": "no_search_tool",
            "error": "no_search_tool",
            "available": [str(t.get("name")) for t in tools if isinstance(t, dict)][:50],
            "fix": (
                "The server exposes no search-shaped tool. Re-check with "
                "cuda_docs_tools(); upstream may have renamed it."
            ),
        }
    arguments = _search_arguments(tool, query, limit)
    session_id, error = _open_session()
    if error:
        return error
    _sid, result, error = _rpc(
        "tools/call",
        {"name": tool.get("name"), "arguments": arguments},
        session_id=session_id,
        request_id=3,
    )
    if error:
        return error
    texts = _extract_text(result)
    return {
        "status": "ok",
        "tool": tool.get("name"),
        "arguments": arguments,
        "count": len(texts),
        "results": texts,
        "note": (
            "0 results means the wording did not match the corpus — retry with the "
            "exact symbol before concluding the API does not exist."
            if not texts
            else ""
        ),
        "is_error": bool(result.get("isError")),
    }


def cuda_docs_tools() -> dict:
    """Report what the hosted MCP server actually exposes right now."""
    denied = _denied()
    if denied:
        return denied
    tools, error = _list_tools()
    if error:
        return error
    return {
        "status": "ok",
        "count": len(tools),
        "tools": [
            {
                "name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or "")[:300],
            }
            for tool in tools
            if isinstance(tool, dict)
        ],
    }


def cuda_docs_status() -> dict:
    """Ask the SERVER whether this lane can answer — not local config."""
    if not config.access_token():
        return {
            "status": "not_authenticated",
            "reachable": False,
            "credential_source": "none",
            "fix": config.FIX,
        }
    tools, error = _list_tools()
    if error:
        return {
            "status": "unreachable",
            "reachable": False,
            "credential_source": config.credential_source(),
            "error": error,
        }
    tool = _search_tool(tools)
    return {
        "status": "reachable",
        "reachable": True,
        "credential_source": config.credential_source(),
        "tool_count": len(tools),
        "search_tool": str(tool.get("name")) if tool else None,
        "note": "" if tool else "reachable, but no search-shaped tool was advertised",
    }


def _authorization_server() -> tuple[dict | None, dict | None]:
    """Discover the authorization server from the 401 or well-known metadata."""
    response = config.http_request("GET", config.MCP_URL, timeout=15.0)
    if response.get("status") != 401:
        return None, {
            "status": "unreachable",
            "message": config.redact(
                str(response.get("error") or f"HTTP {response.get('status')}")
            ),
            "fix": "The CUDA docs server did not answer 401; check connectivity.",
        }
    header = ""
    for key, value in (response.get("headers") or {}).items():
        if str(key).lower() == "www-authenticate":
            header = str(value)
    metadata_url = ""
    for part in header.split(","):
        part = part.strip()
        if part.startswith("resource_metadata="):
            metadata_url = part.split("=", 1)[1].strip().strip('"')
    issuers: list[str] = []
    if metadata_url:
        meta = config.http_request("GET", metadata_url, timeout=15.0)
        parsed = _parse_body(meta.get("text") or "")
        servers = parsed.get("authorization_servers")
        if isinstance(servers, list):
            issuers = [str(s) for s in servers if isinstance(s, str)]
    if not issuers:
        origin = "{0.scheme}://{0.netloc}".format(urlparse.urlsplit(config.MCP_URL))
        issuers = [origin]
    for issuer in issuers:
        base = issuer.rstrip("/")
        for suffix in (
            "/.well-known/oauth-authorization-server",
            "/.well-known/openid-configuration",
        ):
            doc = config.http_request("GET", base + suffix, timeout=15.0)
            if doc.get("status") != 200:
                continue
            parsed = _parse_body(doc.get("text") or "")
            if parsed.get("authorization_endpoint") and parsed.get("token_endpoint"):
                return parsed, None
    return None, {
        "status": "discovery_failed",
        "message": "no authorization-server metadata could be read",
        "fix": "NVIDIA may have changed discovery; run cuda_docs_status() and report the URL.",
    }


def _register_client(registration_endpoint: str, redirect_uri: str) -> tuple[str, str]:
    """Dynamic client registration. Returns (client_id, error_message)."""
    payload = {
        "client_name": "aither-adk cuda docs pack",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    response = config.http_request(
        "POST",
        registration_endpoint,
        body=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=20.0,
    )
    parsed = _parse_body(response.get("text") or "")
    client_id = str(parsed.get("client_id") or "")
    if response.get("status") in (200, 201) and client_id:
        return client_id, ""
    return "", config.redact(
        f"registration failed (HTTP {response.get('status')}): {response.get('text') or ''}"
    )[:400]


def cuda_docs_login(open_browser: bool = True, wait_seconds: int = 180) -> dict:
    """One-time OAuth authorization-code + PKCE approval; stores a refresh token.

    There is no device grant on this server, so a human must approve once in a
    browser (free NVIDIA Developer account). After that the stored refresh token
    carries headless runs. On a headless box call with ``open_browser=False`` to
    receive the sign-in URL instead of blocking — hand it to the operator rather
    than declaring the lane dead.
    """
    if config._load_store().get("refresh_token") or config.access_token():
        return {
            "status": "already_authenticated",
            "token_file": str(config.TOKEN_FILE),
            "credential_source": config.credential_source(),
        }
    server_meta, error = _authorization_server()
    if error:
        return error
    assert server_meta is not None
    registration_endpoint = str(server_meta.get("registration_endpoint") or "")
    authorization_endpoint = str(server_meta.get("authorization_endpoint") or "")
    token_endpoint = str(server_meta.get("token_endpoint") or "")
    if not (registration_endpoint and authorization_endpoint and token_endpoint):
        return {
            "status": "discovery_failed",
            "message": "authorization server metadata is missing an endpoint",
            "fix": "Report the metadata; the server may have changed its OAuth surface.",
        }

    captured: dict[str, str] = {}

    class _Callback(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — stdlib handler name
            query = urlparse.parse_qs(urlparse.urlsplit(self.path).query)
            captured["code"] = (query.get("code") or [""])[0]
            captured["state"] = (query.get("state") or [""])[0]
            body = b"CUDA docs authorization received. You may close this window."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            return

    listener = HTTPServer(("127.0.0.1", 0), _Callback)
    listener.timeout = 1.0
    redirect_uri = f"http://127.0.0.1:{listener.server_address[1]}/callback"
    try:
        client_id, error_message = _register_client(registration_endpoint, redirect_uri)
        if not client_id:
            return {
                "status": "not_authenticated",
                "error": "registration_failed",
                "message": error_message,
                "fix": config.FIX,
            }
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest())
            .decode()
            .rstrip("=")
        )
        state = secrets.token_urlsafe(24)
        auth_url = authorization_endpoint + "?" + urlparse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
        )
        if not open_browser:
            return {
                "status": "authorization_required",
                "url": auth_url,
                "state": state,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "fix": (
                    "Open the URL, approve with a free NVIDIA Developer account, then "
                    "call cuda_docs_login(open_browser=False, wait_seconds=0) is not "
                    "possible here — rerun login in the process that can receive the "
                    "loopback callback."
                ),
            }
        opened = False
        try:
            opened = bool(webbrowser.open(auth_url))
        except Exception:  # noqa: BLE001 — no browser is a status, not a crash
            opened = False
        if not opened:
            return {
                "status": "authorization_required",
                "url": auth_url,
                "state": state,
                "fix": "No browser could be opened; open the URL yourself.",
            }
        deadline = time.time() + max(1, int(wait_seconds))
        while time.time() < deadline and not captured.get("code"):
            listener.handle_request()
        if not captured.get("code"):
            return {
                "status": "authorization_timeout",
                "message": f"no callback within {wait_seconds}s",
                "url": auth_url,
                "fix": "Re-run cuda_docs_login() and complete the browser approval.",
            }
        if captured.get("state") != state:
            return {
                "status": "authorization_failed",
                "message": "state mismatch; the callback did not match this request",
                "fix": "Re-run cuda_docs_login(); never accept a callback you did not start.",
            }
        form = urlparse.urlencode(
            {
                "grant_type": "authorization_code",
                "code": captured["code"],
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            }
        ).encode("utf-8")
        exchange = config.http_request(
            "POST",
            token_endpoint,
            body=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=20.0,
        )
        tokens = _parse_body(exchange.get("text") or "")
        access = str(tokens.get("access_token") or "")
        if exchange.get("status") != 200 or not access:
            return {
                "status": "not_authenticated",
                "error": "token_exchange_failed",
                "message": config.redact(str(exchange.get("text") or ""))[:300],
                "fix": config.FIX,
            }
        store: dict[str, Any] = {
            "access_token": access,
            "refresh_token": str(tokens.get("refresh_token") or ""),
            "token_endpoint": token_endpoint,
            "client_id": client_id,
        }
        if isinstance(tokens.get("expires_in"), (int, float)):
            store["expires_at"] = time.time() + float(tokens["expires_in"])
        saved = config._save_store(store)
        return {
            "status": "authenticated" if saved else "authenticated_unsaved",
            "token_file": str(config.TOKEN_FILE),
            "has_refresh_token": bool(store["refresh_token"]),
            "fix": "" if saved else "The token could not be written; export NVIDIA_CUDA_MCP_TOKEN.",
        }
    finally:
        listener.server_close()


def cuda_docs_logout() -> dict:
    """Forget the stored CUDA docs credential."""
    removed = config.clear_token()
    return {
        "status": "logged_out" if removed else "error",
        "token_file": str(config.TOKEN_FILE),
        "fix": "" if removed else "Remove the credential file by hand; it is 0600.",
    }
