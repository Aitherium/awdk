---
name: awflow
description: "awflow: the deterministic, journaled multi-agent workflow runtime (a library). Use when writing a workflow script that needs replay, resume and a hard token budget."
---

# awflow — deterministic workflow runtime

It is a LIBRARY; the CLI only self-tests.

```bash
awflow --help
awflow --self-test          # stub workflow offline, no LLM calls; 0 pass, 1 fail
```

```python
from awflow import agent, parallel, pipeline, phase, log, get_budget, run_workflow

async def main():
    await phase("triage")
    a, b = await parallel([agent("..."), agent("...")])   # barrier
    await pipeline(items, stage1, stage2)                  # no barrier

await run_workflow(main, journal_path=None, budget_tokens=200_000)
```

Re-running the same script replays cached answers from the journal (call hashing);
exceeding the budget raises immediately.

**Exit codes (CLI):** 0 pass · 1 self-test failed · 2 unrecognized argument.

**Trap:** `parallel()`/`pipeline()` cap at 4096 thunks/items; a failed pipeline stage
DROPS that item rather than raising — check the result length.
