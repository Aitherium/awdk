"""Offline tests for the pluggable voice engine (adk/voice.py).

    cd awdk && python -m pytest tests/test_voice_backends.py -q

No network, no models, no keys — exercises the backend factory, the mock
backend, and the VoiceClient facade.
"""

from __future__ import annotations

import pytest

from adk.voice import (
    EmotionResult,
    MockVoiceBackend,
    ServiceVoiceBackend,
    SynthesisResult,
    TranscriptionResult,
    VoiceClient,
    feel,
    get_voice_backend,
    hear,
    reset_voice_backend,
    say,
)


# ── Mock backend ──────────────────────────────────────────────────────────────

async def test_mock_transcribe_path():
    be = MockVoiceBackend()
    r = await be.transcribe("test_hello_world.wav")
    assert isinstance(r, TranscriptionResult)
    assert r.success and r.text == "hello world"


async def test_mock_transcribe_bytes():
    be = MockVoiceBackend()
    r = await be.transcribe(b"\x00\x01raw-audio")
    assert r.success and r.text == "mock audio"


async def test_mock_synthesize_returns_result_with_bytes():
    be = MockVoiceBackend()
    r = await be.synthesize("hello there")
    assert isinstance(r, SynthesisResult)
    assert r.success and b"MOCK AUDIO" in r.audio_data


async def test_mock_synthesize_writes_file(tmp_path):
    be = MockVoiceBackend()
    out = tmp_path / "sub" / "reply.wav"
    r = await be.synthesize("write me", output_path=str(out))
    assert r.success and out.exists()
    assert r.audio_path == str(out)
    assert out.read_bytes() == r.audio_data


async def test_mock_emotion():
    be = MockVoiceBackend()
    r = await be.analyze_emotion("test_sad.wav")
    assert isinstance(r, EmotionResult)
    assert r.success and r.emotion == "neutral"


# ── Factory ───────────────────────────────────────────────────────────────────

def test_factory_explicit_mock():
    assert isinstance(get_voice_backend("mock"), MockVoiceBackend)


def test_factory_unknown_falls_back_to_service():
    be = get_voice_backend("does-not-exist")
    assert isinstance(be, ServiceVoiceBackend)


def test_factory_default_is_service(monkeypatch):
    monkeypatch.delenv("AITHER_VOICE_BACKEND", raising=False)
    monkeypatch.delenv("AITHER_VOICE_URL", raising=False)
    reset_voice_backend()
    be = get_voice_backend()
    assert isinstance(be, ServiceVoiceBackend)
    assert be._url == "http://localhost:8083"


def test_factory_auto_prefers_service_when_url_set(monkeypatch):
    monkeypatch.setenv("AITHER_VOICE_BACKEND", "auto")
    monkeypatch.setenv("AITHER_VOICE_URL", "http://voice.local:8083")
    reset_voice_backend()
    be = get_voice_backend()
    assert isinstance(be, ServiceVoiceBackend)
    assert be._url == "http://voice.local:8083"


def test_factory_singleton_cached(monkeypatch):
    monkeypatch.setenv("AITHER_VOICE_BACKEND", "mock")
    reset_voice_backend()
    a = get_voice_backend()
    b = get_voice_backend()
    assert a is b
    # Explicit kind bypasses the cached default singleton.
    assert get_voice_backend("mock") is not a


# ── VoiceClient facade ────────────────────────────────────────────────────────

async def test_client_delegates_to_backend():
    client = VoiceClient(backend="mock")
    assert client.backend_name == "mock"
    t = await client.transcribe("test_order_status.wav")
    assert t.text == "order status"
    s = await client.synthesize("hi")
    assert s.success and b"MOCK AUDIO" in s.audio_data


def test_client_service_url_pins_service_backend():
    client = VoiceClient(service_url="http://box:9999")
    assert client.backend_name == "service"
    assert client._url == "http://box:9999"


def test_client_available_voices():
    client = VoiceClient(backend="mock")
    voices = client.available_voices()
    assert any(v["id"] == "nova" for v in voices)


# ── Convenience functions (env-selected backend) ──────────────────────────────

async def test_convenience_functions_with_mock(monkeypatch):
    monkeypatch.setenv("AITHER_VOICE_BACKEND", "mock")
    import adk.voice as v
    v.reset_voice_backend()
    v._instance = None  # reset the get_voice_client() singleton

    assert await hear("test_hello_world.wav") == "hello world"
    audio = await say("hello")
    assert isinstance(audio, bytes) and b"MOCK AUDIO" in audio
    assert await feel("test_clip.wav") == "neutral"
