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


Built from `v3.8.74` (adk 3.8.74).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.74` | [aither-3.8.74.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.74/aither-3.8.74.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.74` | [analyst-3.8.74.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.74/analyst-3.8.74.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.74` | [bead-space-3.8.74.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.74/bead-space-3.8.74.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.74` | [claude-code-3.8.74.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.74/claude-code-3.8.74.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.74` | [dgg_research-3.8.74.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.74/dgg_research-3.8.74.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.74` | [gobbonet-3.8.74.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.74/gobbonet-3.8.74.tar.gz) | 65.7 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.74` | [hermes-3.8.74.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.74/hermes-3.8.74.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.74` | [iris-3.8.74.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.74/iris-3.8.74.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.74` | [openclaw-3.8.74.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.74/openclaw-3.8.74.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.74` | [persona-3.8.74.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.74/persona-3.8.74.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.74` — skills  
  `sha256:674ddedcc4c98266…`
- **analyst** `3.8.74` — agent config, skills  
  `sha256:b8812a4fa0a638c5…`
- **bead-space** `3.8.74` — brain pack only  
  `sha256:c1ad6981805b34ed…`
- **claude-code** `3.8.74` — agent config, skills  
  `sha256:2c0fa968a067036b…`
- **dgg_research** `3.8.74` — agent config, skills  
  `sha256:d6c28a5c79a2c180…`
- **gobbonet** `3.8.74` — agent config, Python  
  `sha256:f645ba5f58e93ef6…`
- **hermes** `3.8.74` — agent config, skills  
  `sha256:87c06579a258f300…`
- **iris** `3.8.74` — skills  
  `sha256:f89fb3e6956f9ed2…`
- **openclaw** `3.8.74` — agent config, skills  
  `sha256:ce03b87a9bf20020…`
- **persona** `3.8.74` — brain pack only  
  `sha256:0318b436480cbcc7…`
