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
  `sha256:da8dd7783efca136…`
- **analyst** `3.8.25` — agent config, skills  
  `sha256:28f54f730a958f95…`
- **bead-space** `3.8.25` — brain pack only  
  `sha256:07288679b3d4fb2c…`
- **claude-code** `3.8.25` — agent config, skills  
  `sha256:195c3f28e87fd908…`
- **dgg_research** `3.8.25` — agent config, skills  
  `sha256:7911a03ba026dc3e…`
- **gobbonet** `3.8.25` — agent config, Python  
  `sha256:170371efb3422bbd…`
- **hermes** `3.8.25` — agent config, skills  
  `sha256:d3e2784ceda1df67…`
- **iris** `3.8.25` — skills  
  `sha256:25d836d2c9b92605…`
- **openclaw** `3.8.25` — agent config, skills  
  `sha256:04403c46b375fcba…`
- **persona** `3.8.25` — brain pack only  
  `sha256:2e69874d9fbb4461…`
