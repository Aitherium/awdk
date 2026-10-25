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


Built from `v3.8.92` (adk 3.8.92).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.92` | [aither-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/aither-3.8.92.tar.gz) | 10.0 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.92` | [analyst-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/analyst-3.8.92.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.92` | [bead-space-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/bead-space-3.8.92.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.92` | [claude-code-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/claude-code-3.8.92.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.92` | [dgg_research-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/dgg_research-3.8.92.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.92` | [gobbonet-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/gobbonet-3.8.92.tar.gz) | 65.9 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.92` | [hermes-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/hermes-3.8.92.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.92` | [iris-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/iris-3.8.92.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.92` | [openclaw-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/openclaw-3.8.92.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.92` | [persona-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/persona-3.8.92.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.92` — skills  
  `sha256:1639984b27e40967…`
- **analyst** `3.8.92` — agent config, skills  
  `sha256:75ede2382c53d726…`
- **bead-space** `3.8.92` — brain pack only  
  `sha256:a6ce1bb252f2e28b…`
- **claude-code** `3.8.92` — agent config, skills  
  `sha256:e894106589f460b0…`
- **dgg_research** `3.8.92` — agent config, skills  
  `sha256:a33182b0f3b3c524…`
- **gobbonet** `3.8.92` — agent config, Python  
  `sha256:2c396aea04d0f730…`
- **hermes** `3.8.92` — agent config, skills  
  `sha256:2ea1c23f7de45316…`
- **iris** `3.8.92` — skills  
  `sha256:5dc536babf5f43be…`
- **openclaw** `3.8.92` — agent config, skills  
  `sha256:6222eba94e66e482…`
- **persona** `3.8.92` — brain pack only  
  `sha256:94798aea639d5b45…`
