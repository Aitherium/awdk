---
name: awvoice
description: "Speech to text and text to speech against a service you host; make the desk avatar speak. Use to transcribe audio, synthesize a reply, or say something aloud."
---

# awvoice — transcribe, synthesize, speak

```bash
awvoice transcribe clip.wav [-o out.txt]          # STT
awvoice synthesize --help                         # TTS to a file
awvoice say "build is green" [--voice V] [--speed 1.35]   # via the local awdesk bridge
awvoice say --voice local:aither "Hello." [-o out.wav]    # on THIS machine, no service
awvoice synthesize "Hello." --voice local:aither -o hi.wav
awvoice listen                                    # record the mic, print what was heard
awvoice --self-test                               # no external service needed
```

Endpoints: `--stt-url` / `--tts-url` (top-level, before the verb) or
`AWVOICE_STT_URL` / `AWVOICE_TTS_URL`; `say` uses `AWVOICE_DESK_URL`.
`local:aither` needs `pip install "awvoice[local]"` (onnxruntime + espeakng-loader); the
first use fetches the voice sha256-pinned into the shared model dir (`AITHER_VOICE_DIR`),
the same files `adk home voice` uses. `custom:<name>` still goes to your workspace.

**Exit codes:** 0 done · non-zero when the service is unreachable or the input is bad.
`reply` (the Stop-hook entry) always exits 0 by design.

**Trap:** `reply` is opt-in — it speaks only with `AITHER_SPEAK_REPLIES=1`; silence
without it is correct, not a broken hook.
