"""Strata — unified storage and telemetry for AitherOS agents.

Two concerns live here:

1. **StrataIngest** — fire-and-forget session telemetry to AitherOS Strata
   service (port 8136). Opt-in via AITHER_STRATA_URL.

2. **Strata** — unified storage abstraction over multiple backends:
   - local: ~/.aither/strata/ filesystem (default, always available)
   - s3: S3-compatible (MinIO, AWS S3, R2) — when configured
   - aitheros: full AitherOS Strata service at port 8136 — when running

Usage (storage):
    from adk.strata import get_strata

    strata = get_strata()

    # Write — resolves to best available backend
    await strata.write("codegraph/index.json", data)

    # Read — checks local first, then networked
    data = await strata.read("codegraph/index.json")

    # Namespaced by tenant
    await strata.write("tenant:acme/training/data.jsonl", payload)

    # List
    files = await strata.list("codegraph/")

    # Delete
    await strata.delete("codegraph/old_index.json")
"""

from __future__ import annotations

import abc
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("adk.strata")

# Default Strata URL — only used if AITHER_STRATA_URL is set or localhost is reachable
_DEFAULT_STRATA_URL = "http://localhost:8136"
_QUEUE_MAX_LINES = 2000


class StrataIngest:
    """Fire-and-forget ingest client for AitherOS Strata."""

    def __init__(
        self,
        strata_url: str = "",
        data_dir: str | Path | None = None,
    ):
        self.strata_url = (
            strata_url
            or os.getenv("AITHER_STRATA_URL", "")
        ).rstrip("/")
        self._data_dir = Path(
            data_dir or os.getenv("AITHER_DATA_DIR", os.path.expanduser("~/.aither"))
        )
        self._queue_path = self._data_dir / "strata_queue.jsonl"
        self._enabled: bool | None = None  # lazy — checked on first send

    @property
    def enabled(self) -> bool:
        """Check if Strata ingest is active (URL configured)."""
        if self._enabled is not None:
            return self._enabled
        self._enabled = bool(self.strata_url)
        return self._enabled

    async def ingest_chat(
        self,
        *,
        agent: str,
        session_id: str,
        user_message: str,
        assistant_response: str,
        model: str = "",
        tokens_used: int = 0,
        latency_ms: int = 0,
        tool_calls: list[str] | None = None,
    ) -> bool:
        """Send a chat exchange to Strata. Returns True if sent successfully.

        This is fire-and-forget — never raises, never blocks the chat response.
        """
        if not self.enabled:
            return False

        payload = {
            "source": "adk",
            "type": "chat_exchange",
            "timestamp": time.time(),
            "agent": agent,
            "session_id": session_id,
            "user_message": user_message,
            "assistant_response": assistant_response,
            "model": model,
            "tokens_used": tokens_used,
            "latency_ms": latency_ms,
            "tool_calls": tool_calls or [],
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{self.strata_url}/api/v1/ingest/adk-session",
                    json=payload,
                )
                if resp.status_code < 300:
                    return True
                logger.debug("Strata ingest returned %d", resp.status_code)
        except Exception as exc:
            logger.debug("Strata ingest failed, queuing offline: %s", exc)

        self._queue_offline(payload)
        return False

    async def ingest_session_end(
        self,
        *,
        agent: str,
        session_id: str,
        message_count: int = 0,
        total_tokens: int = 0,
        duration_seconds: float = 0.0,
    ) -> bool:
        """Notify Strata that a session has ended."""
        if not self.enabled:
            return False

        payload = {
            "source": "adk",
            "type": "session_end",
            "timestamp": time.time(),
            "agent": agent,
            "session_id": session_id,
            "message_count": message_count,
            "total_tokens": total_tokens,
            "duration_seconds": duration_seconds,
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{self.strata_url}/api/v1/ingest/adk-session",
                    json=payload,
                )
                return resp.status_code < 300
        except Exception:
            self._queue_offline(payload)
            return False

    def _queue_offline(self, payload: dict):
        """Write to JSONL disk queue for retry when Strata comes back."""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with open(self._queue_path, "a") as f:
                f.write(json.dumps(payload) + "\n")
            _cap_jsonl(self._queue_path, _QUEUE_MAX_LINES)
        except Exception as exc:
            logger.debug("Failed to queue Strata data: %s", exc)

    async def flush_queue(self) -> int:
        """Try to send queued entries to Strata. Returns count of successfully sent."""
        if not self._queue_path.exists() or not self.enabled:
            return 0

        try:
            lines = self._queue_path.read_text().strip().split("\n")
        except Exception:
            return 0

        if not lines or lines == [""]:
            return 0

        sent = 0
        remaining = []

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                for line in lines:
                    try:
                        payload = json.loads(line)
                        resp = await client.post(
                            f"{self.strata_url}/api/v1/ingest/adk-session",
                            json=payload,
                        )
                        if resp.status_code < 300:
                            sent += 1
                        else:
                            remaining.append(line)
                    except Exception:
                        remaining.append(line)
        except Exception:
            remaining = lines

        if remaining:
            self._queue_path.write_text("\n".join(remaining) + "\n")
        else:
            self._queue_path.unlink(missing_ok=True)

        return sent


