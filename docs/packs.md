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


Built from `v3.8.72` (adk 3.8.72).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.72` | [aither-3.8.72.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.72/aither-3.8.72.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.72` | [analyst-3.8.72.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.72/analyst-3.8.72.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.72` | [bead-space-3.8.72.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.72/bead-space-3.8.72.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.72` | [claude-code-3.8.72.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.72/claude-code-3.8.72.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.72` | [dgg_research-3.8.72.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.72/dgg_research-3.8.72.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.72` | [gobbonet-3.8.72.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.72/gobbonet-3.8.72.tar.gz) | 65.7 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.72` | [hermes-3.8.72.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.72/hermes-3.8.72.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.72` | [iris-3.8.72.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.72/iris-3.8.72.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.72` | [openclaw-3.8.72.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.72/openclaw-3.8.72.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.72` | [persona-3.8.72.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.72/persona-3.8.72.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.72` — skills  
  `sha256:027d634e250fc721…`
- **analyst** `3.8.72` — agent config, skills  
  `sha256:292510de213cfc6b…`
- **bead-space** `3.8.72` — brain pack only  
  `sha256:08a8b8d44542d587…`
- **claude-code** `3.8.72` — agent config, skills  
  `sha256:6801b033177eb62e…`
- **dgg_research** `3.8.72` — agent config, skills  
  `sha256:2f5a20bb7d252854…`
- **gobbonet** `3.8.72` — agent config, Python  
  `sha256:ef9314043dc3cf68…`
- **hermes** `3.8.72` — agent config, skills  
  `sha256:e48ee1ea3a4a323c…`
- **iris** `3.8.72` — skills  
  `sha256:dfef47210d1f7c39…`
- **openclaw** `3.8.72` — agent config, skills  
  `sha256:f1e71586abfcf6e9…`
- **persona** `3.8.72` — brain pack only  
  `sha256:7a0ceb564e592288…`
