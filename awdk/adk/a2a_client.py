"""A2A outbound client — sign and invoke remote agent tools over A2A protocol.

Makes signed A2A requests (JSON-RPC 2.0) to remote agents:
  - skills/invoke: call a remote agent's tool, get result back
  - message/send: send a chat message (same as built-in A2A).

Signing uses Ed25519 keypairs stored locally (~/.aither/agent_key.{name}.pem).
Requests carry X-Signature + X-Public-Key headers for authentication.

Usage::

    from adk.a2a_client import invoke_skill, send_message

    # Invoke a tool on a remote agent by name (auto-resolved via mesh)
    result = await invoke_skill("opti-agent", "list_models", {"limit": 5})

    # Or invoke directly by URL
    result = await invoke_skill_at_url(
        invoke_url="http://opti-agent:8080",
        skill="list_models",
        args={"limit": 5},
    )

    # Send a chat message
    task = await send_message("opti-agent", "What's your status?")
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger("adk.a2a_client")


# ─────────────────────────────────────────────────────────────────────────────
# Keypair Management
# ─────────────────────────────────────────────────────────────────────────────


def _keypair_path(agent_name: str) -> Path:
    """Resolve keypair file path for an agent.

    Returns ~/.aither/agent_key.{name}.pem (or override via AITHER_HOME).
    """
    home = (
        os.environ.get("AITHER_HOME")
        or os.environ.get("HOME")
        or os.environ.get("USERPROFILE")
        or "."
    )
    aither_dir = Path(home) / ".aither"
    aither_dir.mkdir(parents=True, exist_ok=True)
    return aither_dir / f"agent_key.{agent_name}.pem"


def load_or_generate_keypair(agent_name: str) -> tuple:
    """Load or generate this agent's Ed25519 keypair.

    Returns (private_key_obj, public_key_hex) where public_key_hex is a 64-char
    hex string.

    - First tries to load existing PEM file at ~/.aither/agent_key.{name}.pem
    - If missing, generates a new Ed25519 keypair and persists it (mode 0o600)
    - Returns the private key object and public key as 64-char hex
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, NoEncryption, PrivateFormat, PublicFormat,
            load_pem_private_key,
        )
    except ImportError as e:
        raise ImportError(
            "cryptography library required for A2A signing. "
            "Install with: pip install cryptography"
        ) from e

    path = _keypair_path(agent_name)

    # Try to load existing key
    if path.exists():
        try:
            pem_bytes = path.read_bytes()
            private_key = load_pem_private_key(pem_bytes, password=None)
            public_hex = private_key.public_key().public_bytes(
                Encoding.Raw, PublicFormat.Raw
            ).hex()
            logger.debug(f"Loaded keypair for agent '{agent_name}' from {path}")
            return private_key, public_hex
        except Exception as e:
            logger.warning(f"Failed to load keypair from {path}: {e}")

    # Generate new keypair
    logger.info(f"Generating new Ed25519 keypair for agent '{agent_name}'")
    private_key = Ed25519PrivateKey.generate()

    # Persist PEM (mode 0o600 = owner-read/write only)
    try:
        pem_bytes = private_key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
        path.write_bytes(pem_bytes)
        path.chmod(0o600)
        logger.info(f"Saved keypair for agent '{agent_name}' to {path} (mode 0o600)")
    except Exception as e:
        logger.warning(f"Failed to persist keypair to {path}: {e}")

    public_hex = private_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    ).hex()
    return private_key, public_hex


# ─────────────────────────────────────────────────────────────────────────────
# Signing
# ─────────────────────────────────────────────────────────────────────────────


def sign_request_body(private_key, body_bytes: bytes) -> str:
    """Sign a request body with Ed25519.

    Args:
        private_key: cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey
        body_bytes: Raw request body (JSON bytes)

    Returns:
        128-char hex string of Ed25519 signature.

    Raises:
        Exception: If signing fails.
    """
    signature_bytes = private_key.sign(body_bytes)
    return signature_bytes.hex()


# ─────────────────────────────────────────────────────────────────────────────
# Outbound A2A Requests
# ─────────────────────────────────────────────────────────────────────────────


