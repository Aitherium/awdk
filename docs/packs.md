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


Built from `v3.8.25` (adk 3.8.25).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.25` | [aither-3.8.25.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.25/aither-3.8.25.tar.gz) | 9.9 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.25` | [analyst-3.8.25.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.25/analyst-3.8.25.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.25` | [bead-space-3.8.25.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.25/bead-space-3.8.25.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.25` | [claude-code-3.8.25.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.25/claude-code-3.8.25.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.25` | [dgg_research-3.8.25.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.25/dgg_research-3.8.25.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.25` | [gobbonet-3.8.25.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.25/gobbonet-3.8.25.tar.gz) | 47.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.25` | [hermes-3.8.25.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.25/hermes-3.8.25.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.25` | [iris-3.8.25.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.25/iris-3.8.25.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.25` | [openclaw-3.8.25.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.25/openclaw-3.8.25.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.25` | [persona-3.8.25.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.25/persona-3.8.25.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.25` — skills  
  `sha256:4560610d7e099d02…`
- **analyst** `3.8.25` — agent config, skills  
  `sha256:7577cbfc13adcb9d…`
- **bead-space** `3.8.25` — brain pack only  
  `sha256:6151261f06b6fb5a…`
- **claude-code** `3.8.25` — agent config, skills  
  `sha256:dd6f8d94f75bc8a7…`
- **dgg_research** `3.8.25` — agent config, skills  
  `sha256:ec641ef31760828e…`
- **gobbonet** `3.8.25` — agent config, Python  
  `sha256:2bb37d0c3337fb9e…`
- **hermes** `3.8.25` — agent config, skills  
  `sha256:345100c3e198ac2e…`
- **iris** `3.8.25` — skills  
  `sha256:af9ea1fcca8ba505…`
- **openclaw** `3.8.25` — agent config, skills  
  `sha256:fbccbce60e61bbe2…`
- **persona** `3.8.25` — brain pack only  
  `sha256:ace10d4544cb4e60…`
