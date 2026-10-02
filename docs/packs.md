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


Built from `v3.8.40` (adk 3.8.40).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.40` | [aither-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/aither-3.8.40.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.40` | [analyst-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/analyst-3.8.40.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.40` | [bead-space-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/bead-space-3.8.40.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.40` | [claude-code-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/claude-code-3.8.40.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.40` | [dgg_research-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/dgg_research-3.8.40.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.40` | [gobbonet-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/gobbonet-3.8.40.tar.gz) | 47.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.40` | [hermes-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/hermes-3.8.40.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.40` | [iris-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/iris-3.8.40.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.40` | [openclaw-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/openclaw-3.8.40.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.40` | [persona-3.8.40.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.40/persona-3.8.40.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.40` — skills  
  `sha256:144d98e31dc8b033…`
- **analyst** `3.8.40` — agent config, skills  
  `sha256:9a04de1294c5ac28…`
- **bead-space** `3.8.40` — brain pack only  
  `sha256:99b996b63ffbdbc6…`
- **claude-code** `3.8.40` — agent config, skills  
  `sha256:cf47506d98f9d2ef…`
- **dgg_research** `3.8.40` — agent config, skills  
  `sha256:47a4aadfe5208a71…`
- **gobbonet** `3.8.40` — agent config, Python  
  `sha256:4c0354dd86521f98…`
- **hermes** `3.8.40` — agent config, skills  
  `sha256:c1a99bb93fa02de2…`
- **iris** `3.8.40` — skills  
  `sha256:636cea747fffd3aa…`
- **openclaw** `3.8.40` — agent config, skills  
  `sha256:524cbfa21380bb00…`
- **persona** `3.8.40` — brain pack only  
  `sha256:89b23249af18a2df…`
