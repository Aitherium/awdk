---
name: awmine
description: "Mine Claude Code, Codex and Pi transcripts for outcomes, lessons, procedures and cost, and pool a team's results. Use to harvest what past sessions learned, compare harnesses, export training/skill candidates, or audit redaction."
---

# awmine — transcripts into lessons, procedures, cost

```bash
awmine run --since 7d                  # mine new bytes (incremental; --full re-mines)
awmine report                          # counts, cost, top lessons/procedures, redaction hits
awmine export --skills                 # consumer shapes: --harvest | --codex | --teach | --skills
awmine merge --out team/ a=DIR b=DIR   # pool several people's output dirs into one team view
awmine share --share --top 5           # OPT-IN: render procedures as awskills candidates
awmine --self-test                     # prove extractors and redaction can still fail
```

Output lands in `$AWMINE_OUT` or `~/.aither/awmine`; `--roots "P;Q"` overrides the
transcript roots (semicolon-separated, not comma). Default roots: `~/.claude/projects`,
plus `~/.codex/sessions` and `~/.pi/agent/sessions` when they exist; `cost.jsonl` names each
session's `harness`. `--deny a,b` adds redaction terms.

**Exit codes:** 0 clean · 1 a measured failure (a row failed redaction/validation, a
residual hit) · 2 could not judge (no transcripts, unreadable manifest, unwritable out).

**Trap:** `share` does nothing without `--share` — the default is off on purpose. Read
`report`'s redaction-hit count before any export leaves the machine.

**Team view:** `merge` rebuilds the team dir (never appends), re-redacts every file with
the team dir's `denylist.txt`, refuses a bad input before touching anything (exit 1), and
counts a directory handed over twice once. `report --out team/` adds per-harness and
per-contributor tables.