# Module-level singleton
_instance: StrataIngest | None = None


def get_strata_ingest() -> StrataIngest:
    """Get or create the module-level StrataIngest singleton."""
    global _instance
    if _instance is None:
        _instance = StrataIngest()
    return _instance


def _cap_jsonl(path: Path, max_lines: int):
    """Cap a JSONL file at max_lines, keeping the newest."""
    try:
        lines = path.read_text().strip().split("\n")
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Unified Storage Abstraction
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_TENANT = "default"


@dataclass
class StrataEntry:
    """Metadata about a stored object."""
    key: str
    size: int = 0
    content_type: str = "application/octet-stream"
    tenant: str = _DEFAULT_TENANT
    backend: str = "local"
    modified_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


def parse_path(raw_path: str, default_tenant: str = _DEFAULT_TENANT) -> tuple[str, str]:
    """Parse a Strata path into (tenant, relative_path).

    Supports ``tenant:name/path`` syntax. If no tenant prefix is present,
    uses *default_tenant*.

    Args:
        raw_path: The path string, optionally prefixed with ``tenant:name/``.
        default_tenant: Tenant to use when no prefix is found.

    Returns:
        Tuple of (tenant, path) with leading/trailing slashes stripped.

    Raises:
        ValueError: If raw_path is empty.
    """
    if not raw_path or not raw_path.strip():
        raise ValueError("Strata path must not be empty")

    raw_path = raw_path.strip()

    if raw_path.startswith("tenant:"):
        rest = raw_path[len("tenant:"):]
        if "/" in rest:
            tenant, path = rest.split("/", 1)
        else:
            tenant = rest
            path = ""
        tenant = tenant.strip()
        path = path.strip("/")
        if not tenant:
            tenant = default_tenant
        return (tenant, path)

    return (default_tenant, raw_path.strip("/"))


class StrataBackend(abc.ABC):
    """Abstract base class for Strata storage backends.

    All methods are async and must not raise on operational failures --
    they return ``None`` for reads that fail and ``False`` for writes
    that fail, letting the Strata router handle fallback.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Short identifier for this backend (e.g. 'local', 's3', 'aitheros')."""

    @abc.abstractmethod
    async def read(self, tenant: str, path: str) -> bytes | None:
        """Read raw bytes for *path* under *tenant*. Returns None if not found."""

    @abc.abstractmethod
    async def write(self, tenant: str, path: str, data: bytes | str) -> bool:
        """Write *data* to *path* under *tenant*. Returns True on success."""

    @abc.abstractmethod
    async def delete(self, tenant: str, path: str) -> bool:
        """Delete *path* under *tenant*. Returns True on success."""

    @abc.abstractmethod
    async def exists(self, tenant: str, path: str) -> bool:
        """Check if *path* exists under *tenant*."""

    @abc.abstractmethod
    async def list(self, tenant: str, prefix: str = "") -> list[str]:
        """List keys under *tenant* matching *prefix*."""


