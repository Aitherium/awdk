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
`AWVISION_URL` / `AWVISION_MODEL`. The built-in default is a fleet tailnet address —
set `AWVISION_URL` on any other machine.

**Exit codes:** 0 answered · non-zero on endpoint/image error.

**Trap:** frames are NOT kept by default; `see --keep-frames` opts in and
`awvision forget --older-than HOURS` (or `--all`) removes them. `--publish/--say`
emit room events — do not add them to a routine without meaning to.
