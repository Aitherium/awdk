---
name: awscreen
description: "Find a clickable UI element in a screenshot by plain-language description. Use for vision-based GUI automation when there is no DOM or accessibility tree."
---

# awscreen — find an element by what it looks like

```bash
export AWSCREEN_URL=<local vision endpoint>     # falls back to AWVISION_URL
awscreen shot.png "the blue Save button"                # coordinates + box
awscreen shot.png "search field" --format json
awscreen --self-test                                    # offline
```

**Exit codes:** 0 element(s) found · 1 nothing matched the description · 2 cannot
proceed (no endpoint/key, unreadable image, API error).

**Trap:** 1 and 2 mean different things — "not on screen" versus "could not look".
Retry or re-screenshot on 1; fix configuration on 2. For web pages prefer a DOM tool
(awbrowse, AitherBrowser) — pixels are the last resort.