class LocalBackend(StrataBackend):
    """Filesystem-backed storage under ``~/.aither/strata/``.

    Always available. This is the default backend and the fallback
    when networked backends are unreachable.
    """

    def __init__(self, base_dir: str | Path | None = None):
        self._base = Path(
            base_dir
            or os.getenv("AITHER_DATA_DIR", os.path.expanduser("~/.aither"))
        ) / "strata"

    @property
    def name(self) -> str:
        return "local"

    @property
    def base_dir(self) -> Path:
        """Return the resolved base directory for inspection/testing."""
        return self._base

    def _resolve(self, tenant: str, path: str) -> Path:
        """Resolve to an absolute filesystem path.

        Normalises the path and prevents directory-traversal attacks by
        ensuring the resolved path stays within the base directory.
        """
        resolved = (self._base / tenant / path).resolve()
        base_resolved = self._base.resolve()
        if not str(resolved).startswith(str(base_resolved)):
            raise ValueError(f"Path traversal detected: {path}")
        return resolved

    async def read(self, tenant: str, path: str) -> bytes | None:
        try:
            fp = self._resolve(tenant, path)
            if not fp.exists() or not fp.is_file():
                return None
            return fp.read_bytes()
        except Exception as exc:
            logger.debug("LocalBackend read failed for %s/%s: %s", tenant, path, exc)
            return None

    async def write(self, tenant: str, path: str, data: bytes | str) -> bool:
        try:
            fp = self._resolve(tenant, path)
            fp.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(data, str):
                fp.write_text(data, encoding="utf-8")
            else:
                fp.write_bytes(data)
            return True
        except Exception as exc:
            logger.debug("LocalBackend write failed for %s/%s: %s", tenant, path, exc)
            return False

    async def delete(self, tenant: str, path: str) -> bool:
        try:
            fp = self._resolve(tenant, path)
            if fp.exists():
                fp.unlink()
                return True
            return False
        except Exception as exc:
            logger.debug("LocalBackend delete failed for %s/%s: %s", tenant, path, exc)
            return False

    async def exists(self, tenant: str, path: str) -> bool:
        try:
            fp = self._resolve(tenant, path)
            return fp.exists() and fp.is_file()
        except Exception:
            return False

    async def list(self, tenant: str, prefix: str = "") -> list[str]:
        try:
            base = self._resolve(tenant, prefix) if prefix else (self._base / tenant)
            if not base.exists():
                return []
            # If prefix points to a directory, list its contents recursively
            if base.is_dir():
                scan_root = base
            else:
                # Prefix might be a partial filename — list parent and filter
                scan_root = base.parent
            tenant_root = (self._base / tenant).resolve()
            results = []
            for fp in scan_root.rglob("*"):
                if fp.is_file():
                    rel = fp.resolve().relative_to(tenant_root)
                    rel_str = str(rel).replace("\\", "/")
                    if not prefix or rel_str.startswith(prefix.rstrip("/")):
                        results.append(rel_str)
            return sorted(results)
        except Exception as exc:
            logger.debug("LocalBackend list failed for %s/%s: %s", tenant, prefix, exc)
            return []


class S3Backend(StrataBackend):
    """S3-compatible storage backend.

    Activated when AITHER_S3_BUCKET is set. Currently a configuration-aware
    stub -- detects S3 config and logs availability, but delegates actual
    storage to the local backend until a full S3 implementation is needed.

    Configuration via environment variables:
    - AITHER_S3_BUCKET: Bucket name (required to activate)
    - AITHER_S3_ENDPOINT: Endpoint URL (for MinIO, R2, etc.)
    - AITHER_S3_KEY: Access key
    - AITHER_S3_SECRET: Secret key
    - AITHER_S3_REGION: Region (default: us-east-1)
    """

    def __init__(self):
        self.bucket = os.getenv("AITHER_S3_BUCKET", "")
        self.endpoint = os.getenv("AITHER_S3_ENDPOINT", "")
        self.access_key = os.getenv("AITHER_S3_KEY", "")
        self.secret_key = os.getenv("AITHER_S3_SECRET", "")
        self.region = os.getenv("AITHER_S3_REGION", "us-east-1")
        self._available: bool | None = None

    @property
    def name(self) -> str:
        return "s3"

    @property
    def configured(self) -> bool:
        """True if S3 environment variables are set."""
        return bool(self.bucket)

    async def read(self, tenant: str, path: str) -> bytes | None:
        if not self.configured:
            return None
        logger.debug("S3Backend read: s3://%s/%s/%s (stub — not implemented)", self.bucket, tenant, path)
        return None

    async def write(self, tenant: str, path: str, data: bytes | str) -> bool:
        if not self.configured:
            return False
        logger.debug("S3Backend write: s3://%s/%s/%s (stub — not implemented)", self.bucket, tenant, path)
        return False

    async def delete(self, tenant: str, path: str) -> bool:
        if not self.configured:
            return False
        logger.debug("S3Backend delete: s3://%s/%s/%s (stub — not implemented)", self.bucket, tenant, path)
        return False

    async def exists(self, tenant: str, path: str) -> bool:
        if not self.configured:
            return False
        return False

    async def list(self, tenant: str, prefix: str = "") -> list[str]:
        if not self.configured:
            return []
        return []


