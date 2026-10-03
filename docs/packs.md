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


Built from `v3.8.52` (adk 3.8.52).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.52` | [aither-3.8.52.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.52/aither-3.8.52.tar.gz) | 9.9 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.52` | [analyst-3.8.52.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.52/analyst-3.8.52.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.52` | [bead-space-3.8.52.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.52/bead-space-3.8.52.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.52` | [claude-code-3.8.52.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.52/claude-code-3.8.52.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.52` | [dgg_research-3.8.52.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.52/dgg_research-3.8.52.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.52` | [gobbonet-3.8.52.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.52/gobbonet-3.8.52.tar.gz) | 58.2 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.52` | [hermes-3.8.52.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.52/hermes-3.8.52.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.52` | [iris-3.8.52.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.52/iris-3.8.52.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.52` | [openclaw-3.8.52.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.52/openclaw-3.8.52.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.52` | [persona-3.8.52.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.52/persona-3.8.52.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.52` — skills  
  `sha256:890b3e0cb430726c…`
- **analyst** `3.8.52` — agent config, skills  
  `sha256:8b57f48fd6b73499…`
- **bead-space** `3.8.52` — brain pack only  
  `sha256:c7bca406c22c3317…`
- **claude-code** `3.8.52` — agent config, skills  
  `sha256:185289972f45ce66…`
- **dgg_research** `3.8.52` — agent config, skills  
  `sha256:9550ef64a316706d…`
- **gobbonet** `3.8.52` — agent config, Python  
  `sha256:4e30759cfc300f86…`
- **hermes** `3.8.52` — agent config, skills  
  `sha256:18ee4cb5bfcff342…`
- **iris** `3.8.52` — skills  
  `sha256:6dbf7fb0f9b7f41f…`
- **openclaw** `3.8.52` — agent config, skills  
  `sha256:f30047ee5b657f3b…`
- **persona** `3.8.52` — brain pack only  
  `sha256:61954b0ebd65baac…`
