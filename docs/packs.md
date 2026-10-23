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


Built from `v3.8.91` (adk 3.8.91).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.91` | [aither-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/aither-3.8.91.tar.gz) | 10.0 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.91` | [analyst-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/analyst-3.8.91.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.91` | [bead-space-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/bead-space-3.8.91.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.91` | [claude-code-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/claude-code-3.8.91.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.91` | [dgg_research-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/dgg_research-3.8.91.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.91` | [gobbonet-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/gobbonet-3.8.91.tar.gz) | 65.9 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.91` | [hermes-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/hermes-3.8.91.tar.gz) | 4.8 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.91` | [iris-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/iris-3.8.91.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.91` | [openclaw-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/openclaw-3.8.91.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.91` | [persona-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/persona-3.8.91.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.91` — skills  
  `sha256:69ec14718a909644…`
- **analyst** `3.8.91` — agent config, skills  
  `sha256:63f3024ffd78c733…`
- **bead-space** `3.8.91` — brain pack only  
  `sha256:cdbdbd568873a155…`
- **claude-code** `3.8.91` — agent config, skills  
  `sha256:3ab9176a48796ec8…`
- **dgg_research** `3.8.91` — agent config, skills  
  `sha256:5a52d6c3acb128bf…`
- **gobbonet** `3.8.91` — agent config, Python  
  `sha256:c39e542e7ae15d6c…`
- **hermes** `3.8.91` — agent config, skills  
  `sha256:a96c884f839e6eb8…`
- **iris** `3.8.91` — skills  
  `sha256:f2d2c9e37e6d64bd…`
- **openclaw** `3.8.91` — agent config, skills  
  `sha256:bac8e22196e87a36…`
- **persona** `3.8.91` — brain pack only  
  `sha256:0753a432744880c7…`