class AitherOSBackend(StrataBackend):
    """Proxy to the full AitherOS Strata service at port 8136.

    Only activated when the Strata service is reachable. Uses HTTP API
    for all operations, falling back gracefully when the service is down.
    """

    def __init__(self, strata_url: str = ""):
        self._url = (
            strata_url
            or os.getenv("AITHER_STRATA_URL", "")
        ).rstrip("/")
        self._available: bool | None = None

    @property
    def name(self) -> str:
        return "aitheros"

    @property
    def configured(self) -> bool:
        """True if the Strata URL is set."""
        return bool(self._url)

    async def _check_available(self) -> bool:
        """Probe the Strata health endpoint once, then cache."""
        if self._available is not None:
            return self._available
        if not self._url:
            self._available = False
            return False
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self._url}/health")
                self._available = resp.status_code == 200
        except Exception:
            self._available = False
        if self._available:
            logger.info("AitherOS Strata available at %s", self._url)
        return self._available

    async def read(self, tenant: str, path: str) -> bytes | None:
        if not await self._check_available():
            return None
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self._url}/api/v1/storage/{tenant}/{path}",
                )
                if resp.status_code == 200:
                    return resp.content
                return None
        except Exception as exc:
            logger.debug("AitherOSBackend read failed: %s", exc)
            return None

    async def write(self, tenant: str, path: str, data: bytes | str) -> bool:
        if not await self._check_available():
            return False
        try:
            raw = data.encode("utf-8") if isinstance(data, str) else data
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.put(
                    f"{self._url}/api/v1/storage/{tenant}/{path}",
                    content=raw,
                    headers={"Content-Type": "application/octet-stream"},
                )
                return resp.status_code < 300
        except Exception as exc:
            logger.debug("AitherOSBackend write failed: %s", exc)
            return False

    async def delete(self, tenant: str, path: str) -> bool:
        if not await self._check_available():
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.delete(
                    f"{self._url}/api/v1/storage/{tenant}/{path}",
                )
                return resp.status_code < 300
        except Exception as exc:
            logger.debug("AitherOSBackend delete failed: %s", exc)
            return False

    async def exists(self, tenant: str, path: str) -> bool:
        if not await self._check_available():
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.head(
                    f"{self._url}/api/v1/storage/{tenant}/{path}",
                )
                return resp.status_code == 200
        except Exception:
            return False

    async def list(self, tenant: str, prefix: str = "") -> list[str]:
        if not await self._check_available():
            return []
        try:
            params = {"prefix": prefix} if prefix else {}
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self._url}/api/v1/storage/{tenant}/",
                    params=params,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("keys", data.get("files", []))
                return []
        except Exception as exc:
            logger.debug("AitherOSBackend list failed: %s", exc)
            return []


