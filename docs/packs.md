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


Built from `v3.8.67` (adk 3.8.67).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.67` | [aither-3.8.67.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.67/aither-3.8.67.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.67` | [analyst-3.8.67.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.67/analyst-3.8.67.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.67` | [bead-space-3.8.67.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.67/bead-space-3.8.67.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.67` | [claude-code-3.8.67.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.67/claude-code-3.8.67.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.67` | [dgg_research-3.8.67.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.67/dgg_research-3.8.67.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.67` | [gobbonet-3.8.67.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.67/gobbonet-3.8.67.tar.gz) | 65.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.67` | [hermes-3.8.67.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.67/hermes-3.8.67.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.67` | [iris-3.8.67.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.67/iris-3.8.67.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.67` | [openclaw-3.8.67.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.67/openclaw-3.8.67.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.67` | [persona-3.8.67.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.67/persona-3.8.67.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.67` — skills  
  `sha256:abf8aa58791f4a8d…`
- **analyst** `3.8.67` — agent config, skills  
  `sha256:2e67ed247d6a00bc…`
- **bead-space** `3.8.67` — brain pack only  
  `sha256:73141d7dc2cabb70…`
- **claude-code** `3.8.67` — agent config, skills  
  `sha256:b1340f7826a0b0b9…`
- **dgg_research** `3.8.67` — agent config, skills  
  `sha256:d60e770d9ef720da…`
- **gobbonet** `3.8.67` — agent config, Python  
  `sha256:5f4e2e37436644b6…`
- **hermes** `3.8.67` — agent config, skills  
  `sha256:c71e7570d1d9e954…`
- **iris** `3.8.67` — skills  
  `sha256:6c95e26d2b4a5d22…`
- **openclaw** `3.8.67` — agent config, skills  
  `sha256:5130c98666cd787b…`
- **persona** `3.8.67` — brain pack only  
  `sha256:4970c7c3db724c6d…`
