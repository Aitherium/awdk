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


Built from `v3.8.41` (adk 3.8.41).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.41` | [aither-3.8.41.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.41/aither-3.8.41.tar.gz) | 9.9 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.41` | [analyst-3.8.41.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.41/analyst-3.8.41.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.41` | [bead-space-3.8.41.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.41/bead-space-3.8.41.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.41` | [claude-code-3.8.41.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.41/claude-code-3.8.41.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.41` | [dgg_research-3.8.41.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.41/dgg_research-3.8.41.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.41` | [gobbonet-3.8.41.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.41/gobbonet-3.8.41.tar.gz) | 58.3 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.41` | [hermes-3.8.41.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.41/hermes-3.8.41.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.41` | [iris-3.8.41.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.41/iris-3.8.41.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.41` | [openclaw-3.8.41.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.41/openclaw-3.8.41.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.41` | [persona-3.8.41.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.41/persona-3.8.41.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.41` — skills  
  `sha256:909e85e92209d1e0…`
- **analyst** `3.8.41` — agent config, skills  
  `sha256:382622281ebf5fb6…`
- **bead-space** `3.8.41` — brain pack only  
  `sha256:26c3e4c31bb6b6a2…`
- **claude-code** `3.8.41` — agent config, skills  
  `sha256:a8247a400c8c520e…`
- **dgg_research** `3.8.41` — agent config, skills  
  `sha256:0b42ec3705c7c61e…`
- **gobbonet** `3.8.41` — agent config, Python  
  `sha256:78767eaa1663ca3c…`
- **hermes** `3.8.41` — agent config, skills  
  `sha256:48c309a98351f674…`
- **iris** `3.8.41` — skills  
  `sha256:5b0d57de78fa23cb…`
- **openclaw** `3.8.41` — agent config, skills  
  `sha256:7982125415a0fef6…`
- **persona** `3.8.41` — brain pack only  
  `sha256:652c2fc384b98551…`
