"""Tests for adk.strata — unified storage abstraction.

Covers LocalBackend, S3Backend (stub), AitherOSBackend, Strata router,
path parsing, fallback chains, singleton, and agent integration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adk.strata import (
    AitherOSBackend,
    LocalBackend,
    S3Backend,
    Strata,
    StrataBackend,
    StrataEntry,
    get_strata,
    parse_path,
    _DEFAULT_TENANT,
)


# ─────────────────────────────────────────────────────────────────────────────
# Path Parsing
# ─────────────────────────────────────────────────────────────────────────────


class TestParsePath:
    def test_simple_path_uses_default_tenant(self):
        tenant, path = parse_path("codegraph/index.json")
        assert tenant == _DEFAULT_TENANT
        assert path == "codegraph/index.json"

    def test_tenant_prefix(self):
        tenant, path = parse_path("tenant:acme/training/data.jsonl")
        assert tenant == "acme"
        assert path == "training/data.jsonl"

    def test_tenant_prefix_no_subpath(self):
        tenant, path = parse_path("tenant:acme")
        assert tenant == "acme"
        assert path == ""

    def test_tenant_prefix_with_slash_only(self):
        tenant, path = parse_path("tenant:acme/")
        assert tenant == "acme"
        assert path == ""

    def test_custom_default_tenant(self):
        tenant, path = parse_path("myfile.txt", default_tenant="org42")
        assert tenant == "org42"
        assert path == "myfile.txt"

    def test_empty_path_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            parse_path("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            parse_path("   ")

    def test_strips_leading_trailing_slashes(self):
        tenant, path = parse_path("/codegraph/index.json/")
        assert path == "codegraph/index.json"

    def test_tenant_empty_after_prefix_uses_default(self):
        tenant, path = parse_path("tenant:/some/path")
        assert tenant == _DEFAULT_TENANT
        assert path == "some/path"

    def test_nested_path(self):
        tenant, path = parse_path("tenant:prod/a/b/c/d/e.json")
        assert tenant == "prod"
        assert path == "a/b/c/d/e.json"


# ─────────────────────────────────────────────────────────────────────────────
# LocalBackend
# ─────────────────────────────────────────────────────────────────────────────


class TestLocalBackend:
    @pytest.fixture
    def backend(self, tmp_path):
        return LocalBackend(base_dir=tmp_path)

    @pytest.mark.asyncio
    async def test_write_and_read_bytes(self, backend):
        data = b"hello world"
        assert await backend.write("default", "test.bin", data) is True
        result = await backend.read("default", "test.bin")
        assert result == data

    @pytest.mark.asyncio
    async def test_write_and_read_string(self, backend):
        data = "hello world"
        assert await backend.write("default", "test.txt", data) is True
        result = await backend.read("default", "test.txt")
        assert result == b"hello world"

    @pytest.mark.asyncio
    async def test_read_nonexistent_returns_none(self, backend):
        result = await backend.read("default", "nope.txt")
        assert result is None

    @pytest.mark.asyncio
    async def test_exists_true(self, backend):
        await backend.write("default", "exists.txt", "yes")
        assert await backend.exists("default", "exists.txt") is True

    @pytest.mark.asyncio
    async def test_exists_false(self, backend):
        assert await backend.exists("default", "nope.txt") is False

    @pytest.mark.asyncio
    async def test_delete(self, backend):
        await backend.write("default", "todelete.txt", "bye")
        assert await backend.exists("default", "todelete.txt") is True
        result = await backend.delete("default", "todelete.txt")
        assert result is True
        assert await backend.exists("default", "todelete.txt") is False

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self, backend):
        result = await backend.delete("default", "nope.txt")
        assert result is False

    @pytest.mark.asyncio
    async def test_list_empty(self, backend):
        result = await backend.list("default")
        assert result == []

    @pytest.mark.asyncio
    async def test_list_files(self, backend):
        await backend.write("default", "a.txt", "a")
        await backend.write("default", "b.txt", "b")
        await backend.write("default", "sub/c.txt", "c")
        result = await backend.list("default")
        assert "a.txt" in result
        assert "b.txt" in result
        assert "sub/c.txt" in result

    @pytest.mark.asyncio
    async def test_list_with_prefix(self, backend):
        await backend.write("default", "codegraph/a.json", "{}")
        await backend.write("default", "codegraph/b.json", "{}")
        await backend.write("default", "other/c.json", "{}")
        result = await backend.list("default", prefix="codegraph/")
        assert len(result) == 2
        assert all(r.startswith("codegraph/") for r in result)

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, backend):
        await backend.write("tenant_a", "file.txt", "a")
        await backend.write("tenant_b", "file.txt", "b")
        a = await backend.read("tenant_a", "file.txt")
        b = await backend.read("tenant_b", "file.txt")
        assert a == b"a"
        assert b == b"b"

    @pytest.mark.asyncio
    async def test_nested_directories(self, backend):
        await backend.write("default", "a/b/c/deep.json", '{"deep": true}')
        result = await backend.read("default", "a/b/c/deep.json")
        assert result == b'{"deep": true}'

    @pytest.mark.asyncio
    async def test_overwrite(self, backend):
        await backend.write("default", "file.txt", "v1")
        await backend.write("default", "file.txt", "v2")
        result = await backend.read("default", "file.txt")
        assert result == b"v2"

    @pytest.mark.asyncio
    async def test_name(self, backend):
        assert backend.name == "local"

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self, backend):
        result = await backend.write("default", "../../etc/passwd", "evil")
        assert result is False

    @pytest.mark.asyncio
    async def test_base_dir_property(self, tmp_path):
        backend = LocalBackend(base_dir=tmp_path)
        assert backend.base_dir == tmp_path / "strata"


# ─────────────────────────────────────────────────────────────────────────────
# S3Backend (stub)
# ─────────────────────────────────────────────────────────────────────────────


class TestS3Backend:
    def test_not_configured_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            for k in ("AITHER_S3_BUCKET", "AITHER_S3_ENDPOINT", "AITHER_S3_KEY", "AITHER_S3_SECRET"):
                os.environ.pop(k, None)
            backend = S3Backend()
            assert backend.configured is False

    def test_configured_when_bucket_set(self):
        with patch.dict(os.environ, {"AITHER_S3_BUCKET": "my-bucket"}):
            backend = S3Backend()
            assert backend.configured is True
            assert backend.bucket == "my-bucket"

    @pytest.mark.asyncio
    async def test_read_returns_none_when_not_configured(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AITHER_S3_BUCKET", None)
            backend = S3Backend()
            result = await backend.read("default", "file.txt")
            assert result is None

    @pytest.mark.asyncio
    async def test_write_returns_false_when_not_configured(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AITHER_S3_BUCKET", None)
            backend = S3Backend()
            result = await backend.write("default", "file.txt", b"data")
            assert result is False

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_configured(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AITHER_S3_BUCKET", None)
            backend = S3Backend()
            result = await backend.delete("default", "file.txt")
            assert result is False

    @pytest.mark.asyncio
    async def test_exists_returns_false_when_not_configured(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AITHER_S3_BUCKET", None)
            backend = S3Backend()
            result = await backend.exists("default", "file.txt")
            assert result is False

    @pytest.mark.asyncio
    async def test_list_returns_empty_when_not_configured(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AITHER_S3_BUCKET", None)
            backend = S3Backend()
            result = await backend.list("default")
            assert result == []

    @pytest.mark.asyncio
    async def test_stub_read_returns_none_when_configured(self):
        with patch.dict(os.environ, {"AITHER_S3_BUCKET": "test"}):
            backend = S3Backend()
            result = await backend.read("default", "file.txt")
            assert result is None

    @pytest.mark.asyncio
    async def test_stub_write_returns_false_when_configured(self):
        with patch.dict(os.environ, {"AITHER_S3_BUCKET": "test"}):
            backend = S3Backend()
            result = await backend.write("default", "file.txt", b"data")
            assert result is False

    def test_name(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AITHER_S3_BUCKET", None)
            backend = S3Backend()
            assert backend.name == "s3"

    def test_env_config_values(self):
        env = {
            "AITHER_S3_BUCKET": "mybucket",
            "AITHER_S3_ENDPOINT": "https://minio.local:9000",
            "AITHER_S3_KEY": "ak",
            "AITHER_S3_SECRET": "sk",
            "AITHER_S3_REGION": "eu-west-1",
        }
        with patch.dict(os.environ, env):
            backend = S3Backend()
            assert backend.bucket == "mybucket"
            assert backend.endpoint == "https://minio.local:9000"
            assert backend.access_key == "ak"
            assert backend.secret_key == "sk"
            assert backend.region == "eu-west-1"


# ─────────────────────────────────────────────────────────────────────────────
# AitherOSBackend
# ─────────────────────────────────────────────────────────────────────────────


class TestAitherOSBackend:
    def test_not_configured_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AITHER_STRATA_URL", None)
            backend = AitherOSBackend(strata_url="")
            assert backend.configured is False

    def test_configured_when_url_set(self):
        backend = AitherOSBackend(strata_url="http://localhost:8136")
        assert backend.configured is True

    def test_configured_from_env(self):
        with patch.dict(os.environ, {"AITHER_STRATA_URL": "http://strata:8136"}):
            backend = AitherOSBackend()
            assert backend.configured is True

    def test_name(self):
        backend = AitherOSBackend()
        assert backend.name == "aitheros"

    @pytest.mark.asyncio
    async def test_read_when_not_configured(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AITHER_STRATA_URL", None)
            backend = AitherOSBackend(strata_url="")
            result = await backend.read("default", "file.txt")
            assert result is None

    @pytest.mark.asyncio
    async def test_read_success(self):
        backend = AitherOSBackend(strata_url="http://localhost:8136")
        backend._available = True
        with patch("adk.strata.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b"file data"
            mock_client.get.return_value = mock_resp

            result = await backend.read("default", "test.json")
            assert result == b"file data"
            mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_success(self):
        backend = AitherOSBackend(strata_url="http://localhost:8136")
        backend._available = True
        with patch("adk.strata.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_client.put.return_value = mock_resp

            result = await backend.write("default", "test.json", b"data")
            assert result is True

    @pytest.mark.asyncio
    async def test_delete_success(self):
        backend = AitherOSBackend(strata_url="http://localhost:8136")
        backend._available = True
        with patch("adk.strata.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client.delete.return_value = mock_resp

            result = await backend.delete("default", "test.json")
            assert result is True

    @pytest.mark.asyncio
    async def test_exists_success(self):
        backend = AitherOSBackend(strata_url="http://localhost:8136")
        backend._available = True
        with patch("adk.strata.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client.head.return_value = mock_resp

            result = await backend.exists("default", "test.json")
            assert result is True

    @pytest.mark.asyncio
    async def test_list_success(self):
        backend = AitherOSBackend(strata_url="http://localhost:8136")
        backend._available = True
        with patch("adk.strata.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"keys": ["a.json", "b.json"]}
            mock_client.get.return_value = mock_resp

            result = await backend.list("default")
            assert result == ["a.json", "b.json"]

    @pytest.mark.asyncio
    async def test_read_fails_on_http_error(self):
        backend = AitherOSBackend(strata_url="http://localhost:8136")
        backend._available = True
        with patch("adk.strata.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_client.get.return_value = mock_resp

            result = await backend.read("default", "nope.json")
            assert result is None

    @pytest.mark.asyncio
    async def test_check_available_caches_result(self):
        backend = AitherOSBackend(strata_url="http://localhost:8136")
        backend._available = True
        # Should not make HTTP call since cached
        assert await backend._check_available() is True


# ─────────────────────────────────────────────────────────────────────────────
# Strata Router
# ─────────────────────────────────────────────────────────────────────────────


class TestStrata:
    @pytest.fixture
    def strata(self, tmp_path):
        """Strata with only local backend for isolation."""
        local = LocalBackend(base_dir=tmp_path)
        return Strata(backends=[local], data_dir=tmp_path)

    @pytest.mark.asyncio
    async def test_write_and_read(self, strata):
        assert await strata.write("test.txt", "hello") is True
        data = await strata.read("test.txt")
        assert data == b"hello"

    @pytest.mark.asyncio
    async def test_write_and_read_bytes(self, strata):
        payload = b"\x00\x01\x02\x03"
        assert await strata.write("binary.bin", payload) is True
        data = await strata.read("binary.bin")
        assert data == payload

    @pytest.mark.asyncio
    async def test_read_text(self, strata):
        await strata.write("text.txt", "hello world")
        text = await strata.read_text("text.txt")
        assert text == "hello world"

    @pytest.mark.asyncio
    async def test_read_text_nonexistent(self, strata):
        text = await strata.read_text("nope.txt")
        assert text is None

    @pytest.mark.asyncio
    async def test_write_and_read_json(self, strata):
        obj = {"key": "value", "nested": [1, 2, 3]}
        assert await strata.write_json("data.json", obj) is True
        result = await strata.read_json("data.json")
        assert result == obj

    @pytest.mark.asyncio
    async def test_read_json_nonexistent(self, strata):
        result = await strata.read_json("nope.json")
        assert result is None

    @pytest.mark.asyncio
    async def test_read_json_invalid(self, strata):
        await strata.write("bad.json", "not json{{{")
        result = await strata.read_json("bad.json")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete(self, strata):
        await strata.write("todelete.txt", "bye")
        assert await strata.exists("todelete.txt") is True
        assert await strata.delete("todelete.txt") is True
        assert await strata.exists("todelete.txt") is False

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, strata):
        assert await strata.delete("nope.txt") is False

    @pytest.mark.asyncio
    async def test_exists(self, strata):
        assert await strata.exists("nope.txt") is False
        await strata.write("yes.txt", "here")
        assert await strata.exists("yes.txt") is True

    @pytest.mark.asyncio
    async def test_list(self, strata):
        await strata.write("a.txt", "a")
        await strata.write("b.txt", "b")
        result = await strata.list()
        assert "a.txt" in result
        assert "b.txt" in result

    @pytest.mark.asyncio
    async def test_tenant_prefix_in_write_read(self, strata):
        await strata.write("tenant:acme/config.json", '{"org": "acme"}')
        data = await strata.read("tenant:acme/config.json")
        assert data == b'{"org": "acme"}'
        # Default tenant should not have it
        default_data = await strata.read("config.json")
        assert default_data is None

    @pytest.mark.asyncio
    async def test_write_empty_path_raises(self, strata):
        with pytest.raises(ValueError, match="must not be empty"):
            await strata.write("", "data")

    @pytest.mark.asyncio
    async def test_write_tenant_only_raises(self, strata):
        with pytest.raises(ValueError, match="must include at least a filename"):
            await strata.write("tenant:acme", "data")

    @pytest.mark.asyncio
    async def test_default_tenant(self, strata):
        assert strata.default_tenant == _DEFAULT_TENANT

    @pytest.mark.asyncio
    async def test_custom_default_tenant(self, tmp_path):
        s = Strata(default_tenant="myorg", data_dir=tmp_path)
        assert s.default_tenant == "myorg"

    @pytest.mark.asyncio
    async def test_stats(self, strata):
        stats = await strata.stats()
        assert "default_tenant" in stats
        assert "backends" in stats
        assert any(b["name"] == "local" for b in stats["backends"])

    @pytest.mark.asyncio
    async def test_backends_property(self, strata):
        backends = strata.backends
        assert len(backends) >= 1
        assert any(b.name == "local" for b in backends)

    @pytest.mark.asyncio
    async def test_read_empty_key_returns_none(self, strata):
        # parse_path with just "tenant:acme" yields path=""
        result = await strata.read("tenant:acme/")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_with_tenant_prefix(self, strata):
        await strata.write("tenant:org/file1.txt", "1")
        await strata.write("tenant:org/file2.txt", "2")
        result = await strata.list(prefix="tenant:org/")
        assert len(result) >= 2


# ─────────────────────────────────────────────────────────────────────────────
# Fallback Chain
# ─────────────────────────────────────────────────────────────────────────────


class TestFallbackChain:
    @pytest.mark.asyncio
    async def test_write_falls_back_to_local_when_primary_fails(self, tmp_path):
        """When a networked backend fails write, local should still succeed."""
        failing = AsyncMock(spec=StrataBackend)
        failing.name = "failing"
        failing.write = AsyncMock(return_value=False)
        failing.read = AsyncMock(return_value=None)
        failing.delete = AsyncMock(return_value=False)
        failing.exists = AsyncMock(return_value=False)
        failing.list = AsyncMock(return_value=[])

        local = LocalBackend(base_dir=tmp_path)
        strata = Strata(backends=[failing, local], data_dir=tmp_path)

        assert await strata.write("test.txt", "fallback data") is True
        data = await strata.read("test.txt")
        assert data == b"fallback data"

    @pytest.mark.asyncio
    async def test_read_checks_local_first(self, tmp_path):
        """Local cache hit should prevent network call."""
        network = AsyncMock(spec=StrataBackend)
        network.name = "network"
        network.read = AsyncMock(return_value=b"network data")
        network.write = AsyncMock(return_value=True)
        network.list = AsyncMock(return_value=[])

        local = LocalBackend(base_dir=tmp_path)
        strata = Strata(backends=[network, local], data_dir=tmp_path)

        # Write directly to local cache
        await local.write("default", "cached.txt", "local data")

        data = await strata.read("cached.txt")
        assert data == b"local data"
        # Network should NOT have been called
        network.read.assert_not_called()

    @pytest.mark.asyncio
    async def test_read_falls_through_to_network(self, tmp_path):
        """When local has no cache, read from network and cache locally."""
        network = AsyncMock(spec=StrataBackend)
        network.name = "network"
        network.read = AsyncMock(return_value=b"from network")
        network.write = AsyncMock(return_value=True)
        network.list = AsyncMock(return_value=[])

        local = LocalBackend(base_dir=tmp_path)
        strata = Strata(backends=[network, local], data_dir=tmp_path)

        data = await strata.read("remote_only.txt")
        assert data == b"from network"
        # Should have been cached locally
        cached = await local.read("default", "remote_only.txt")
        assert cached == b"from network"

    @pytest.mark.asyncio
    async def test_write_caches_locally_after_primary(self, tmp_path):
        """Write to primary backend also caches locally."""
        primary = AsyncMock(spec=StrataBackend)
        primary.name = "primary"
        primary.write = AsyncMock(return_value=True)

        local = LocalBackend(base_dir=tmp_path)
        strata = Strata(backends=[primary, local], data_dir=tmp_path)

        assert await strata.write("cached.txt", "primary data") is True
        # Check it was cached locally
        cached = await local.read("default", "cached.txt")
        assert cached == b"primary data"

    @pytest.mark.asyncio
    async def test_delete_removes_from_all_backends(self, tmp_path):
        """Delete should attempt removal from all backends."""
        backend_a = AsyncMock(spec=StrataBackend)
        backend_a.name = "a"
        backend_a.delete = AsyncMock(return_value=True)
        backend_a.write = AsyncMock(return_value=True)
        backend_a.exists = AsyncMock(return_value=True)
        backend_a.list = AsyncMock(return_value=[])

        local = LocalBackend(base_dir=tmp_path)
        strata = Strata(backends=[backend_a, local], data_dir=tmp_path)

        await strata.write("todelete.txt", "bye")
        result = await strata.delete("todelete.txt")
        assert result is True
        backend_a.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_exists_checks_all_backends(self, tmp_path):
        """Exists returns True if any backend has the key."""
        empty_local = LocalBackend(base_dir=tmp_path)
        network = AsyncMock(spec=StrataBackend)
        network.name = "network"
        network.exists = AsyncMock(return_value=True)
        network.list = AsyncMock(return_value=[])

        strata = Strata(backends=[network, empty_local], data_dir=tmp_path)
        assert await strata.exists("remote_file.txt") is True

    @pytest.mark.asyncio
    async def test_list_merges_all_backends(self, tmp_path):
        """List should merge and deduplicate results from all backends."""
        network = AsyncMock(spec=StrataBackend)
        network.name = "network"
        network.list = AsyncMock(return_value=["a.txt", "c.txt"])

        local = LocalBackend(base_dir=tmp_path)
        await local.write("default", "a.txt", "a")
        await local.write("default", "b.txt", "b")

        strata = Strata(backends=[network, local], data_dir=tmp_path)
        result = await strata.list()
        # Should be deduped and sorted
        assert "a.txt" in result
        assert "b.txt" in result
        assert "c.txt" in result
        assert len(set(result)) == len(result)  # no duplicates

    @pytest.mark.asyncio
    async def test_all_backends_fail_write_returns_false(self, tmp_path):
        """When every backend fails, write returns False."""
        failing1 = AsyncMock(spec=StrataBackend)
        failing1.name = "fail1"
        failing1.write = AsyncMock(return_value=False)

        failing2 = AsyncMock(spec=StrataBackend)
        failing2.name = "fail2"
        failing2.write = AsyncMock(return_value=False)

        # Also make local fail by using a read-only path
        strata = Strata(backends=[failing1, failing2], data_dir=tmp_path)
        # Monkey-patch the local backend to also fail
        strata._local.write = AsyncMock(return_value=False)

        result = await strata.write("doom.txt", "data")
        assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# Auto-detection
# ─────────────────────────────────────────────────────────────────────────────


class TestAutoDetection:
    def test_local_always_present(self, tmp_path):
        with patch.dict(os.environ, {}, clear=False):
            for k in ("AITHER_STRATA_URL", "AITHER_S3_BUCKET"):
                os.environ.pop(k, None)
            strata = Strata(data_dir=tmp_path)
            names = [b.name for b in strata.backends]
            assert "local" in names

    def test_aitheros_added_when_configured(self, tmp_path):
        with patch.dict(os.environ, {"AITHER_STRATA_URL": "http://strata:8136"}):
            strata = Strata(data_dir=tmp_path)
            names = [b.name for b in strata.backends]
            assert "aitheros" in names
            # AitherOS should be before local (higher priority)
            assert names.index("aitheros") < names.index("local")

    def test_s3_added_when_configured(self, tmp_path):
        with patch.dict(os.environ, {"AITHER_S3_BUCKET": "test-bucket"}):
            strata = Strata(data_dir=tmp_path)
            names = [b.name for b in strata.backends]
            assert "s3" in names

    def test_all_backends_when_fully_configured(self, tmp_path):
        env = {
            "AITHER_STRATA_URL": "http://strata:8136",
            "AITHER_S3_BUCKET": "test-bucket",
        }
        with patch.dict(os.environ, env):
            strata = Strata(data_dir=tmp_path)
            names = [b.name for b in strata.backends]
            assert names == ["aitheros", "s3", "local"]


# ─────────────────────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────────────────────


class TestGetStrata:
    def test_returns_same_instance(self, tmp_path):
        import adk.strata
        adk.strata._strata_instance = None  # reset
        s1 = get_strata(data_dir=str(tmp_path))
        s2 = get_strata()
        assert s1 is s2
        adk.strata._strata_instance = None  # cleanup

    def test_singleton_respects_first_config(self, tmp_path):
        import adk.strata
        adk.strata._strata_instance = None  # reset
        s1 = get_strata(default_tenant="org1", data_dir=str(tmp_path))
        s2 = get_strata(default_tenant="org2")  # Should be ignored
        assert s1.default_tenant == "org1"
        assert s2.default_tenant == "org1"
        adk.strata._strata_instance = None  # cleanup


# ─────────────────────────────────────────────────────────────────────────────
# StrataEntry dataclass
# ─────────────────────────────────────────────────────────────────────────────


class TestStrataEntry:
    def test_defaults(self):
        entry = StrataEntry(key="test.txt")
        assert entry.key == "test.txt"
        assert entry.size == 0
        assert entry.tenant == _DEFAULT_TENANT
        assert entry.backend == "local"
        assert entry.metadata == {}

    def test_custom_values(self):
        entry = StrataEntry(
            key="data.json", size=1024,
            content_type="application/json",
            tenant="acme", backend="s3",
            modified_at=1234567890.0,
            metadata={"version": 2},
        )
        assert entry.size == 1024
        assert entry.tenant == "acme"
        assert entry.backend == "s3"
        assert entry.metadata["version"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# Agent Integration
# ─────────────────────────────────────────────────────────────────────────────


class TestAgentIntegration:
    def test_agent_has_strata_property(self):
        """AitherAgent should have a strata property that returns a Strata."""
        with patch("adk.agent.LLMRouter"):
            with patch("adk.agent.load_identity") as mock_id:
                from adk.identity import Identity
                mock_id.return_value = Identity(name="test")
                from adk.agent import AitherAgent
                agent = AitherAgent(
                    name="test",
                    builtin_tools=False,
                    system_prompt="test",
                )
                assert agent._strata is None
                strata = agent.strata
                assert strata is not None
                assert isinstance(strata, Strata)

    def test_agent_strata_is_lazy(self):
        """Strata should not be initialized until accessed."""
        with patch("adk.agent.LLMRouter"):
            with patch("adk.agent.load_identity") as mock_id:
                from adk.identity import Identity
                mock_id.return_value = Identity(name="test")
                from adk.agent import AitherAgent
                agent = AitherAgent(
                    name="test2",
                    builtin_tools=False,
                    system_prompt="test",
                )
                assert agent._strata is None
                # Access triggers init
                _ = agent.strata
                assert agent._strata is not None

    @pytest.mark.asyncio
    async def test_agent_strata_write_read(self, tmp_path):
        """Agent can write and read through strata."""
        import adk.strata
        adk.strata._strata_instance = None  # reset singleton

        with patch("adk.agent.LLMRouter"):
            with patch("adk.agent.load_identity") as mock_id:
                from adk.identity import Identity
                mock_id.return_value = Identity(name="test")
                from adk.agent import AitherAgent
                agent = AitherAgent(
                    name="test3",
                    builtin_tools=False,
                    system_prompt="test",
                )
                # Inject a local-only Strata for testing
                local = LocalBackend(base_dir=tmp_path)
                agent._strata = Strata(backends=[local], data_dir=tmp_path)

                await agent.strata.write("agent/config.json", '{"name": "test3"}')
                data = await agent.strata.read_text("agent/config.json")
                assert data == '{"name": "test3"}'

        adk.strata._strata_instance = None  # cleanup


# ─────────────────────────────────────────────────────────────────────────────
# StrataBackend ABC
# ─────────────────────────────────────────────────────────────────────────────


class TestStrataBackendABC:
    def test_cannot_instantiate_abstract(self):
        """StrataBackend is abstract and cannot be instantiated directly."""
        with pytest.raises(TypeError):
            StrataBackend()

    def test_concrete_subclass(self):
        """A concrete subclass with all methods implemented should work."""

        class TestBackend(StrataBackend):
            @property
            def name(self) -> str:
                return "test"

            async def read(self, tenant, path):
                return None

            async def write(self, tenant, path, data):
                return True

            async def delete(self, tenant, path):
                return True

            async def exists(self, tenant, path):
                return False

            async def list(self, tenant, prefix=""):
                return []

        backend = TestBackend()
        assert backend.name == "test"


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_large_binary_data(self, tmp_path):
        local = LocalBackend(base_dir=tmp_path)
        strata = Strata(backends=[local], data_dir=tmp_path)
        big_data = b"\x00" * (1024 * 1024)  # 1MB
        assert await strata.write("big.bin", big_data) is True
        result = await strata.read("big.bin")
        assert result == big_data

    @pytest.mark.asyncio
    async def test_unicode_content(self, tmp_path):
        local = LocalBackend(base_dir=tmp_path)
        strata = Strata(backends=[local], data_dir=tmp_path)
        text = "Hello, world! Accent: cafe\u0301 Emoji: not allowed per rules"
        assert await strata.write("unicode.txt", text) is True
        result = await strata.read_text("unicode.txt")
        assert result == text

    @pytest.mark.asyncio
    async def test_special_chars_in_path(self, tmp_path):
        local = LocalBackend(base_dir=tmp_path)
        strata = Strata(backends=[local], data_dir=tmp_path)
        assert await strata.write("dir with space/file name.txt", "data") is True
        result = await strata.read_text("dir with space/file name.txt")
        assert result == "data"

    @pytest.mark.asyncio
    async def test_delete_empty_path_returns_false(self, tmp_path):
        local = LocalBackend(base_dir=tmp_path)
        strata = Strata(backends=[local], data_dir=tmp_path)
        result = await strata.delete("tenant:acme/")
        assert result is False

    @pytest.mark.asyncio
    async def test_exists_empty_key_returns_false(self, tmp_path):
        local = LocalBackend(base_dir=tmp_path)
        strata = Strata(backends=[local], data_dir=tmp_path)
        result = await strata.exists("tenant:acme/")
        assert result is False

    @pytest.mark.asyncio
    async def test_write_json_with_non_serializable(self, tmp_path):
        """write_json should handle non-standard types via default=str."""
        local = LocalBackend(base_dir=tmp_path)
        strata = Strata(backends=[local], data_dir=tmp_path)
        from datetime import datetime
        obj = {"time": datetime(2026, 1, 1)}
        assert await strata.write_json("time.json", obj) is True
        result = await strata.read_json("time.json")
        assert "2026" in str(result["time"])

    @pytest.mark.asyncio
    async def test_backends_always_has_local(self, tmp_path):
        """Even with empty backends list, local is ensured."""
        strata = Strata(backends=[], data_dir=tmp_path)
        assert any(b.name == "local" for b in strata.backends)

    @pytest.mark.asyncio
    async def test_strata_url_constructor_param(self, tmp_path):
        """strata_url parameter should configure AitherOS backend."""
        strata = Strata(
            strata_url="http://custom:8136",
            data_dir=tmp_path,
        )
        names = [b.name for b in strata.backends]
        assert "aitheros" in names
