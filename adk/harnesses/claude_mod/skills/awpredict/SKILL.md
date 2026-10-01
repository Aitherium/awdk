---
name: awpredict
description: "List usable world-model engines and check a class satisfies the WorldModel/EnvironmentAdapter protocol. Use before wiring a new engine or adapter into awpredict."
---

# awpredict — world-model engines and their contracts

```bash
awpredict engines                                  # which engines are usable here, and why not
awpredict --json engines                           # same, machine-readable
awpredict conforms --module my.pkg --name MyModel --protocol WorldModel
awpredict conforms --module my.pkg --name MyEnv --protocol EnvironmentAdapter
```

Output is `ok <engine> <reason>` or `FAIL <engine> <reason>` per engine; a gated-off
engine names the switch (e.g. `lewm` needs `ARC_WORLD_ENGINE=1`).

**Exit codes:** `engines` exits 0 even when some engines FAIL — read the rows.
`conforms` is the gate: non-zero when the class does not satisfy the protocol.

**Trap:** `--json` is a top-level flag — it goes BEFORE the subcommand
(`awpredict --json engines`), not after it.
