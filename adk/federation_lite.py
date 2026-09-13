"""Lightweight federation client for sovereign ADK agents.

Protocol-compatible with the platform's full federation client, but with
zero AitherOS dependencies.  Uses ``httpx`` (already an ADK core dep) and
optionally ``cryptography`` for Ed25519 request signing.

Usage::

    client = FederationLiteClient(
        hub_url="https://api.aitherium.com",
        api_key="...",
        node_id="acme-a3f8b2c1-7x9k",
        node_id="node-a3f8b2c1-7x9k",
        key_dir=Path(".aither/keys"),
    )
    await client.upsert_agents([{...}])
    await client.heartbeat(status="online", agents=[...])
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("adk.federation_lite")

# Ed25519 support is optional — portal accepts unsigned requests.
_CRYPTO_HAS_PORTAL = False
try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
        load_pem_private_key,
    )
    _CRYPTO_AVAILABLE = True
except ImportError:
    pass


def generate_keypair(key_dir: Path, name: str) -> Path:
    """Generate an Ed25519 keypair and write PEM to *key_dir*/*name*.ed25519.

    Returns the path to the private key file.  Requires ``cryptography``.
    """
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography package is required for keypair generation")

    key_dir.mkdir(parents=True, exist_ok=True)
    key_path = key_dir / f"{name}.ed25519"

    if key_path.exists():
        return key_path

    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    key_path.write_bytes(pem)
    # Best-effort chmod 600 (may fail on Windows)
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    log.info("Generated Ed25519 keypair at %s", key_path)
    return key_path


class FederationLiteClient:
    """Lightweight portal federation client for sovereign ADK agents."""

    def __init__(
        self,
        hub_url: str = "https://api.aitherium.com",
        api_key: Optional[str] = None,
        node_id: Optional[str] = None,
        key_dir: Optional[Path] = None,
        key_name: Optional[str] = None,
    ):
        self.hub_url = hub_url.rstrip("/")
        self.api_key = api_key or os.environ.get("AITHER_PORTAL_TOKEN", "")
        self.node_id = node_id or ""
        self._private_key_bytes: Optional[bytes] = None
        self._public_key_hex: Optional[str] = None

        if key_dir and key_name:
            self._load_identity(key_dir, key_name)

    # -- Identity / signing ---------------------------------------------------

    def _load_identity(self, key_dir: Path, key_name: str) -> None:
        """Load an Ed25519 private key from *key_dir*/*key_name*.ed25519."""
        if not _CRYPTO_AVAILABLE:
            log.debug("cryptography not available — Ed25519 signing disabled")
            return

        key_path = key_dir / f"{key_name}.ed25519"
        if not key_path.is_file():
            log.debug("No key file at %s — signing disabled", key_path)
            return

        try:
            raw = key_path.read_bytes()
            key = load_pem_private_key(raw, password=None)
            pub_bytes = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            self._private_key_bytes = raw
            self._public_key_hex = pub_bytes.hex()
            if not self.node_id:
                self.node_id = hashlib.sha256(pub_bytes).hexdigest()[:16]
            log.debug("Loaded federation identity: node_id=%s", self.node_id)
        except Exception as e:
            log.warning("Failed to load key from %s: %s", key_path, e)

    def _sign_payload(self, payload: bytes) -> Optional[str]:
        """Sign *payload* with the loaded Ed25519 private key (hex-encoded)."""
        if not self._private_key_bytes or not _CRYPTO_AVAILABLE:
            return None
        try:
            key = load_pem_private_key(self._private_key_bytes, password=None)
            signature = key.sign(payload)
            return signature.hex()
        except Exception as e:
            log.warning("Failed to sign payload: %s", e)
            return None

    def _build_headers(self, body: Optional[bytes] = None) -> Dict[str, str]:
        """Build request headers matching FederationClient protocol."""
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "X-Node-ID": self.node_id or "unknown",
            "X-Timestamp": str(int(time.time())),
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if body and self._private_key_bytes:
            sig = self._sign_payload(body)
            if sig:
                headers["X-Signature"] = sig
                headers["X-Public-Key"] = self._public_key_hex or ""
        return headers

    # -- HTTP -----------------------------------------------------------------

    async def _request(
        self, method: str, path: str, data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Make an authenticated request to the hub."""
        url = f"{self.hub_url}{path}"
        body = json.dumps(data).encode() if data else None
        headers = self._build_headers(body)

        async with httpx.AsyncClient(timeout=30, verify=True) as client:
            if method == "GET":
                resp = await client.get(url, headers=headers)
            elif method == "POST":
                resp = await client.post(url, content=body, headers=headers)
            else:
                resp = await client.request(method, url, content=body, headers=headers)

            if resp.status_code >= 400:
                log.warning("Hub %s %s returned %s: %s", method, path, resp.status_code, resp.text[:200])
                return {"error": True, "status": resp.status_code, "detail": resp.text[:500]}
            try:
                return resp.json()
            except Exception:
                return {"ok": True, "raw": resp.text[:500]}

    # -- Public API -----------------------------------------------------------

    async def register(self, tenant_slug: str) -> Dict[str, Any]:
        """Register this node with the hub (POST /federation/register)."""
        data = {
            "tenant_slug": tenant_slug,
            "node_id": self.node_id,
            "public_key": self._public_key_hex,
            "timestamp": int(time.time()),
        }
        result = await self._request("POST", "/federation/register", data)
        if not result.get("error"):
            if result.get("api_key"):
                self.api_key = result["api_key"]
            if result.get("node_id"):
                self.node_id = result["node_id"]
        return result

    async def heartbeat(
        self,
        status: str = "online",
        metrics: Optional[Dict[str, Any]] = None,
        agents: Optional[List[Dict[str, Any]]] = None,
        addons: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Send heartbeat + agent list + addon inventory to hub."""
        data: Dict[str, Any] = {
            "node_id": self.node_id,
            "status": status,
            "timestamp": int(time.time()),
            "metrics": metrics or {},
        }
        if agents:
            data["agents"] = agents
        if addons:
            data["addons"] = addons
        return await self._request("POST", "/federation/heartbeat", data)

    async def upsert_agents(
        self, agents: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Bulk-upsert hosted agents (POST /v1/agents/upsert-batch)."""
        if not agents:
            return {"ok": True, "count": 0, "skipped": True}
        data = {
            "node_id": self.node_id,
            "timestamp": int(time.time()),
            "agents": agents,
        }
        return await self._request("POST", "/v1/agents/upsert-batch", data)

    async def report_addon_usage(
        self, addon_id: str, events: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Batch-report addon usage events to hub (POST /federation/addon-usage)."""
        data = {
            "node_id": self.node_id,
            "addon_id": addon_id,
            "events": events,
            "timestamp": int(time.time()),
        }
        return await self._request("POST", "/federation/addon-usage", data)

    async def pull_addon_secrets(self, addon_id: str) -> Dict[str, Any]:
        """Pull secrets for a specific addon (GET /federation/addon-secrets/{id})."""
        return await self._request("GET", f"/federation/addon-secrets/{addon_id}")

    async def discover_remote_agents(
        self,
        tenant_id: str = "",
        capabilities_filter: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Discover online agents in the federation (POST /v1/agents/discover)."""
        params: Dict[str, Any] = {"node_id": self.node_id}
        if tenant_id:
            params["tenant_id"] = tenant_id
        if capabilities_filter:
            params["capabilities"] = ",".join(capabilities_filter)
        return await self._request("POST", "/v1/agents/discover", params)