async def invoke_skill_at_url(
    invoke_url: str,
    skill: str,
    args: dict | None = None,
    this_agent_name: str = "adk-client",
    timeout: int = 30,
) -> dict:
    """Invoke a remote agent's tool at a specific URL.

    Args:
        invoke_url: Base URL of the remote agent (e.g. "http://localhost:8080")
        skill: Tool name to invoke
        args: Arguments dict for the tool (default {})
        this_agent_name: Name of the calling agent (for keypair lookup)
        timeout: Request timeout in seconds

    Returns:
        {"skill": skill_name, "output": result_data} on success
        {"error": error_message, ...} on failure

    Raises:
        ValueError: If invoke_url is invalid or empty
        httpx.HTTPError: On network errors
        json.JSONDecodeError: On response parse errors
    """
    if not invoke_url or not invoke_url.strip():
        raise ValueError("invoke_url is required and must not be empty")
    if not skill or not skill.strip():
        raise ValueError("skill name is required and must not be empty")

    invoke_url = invoke_url.rstrip("/")
    args = args or {}

    # Load this agent's keypair
    try:
        private_key, public_key_hex = load_or_generate_keypair(this_agent_name)
    except Exception as e:
        return {
            "error": "keypair_load_failed",
            "message": f"Failed to load keypair for '{this_agent_name}': {e}",
        }

    # Build JSON-RPC request with replay protection (ts + nonce)
    request_dict = {
        "jsonrpc": "2.0",
        "method": "skills/invoke",
        "params": {
            "skill": skill,
            "args": args,
        },
        "id": 1,
        "ts": int(time.time()),
        "nonce": os.urandom(16).hex(),
    }

    # Serialize to JSON bytes
    body_json = json.dumps(request_dict, separators=(",", ":"))
    body_bytes = body_json.encode("utf-8")

    # Sign the body
    try:
        signature_hex = sign_request_body(private_key, body_bytes)
    except Exception as e:
        return {
            "error": "signing_failed",
            "message": f"Failed to sign request: {e}",
        }

    # POST to remote agent's /a2a endpoint
    headers = {
        "Content-Type": "application/json",
        "X-Signature": signature_hex,
        "X-Public-Key": public_key_hex,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{invoke_url}/a2a",
                content=body_bytes,  # Use raw bytes to preserve signing
                headers=headers,
            )

        # Parse response JSON
        try:
            resp_dict = response.json()
        except json.JSONDecodeError as e:
            return {
                "error": "invalid_response",
                "message": f"Remote agent returned invalid JSON: {e}",
                "status_code": response.status_code,
                "body": response.text[:200],
            }

        # Handle error response (both JSON-RPC and plain error responses)
        if "error" in resp_dict:
            error_obj = resp_dict.get("error", {})
            # Handle both JSON-RPC error format (dict) and plain error format (string)
            if isinstance(error_obj, dict):
                return {
                    "error": "rpc_error",
                    "code": error_obj.get("code"),
                    "message": error_obj.get("message"),
                }
            else:
                # Plain error string (from trust/auth checks)
                reason = resp_dict.get("reason", "")
                return {
                    "error": "http_error",
                    "status_code": response.status_code,
                    "message": error_obj if isinstance(error_obj, str) else str(error_obj),
                    "reason": reason,
                }

        # Return success result
        if "result" in resp_dict:
            result_obj = resp_dict.get("result", {})
            return {
                "skill": skill,
                "output": result_obj.get("output"),
            }

        return {
            "error": "invalid_response",
            "message": "Response has neither 'result' nor 'error' field",
        }

    except httpx.HTTPStatusError as e:
        return {
            "error": "http_error",
            "status_code": e.response.status_code,
            "message": str(e),
        }
    except httpx.TimeoutException as e:
        return {
            "error": "timeout",
            "message": f"Request timed out after {timeout}s: {e}",
        }
    except httpx.RequestError as e:
        return {
            "error": "request_failed",
            "message": f"Failed to reach {invoke_url}/a2a: {e}",
        }


async def invoke_skill(
    agent_name: str,
    skill: str,
    args: dict | None = None,
    this_agent_name: str = "adk-client",
    timeout: int = 30,
) -> dict:
    """Invoke a remote agent's tool by agent name (mesh-aware).

    Resolves agent_name to invoke_url via mesh_discovery.resolve_agent().

    Args:
        agent_name: Name of remote agent (case-insensitive, resolved via mesh)
        skill: Tool name to invoke
        args: Arguments dict for the tool (default {})
        this_agent_name: Name of the calling agent (for keypair lookup)
        timeout: Request timeout in seconds

    Returns:
        {"skill": skill_name, "output": result_data} on success
        {"error": error_message, ...} on failure (including not found)
    """
    from adk.mesh_discovery import resolve_agent

    # Resolve agent name to AgentRef
    try:
        agent_ref = await resolve_agent(agent_name)
        if agent_ref is None:
            return {
                "error": "agent_not_found",
                "message": f"Agent '{agent_name}' not found in mesh",
            }
        if not agent_ref.invoke_url:
            return {
                "error": "no_invoke_url",
                "message": f"Agent '{agent_name}' has no invoke_url",
            }
    except Exception as e:
        return {
            "error": "discovery_failed",
            "message": f"Failed to discover agent '{agent_name}': {e}",
        }

    # Invoke at resolved URL
    return await invoke_skill_at_url(
        invoke_url=agent_ref.invoke_url,
        skill=skill,
        args=args,
        this_agent_name=this_agent_name,
        timeout=timeout,
    )


