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


Built from `v3.8.18` (adk 3.8.18).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.18` | [aither-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/aither-3.8.18.tar.gz) | 4.4 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.18` | [analyst-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/analyst-3.8.18.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.18` | [bead-space-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/bead-space-3.8.18.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.18` | [claude-code-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/claude-code-3.8.18.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.18` | [dgg_research-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/dgg_research-3.8.18.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.18` | [gobbonet-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/gobbonet-3.8.18.tar.gz) | 45.7 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.18` | [hermes-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/hermes-3.8.18.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.18` | [iris-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/iris-3.8.18.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.18` | [openclaw-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/openclaw-3.8.18.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.18` | [persona-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/persona-3.8.18.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.18` — skills  
  `sha256:cf69a1bb959173fc…`
- **analyst** `3.8.18` — agent config, skills  
  `sha256:4ace8d1a180927bf…`
- **bead-space** `3.8.18` — brain pack only  
  `sha256:59c585999b7ce81a…`
- **claude-code** `3.8.18` — agent config, skills  
  `sha256:d6be0f6e837867d8…`
- **dgg_research** `3.8.18` — agent config, skills  
  `sha256:218520fd9333ba58…`
- **gobbonet** `3.8.18` — agent config, Python  
  `sha256:e611528f37676943…`
- **hermes** `3.8.18` — agent config, skills  
  `sha256:53f59482a7cd4bac…`
- **iris** `3.8.18` — skills  
  `sha256:1793d48e52092b8c…`
- **openclaw** `3.8.18` — agent config, skills  
  `sha256:3deaaf4bbec86bf0…`
- **persona** `3.8.18` — brain pack only  
  `sha256:6f61b07952f8cfe8…`
