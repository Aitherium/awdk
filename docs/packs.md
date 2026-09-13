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


Built from `v3.8.17` (adk 3.8.17).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.17` | [aither-3.8.17.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.17/aither-3.8.17.tar.gz) | 4.4 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.17` | [analyst-3.8.17.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.17/analyst-3.8.17.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.17` | [bead-space-3.8.17.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.17/bead-space-3.8.17.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.17` | [claude-code-3.8.17.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.17/claude-code-3.8.17.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.17` | [dgg_research-3.8.17.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.17/dgg_research-3.8.17.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.17` | [gobbonet-3.8.17.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.17/gobbonet-3.8.17.tar.gz) | 45.7 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.17` | [hermes-3.8.17.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.17/hermes-3.8.17.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.17` | [iris-3.8.17.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.17/iris-3.8.17.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.17` | [openclaw-3.8.17.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.17/openclaw-3.8.17.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.17` | [persona-3.8.17.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.17/persona-3.8.17.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.17` — skills  
  `sha256:87c34a5fa7589f55…`
- **analyst** `3.8.17` — agent config, skills  
  `sha256:f600d33ea573b589…`
- **bead-space** `3.8.17` — brain pack only  
  `sha256:242d519389823afc…`
- **claude-code** `3.8.17` — agent config, skills  
  `sha256:702f9f24147ff04c…`
- **dgg_research** `3.8.17` — agent config, skills  
  `sha256:92b2f296f1468926…`
- **gobbonet** `3.8.17` — agent config, Python  
  `sha256:703f711708a8a8dc…`
- **hermes** `3.8.17` — agent config, skills  
  `sha256:4bfc0429d999176f…`
- **iris** `3.8.17` — skills  
  `sha256:481446e708171fbe…`
- **openclaw** `3.8.17` — agent config, skills  
  `sha256:8d0a211ddfd2f06e…`
- **persona** `3.8.17` — brain pack only  
  `sha256:f83d7e3d07ed1b76…`
