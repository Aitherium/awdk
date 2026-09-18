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


Built from `v3.8.20` (adk 3.8.20).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.20` | [aither-3.8.20.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.20/aither-3.8.20.tar.gz) | 4.4 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.20` | [analyst-3.8.20.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.20/analyst-3.8.20.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.20` | [bead-space-3.8.20.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.20/bead-space-3.8.20.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.20` | [claude-code-3.8.20.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.20/claude-code-3.8.20.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.20` | [dgg_research-3.8.20.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.20/dgg_research-3.8.20.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.20` | [gobbonet-3.8.20.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.20/gobbonet-3.8.20.tar.gz) | 47.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.20` | [hermes-3.8.20.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.20/hermes-3.8.20.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.20` | [iris-3.8.20.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.20/iris-3.8.20.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.20` | [openclaw-3.8.20.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.20/openclaw-3.8.20.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.20` | [persona-3.8.20.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.20/persona-3.8.20.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.20` — skills  
  `sha256:9a6aba190bf42d0d…`
- **analyst** `3.8.20` — agent config, skills  
  `sha256:e00317df77027239…`
- **bead-space** `3.8.20` — brain pack only  
  `sha256:445d795e7b44e961…`
- **claude-code** `3.8.20` — agent config, skills  
  `sha256:5f9d8f107adbfa5f…`
- **dgg_research** `3.8.20` — agent config, skills  
  `sha256:7e3ebe2c0d1e1f46…`
- **gobbonet** `3.8.20` — agent config, Python  
  `sha256:875c047de620688f…`
- **hermes** `3.8.20` — agent config, skills  
  `sha256:15a37c4d26aa4282…`
- **iris** `3.8.20` — skills  
  `sha256:43cb6623cb7965e5…`
- **openclaw** `3.8.20` — agent config, skills  
  `sha256:8ed488291da491cc…`
- **persona** `3.8.20` — brain pack only  
  `sha256:a1ca03c17369cff9…`
