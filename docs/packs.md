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


Built from `v3.8.56` (adk 3.8.56).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.56` | [aither-3.8.56.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.56/aither-3.8.56.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.56` | [analyst-3.8.56.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.56/analyst-3.8.56.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.56` | [bead-space-3.8.56.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.56/bead-space-3.8.56.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.56` | [claude-code-3.8.56.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.56/claude-code-3.8.56.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.56` | [dgg_research-3.8.56.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.56/dgg_research-3.8.56.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.56` | [gobbonet-3.8.56.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.56/gobbonet-3.8.56.tar.gz) | 58.2 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.56` | [hermes-3.8.56.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.56/hermes-3.8.56.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.56` | [iris-3.8.56.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.56/iris-3.8.56.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.56` | [openclaw-3.8.56.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.56/openclaw-3.8.56.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.56` | [persona-3.8.56.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.56/persona-3.8.56.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.56` — skills  
  `sha256:70f62bde99959d54…`
- **analyst** `3.8.56` — agent config, skills  
  `sha256:b14a7047312af769…`
- **bead-space** `3.8.56` — brain pack only  
  `sha256:8f1df9f476bb6dc5…`
- **claude-code** `3.8.56` — agent config, skills  
  `sha256:40900564ec5e3248…`
- **dgg_research** `3.8.56` — agent config, skills  
  `sha256:a80e0148415bb3d7…`
- **gobbonet** `3.8.56` — agent config, Python  
  `sha256:296f3bdcf24a48ea…`
- **hermes** `3.8.56` — agent config, skills  
  `sha256:15c82f0a8514f306…`
- **iris** `3.8.56` — skills  
  `sha256:2d488015f8f28127…`
- **openclaw** `3.8.56` — agent config, skills  
  `sha256:5d4de3d6143e5b8b…`
- **persona** `3.8.56` — brain pack only  
  `sha256:d158feaf5f1f827c…`
