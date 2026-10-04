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


Built from `v3.8.58` (adk 3.8.58).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.58` | [aither-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/aither-3.8.58.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.58` | [analyst-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/analyst-3.8.58.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.58` | [bead-space-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/bead-space-3.8.58.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.58` | [claude-code-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/claude-code-3.8.58.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.58` | [dgg_research-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/dgg_research-3.8.58.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.58` | [gobbonet-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/gobbonet-3.8.58.tar.gz) | 58.2 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.58` | [hermes-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/hermes-3.8.58.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.58` | [iris-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/iris-3.8.58.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.58` | [openclaw-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/openclaw-3.8.58.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.58` | [persona-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/persona-3.8.58.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.58` — skills  
  `sha256:e6846abf9a42a967…`
- **analyst** `3.8.58` — agent config, skills  
  `sha256:754f6cd3304344ee…`
- **bead-space** `3.8.58` — brain pack only  
  `sha256:c6410ef9949fa110…`
- **claude-code** `3.8.58` — agent config, skills  
  `sha256:e6537b0f9149f567…`
- **dgg_research** `3.8.58` — agent config, skills  
  `sha256:b05539eaf273717d…`
- **gobbonet** `3.8.58` — agent config, Python  
  `sha256:3d2dfbb60af0c19f…`
- **hermes** `3.8.58` — agent config, skills  
  `sha256:5c3b3997b047170d…`
- **iris** `3.8.58` — skills  
  `sha256:bc1b195844f383b3…`
- **openclaw** `3.8.58` — agent config, skills  
  `sha256:2657a54cc471cdd0…`
- **persona** `3.8.58` — brain pack only  
  `sha256:abe41e036ca5a3d6…`
