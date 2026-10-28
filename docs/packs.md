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


Built from `v3.8.94` (adk 3.8.94).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.94` | [aither-3.8.94.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.94/aither-3.8.94.tar.gz) | 10.0 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.94` | [analyst-3.8.94.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.94/analyst-3.8.94.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.94` | [bead-space-3.8.94.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.94/bead-space-3.8.94.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.94` | [claude-code-3.8.94.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.94/claude-code-3.8.94.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.94` | [dgg_research-3.8.94.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.94/dgg_research-3.8.94.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.94` | [gobbonet-3.8.94.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.94/gobbonet-3.8.94.tar.gz) | 66.2 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.94` | [hermes-3.8.94.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.94/hermes-3.8.94.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.94` | [iris-3.8.94.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.94/iris-3.8.94.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.94` | [openclaw-3.8.94.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.94/openclaw-3.8.94.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.94` | [persona-3.8.94.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.94/persona-3.8.94.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.94` — skills  
  `sha256:8e211ecf02d1a57f…`
- **analyst** `3.8.94` — agent config, skills  
  `sha256:55ce5c4fe7c6f63e…`
- **bead-space** `3.8.94` — brain pack only  
  `sha256:3f0b3d239b6f8a95…`
- **claude-code** `3.8.94` — agent config, skills  
  `sha256:8a21b33c8b631295…`
- **dgg_research** `3.8.94` — agent config, skills  
  `sha256:fb11947faa1b6bfd…`
- **gobbonet** `3.8.94` — agent config, Python  
  `sha256:fef39b7bcd48e818…`
- **hermes** `3.8.94` — agent config, skills  
  `sha256:819989dea634fe4f…`
- **iris** `3.8.94` — skills  
  `sha256:7f3c3b61537bb88a…`
- **openclaw** `3.8.94` — agent config, skills  
  `sha256:0001d48c5df3e02e…`
- **persona** `3.8.94` — brain pack only  
  `sha256:bffda69104219f01…`
