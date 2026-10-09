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


Built from `v3.8.65` (adk 3.8.65).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.65` | [aither-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/aither-3.8.65.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.65` | [analyst-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/analyst-3.8.65.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.65` | [bead-space-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/bead-space-3.8.65.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.65` | [claude-code-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/claude-code-3.8.65.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.65` | [dgg_research-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/dgg_research-3.8.65.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.65` | [gobbonet-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/gobbonet-3.8.65.tar.gz) | 65.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.65` | [hermes-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/hermes-3.8.65.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.65` | [iris-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/iris-3.8.65.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.65` | [openclaw-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/openclaw-3.8.65.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.65` | [persona-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/persona-3.8.65.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.65` — skills  
  `sha256:74a0889a42a38d9e…`
- **analyst** `3.8.65` — agent config, skills  
  `sha256:e707132b11aa809c…`
- **bead-space** `3.8.65` — brain pack only  
  `sha256:aaf3bb7f5c7f50df…`
- **claude-code** `3.8.65` — agent config, skills  
  `sha256:24a152cd29dca6e5…`
- **dgg_research** `3.8.65` — agent config, skills  
  `sha256:e2a7104f2cb174f9…`
- **gobbonet** `3.8.65` — agent config, Python  
  `sha256:57c21766930568bb…`
- **hermes** `3.8.65` — agent config, skills  
  `sha256:2702a02ae86bf5f6…`
- **iris** `3.8.65` — skills  
  `sha256:2942e70ce2a2e3c9…`
- **openclaw** `3.8.65` — agent config, skills  
  `sha256:3ec81456908d5e18…`
- **persona** `3.8.65` — brain pack only  
  `sha256:8e85ee9adb18118e…`
