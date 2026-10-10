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


Built from `v3.8.66` (adk 3.8.66).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.66` | [aither-3.8.66.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.66/aither-3.8.66.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.66` | [analyst-3.8.66.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.66/analyst-3.8.66.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.66` | [bead-space-3.8.66.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.66/bead-space-3.8.66.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.66` | [claude-code-3.8.66.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.66/claude-code-3.8.66.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.66` | [dgg_research-3.8.66.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.66/dgg_research-3.8.66.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.66` | [gobbonet-3.8.66.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.66/gobbonet-3.8.66.tar.gz) | 65.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.66` | [hermes-3.8.66.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.66/hermes-3.8.66.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.66` | [iris-3.8.66.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.66/iris-3.8.66.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.66` | [openclaw-3.8.66.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.66/openclaw-3.8.66.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.66` | [persona-3.8.66.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.66/persona-3.8.66.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.66` — skills  
  `sha256:4c03facb12cb1d07…`
- **analyst** `3.8.66` — agent config, skills  
  `sha256:805300d45dee0e87…`
- **bead-space** `3.8.66` — brain pack only  
  `sha256:caf6fca94340ab68…`
- **claude-code** `3.8.66` — agent config, skills  
  `sha256:c0449162e8d93bc3…`
- **dgg_research** `3.8.66` — agent config, skills  
  `sha256:a02a939f912a6847…`
- **gobbonet** `3.8.66` — agent config, Python  
  `sha256:a85063ed4c76d9e0…`
- **hermes** `3.8.66` — agent config, skills  
  `sha256:27da706a2740286b…`
- **iris** `3.8.66` — skills  
  `sha256:ec273b29c4699315…`
- **openclaw** `3.8.66` — agent config, skills  
  `sha256:4125d78cd0da7308…`
- **persona** `3.8.66` — brain pack only  
  `sha256:68c09996f28a23dc…`
