---
name: awresearch
description: "Ask a research question and get a cited report whose claims were checked against fetched sources. Use for external research where you need citations, not a plausible essay."
---

# awresearch — cited, checkable research

```bash
awresearch --question "What changed in X between v2 and v3?"
awresearch --question "..." --depth deep --output json --out-file report.json
awresearch --version
```

No subcommands — it is one flag-driven command. `--depth standard|deep`,
`--output markdown|json`.

Config: `AWRESEARCH_SEARCH_BACKEND` (ddgs, searxng, ...; default multi-engine),
`AITHER_DATA_DIR` for artifacts. An LLM key/endpoint must be reachable.

**Exit codes:** 0 report written · non-zero on a failed run.

**Trap:** every search and fetch is a real HTTP call — a deep run takes 30-60 s per
question. Sources are cross-checked against retrieved pages, not verified as true:
read the citations before repeating a claim.
