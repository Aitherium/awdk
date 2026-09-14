"""VoiceAgent — give any AitherAgent ears and a mouth.

Wraps :class:`adk.agent.AitherAgent` so a turn can start from audio and end in
audio, while the agent's own ReAct loop stays *text-only* (so every LLM provider
works — Anthropic, DeepSeek, local vLLM, etc.). The flow is:

    audio --(STT)--> text --> agent.chat() --> reply text --(TTS)--> audio

The STT/TTS engine is the pluggable :mod:`adk.voice` backend, selected by
``AITHER_VOICE_BACKEND`` (service | openai | sarvam | local | mock).

Usage:
    from adk.voice_agent import VoiceAgent

    va = VoiceAgent("lyra")                       # any identity
    out = await va.listen("caller.wav")           # transcribe -> chat -> synthesize
    print(out.transcript, "->", out.reply)
    open("reply.wav", "wb").write(out.reply_audio)

    # Text-in / audio-out, or audio-in / text-out:
    audio = await va.speak("Your order has shipped.")
    text  = await va.transcribe("voice_note.ogg")

This is complementary to the builtin ``hear`` / ``say`` *tools* (which let the
LLM invoke STT/TTS mid-loop). Use VoiceAgent when YOU own the audio I/O at the
edge (a phone call, a voice note); use the tools when the MODEL should decide.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from adk.agent import AgentResponse, AitherAgent
from adk.voice import (
    EmotionResult,
    SynthesisResult,
    TranscriptionResult,
    VoiceClient,
    get_voice_client,
)

logger = logging.getLogger("adk.voice_agent")


@dataclass
class VoiceTurn:
    """Outcome of one audio→agent→audio turn."""
    transcript: str = ""
    reply: str = ""
    reply_audio: bytes = b""
    reply_audio_path: str = ""
    emotion: str = ""
    response: AgentResponse | None = None
    error: str = ""
    ok: bool = False


class VoiceAgent:
    """An AitherAgent that can listen and speak.

    Args:
        name: agent identity name (passed to AitherAgent).
        voice_backend: explicit ``adk.voice`` backend; else AITHER_VOICE_BACKEND.
        voice_service_url: pin the ``service`` backend to this URL (back-compat).
        default_voice: TTS voice id for synthesized replies.
        detect_emotion: if True, analyze caller emotion and steer the reply tone.
        agent: an existing AitherAgent to wrap (overrides name/agent_kwargs).
        **agent_kwargs: forwarded to AitherAgent(name, ...).
    """

    def __init__(
        self,
        name: str | None = None,
        *,
        voice_backend: str = "",
        voice_service_url: str = "",
        default_voice: str = "nova",
        detect_emotion: bool = False,
        agent: AitherAgent | None = None,
        **agent_kwargs,
    ):
        self._agent = agent or AitherAgent(name, **agent_kwargs)
        self.name = self._agent.name
        self._default_voice = default_voice
        self._detect_emotion = detect_emotion
        if voice_service_url or voice_backend:
            self._voice: VoiceClient = VoiceClient(service_url=voice_service_url, backend=voice_backend)
        else:
            self._voice = get_voice_client()

    @property
    def agent(self) -> AitherAgent:
        """The wrapped AitherAgent (for direct .chat(), tool registration, etc.)."""
        return self._agent

    @property
    def voice(self) -> VoiceClient:
        return self._voice

    # ── primitives ────────────────────────────────────────────────────────────

    async def transcribe(self, audio: str | bytes, language: str | None = None) -> TranscriptionResult:
        """Audio → text."""
        return await self._voice.transcribe(audio, language=language)

    async def speak(self, text: str, output_path: str | None = None, voice: str | None = None) -> SynthesisResult:
        """Text → audio."""
        return await self._voice.synthesize(text, voice=voice or self._default_voice, output_path=output_path)

    async def feel(self, audio: str | bytes) -> EmotionResult:
        """Audio → vocal emotion (backend-dependent)."""
        return await self._voice.analyze_emotion(audio)

    # ── full turn ───────────────────────────────────────────────────────────────

    async def listen(
        self,
        audio: str | bytes,
        *,
        language: str | None = None,
        synthesize: bool = True,
        output_path: str | None = None,
        session_id: str | None = None,
        **chat_kwargs,
    ) -> VoiceTurn:
        """Run one full turn: transcribe → chat → (optionally) synthesize.

        Args:
            audio: input audio path or raw bytes.
            language: optional STT language hint.
            synthesize: if True, synthesize the agent's reply to audio.
            output_path: optional path to write the reply audio to.
            session_id: conversation/session id forwarded to chat().
            **chat_kwargs: extra kwargs forwarded to AitherAgent.chat().

        Returns:
            VoiceTurn with transcript, reply, reply_audio, emotion, ok/error.
        """
        turn = VoiceTurn()

        stt = await self._voice.transcribe(audio, language=language)
        if not stt.success or not stt.text.strip():
            turn.error = stt.error or "empty transcript"
            logger.warning("VoiceAgent.listen transcription failed: %s", turn.error)
            return turn
        turn.transcript = stt.text

        if self._detect_emotion:
            emo = await self._voice.analyze_emotion(audio)
            if emo.success and emo.emotion:
                turn.emotion = emo.emotion

        prompt = turn.transcript
        if turn.emotion:
            # Steer tone without polluting the transcript the agent reasons over.
            prompt = (
                f"{turn.transcript}\n\n"
                f"[voice context: the speaker sounds {turn.emotion}; "
                f"respond with appropriate tone and empathy]"
            )

        try:
            resp = await self._agent.chat(prompt, session_id=session_id, **chat_kwargs)
        except Exception as exc:  # noqa: BLE001 - reported on the turn, never raised
            turn.error = f"agent chat failed: {exc}"
            logger.error(turn.error)
            return turn

        turn.response = resp
        turn.reply = resp.content or ""

        if synthesize and turn.reply.strip():
            tts = await self._voice.synthesize(turn.reply, voice=self._default_voice, output_path=output_path)
            if tts.success:
                turn.reply_audio = tts.audio_data
                turn.reply_audio_path = tts.audio_path
            else:
                logger.warning("VoiceAgent.listen synthesis failed: %s", tts.error)

        turn.ok = True
        return turn
