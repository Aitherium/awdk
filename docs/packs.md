# Agent packs

Each pack is a standalone download. Take one, run its installer, and adk finds
it — you do not need the rest of the framework to try a single pack.

```bash
tar xzf <pack>-<version>.tar.gz
python <pack>/install.py
```

That copies the pack to `~/.aither/packs/<name>/`, a location adk discovers with
no configuration, then **verifies** the pack is discoverable rather than assuming
it. If adk is not installed yet the installer says so and still places the files,
so the order does not matter:

```bash
pip install aither-adk
```

Every artifact ships a `.sha256` next to it. Verify before you trust it:

```bash
sha256sum -c <pack>-<version>.sha256
```


Built from `v3.8.61` (adk 3.8.61).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.61` | [aither-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/aither-3.8.61.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.61` | [analyst-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/analyst-3.8.61.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.61` | [bead-space-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/bead-space-3.8.61.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.61` | [claude-code-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/claude-code-3.8.61.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.61` | [dgg_research-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/dgg_research-3.8.61.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.61` | [gobbonet-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/gobbonet-3.8.61.tar.gz) | 58.5 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.61` | [hermes-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/hermes-3.8.61.tar.gz) | 4.8 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.61` | [iris-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/iris-3.8.61.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.61` | [openclaw-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/openclaw-3.8.61.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.61` | [persona-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/persona-3.8.61.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.61` — skills  
  `sha256:228b19f1f107b481…`
- **analyst** `3.8.61` — agent config, skills  
  `sha256:1d73ce3e67973481…`
- **bead-space** `3.8.61` — brain pack only  
  `sha256:477e37de6deafa1b…`
- **claude-code** `3.8.61` — agent config, skills  
  `sha256:dbd08f90c5f9f07c…`
- **dgg_research** `3.8.61` — agent config, skills  
  `sha256:b60e5d86af5828a4…`
- **gobbonet** `3.8.61` — agent config, Python  
  `sha256:12d1a4ba4d1b0896…`
- **hermes** `3.8.61` — agent config, skills  
  `sha256:fcf271f82928c6c1…`
- **iris** `3.8.61` — skills  
  `sha256:e5f61d1f85ef911d…`
- **openclaw** `3.8.61` — agent config, skills  
  `sha256:8d7e47b423ef00b2…`
- **persona** `3.8.61` — brain pack only  
  `sha256:24a77c16b971a0b8…`
