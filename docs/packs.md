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


Built from `v3.8.89` (adk 3.8.89).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.89` | [aither-3.8.89.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.89/aither-3.8.89.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.89` | [analyst-3.8.89.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.89/analyst-3.8.89.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.89` | [bead-space-3.8.89.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.89/bead-space-3.8.89.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.89` | [claude-code-3.8.89.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.89/claude-code-3.8.89.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.89` | [dgg_research-3.8.89.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.89/dgg_research-3.8.89.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.89` | [gobbonet-3.8.89.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.89/gobbonet-3.8.89.tar.gz) | 65.9 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.89` | [hermes-3.8.89.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.89/hermes-3.8.89.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.89` | [iris-3.8.89.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.89/iris-3.8.89.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.89` | [openclaw-3.8.89.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.89/openclaw-3.8.89.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.89` | [persona-3.8.89.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.89/persona-3.8.89.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.89` — skills  
  `sha256:a3ed9d444bb945c4…`
- **analyst** `3.8.89` — agent config, skills  
  `sha256:f8139c9feb9fa685…`
- **bead-space** `3.8.89` — brain pack only  
  `sha256:f170fa75c84a8258…`
- **claude-code** `3.8.89` — agent config, skills  
  `sha256:a5c95445f78dfda6…`
- **dgg_research** `3.8.89` — agent config, skills  
  `sha256:55b8a56321160766…`
- **gobbonet** `3.8.89` — agent config, Python  
  `sha256:2dd8c91a4e9a6139…`
- **hermes** `3.8.89` — agent config, skills  
  `sha256:02234be23c3205b1…`
- **iris** `3.8.89` — skills  
  `sha256:164fb5f46e867d4c…`
- **openclaw** `3.8.89` — agent config, skills  
  `sha256:867e87eba8dce20d…`
- **persona** `3.8.89` — brain pack only  
  `sha256:8112b6378ebc301a…`
