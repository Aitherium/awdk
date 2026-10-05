---
name: awvision
description: "Ask a vision model about an image, the screen or a camera frame; compare two images; watch a source for changes. Use when you need to see pixels, not read text."
---

# awvision — ask questions about images

```bash
awvision ask photo.png "what error is shown?"
awvision describe photo.png
awvision compare before.png after.png
awvision see --screen --prompt "what app is focused?"    # or --rtsp URL / --device NAME
awvision watch --help                                     # look only when the picture changes
awvision --self-test
```

Endpoint/model: `--endpoint` / `--model` (top-level, before the verb) or
`AWVISION_URL` / `AWVISION_MODEL`. With neither set it resolves itself (0.3.2+): the
local MicroScheduler `https://127.0.0.1:8150` if it answers `/v1/models`, else the Spark
mesh address; model = the first of `gemma4-12b`, `bonsai2-27b` the endpoint reports
available. Thinking is off and `max_tokens` 512 by default (`AWVISION_THINKING=1`,
`AWVISION_MAX_TOKENS`, `AWVISION_TIMEOUT`).
**Trap:** "Cannot reach ... 100.64.0.38:8124" with no env set means the scheduler probe
timed out (it stalls TLS handshakes under load) — retry, or pin `AWVISION_URL`.

**Exit codes:** 0 answered · non-zero on endpoint/image error.

**Trap:** frames are NOT kept by default; `see --keep-frames` opts in and
`awvision forget --older-than HOURS` (or `--all`) removes them. `--publish/--say`
emit room events — do not add them to a routine without meaning to.
