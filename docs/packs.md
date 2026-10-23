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


Built from `v3.8.90` (adk 3.8.90).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.90` | [aither-3.8.90.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.90/aither-3.8.90.tar.gz) | 10.0 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.90` | [analyst-3.8.90.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.90/analyst-3.8.90.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.90` | [bead-space-3.8.90.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.90/bead-space-3.8.90.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.90` | [claude-code-3.8.90.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.90/claude-code-3.8.90.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.90` | [dgg_research-3.8.90.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.90/dgg_research-3.8.90.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.90` | [gobbonet-3.8.90.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.90/gobbonet-3.8.90.tar.gz) | 65.8 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.90` | [hermes-3.8.90.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.90/hermes-3.8.90.tar.gz) | 4.8 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.90` | [iris-3.8.90.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.90/iris-3.8.90.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.90` | [openclaw-3.8.90.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.90/openclaw-3.8.90.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.90` | [persona-3.8.90.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.90/persona-3.8.90.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.90` — skills  
  `sha256:0b458d49f54223b0…`
- **analyst** `3.8.90` — agent config, skills  
  `sha256:635a65b79aba8c89…`
- **bead-space** `3.8.90` — brain pack only  
  `sha256:201ba39fdd1df797…`
- **claude-code** `3.8.90` — agent config, skills  
  `sha256:a9cdc001d92133d6…`
- **dgg_research** `3.8.90` — agent config, skills  
  `sha256:47a3ba4a80cc5840…`
- **gobbonet** `3.8.90` — agent config, Python  
  `sha256:e5e4f8408d54a8a4…`
- **hermes** `3.8.90` — agent config, skills  
  `sha256:934aa20e8ae517f8…`
- **iris** `3.8.90` — skills  
  `sha256:ef7dc4a6caf8cc26…`
- **openclaw** `3.8.90` — agent config, skills  
  `sha256:78aad7525ec953c8…`
- **persona** `3.8.90` — brain pack only  
  `sha256:a43e78993897a601…`
