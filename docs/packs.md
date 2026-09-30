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


Built from `v3.8.34` (adk 3.8.34).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.34` | [aither-3.8.34.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.34/aither-3.8.34.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.34` | [analyst-3.8.34.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.34/analyst-3.8.34.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.34` | [bead-space-3.8.34.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.34/bead-space-3.8.34.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.34` | [claude-code-3.8.34.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.34/claude-code-3.8.34.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.34` | [dgg_research-3.8.34.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.34/dgg_research-3.8.34.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.34` | [gobbonet-3.8.34.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.34/gobbonet-3.8.34.tar.gz) | 47.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.34` | [hermes-3.8.34.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.34/hermes-3.8.34.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.34` | [iris-3.8.34.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.34/iris-3.8.34.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.34` | [openclaw-3.8.34.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.34/openclaw-3.8.34.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.34` | [persona-3.8.34.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.34/persona-3.8.34.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.34` — skills  
  `sha256:97ae7d9dacd2e4f1…`
- **analyst** `3.8.34` — agent config, skills  
  `sha256:f9761b18ac331887…`
- **bead-space** `3.8.34` — brain pack only  
  `sha256:04c7342109324cdb…`
- **claude-code** `3.8.34` — agent config, skills  
  `sha256:d60dca58087be801…`
- **dgg_research** `3.8.34` — agent config, skills  
  `sha256:bf884907c1bd5cca…`
- **gobbonet** `3.8.34` — agent config, Python  
  `sha256:37d72e550dcc169b…`
- **hermes** `3.8.34` — agent config, skills  
  `sha256:096750a7c7a5a0ef…`
- **iris** `3.8.34` — skills  
  `sha256:a2e14f4c71cb120d…`
- **openclaw** `3.8.34` — agent config, skills  
  `sha256:55b8b7dfa8e25f8f…`
- **persona** `3.8.34` — brain pack only  
  `sha256:c6e32e26bae5aa68…`
