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
  `sha256:ca038413eed91ca3…`
- **analyst** `3.8.20` — agent config, skills  
  `sha256:9d72cc1dd895f14a…`
- **bead-space** `3.8.20` — brain pack only  
  `sha256:c29e266cf0adf87f…`
- **claude-code** `3.8.20` — agent config, skills  
  `sha256:01b41e59d0e49471…`
- **dgg_research** `3.8.20` — agent config, skills  
  `sha256:7afc90de172b1252…`
- **gobbonet** `3.8.20` — agent config, Python  
  `sha256:bb4cb4caa37d05a7…`
- **hermes** `3.8.20` — agent config, skills  
  `sha256:0be59859f82bf9bc…`
- **iris** `3.8.20` — skills  
  `sha256:5f7765353da87782…`
- **openclaw** `3.8.20` — agent config, skills  
  `sha256:b5492e3802967607…`
- **persona** `3.8.20` — brain pack only  
  `sha256:8ed53c849a313b8a…`
