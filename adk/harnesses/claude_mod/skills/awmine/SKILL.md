---
name: awmine
description: "Mine agent transcripts for outcomes, lessons, procedures and cost. Use to harvest what past sessions learned, export training/skill candidates, or audit redaction."
---

# awmine — transcripts into lessons, procedures, cost

```bash
awmine run --since 7d                  # mine new bytes (incremental; --full re-mines)
awmine report                          # counts, cost, top lessons/procedures, redaction hits
awmine export --skills                 # consumer shapes: --harvest | --codex | --teach | --skills
awmine share --share --top 5           # OPT-IN: render procedures as awskills candidates
awmine --self-test                     # prove extractors and redaction can still fail
```

Output lands in `$AWMINE_OUT` or `~/.aither/awmine`; `--roots "P;Q"` overrides the
transcript roots (semicolon-separated, not comma). `--deny a,b` adds redaction terms.

**Exit codes:** 0 clean · 1 a measured failure (a row failed redaction/validation, a
residual hit) · 2 could not judge (no transcripts, unreadable manifest, unwritable out).

**Trap:** `share` does nothing without `--share` — the default is off on purpose. Read
`report`'s redaction-hit count before any export leaves the machine.
