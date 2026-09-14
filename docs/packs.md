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


Built from `v3.8.19` (adk 3.8.19).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.19` | [aither-3.8.19.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.19/aither-3.8.19.tar.gz) | 4.4 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.19` | [analyst-3.8.19.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.19/analyst-3.8.19.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.19` | [bead-space-3.8.19.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.19/bead-space-3.8.19.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.19` | [claude-code-3.8.19.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.19/claude-code-3.8.19.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.19` | [dgg_research-3.8.19.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.19/dgg_research-3.8.19.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.19` | [gobbonet-3.8.19.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.19/gobbonet-3.8.19.tar.gz) | 45.7 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.19` | [hermes-3.8.19.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.19/hermes-3.8.19.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.19` | [iris-3.8.19.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.19/iris-3.8.19.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.19` | [openclaw-3.8.19.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.19/openclaw-3.8.19.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.19` | [persona-3.8.19.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.19/persona-3.8.19.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.19` — skills  
  `sha256:e3ee104bc1fcb818…`
- **analyst** `3.8.19` — agent config, skills  
  `sha256:ce6ccebdaa139f02…`
- **bead-space** `3.8.19` — brain pack only  
  `sha256:ea4db543a44946a4…`
- **claude-code** `3.8.19` — agent config, skills  
  `sha256:91cacc0caec8cbe0…`
- **dgg_research** `3.8.19` — agent config, skills  
  `sha256:5651c30c5e13592b…`
- **gobbonet** `3.8.19` — agent config, Python  
  `sha256:00cbc142054c1265…`
- **hermes** `3.8.19` — agent config, skills  
  `sha256:4ac5f33ad4cb5cc2…`
- **iris** `3.8.19` — skills  
  `sha256:cfacd1d95f11d331…`
- **openclaw** `3.8.19` — agent config, skills  
  `sha256:4467ec06880eebcb…`
- **persona** `3.8.19` — brain pack only  
  `sha256:02bb46c05f070608…`
