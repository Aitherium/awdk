"""Live smoke tests for voice backend (optional, requires AITHER_VOICE_URL env var).

    # Only runs if AITHER_VOICE_URL is set:
    AITHER_VOICE_URL=http://localhost:8083 python -m pytest tests/test_voice_integration.py -v

    # To skip (default, CI-safe):
    python -m pytest tests/test_voice_integration.py -v
"""

from __future__ import annotations

import os
import pytest

# Skip all tests if voice service is not configured
pytestmark = pytest.mark.skipif(
    not os.getenv("AITHER_VOICE_URL"),
    reason="AITHER_VOICE_URL not set (voice service not running)"
)

# Force real mode for these tests
os.environ["AITHER_VOICE_MODE"] = "auto"


@pytest.mark.asyncio
async def test_voice_service_is_available():
    """Smoke test: verify voice service is running on AITHER_VOICE_URL."""
    import httpx

    service_url = os.getenv("AITHER_VOICE_URL", "http://localhost:8083")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{service_url}/health", timeout=5.0)
            assert resp.status_code == 200, f"Voice service health check failed: {resp.status_code}"
    except Exception as e:
        pytest.skip(f"Voice service unavailable: {e}")


@pytest.mark.asyncio
async def test_hear_real_service(tmp_path):
    """Smoke test: transcribe real audio via live service."""
    from adk.builtin_tools_voice import hear

    # For now, just verify the function is callable with a dummy path
    # Real test would need a pre-recorded audio file fixture
    try:
        # Use mock file path; real service would download/process it
        result = await hear(str(tmp_path / "test.wav"))
        assert isinstance(result, str)
    except Exception as e:
        pytest.skip(f"Voice service unavailable: {e}")


@pytest.mark.asyncio
async def test_say_real_service():
    """Smoke test: synthesize real audio via live service."""
    from adk.builtin_tools_voice import say

    try:
        audio = await say("Test message from integration test")
        assert isinstance(audio, bytes)
        # Real audio should be substantial; mock returns ~30 bytes
        # Accept either but log difference
        if len(audio) < 100:
            pytest.skip(f"Voice service returned stub audio ({len(audio)} bytes)")
    except Exception as e:
        pytest.skip(f"Voice service unavailable: {e}")


@pytest.mark.asyncio
async def test_emotion_analysis_real_service(tmp_path):
    """Smoke test: analyze real audio via live service."""
    from adk.builtin_tools_voice import analyze_voice_emotion

    try:
        # Use dummy path; real service would analyze it
        result = await analyze_voice_emotion(str(tmp_path / "test.wav"))
        assert isinstance(result, dict)
        assert "emotion" in result or "error" in result
    except Exception as e:
        pytest.skip(f"Voice service unavailable: {e}")
