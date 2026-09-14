"""Offline tests for VoiceAgent (adk/voice_agent.py).

    cd awdk && python -m pytest tests/test_voice_agent.py -q

Uses the mock voice backend + a fake LLM agent, so no network, models, or keys.
"""

from __future__ import annotations

from adk.agent import AgentResponse
from adk.voice_agent import VoiceAgent, VoiceTurn


class FakeAgent:
    """Minimal AitherAgent stand-in: records the prompt, echoes it back."""

    def __init__(self, name: str = "tester"):
        self.name = name
        self.last_prompt: str | None = None
        self.calls = 0

    async def chat(self, message: str, session_id=None, **kwargs) -> AgentResponse:
        self.last_prompt = message
        self.calls += 1
        return AgentResponse(content=f"echo: {message}")


def _va(fake=None, **kw) -> VoiceAgent:
    return VoiceAgent(agent=fake or FakeAgent(), voice_backend="mock", **kw)


async def test_listen_full_turn():
    fake = FakeAgent()
    va = _va(fake)
    turn = await va.listen("test_hello_world.wav")
    assert isinstance(turn, VoiceTurn)
    assert turn.ok
    assert turn.transcript == "hello world"
    assert fake.last_prompt == "hello world"          # text-only into the agent
    assert turn.reply == "echo: hello world"
    assert b"MOCK AUDIO" in turn.reply_audio           # reply synthesized
    assert turn.response is not None


async def test_listen_without_synthesis():
    fake = FakeAgent()
    va = _va(fake)
    turn = await va.listen("test_billing_question.wav", synthesize=False)
    assert turn.ok
    assert turn.reply == "echo: billing question"
    assert turn.reply_audio == b""                     # synthesis skipped


async def test_listen_writes_reply_audio_file(tmp_path):
    out = tmp_path / "reply.wav"
    va = _va()
    turn = await va.listen("test_hi.wav", output_path=str(out))
    assert turn.ok and out.exists()
    assert turn.reply_audio_path == str(out)


async def test_listen_transcription_failure_skips_agent():
    fake = FakeAgent()
    # 'service' backend on a missing file fails locally (file-not-found) with no network.
    va = VoiceAgent(agent=fake, voice_backend="service")
    turn = await va.listen("/nonexistent/path/to/audio.wav")
    assert not turn.ok
    assert turn.error
    assert fake.calls == 0                              # agent never invoked on bad audio


async def test_detect_emotion_steers_prompt():
    fake = FakeAgent()
    va = _va(fake, detect_emotion=True)
    turn = await va.listen("test_refund_please.wav")
    assert turn.ok
    assert turn.emotion == "neutral"                   # mock emotion
    # Emotion is appended as voice-context steering, transcript stays clean-leading.
    assert fake.last_prompt.startswith("refund please")
    assert "voice context" in fake.last_prompt


async def test_primitives_transcribe_and_speak():
    va = _va()
    t = await va.transcribe("test_quarterly_report.wav")
    assert t.success and t.text == "quarterly report"
    s = await va.speak("done")
    assert s.success and b"MOCK AUDIO" in s.audio_data
