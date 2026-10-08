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


Built from `v3.8.64` (adk 3.8.64).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.64` | [aither-3.8.64.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.64/aither-3.8.64.tar.gz) | 9.9 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.64` | [analyst-3.8.64.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.64/analyst-3.8.64.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.64` | [bead-space-3.8.64.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.64/bead-space-3.8.64.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.64` | [claude-code-3.8.64.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.64/claude-code-3.8.64.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.64` | [dgg_research-3.8.64.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.64/dgg_research-3.8.64.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.64` | [gobbonet-3.8.64.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.64/gobbonet-3.8.64.tar.gz) | 65.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.64` | [hermes-3.8.64.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.64/hermes-3.8.64.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.64` | [iris-3.8.64.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.64/iris-3.8.64.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.64` | [openclaw-3.8.64.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.64/openclaw-3.8.64.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.64` | [persona-3.8.64.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.64/persona-3.8.64.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.64` — skills  
  `sha256:fc61bf4567c06ca2…`
- **analyst** `3.8.64` — agent config, skills  
  `sha256:00e57ad41f4085fa…`
- **bead-space** `3.8.64` — brain pack only  
  `sha256:f48cdb16c5b5c971…`
- **claude-code** `3.8.64` — agent config, skills  
  `sha256:d95ed9a592372070…`
- **dgg_research** `3.8.64` — agent config, skills  
  `sha256:0891fe21eb6a1b80…`
- **gobbonet** `3.8.64` — agent config, Python  
  `sha256:08189f65ccd0e27a…`
- **hermes** `3.8.64` — agent config, skills  
  `sha256:c53e6b4832013e02…`
- **iris** `3.8.64` — skills  
  `sha256:84ccf02a633f9bb9…`
- **openclaw** `3.8.64` — agent config, skills  
  `sha256:a83194d99258e02e…`
- **persona** `3.8.64` — brain pack only  
  `sha256:a0889b7ec8a4ded3…`
