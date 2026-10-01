---
name: awdelphi
description: "Run an anonymous multi-round expert panel (Delphi method) to a converged answer with a trace. Use for a contested design decision or an adversarial multi-reviewer review."
---

# awdelphi — anonymous expert panels

```bash
awdelphi run "Should X use a queue or polling?" --experts demiurge,athena,hydra \
    --context "constraints..." --max-rounds 3 [--mode decision|review] [--json]
awdelphi list                         # local runs
awdelphi status RUN_ID
awdelphi show RUN_ID                  # the deliverable + per-round trace
awdelphi cancel RUN_ID
awdelphi self-test                    # prove the machinery offline
```

Experts are reached through the MCP gateway (`--gateway`, `AWDELPHI_GATEWAY`, default
`http://127.0.0.1:8182/mcp`). `--mode review` asks round 1 for numbered findings.

**Failure is loud:** a panel where no round ran says `no rounds were run` rather
than returning an empty answer; check `status` before trusting `show`.

**Trap:** `self-test` is a subcommand here, not `--self-test`. `--arena` /
`--relay-channel` publish the result outward — omit them for a private run.
