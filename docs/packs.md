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


Built from `v3.8.40` (adk 3.8.40).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.40` | [aither-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/aither-3.8.40.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.40` | [analyst-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/analyst-3.8.40.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.40` | [bead-space-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/bead-space-3.8.40.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.40` | [claude-code-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/claude-code-3.8.40.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.40` | [dgg_research-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/dgg_research-3.8.40.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.40` | [gobbonet-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/gobbonet-3.8.40.tar.gz) | 47.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.40` | [hermes-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/hermes-3.8.40.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.40` | [iris-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/iris-3.8.40.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.40` | [openclaw-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/openclaw-3.8.40.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.40` | [persona-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/persona-3.8.40.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.40` — skills  
  `sha256:45b6fe1a68d7d85c…`
- **analyst** `3.8.40` — agent config, skills  
  `sha256:b6f63e6bb0279fb6…`
- **bead-space** `3.8.40` — brain pack only  
  `sha256:eccd03ae499b42c2…`
- **claude-code** `3.8.40` — agent config, skills  
  `sha256:ea55a86a0e303875…`
- **dgg_research** `3.8.40` — agent config, skills  
  `sha256:57a5a4c55f45ec18…`
- **gobbonet** `3.8.40` — agent config, Python  
  `sha256:061462a1055d3fe4…`
- **hermes** `3.8.40` — agent config, skills  
  `sha256:08e8c96d47e8ddbb…`
- **iris** `3.8.40` — skills  
  `sha256:911dcb2bcf6b229f…`
- **openclaw** `3.8.40` — agent config, skills  
  `sha256:73272116ede52598…`
- **persona** `3.8.40` — brain pack only  
  `sha256:8821bc82ce3e843c…`
