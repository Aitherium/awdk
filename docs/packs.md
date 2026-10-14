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


Built from `v3.8.68` (adk 3.8.68).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.68` | [aither-3.8.68.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.68/aither-3.8.68.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.68` | [analyst-3.8.68.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.68/analyst-3.8.68.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.68` | [bead-space-3.8.68.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.68/bead-space-3.8.68.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.68` | [claude-code-3.8.68.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.68/claude-code-3.8.68.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.68` | [dgg_research-3.8.68.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.68/dgg_research-3.8.68.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.68` | [gobbonet-3.8.68.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.68/gobbonet-3.8.68.tar.gz) | 65.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.68` | [hermes-3.8.68.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.68/hermes-3.8.68.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.68` | [iris-3.8.68.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.68/iris-3.8.68.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.68` | [openclaw-3.8.68.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.68/openclaw-3.8.68.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.68` | [persona-3.8.68.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.68/persona-3.8.68.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.68` — skills  
  `sha256:d0bfa1a6ef4ba116…`
- **analyst** `3.8.68` — agent config, skills  
  `sha256:c2bf1913ae77a7c3…`
- **bead-space** `3.8.68` — brain pack only  
  `sha256:21471709385be0e7…`
- **claude-code** `3.8.68` — agent config, skills  
  `sha256:69e5f0ae8fcb1b8d…`
- **dgg_research** `3.8.68` — agent config, skills  
  `sha256:0ea2fe935ef8b363…`
- **gobbonet** `3.8.68` — agent config, Python  
  `sha256:c832d5adf1041deb…`
- **hermes** `3.8.68` — agent config, skills  
  `sha256:b2f6aa62e22c6a1c…`
- **iris** `3.8.68` — skills  
  `sha256:7e94e1e03e4a9787…`
- **openclaw** `3.8.68` — agent config, skills  
  `sha256:0893fe039224f333…`
- **persona** `3.8.68` — brain pack only  
  `sha256:d29a446d6f5f4c43…`