class Strata:
    """Unified storage API over multiple backends.

    Backends form a priority chain: AitherOS > S3 > Local.
    Writes go to the primary (highest-priority available) backend and
    also to local for caching. Reads check local first, then walk the
    chain upward.

    Args:
        default_tenant: Default tenant namespace for paths without a
            ``tenant:name/`` prefix.
        backends: Override the default backend list. If not provided,
            auto-detects which backends are available.
        data_dir: Override the base data directory (default: AITHER_DATA_DIR
            or ``~/.aither``).
        strata_url: Override the AitherOS Strata URL.
    """

    def __init__(
        self,
        default_tenant: str = _DEFAULT_TENANT,
        backends: list[StrataBackend] | None = None,
        data_dir: str | Path | None = None,
        strata_url: str = "",
    ):
        self._default_tenant = default_tenant
        self._local = LocalBackend(base_dir=data_dir)

        if backends is not None:
            self._backends = list(backends)
        else:
            self._backends = self._auto_detect_backends(strata_url, data_dir)

        # Ensure local is always in the list
        has_local = any(b.name == "local" for b in self._backends)
        if not has_local:
            self._backends.append(self._local)

    def _auto_detect_backends(
        self, strata_url: str, data_dir: str | Path | None
    ) -> list[StrataBackend]:
        """Build the backend chain based on environment configuration."""
        backends: list[StrataBackend] = []

        # AitherOS Strata (highest priority)
        aitheros = AitherOSBackend(strata_url=strata_url)
        if aitheros.configured:
            backends.append(aitheros)

        # S3 (medium priority)
        s3 = S3Backend()
        if s3.configured:
            backends.append(s3)

        # Local (always present, lowest priority)
        backends.append(self._local)
        return backends

    @property
    def backends(self) -> list[StrataBackend]:
        """Return the ordered list of backends (highest priority first)."""
        return list(self._backends)

    @property
    def default_tenant(self) -> str:
        """Return the default tenant namespace."""
        return self._default_tenant

    def _parse(self, raw_path: str) -> tuple[str, str]:
        """Parse a raw path into (tenant, path)."""
        return parse_path(raw_path, default_tenant=self._default_tenant)

    async def write(self, path: str, data: bytes | str, **kwargs: Any) -> bool:
        """Write data to the best available backend.

        Writes to the primary backend first, then to local for caching
        (if primary is not local). Returns True if at least one backend
        succeeded.

        Args:
            path: Storage key, optionally prefixed with ``tenant:name/``.
            data: Raw bytes or string to store.

        Returns:
            True if at least one backend wrote successfully.
        """
        tenant, key = self._parse(path)
        if not key:
            raise ValueError("Storage path must include at least a filename")

        success = False
        wrote_to_local = False

        for backend in self._backends:
            try:
                result = await backend.write(tenant, key, data)
                if result:
                    success = True
                    if backend.name == "local":
                        wrote_to_local = True
                    logger.debug("Wrote %s/%s to %s", tenant, key, backend.name)
                    break  # Primary write succeeded
            except Exception as exc:
                logger.debug("Backend %s write failed, trying next: %s", backend.name, exc)

        # Cache locally if primary was not local
        if success and not wrote_to_local:
            try:
                await self._local.write(tenant, key, data)
            except Exception:
                pass  # Non-fatal — local cache is best-effort

        # If nothing worked, try local as last resort
        if not success:
            try:
                result = await self._local.write(tenant, key, data)
                success = result
            except Exception:
                pass

        return success

    async def read(self, path: str, **kwargs: Any) -> bytes | None:
        """Read data, checking backends in order.

        Checks local first (fast), then walks the priority chain.

        Args:
            path: Storage key, optionally prefixed with ``tenant:name/``.

        Returns:
            Raw bytes if found, None otherwise.
        """
        tenant, key = self._parse(path)
        if not key:
            return None

        # Check local first for cache hit
        try:
            local_data = await self._local.read(tenant, key)
            if local_data is not None:
                return local_data
        except Exception:
            pass

        # Walk the chain for networked backends
        for backend in self._backends:
            if backend.name == "local":
                continue  # Already checked
            try:
                data = await backend.read(tenant, key)
                if data is not None:
                    # Cache locally for next time
                    try:
                        await self._local.write(tenant, key, data)
                    except Exception:
                        pass
                    return data
            except Exception as exc:
                logger.debug("Backend %s read failed, trying next: %s", backend.name, exc)

        return None

    async def read_text(self, path: str, encoding: str = "utf-8", **kwargs: Any) -> str | None:
        """Read data as a text string.

        Convenience wrapper around :meth:`read` that decodes bytes.

        Args:
            path: Storage key.
            encoding: Text encoding (default: utf-8).

        Returns:
            Decoded string if found, None otherwise.
        """
        data = await self.read(path, **kwargs)
        if data is None:
            return None
        return data.decode(encoding)

    async def write_json(self, path: str, obj: Any, **kwargs: Any) -> bool:
        """Write a JSON-serializable object.

        Args:
            path: Storage key.
            obj: Object to serialize as JSON.

        Returns:
            True if write succeeded.
        """
        data = json.dumps(obj, indent=2, default=str)
        return await self.write(path, data, **kwargs)

    async def read_json(self, path: str, **kwargs: Any) -> Any | None:
        """Read and parse JSON data.

        Args:
            path: Storage key.

        Returns:
            Parsed JSON object if found, None otherwise.
        """
        text = await self.read_text(path, **kwargs)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            logger.debug("Failed to parse JSON from %s: %s", path, exc)
            return None

    async def delete(self, path: str, **kwargs: Any) -> bool:
        """Delete data from all backends.

        Args:
            path: Storage key, optionally prefixed with ``tenant:name/``.

        Returns:
            True if at least one backend deleted successfully.
        """
        tenant, key = self._parse(path)
        if not key:
            return False

        success = False
        for backend in self._backends:
            try:
                result = await backend.delete(tenant, key)
                if result:
                    success = True
            except Exception as exc:
                logger.debug("Backend %s delete failed: %s", backend.name, exc)

        return success

    async def exists(self, path: str, **kwargs: Any) -> bool:
        """Check if a key exists in any backend.

        Args:
            path: Storage key, optionally prefixed with ``tenant:name/``.

        Returns:
            True if the key exists in at least one backend.
        """
        tenant, key = self._parse(path)
        if not key:
            return False

        for backend in self._backends:
            try:
                if await backend.exists(tenant, key):
                    return True
            except Exception:
                pass
        return False

    async def list(self, prefix: str = "", tenant: str = "", **kwargs: Any) -> list[str]:
        """List keys matching a prefix.

        Merges results from all backends and deduplicates.

        Args:
            prefix: Key prefix to filter by.
            tenant: Tenant namespace. If empty, uses default tenant.

        Returns:
            Sorted list of unique keys.
        """
        effective_tenant = tenant or self._default_tenant

        # If prefix has tenant: syntax, parse it
        if prefix.startswith("tenant:"):
            effective_tenant, prefix = parse_path(prefix, self._default_tenant)

        all_keys: set[str] = set()
        for backend in self._backends:
            try:
                keys = await backend.list(effective_tenant, prefix)
                all_keys.update(keys)
            except Exception as exc:
                logger.debug("Backend %s list failed: %s", backend.name, exc)

        return sorted(all_keys)

    async def stats(self) -> dict[str, Any]:
        """Return storage statistics.

        Returns:
            Dict with backend info, availability, and basic counts.
        """
        info: dict[str, Any] = {
            "default_tenant": self._default_tenant,
            "backends": [],
        }
        for b in self._backends:
            entry: dict[str, Any] = {"name": b.name}
            if hasattr(b, "configured"):
                entry["configured"] = b.configured
            if hasattr(b, "base_dir"):
                entry["base_dir"] = str(b.base_dir)
            info["backends"].append(entry)
        return info


# ─────────────────────────────────────────────────────────────────────────────
# Strata singleton
# ─────────────────────────────────────────────────────────────────────────────

_strata_instance: Strata | None = None


def get_strata(
    default_tenant: str = _DEFAULT_TENANT,
    data_dir: str | Path | None = None,
    strata_url: str = "",
) -> Strata:
    """Get or create the module-level Strata singleton.

    Args:
        default_tenant: Default tenant namespace.
        data_dir: Override the base data directory.
        strata_url: Override the AitherOS Strata URL.

    Returns:
        The global Strata instance.
    """
    global _strata_instance
    if _strata_instance is None:
        _strata_instance = Strata(
            default_tenant=default_tenant,
            data_dir=data_dir,
            strata_url=strata_url,
        )
    return _strata_instance
