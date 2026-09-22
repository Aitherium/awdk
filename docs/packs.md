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


Built from `v3.8.24` (adk 3.8.24).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.24` | [aither-3.8.24.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.24/aither-3.8.24.tar.gz) | 9.9 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.24` | [analyst-3.8.24.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.24/analyst-3.8.24.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.24` | [bead-space-3.8.24.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.24/bead-space-3.8.24.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.24` | [claude-code-3.8.24.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.24/claude-code-3.8.24.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.24` | [dgg_research-3.8.24.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.24/dgg_research-3.8.24.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.24` | [gobbonet-3.8.24.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.24/gobbonet-3.8.24.tar.gz) | 47.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.24` | [hermes-3.8.24.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.24/hermes-3.8.24.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.24` | [iris-3.8.24.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.24/iris-3.8.24.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.24` | [openclaw-3.8.24.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.24/openclaw-3.8.24.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.24` | [persona-3.8.24.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.24/persona-3.8.24.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.24` — skills  
  `sha256:7f1449c988f7b3a6…`
- **analyst** `3.8.24` — agent config, skills  
  `sha256:7d0de9f8a29f44eb…`
- **bead-space** `3.8.24` — brain pack only  
  `sha256:9c3a0f5a11b5c2e8…`
- **claude-code** `3.8.24` — agent config, skills  
  `sha256:f1f0580f746d6197…`
- **dgg_research** `3.8.24` — agent config, skills  
  `sha256:8d56d0d6af1c1e7f…`
- **gobbonet** `3.8.24` — agent config, Python  
  `sha256:0ba6e3da2eafd9b2…`
- **hermes** `3.8.24` — agent config, skills  
  `sha256:1d71c81cac18610e…`
- **iris** `3.8.24` — skills  
  `sha256:bc5f31518b655e67…`
- **openclaw** `3.8.24` — agent config, skills  
  `sha256:40b2b4c17bf0128b…`
- **persona** `3.8.24` — brain pack only  
  `sha256:b1ae57d76971147d…`
