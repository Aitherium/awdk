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


Built from `v3.8.49` (adk 3.8.49).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.49` | [aither-3.8.49.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/aither-3.8.49.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.49` | [analyst-3.8.49.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/analyst-3.8.49.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.49` | [bead-space-3.8.49.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/bead-space-3.8.49.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.49` | [claude-code-3.8.49.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/claude-code-3.8.49.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.49` | [dgg_research-3.8.49.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/dgg_research-3.8.49.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.49` | [gobbonet-3.8.49.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/gobbonet-3.8.49.tar.gz) | 58.3 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.49` | [hermes-3.8.49.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/hermes-3.8.49.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.49` | [iris-3.8.49.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/iris-3.8.49.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.49` | [openclaw-3.8.49.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/openclaw-3.8.49.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.49` | [persona-3.8.49.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/persona-3.8.49.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.49` — skills  
  `sha256:3a99114d6ecf2b60…`
- **analyst** `3.8.49` — agent config, skills  
  `sha256:44cf1b7dbf455706…`
- **bead-space** `3.8.49` — brain pack only  
  `sha256:78b84b7bc07388b8…`
- **claude-code** `3.8.49` — agent config, skills  
  `sha256:5ceead14da43c003…`
- **dgg_research** `3.8.49` — agent config, skills  
  `sha256:1a3db9b30ca091bc…`
- **gobbonet** `3.8.49` — agent config, Python  
  `sha256:3342411710f8b07d…`
- **hermes** `3.8.49` — agent config, skills  
  `sha256:4ed6f4c52a9895d6…`
- **iris** `3.8.49` — skills  
  `sha256:cbb09d884f3a8cf7…`
- **openclaw** `3.8.49` — agent config, skills  
  `sha256:6c9d5a5f6cd2b370…`
- **persona** `3.8.49` — brain pack only  
  `sha256:9852922e7b7b2cd0…`
