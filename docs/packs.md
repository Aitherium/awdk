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


Built from `v3.8.60` (adk 3.8.60).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.60` | [aither-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/aither-3.8.60.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.60` | [analyst-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/analyst-3.8.60.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.60` | [bead-space-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/bead-space-3.8.60.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.60` | [claude-code-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/claude-code-3.8.60.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.60` | [dgg_research-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/dgg_research-3.8.60.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.60` | [gobbonet-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/gobbonet-3.8.60.tar.gz) | 58.5 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.60` | [hermes-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/hermes-3.8.60.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.60` | [iris-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/iris-3.8.60.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.60` | [openclaw-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/openclaw-3.8.60.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.60` | [persona-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/persona-3.8.60.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.60` — skills  
  `sha256:3d5783cc9e38505e…`
- **analyst** `3.8.60` — agent config, skills  
  `sha256:e9a79e6aa7ff09c9…`
- **bead-space** `3.8.60` — brain pack only  
  `sha256:00d95cc870b3d220…`
- **claude-code** `3.8.60` — agent config, skills  
  `sha256:876975a2b36ad424…`
- **dgg_research** `3.8.60` — agent config, skills  
  `sha256:0a84d4f20a38e594…`
- **gobbonet** `3.8.60` — agent config, Python  
  `sha256:a5c55cc848d4aea3…`
- **hermes** `3.8.60` — agent config, skills  
  `sha256:675ce77773037043…`
- **iris** `3.8.60` — skills  
  `sha256:d8d8f694ed359354…`
- **openclaw** `3.8.60` — agent config, skills  
  `sha256:45fc217cc8409e56…`
- **persona** `3.8.60` — brain pack only  
  `sha256:982ddedb2f3c2a65…`
