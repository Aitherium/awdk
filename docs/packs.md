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


Built from `v3.8.91` (adk 3.8.91).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.91` | [aither-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/aither-3.8.91.tar.gz) | 10.0 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.91` | [analyst-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/analyst-3.8.91.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.91` | [bead-space-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/bead-space-3.8.91.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.91` | [claude-code-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/claude-code-3.8.91.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.91` | [dgg_research-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/dgg_research-3.8.91.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.91` | [gobbonet-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/gobbonet-3.8.91.tar.gz) | 65.9 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.91` | [hermes-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/hermes-3.8.91.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.91` | [iris-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/iris-3.8.91.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.91` | [openclaw-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/openclaw-3.8.91.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.91` | [persona-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/persona-3.8.91.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.91` — skills  
  `sha256:b99a25b5673e7ada…`
- **analyst** `3.8.91` — agent config, skills  
  `sha256:32610c293c8b8b5b…`
- **bead-space** `3.8.91` — brain pack only  
  `sha256:c565a2dc454e86ae…`
- **claude-code** `3.8.91` — agent config, skills  
  `sha256:e89115400f446c49…`
- **dgg_research** `3.8.91` — agent config, skills  
  `sha256:21651ffbbad284fb…`
- **gobbonet** `3.8.91` — agent config, Python  
  `sha256:80d3d0f10db878a3…`
- **hermes** `3.8.91` — agent config, skills  
  `sha256:541b2fc8bf1129be…`
- **iris** `3.8.91` — skills  
  `sha256:4b4c4f967d8cc646…`
- **openclaw** `3.8.91` — agent config, skills  
  `sha256:dae57f0628885836…`
- **persona** `3.8.91` — brain pack only  
  `sha256:536f88f237c54fc6…`
