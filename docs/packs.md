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


Built from `v3.8.84` (adk 3.8.84).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.84` | [aither-3.8.84.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.84/aither-3.8.84.tar.gz) | 9.9 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.84` | [analyst-3.8.84.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.84/analyst-3.8.84.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.84` | [bead-space-3.8.84.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.84/bead-space-3.8.84.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.84` | [claude-code-3.8.84.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.84/claude-code-3.8.84.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.84` | [dgg_research-3.8.84.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.84/dgg_research-3.8.84.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.84` | [gobbonet-3.8.84.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.84/gobbonet-3.8.84.tar.gz) | 65.8 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.84` | [hermes-3.8.84.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.84/hermes-3.8.84.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.84` | [iris-3.8.84.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.84/iris-3.8.84.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.84` | [openclaw-3.8.84.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.84/openclaw-3.8.84.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.84` | [persona-3.8.84.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.84/persona-3.8.84.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.84` — skills  
  `sha256:610a199bea78136a…`
- **analyst** `3.8.84` — agent config, skills  
  `sha256:625581fd4eaa8928…`
- **bead-space** `3.8.84` — brain pack only  
  `sha256:41af79681a3c74fc…`
- **claude-code** `3.8.84` — agent config, skills  
  `sha256:506f4c406fea1aee…`
- **dgg_research** `3.8.84` — agent config, skills  
  `sha256:58a7885a21263186…`
- **gobbonet** `3.8.84` — agent config, Python  
  `sha256:ffce7fdcb952b42e…`
- **hermes** `3.8.84` — agent config, skills  
  `sha256:771a023d09fa08f4…`
- **iris** `3.8.84` — skills  
  `sha256:ae7d002f7667092d…`
- **openclaw** `3.8.84` — agent config, skills  
  `sha256:d7c9c4b63f077b54…`
- **persona** `3.8.84` — brain pack only  
  `sha256:dffca99a0e96d319…`
