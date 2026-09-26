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


Built from `v3.8.27` (adk 3.8.27).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.27` | [aither-3.8.27.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.27/aither-3.8.27.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.27` | [analyst-3.8.27.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.27/analyst-3.8.27.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.27` | [bead-space-3.8.27.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.27/bead-space-3.8.27.tar.gz) | 1.6 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.27` | [claude-code-3.8.27.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.27/claude-code-3.8.27.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.27` | [dgg_research-3.8.27.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.27/dgg_research-3.8.27.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.27` | [gobbonet-3.8.27.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.27/gobbonet-3.8.27.tar.gz) | 47.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.27` | [hermes-3.8.27.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.27/hermes-3.8.27.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.27` | [iris-3.8.27.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.27/iris-3.8.27.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.27` | [openclaw-3.8.27.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.27/openclaw-3.8.27.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.27` | [persona-3.8.27.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.27/persona-3.8.27.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.27` — skills  
  `sha256:bae2038036bc50e5…`
- **analyst** `3.8.27` — agent config, skills  
  `sha256:900cecafe392b918…`
- **bead-space** `3.8.27` — brain pack only  
  `sha256:a19dc5b4af77ec94…`
- **claude-code** `3.8.27` — agent config, skills  
  `sha256:471cc8645b294b4a…`
- **dgg_research** `3.8.27` — agent config, skills  
  `sha256:0634f70a16fc4099…`
- **gobbonet** `3.8.27` — agent config, Python  
  `sha256:12c7ab82cfbd0ae8…`
- **hermes** `3.8.27` — agent config, skills  
  `sha256:e1b272fe9582cc6c…`
- **iris** `3.8.27` — skills  
  `sha256:4aff5ed37c3aec5b…`
- **openclaw** `3.8.27` — agent config, skills  
  `sha256:683d6174136830e3…`
- **persona** `3.8.27` — brain pack only  
  `sha256:e6d514b905771f70…`
