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


Built from `v3.8.58` (adk 3.8.58).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.58` | [aither-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/aither-3.8.58.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.58` | [analyst-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/analyst-3.8.58.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.58` | [bead-space-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/bead-space-3.8.58.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.58` | [claude-code-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/claude-code-3.8.58.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.58` | [dgg_research-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/dgg_research-3.8.58.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.58` | [gobbonet-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/gobbonet-3.8.58.tar.gz) | 58.3 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.58` | [hermes-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/hermes-3.8.58.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.58` | [iris-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/iris-3.8.58.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.58` | [openclaw-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/openclaw-3.8.58.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.58` | [persona-3.8.58.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.58/persona-3.8.58.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.58` — skills  
  `sha256:5261bb21fb016c1b…`
- **analyst** `3.8.58` — agent config, skills  
  `sha256:fb87c4509ff120fe…`
- **bead-space** `3.8.58` — brain pack only  
  `sha256:22584ad268669357…`
- **claude-code** `3.8.58` — agent config, skills  
  `sha256:6d91a54b9130b9e1…`
- **dgg_research** `3.8.58` — agent config, skills  
  `sha256:005acd25e20f696d…`
- **gobbonet** `3.8.58` — agent config, Python  
  `sha256:3415a80c889537e5…`
- **hermes** `3.8.58` — agent config, skills  
  `sha256:a839d4839ce9e4f9…`
- **iris** `3.8.58` — skills  
  `sha256:6de4d3b6b237bb0b…`
- **openclaw** `3.8.58` — agent config, skills  
  `sha256:f1a8a9eaa4bc9c31…`
- **persona** `3.8.58` — brain pack only  
  `sha256:8b3b41684e86c791…`
