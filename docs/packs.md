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
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.74` | [hermes-3.8.74.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.74/hermes-3.8.74.tar.gz) | 4.8 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.74` | [iris-3.8.74.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.74/iris-3.8.74.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.74` | [openclaw-3.8.74.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.74/openclaw-3.8.74.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.74` | [persona-3.8.74.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.74/persona-3.8.74.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.74` — skills  
  `sha256:d01a62245726f173…`
- **analyst** `3.8.74` — agent config, skills  
  `sha256:b7877642227a828f…`
- **bead-space** `3.8.74` — brain pack only  
  `sha256:c1d6576cd5147999…`
- **claude-code** `3.8.74` — agent config, skills  
  `sha256:05aa2b22048cb6d2…`
- **dgg_research** `3.8.74` — agent config, skills  
  `sha256:97cc37c1ae509c45…`
- **gobbonet** `3.8.74` — agent config, Python  
  `sha256:1b1ba25b6bcbee0b…`
- **hermes** `3.8.74` — agent config, skills  
  `sha256:8e4a860204eff00f…`
- **iris** `3.8.74` — skills  
  `sha256:236f71c0e6d797c1…`
- **openclaw** `3.8.74` — agent config, skills  
  `sha256:752cd9fa6d6aa03b…`
- **persona** `3.8.74` — brain pack only  
  `sha256:65ef0ef8acb1b884…`
