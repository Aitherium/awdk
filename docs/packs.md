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


Built from `v3.8.28` (adk 3.8.28).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.28` | [aither-3.8.28.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.28/aither-3.8.28.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.28` | [analyst-3.8.28.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.28/analyst-3.8.28.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.28` | [bead-space-3.8.28.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.28/bead-space-3.8.28.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.28` | [claude-code-3.8.28.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.28/claude-code-3.8.28.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.28` | [dgg_research-3.8.28.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.28/dgg_research-3.8.28.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.28` | [gobbonet-3.8.28.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.28/gobbonet-3.8.28.tar.gz) | 47.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.28` | [hermes-3.8.28.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.28/hermes-3.8.28.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.28` | [iris-3.8.28.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.28/iris-3.8.28.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.28` | [openclaw-3.8.28.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.28/openclaw-3.8.28.tar.gz) | 5.2 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.28` | [persona-3.8.28.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.28/persona-3.8.28.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.28` — skills  
  `sha256:2c4024e64a7e278b…`
- **analyst** `3.8.28` — agent config, skills  
  `sha256:7e1574df09d0f254…`
- **bead-space** `3.8.28` — brain pack only  
  `sha256:acdef8b3b09d9e5a…`
- **claude-code** `3.8.28` — agent config, skills  
  `sha256:f57d3ac2508590df…`
- **dgg_research** `3.8.28` — agent config, skills  
  `sha256:3ab38ff845c18960…`
- **gobbonet** `3.8.28` — agent config, Python  
  `sha256:ffda1c10d937d46d…`
- **hermes** `3.8.28` — agent config, skills  
  `sha256:c97e3e9deda0de8e…`
- **iris** `3.8.28` — skills  
  `sha256:bf81bb053971be00…`
- **openclaw** `3.8.28` — agent config, skills  
  `sha256:2abaf31c4f637cf2…`
- **persona** `3.8.28` — brain pack only  
  `sha256:f1e5d7a1e20956dc…`