async def send_message(
    agent_name: str,
    text: str,
    task_id: str = "",
    this_agent_name: str = "adk-client",
    timeout: int = 60,
) -> dict:
    """Send a chat message to a remote agent.

    Args:
        agent_name: Name of remote agent (case-insensitive, resolved via mesh)
        text: Message text
        task_id: Optional existing task ID to continue (empty = new task)
        this_agent_name: Name of the calling agent (for keypair lookup)
        timeout: Request timeout in seconds

    Returns:
        {"task": {...}, "message": {...}} on success
        {"error": error_message, ...} on failure
    """
    from adk.mesh_discovery import resolve_agent

    # Resolve agent name
    try:
        agent_ref = await resolve_agent(agent_name)
        if agent_ref is None:
            return {
                "error": "agent_not_found",
                "message": f"Agent '{agent_name}' not found in mesh",
            }
        if not agent_ref.invoke_url:
            return {
                "error": "no_invoke_url",
                "message": f"Agent '{agent_name}' has no invoke_url",
            }
    except Exception as e:
        return {
            "error": "discovery_failed",
            "message": f"Failed to discover agent '{agent_name}': {e}",
        }

    invoke_url = agent_ref.invoke_url.rstrip("/")

    # Load keypair
    try:
        private_key, public_key_hex = load_or_generate_keypair(this_agent_name)
    except Exception as e:
        return {
            "error": "keypair_load_failed",
            "message": f"Failed to load keypair for '{this_agent_name}': {e}",
        }

    # Build JSON-RPC request with replay protection (ts + nonce)
    message_dict = {
        "parts": [{"type": "text", "text": text}],
    }
    if task_id:
        message_dict["taskId"] = task_id

    request_dict = {
        "jsonrpc": "2.0",
        "method": "message/send",
        "params": {
            "message": message_dict,
        },
        "id": 1,
        "ts": int(time.time()),
        "nonce": os.urandom(16).hex(),
    }

    # Serialize to JSON bytes
    body_json = json.dumps(request_dict, separators=(",", ":"))
    body_bytes = body_json.encode("utf-8")

    # Sign the body
    try:
        signature_hex = sign_request_body(private_key, body_bytes)
    except Exception as e:
        return {
            "error": "signing_failed",
            "message": f"Failed to sign request: {e}",
        }

    # POST to remote agent
    headers = {
        "Content-Type": "application/json",
        "X-Signature": signature_hex,
        "X-Public-Key": public_key_hex,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{invoke_url}/a2a",
                content=body_bytes,
                headers=headers,
            )

        # Parse response
        try:
            resp_dict = response.json()
        except json.JSONDecodeError as e:
            return {
                "error": "invalid_response",
                "message": f"Remote agent returned invalid JSON: {e}",
                "status_code": response.status_code,
            }

        # Check for error response (both JSON-RPC and plain error responses)
        if "error" in resp_dict:
            error_obj = resp_dict.get("error", {})
            # Handle both JSON-RPC error format (dict) and plain error format (string)
            if isinstance(error_obj, dict):
                return {
                    "error": "rpc_error",
                    "code": error_obj.get("code"),
                    "message": error_obj.get("message"),
                }
            else:
                # Plain error string (from trust/auth checks)
                reason = resp_dict.get("reason", "")
                return {
                    "error": "http_error",
                    "status_code": response.status_code,
                    "message": error_obj if isinstance(error_obj, str) else str(error_obj),
                    "reason": reason,
                }

        # Return result
        if "result" in resp_dict:
            return resp_dict.get("result", {})

        return {
            "error": "invalid_response",
            "message": "Response has neither 'result' nor 'error' field",
        }

    except httpx.HTTPStatusError as e:
        return {
            "error": "http_error",
            "status_code": e.response.status_code,
            "message": str(e),
        }
    except httpx.TimeoutException:
        return {
            "error": "timeout",
            "message": f"Request timed out after {timeout}s",
        }
    except httpx.RequestError as e:
        return {
            "error": "request_failed",
            "message": f"Failed to reach {invoke_url}/a2a: {e}",
        }
