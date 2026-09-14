"""Voice service client (port 8083)."""

from adk.client._base import ServiceClient


class VoiceClient(ServiceClient):
    """Client for the AitherVoice service."""

    async def transcribe(self, audio_data: bytes, format: str = "wav") -> dict:
        """Transcribe audio to text."""
        client = await self._get_client()
        mime = {"wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg"}.get(format, "audio/wav")
        resp = await client.post(
            f"{self._base_url}/transcribe",
            files={"file": (f"audio.{format}", audio_data, mime)},
            timeout=30.0,
        )
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    async def synthesize(self, text: str, voice_id: str = "nova") -> dict:
        """Synthesize text to speech."""
        client = await self._get_client()
        resp = await client.post(
            f"{self._base_url}/synthesize",
            params={"text": text, "voice_id": voice_id},
            timeout=30.0,
        )
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
