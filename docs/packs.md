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


Built from `v3.8.63` (adk 3.8.63).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.63` | [aither-3.8.63.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.63/aither-3.8.63.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.63` | [analyst-3.8.63.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.63/analyst-3.8.63.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.63` | [bead-space-3.8.63.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.63/bead-space-3.8.63.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.63` | [claude-code-3.8.63.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.63/claude-code-3.8.63.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.63` | [dgg_research-3.8.63.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.63/dgg_research-3.8.63.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.63` | [gobbonet-3.8.63.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.63/gobbonet-3.8.63.tar.gz) | 65.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.63` | [hermes-3.8.63.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.63/hermes-3.8.63.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.63` | [iris-3.8.63.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.63/iris-3.8.63.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.63` | [openclaw-3.8.63.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.63/openclaw-3.8.63.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.63` | [persona-3.8.63.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.63/persona-3.8.63.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.63` — skills  
  `sha256:df819024fb461ff5…`
- **analyst** `3.8.63` — agent config, skills  
  `sha256:064f9b4945f929d9…`
- **bead-space** `3.8.63` — brain pack only  
  `sha256:6a893d166567eb02…`
- **claude-code** `3.8.63` — agent config, skills  
  `sha256:eea2fb79a229c134…`
- **dgg_research** `3.8.63` — agent config, skills  
  `sha256:73f56709415badb4…`
- **gobbonet** `3.8.63` — agent config, Python  
  `sha256:c7d3fef2b57e3023…`
- **hermes** `3.8.63` — agent config, skills  
  `sha256:5c541d7646cf12f0…`
- **iris** `3.8.63` — skills  
  `sha256:3a747c49417f5b9c…`
- **openclaw** `3.8.63` — agent config, skills  
  `sha256:23900c24c83b2080…`
- **persona** `3.8.63` — brain pack only  
  `sha256:9a30a8f3aed21f0d…`
