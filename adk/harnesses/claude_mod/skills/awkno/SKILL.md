---
name: awkno
description: "The man page for the Aither World: look up any aw* brick, law or guide offline. Use on 'what does awX do', 'which brick handles Y', or before building a new aw* tool."
---

# awkno — man pages for the aw* stack

```bash
awkno                         # overview
awkno list                    # every topic: guides, bricks, stacks, laws
awkno awrise                  # one brick's page
awkno law 5                   # a numbered law
awkno -k "schedule"           # apropos search (like man -k)
awkno awgraph --json          # machine-readable page
```

`--plain` strips ANSI for piping. `awkno doctor` reports install/config state.

**Exit codes:** 0 even for an unknown topic (it prints a not-found line) — read the
output, not the code. `--self-test` is the only gate.

**Trap:** awkno pages describe the PUBLIC brick; for how a brick is wired in this
repo, read `AitherOS/packages/<brick>/README.md`. Before building anything new in
the family, `awkno -k <verb>` first — the brick you want may already exist.
