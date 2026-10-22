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


Built from `v3.8.85` (adk 3.8.85).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.85` | [aither-3.8.85.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.85/aither-3.8.85.tar.gz) | 9.9 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.85` | [analyst-3.8.85.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.85/analyst-3.8.85.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.85` | [bead-space-3.8.85.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.85/bead-space-3.8.85.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.85` | [claude-code-3.8.85.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.85/claude-code-3.8.85.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.85` | [dgg_research-3.8.85.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.85/dgg_research-3.8.85.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.85` | [gobbonet-3.8.85.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.85/gobbonet-3.8.85.tar.gz) | 65.9 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.85` | [hermes-3.8.85.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.85/hermes-3.8.85.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.85` | [iris-3.8.85.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.85/iris-3.8.85.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.85` | [openclaw-3.8.85.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.85/openclaw-3.8.85.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.85` | [persona-3.8.85.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.85/persona-3.8.85.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.85` — skills  
  `sha256:e42a84c52fcb7f8d…`
- **analyst** `3.8.85` — agent config, skills  
  `sha256:39a735f7640cb857…`
- **bead-space** `3.8.85` — brain pack only  
  `sha256:d18895496f2ebc6f…`
- **claude-code** `3.8.85` — agent config, skills  
  `sha256:f60e975438b657d3…`
- **dgg_research** `3.8.85` — agent config, skills  
  `sha256:72b26661a1fe90e0…`
- **gobbonet** `3.8.85` — agent config, Python  
  `sha256:49573c39c62c4b0e…`
- **hermes** `3.8.85` — agent config, skills  
  `sha256:d7b0463006e54c73…`
- **iris** `3.8.85` — skills  
  `sha256:60477076479321f4…`
- **openclaw** `3.8.85` — agent config, skills  
  `sha256:f860ee8b443ea3d9…`
- **persona** `3.8.85` — brain pack only  
  `sha256:222cb08e5f9fa15a…`
