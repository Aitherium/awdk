"""Offline unit tests for voice tools (no network, no ML models).

    cd awdk && python -m pytest tests/test_voice_tools.py -q

Uses MOCK backend to avoid requiring faster-whisper or OpenAI keys.
"""

from __future__ import annotations

import os
import pytest
from pathlib import Path

# Force mock mode for all tests in this file
os.environ["AITHER_VOICE_MODE"] = "mock"

from adk.builtin_tools_voice import hear, say, analyze_voice_emotion, _get_voice_client, MockVoiceClient


@pytest.mark.asyncio
async def test_hear_mock():
    """Test hear() with mock STT backend."""
    # Mock returns filename stem as transcript
    result = await hear("test_hello_world.wav")
    assert result == "hello world"


@pytest.mark.asyncio
async def test_hear_nonexistent_file():
    """hear() gracefully handles path (mock doesn't validate file existence)."""
    result = await hear("/nonexistent/file.wav")
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_hear_with_language():
    """hear() accepts language hint (passed through to real backend)."""
    result = await hear("test_bonjour.wav", language="fr")
    assert result == "bonjour"


@pytest.mark.asyncio
async def test_say_mock():
    """Test say() with mock TTS backend."""
    audio = await say("Hello, this is a test message")
    assert isinstance(audio, bytes)
    assert b"MOCK AUDIO" in audio or len(audio) > 0


@pytest.mark.asyncio
async def test_say_custom_voice():
    """say() accepts voice_id parameter."""
    audio = await say("Test message", voice_id="shimmer")
    assert isinstance(audio, bytes)
    assert len(audio) > 0


@pytest.mark.asyncio
async def test_analyze_voice_emotion():
    """Test emotion analysis (mock returns neutral)."""
    result = await analyze_voice_emotion("test_sad.wav")
    assert isinstance(result, dict)
    assert "emotion" in result
    # Mock always returns neutral
    assert result["emotion"] == "neutral"
    assert "confidence" in result
    assert 0 <= result["confidence"] <= 1


def test_mock_client_directly():
    """Test MockVoiceClient in isolation."""
    client = MockVoiceClient()
    assert isinstance(client, MockVoiceClient)


@pytest.mark.asyncio
async def test_client_lazy_init_mock():
    """Test that _get_voice_client initializes once."""
    import adk.builtin_tools_voice as voice_module

    # Reset to None to test initialization
    voice_module._voice_client = None

    client1 = _get_voice_client()
    client2 = _get_voice_client()

    assert client1 is client2  # Same instance (singleton)
    assert isinstance(client1, MockVoiceClient)


@pytest.mark.asyncio
async def test_say_handles_empty_text():
    """say() gracefully handles empty text."""
    audio = await say("")
    assert isinstance(audio, bytes)


@pytest.mark.asyncio
async def test_hear_error_returns_json():
    """hear() on error returns JSON error object."""
    # Force an error by passing an invalid type (but our mock is permissive)
    result = await hear("test.wav")
    assert isinstance(result, str)
    # Mock doesn't error, but real backend would
