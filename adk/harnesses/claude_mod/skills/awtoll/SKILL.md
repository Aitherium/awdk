---
name: awtoll
description: "Measure what each tool call costs in context from your transcripts. Use on 'what is eating my context', before choosing between two commands, or to gate waste ratcheting down."
---

# awtoll — context toll per tool shape

```bash
awtoll scan --limit 20 --top 15        # toll table: tokens each tool shape admitted
awtoll repeats --limit 20              # the same read paid for more than once
awtoll versus "cmd A" "cmd B"          # run both, compare what reading each costs
awtoll check --init                    # pin a ledger (./awtoll.json) at today's waste
awtoll check                           # gate: waste_ratio may only go down
```

`--root P` (repeatable) points at a transcript file/dir; default is this machine's
Claude Code transcripts. `--json` for machine output.

**Exit codes:** 0 measured, clean · 1 a rule violated (waste above the pin) ·
2 could not judge (no transcripts, no tool calls, unreadable ledger).

**Trap:** token counts are ESTIMATED (chars/3.46), and the "cumulative prompt tokens"
line is not a denominator — every turn re-sends context, so an early expensive read is
paid on every later turn. Compare shapes by `repeats`, not by the grand total.
