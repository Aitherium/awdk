---
name: awbrain
description: "Turn a folder of notes/sessions into a linked-markdown wiki with claims pinned to evidence; ask, verify a claim, trace a figure. Use for grounded recall over your own history."
---

# awbrain — your history as a wiki with receipts

```bash
awbrain harvest ./notes              # build/refresh the wiki (default --brain ./brain)
awbrain ask "what did we decide about X?" --k 5
awbrain verify "the cache is 2 GiB"  # walk a claim back to the file and line
awbrain trace "42%"                  # which source record (and connector) a figure came from
awbrain status                       # sources, pages, claims, embedder mode
awbrain watch ./notes --once         # re-harvest what changed
```

The brain is plain markdown you can open in an editor. `--json` for machine output.

**Exit codes:** 0 even when `verify` answers "claim not in the ledger" — read the
verdict line; the exit code is not the answer.

**Trap:** `--brain` defaults to `./brain` relative to the CWD — run from a different
directory and you get an empty brain. `status` says whether retrieval used the fleet
embedder or the lexical fallback; a lexical answer is weaker, not wrong.
