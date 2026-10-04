"""Custom voices (``custom:<name>``) against a mocked Genesis /voice-builds endpoint.

    cd awdk && python -m pytest tests/test_custom_voices.py -q
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

import adk.custom_voices as cv
from adk.voice import SynthesisResult, VoiceClient

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt fake"
BASE = "https://genesis.test"


def _btv():
    # Imported lazily: test_voice_tools.py sets AITHER_VOICE_MODE=mock at collection,
    # and the module reads it at import time.
    import adk.builtin_tools_voice as btv

    return btv


class _Recorder:
    def __init__(self, voices=None, say_status=200):
        self.requests: list[httpx.Request] = []
        self.voices = voices if voices is not None else []
        self.say_status = say_status
        self.client_kwargs: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/voice-builds/voices":
            return httpx.Response(200, json={"voices": self.voices})
        is_say = path.startswith("/voice-builds/voices/") and path.endswith("/say")
        if request.method == "POST" and is_say:
            if self.say_status != 200:
                detail = {"detail": "No such voice in this workspace"}
                return httpx.Response(self.say_status, json=detail)
            name = path.split("/")[3]
            return httpx.Response(200, json={
                "audio_base64": base64.b64encode(WAV).decode(),
                "format": "wav",
                "voice": f"custom:{name}",
            })
        return httpx.Response(500, json={"detail": f"unexpected {request.method} {path}"})


@pytest.fixture
def genesis(monkeypatch):
    monkeypatch.setenv("AITHER_API_URL", BASE)
    monkeypatch.setenv("AITHER_API_KEY", "k-test")
    rec = _Recorder()
    real = httpx.AsyncClient

    def _client(*args, **kwargs):
        rec.client_kwargs.append(dict(kwargs))
        kwargs["transport"] = httpx.MockTransport(rec)
        return real(*args, **kwargs)

    monkeypatch.setattr(cv.httpx, "AsyncClient", _client)
    return rec


class _StockBackend:
    name = "stock"

    def __init__(self):
        self.calls: list[str] = []

    async def synthesize(self, text, voice="nova", output_path=None):
        self.calls.append(voice)
        return SynthesisResult(success=True, audio_data=b"stock")


@pytest.fixture
def real_client(monkeypatch):
    client = VoiceClient(backend="mock")
    stock = _StockBackend()
    client._backend = stock
    monkeypatch.setattr(_btv(), "_voice_client", client)
    return client, stock


def test_is_custom_and_name():
    assert cv.is_custom("custom:ava")
    assert not cv.is_custom("nova")
    assert not cv.is_custom("")
    assert cv.custom_name("custom:ava") == "ava"
    with pytest.raises(cv.CustomVoiceError):
        cv.custom_name("custom:../etc")


@pytest.mark.asyncio
async def test_empty_list_renders_cleanly(genesis):
    assert await cv.list_custom_voices() == []
    out = json.loads(await _btv().list_custom_voices())
    assert out == {"voices": [], "note": "no custom voices built in this workspace"}
    req = genesis.requests[-1]
    assert str(req.url) == f"{BASE}/voice-builds/voices"
    assert req.headers["authorization"] == "Bearer k-test"


@pytest.mark.asyncio
async def test_list_maps_to_custom_ids(genesis):
    genesis.voices = [{"id": "v1", "name": "ava", "reader": "r", "language": "en",
                       "built_at": "2026-10-03", "gate": {"ok": True}}]
    voices = await cv.list_custom_voices()
    assert voices == [{"id": "custom:ava", "name": "ava", "reader": "r", "language": "en",
                       "built_at": "2026-10-03", "gate": {"ok": True}}]
    out = json.loads(await _btv().list_custom_voices())
    assert out["voices"][0]["id"] == "custom:ava" and "note" not in out


@pytest.mark.asyncio
async def test_missing_key_is_loud(genesis, monkeypatch):
    monkeypatch.delenv("AITHER_API_KEY")
    out = json.loads(await _btv().list_custom_voices())
    assert "AITHER_API_KEY" in out["error"]
    assert genesis.requests == []


@pytest.mark.asyncio
async def test_say_to_file_custom_voice(genesis, real_client, tmp_path: Path):
    _, stock = real_client
    target = tmp_path / "out.wav"
    out = json.loads(await _btv().say_to_file("hello there", str(target), voice_id="custom:ava"))
    assert out == {"audio_path": str(target), "bytes": len(WAV), "voice": "custom:ava"}
    assert target.read_bytes() == WAV
    req = genesis.requests[-1]
    assert req.method == "POST"
    assert str(req.url) == f"{BASE}/voice-builds/voices/ava/say"
    assert json.loads(req.content) == {"text": "hello there", "speed": 1.0}
    assert req.headers["authorization"] == "Bearer k-test"
    assert stock.calls == []


@pytest.mark.asyncio
async def test_custom_voice_404_is_an_error(genesis, real_client, tmp_path: Path):
    genesis.say_status = 404
    raw = await _btv().say_to_file("hi", str(tmp_path / "x.wav"), voice_id="custom:ghost")
    out = json.loads(raw)
    assert "error" in out and "ghost" in out["error"]


@pytest.mark.asyncio
async def test_stock_voice_never_hits_voice_builds(genesis, real_client):
    client, stock = real_client
    result = await client.synthesize("hi", voice="nova")
    assert result.audio_data == b"stock"
    assert stock.calls == ["nova"]
    assert genesis.requests == []


@pytest.mark.asyncio
async def test_custom_calls_use_the_adk_tls_policy(genesis, monkeypatch):
    # In-fleet Genesis (AITHER_API_URL direct) presents the internal CA; the adk-wide
    # policy trusts it. A bare httpx default would fail that handshake.
    monkeypatch.setattr(cv, "tls_verify", lambda: "/ca/bundle.pem")
    await cv.list_custom_voices()
    await cv.synthesize_custom("hi", "custom:ava")
    assert [k.get("verify") for k in genesis.client_kwargs] == ["/ca/bundle.pem"] * 2


@pytest.mark.asyncio
async def test_list_tolerates_prefixed_ids_without_names(genesis):
    genesis.voices = [{"id": "custom:ava"}]
    voices = await cv.list_custom_voices()
    assert voices[0]["id"] == "custom:ava" and voices[0]["name"] == "ava"


@pytest.mark.asyncio
async def test_mock_backend_stays_offline_for_custom_voices(genesis):
    result = await VoiceClient(backend="mock").synthesize("hi", voice="custom:ava")
    assert result.success
    assert genesis.requests == []
