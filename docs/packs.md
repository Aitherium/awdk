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


Built from `v3.8.79` (adk 3.8.79).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.79` | [aither-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/aither-3.8.79.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.79` | [analyst-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/analyst-3.8.79.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.79` | [bead-space-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/bead-space-3.8.79.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.79` | [claude-code-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/claude-code-3.8.79.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.79` | [dgg_research-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/dgg_research-3.8.79.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.79` | [gobbonet-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/gobbonet-3.8.79.tar.gz) | 65.7 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.79` | [hermes-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/hermes-3.8.79.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.79` | [iris-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/iris-3.8.79.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.79` | [openclaw-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/openclaw-3.8.79.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.79` | [persona-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/persona-3.8.79.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.79` — skills  
  `sha256:47b3233470bb85aa…`
- **analyst** `3.8.79` — agent config, skills  
  `sha256:5d135f673c383e36…`
- **bead-space** `3.8.79` — brain pack only  
  `sha256:394ae3287a5eaa59…`
- **claude-code** `3.8.79` — agent config, skills  
  `sha256:d10af0cca9678602…`
- **dgg_research** `3.8.79` — agent config, skills  
  `sha256:d3af9b0d5cb4f7b9…`
- **gobbonet** `3.8.79` — agent config, Python  
  `sha256:fdfcd7ce8a8b0d3a…`
- **hermes** `3.8.79` — agent config, skills  
  `sha256:758dc992cc23d8eb…`
- **iris** `3.8.79` — skills  
  `sha256:aa3c33c3f381d991…`
- **openclaw** `3.8.79` — agent config, skills  
  `sha256:920fbfca66a8299b…`
- **persona** `3.8.79` — brain pack only  
  `sha256:99ba6a085948824c…`
