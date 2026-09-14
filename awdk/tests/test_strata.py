"""Tests for adk.strata — fire-and-forget Strata ingest."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adk.strata import StrataIngest, _cap_jsonl, get_strata_ingest


class TestStrataIngestEnabled:
    def test_disabled_when_no_url(self, tmp_path):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AITHER_STRATA_URL", None)
            si = StrataIngest(strata_url="", data_dir=str(tmp_path))
            assert si.enabled is False

    def test_enabled_when_url_set(self, tmp_path):
        si = StrataIngest(strata_url="http://localhost:8136", data_dir=str(tmp_path))
        assert si.enabled is True

    def test_enabled_from_env(self, tmp_path):
        with patch.dict(os.environ, {"AITHER_STRATA_URL": "http://strata:8136"}):
            si = StrataIngest(data_dir=str(tmp_path))
            assert si.enabled is True
            assert si.strata_url == "http://strata:8136"


class TestStrataIngestChat:
    @pytest.mark.asyncio
    async def test_ingest_chat_success(self, tmp_path):
        si = StrataIngest(strata_url="http://localhost:8136", data_dir=str(tmp_path))
        with patch("adk.strata.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client.post.return_value = mock_resp

            result = await si.ingest_chat(
                agent="atlas",
                session_id="abc123",
                user_message="hello",
                assistant_response="hi there",
                model="llama3.2:3b",
                tokens_used=50,
                latency_ms=200,
            )
            assert result is True
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert "/api/v1/ingest/adk-session" in call_args[0][0]
            payload = call_args[1]["json"]
            assert payload["source"] == "adk"
            assert payload["agent"] == "atlas"
            assert payload["user_message"] == "hello"

    @pytest.mark.asyncio
    async def test_ingest_chat_disabled(self, tmp_path):
        si = StrataIngest(strata_url="", data_dir=str(tmp_path))
        result = await si.ingest_chat(
            agent="x", session_id="y",
            user_message="a", assistant_response="b",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_ingest_chat_queues_on_failure(self, tmp_path):
        si = StrataIngest(strata_url="http://localhost:8136", data_dir=str(tmp_path))
        with patch("adk.strata.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post.side_effect = Exception("connection refused")

            result = await si.ingest_chat(
                agent="atlas", session_id="abc",
                user_message="hello", assistant_response="hi",
            )
            assert result is False
            # Should have queued to disk
            queue = tmp_path / "strata_queue.jsonl"
            assert queue.exists()
            data = json.loads(queue.read_text().strip())
            assert data["agent"] == "atlas"

    @pytest.mark.asyncio
    async def test_ingest_chat_queues_on_http_error(self, tmp_path):
        si = StrataIngest(strata_url="http://localhost:8136", data_dir=str(tmp_path))
        with patch("adk.strata.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_client.post.return_value = mock_resp

            result = await si.ingest_chat(
                agent="atlas", session_id="abc",
                user_message="hello", assistant_response="hi",
            )
            assert result is False
            assert (tmp_path / "strata_queue.jsonl").exists()


class TestStrataIngestSessionEnd:
    @pytest.mark.asyncio
    async def test_session_end_success(self, tmp_path):
        si = StrataIngest(strata_url="http://localhost:8136", data_dir=str(tmp_path))
        with patch("adk.strata.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client.post.return_value = mock_resp

            result = await si.ingest_session_end(
                agent="atlas", session_id="abc",
                message_count=10, total_tokens=500,
            )
            assert result is True

    @pytest.mark.asyncio
    async def test_session_end_disabled(self, tmp_path):
        si = StrataIngest(strata_url="", data_dir=str(tmp_path))
        result = await si.ingest_session_end(
            agent="x", session_id="y",
        )
        assert result is False


class TestStrataFlushQueue:
    @pytest.mark.asyncio
    async def test_flush_sends_queued_entries(self, tmp_path):
        si = StrataIngest(strata_url="http://localhost:8136", data_dir=str(tmp_path))
        queue = tmp_path / "strata_queue.jsonl"
        entries = [
            json.dumps({"agent": "a1", "type": "chat_exchange"}),
            json.dumps({"agent": "a2", "type": "chat_exchange"}),
        ]
        queue.write_text("\n".join(entries) + "\n")

        with patch("adk.strata.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client.post.return_value = mock_resp

            sent = await si.flush_queue()
            assert sent == 2
            assert not queue.exists()

    @pytest.mark.asyncio
    async def test_flush_keeps_failed_entries(self, tmp_path):
        si = StrataIngest(strata_url="http://localhost:8136", data_dir=str(tmp_path))
        queue = tmp_path / "strata_queue.jsonl"
        entries = [
            json.dumps({"agent": "a1"}),
            json.dumps({"agent": "a2"}),
        ]
        queue.write_text("\n".join(entries) + "\n")

        with patch("adk.strata.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            # First succeeds, second fails
            ok_resp = MagicMock()
            ok_resp.status_code = 200
            fail_resp = MagicMock()
            fail_resp.status_code = 500
            mock_client.post.side_effect = [ok_resp, fail_resp]

            sent = await si.flush_queue()
            assert sent == 1
            assert queue.exists()
            remaining = queue.read_text().strip().split("\n")
            assert len(remaining) == 1
            assert json.loads(remaining[0])["agent"] == "a2"

    @pytest.mark.asyncio
    async def test_flush_empty_queue(self, tmp_path):
        si = StrataIngest(strata_url="http://localhost:8136", data_dir=str(tmp_path))
        sent = await si.flush_queue()
        assert sent == 0

    @pytest.mark.asyncio
    async def test_flush_disabled(self, tmp_path):
        si = StrataIngest(strata_url="", data_dir=str(tmp_path))
        queue = tmp_path / "strata_queue.jsonl"
        queue.write_text(json.dumps({"agent": "x"}) + "\n")
        sent = await si.flush_queue()
        assert sent == 0


class TestCapJsonl:
    def test_caps_at_max(self, tmp_path):
        p = tmp_path / "test.jsonl"
        lines = [json.dumps({"n": i}) for i in range(10)]
        p.write_text("\n".join(lines) + "\n")
        _cap_jsonl(p, 5)
        result = p.read_text().strip().split("\n")
        assert len(result) == 5
        # Should keep the newest (last 5)
        assert json.loads(result[0])["n"] == 5

    def test_no_cap_under_limit(self, tmp_path):
        p = tmp_path / "test.jsonl"
        lines = [json.dumps({"n": i}) for i in range(3)]
        p.write_text("\n".join(lines) + "\n")
        _cap_jsonl(p, 10)
        result = p.read_text().strip().split("\n")
        assert len(result) == 3


class TestGetStrataIngestSingleton:
    def test_returns_same_instance(self):
        import adk.strata
        adk.strata._instance = None  # reset
        s1 = get_strata_ingest()
        s2 = get_strata_ingest()
        assert s1 is s2
        adk.strata._instance = None  # cleanup


class TestStrataPayloadFormat:
    @pytest.mark.asyncio
    async def test_chat_payload_has_required_fields(self, tmp_path):
        si = StrataIngest(strata_url="http://localhost:8136", data_dir=str(tmp_path))
        with patch("adk.strata.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client.post.return_value = mock_resp

            await si.ingest_chat(
                agent="demiurge", session_id="sess1",
                user_message="write code", assistant_response="done",
                model="llama3.2:3b", tokens_used=100, latency_ms=500,
                tool_calls=["search", "write_file"],
            )

            payload = mock_client.post.call_args[1]["json"]
            assert payload["source"] == "adk"
            assert payload["type"] == "chat_exchange"
            assert payload["agent"] == "demiurge"
            assert payload["session_id"] == "sess1"
            assert payload["user_message"] == "write code"
            assert payload["assistant_response"] == "done"
            assert payload["model"] == "llama3.2:3b"
            assert payload["tokens_used"] == 100
            assert payload["latency_ms"] == 500
            assert payload["tool_calls"] == ["search", "write_file"]
            assert "timestamp" in payload

    @pytest.mark.asyncio
    async def test_session_end_payload_format(self, tmp_path):
        si = StrataIngest(strata_url="http://localhost:8136", data_dir=str(tmp_path))
        with patch("adk.strata.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client.post.return_value = mock_resp

            await si.ingest_session_end(
                agent="atlas", session_id="sess2",
                message_count=20, total_tokens=1000,
                duration_seconds=120.5,
            )

            payload = mock_client.post.call_args[1]["json"]
            assert payload["source"] == "adk"
            assert payload["type"] == "session_end"
            assert payload["message_count"] == 20
            assert payload["total_tokens"] == 1000
            assert payload["duration_seconds"] == 120.5
