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
  `sha256:e649de9ceeaaec48…`
- **analyst** `3.8.19` — agent config, skills  
  `sha256:8efcbf369a7247c7…`
- **bead-space** `3.8.19` — brain pack only  
  `sha256:55e27b41b26ae59c…`
- **claude-code** `3.8.19` — agent config, skills  
  `sha256:b6a5505cabb0d03d…`
- **dgg_research** `3.8.19` — agent config, skills  
  `sha256:cff6d442ae4ecf9d…`
- **gobbonet** `3.8.19` — agent config, Python  
  `sha256:dc4e3b39f460a09d…`
- **hermes** `3.8.19` — agent config, skills  
  `sha256:339cb38d97f72e3e…`
- **iris** `3.8.19` — skills  
  `sha256:dc8e7f23cc01c51d…`
- **openclaw** `3.8.19` — agent config, skills  
  `sha256:a167fd535cfb9a3a…`
- **persona** `3.8.19` — brain pack only  
  `sha256:3bf96bd804cca6ee…`
