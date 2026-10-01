---
name: awrecurse
description: "Ask a question of a file larger than the model's context window via recursive chunked querying. Use when a log, dump or document is too big to read whole."
---

# awrecurse — query a context bigger than the window

```bash
export AWRECURSE_URL=https://<origin>  AWRECURSE_TOKEN=<bearer>   # or --url/--token
awrecurse health
awrecurse ask --file big.log -q "which request first returned 503?"
awrecurse ask --file dump.txt -q "..." --chunk-size 8000 --max-iterations 20
awrecurse --self-test                                             # offline contract check
```

The answer reports which parts were examined, so it can be checked, not trusted.

**Exit codes:** 0 answered · non-zero on transport/service error.

**Trap:** top-level flags (`--url`, `--token`, `--json`) go BEFORE `ask`. For a file
you can grep, `rg` is cheaper — use awrecurse for questions grep cannot phrase.
