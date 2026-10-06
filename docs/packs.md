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


Built from `v3.8.61` (adk 3.8.61).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.61` | [aither-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/aither-3.8.61.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.61` | [analyst-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/analyst-3.8.61.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.61` | [bead-space-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/bead-space-3.8.61.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.61` | [claude-code-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/claude-code-3.8.61.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.61` | [dgg_research-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/dgg_research-3.8.61.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.61` | [gobbonet-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/gobbonet-3.8.61.tar.gz) | 58.6 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.61` | [hermes-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/hermes-3.8.61.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.61` | [iris-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/iris-3.8.61.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.61` | [openclaw-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/openclaw-3.8.61.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.61` | [persona-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/persona-3.8.61.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.61` — skills  
  `sha256:35444ff7353b0cb7…`
- **analyst** `3.8.61` — agent config, skills  
  `sha256:c84c9c06f5e66e2e…`
- **bead-space** `3.8.61` — brain pack only  
  `sha256:2e5417b771b2b977…`
- **claude-code** `3.8.61` — agent config, skills  
  `sha256:b8432d68a73963a6…`
- **dgg_research** `3.8.61` — agent config, skills  
  `sha256:fc0ed2d765a890fe…`
- **gobbonet** `3.8.61` — agent config, Python  
  `sha256:93c375d5d6de0790…`
- **hermes** `3.8.61` — agent config, skills  
  `sha256:df33f4ade1d3d38a…`
- **iris** `3.8.61` — skills  
  `sha256:06eaa23b06c9d44f…`
- **openclaw** `3.8.61` — agent config, skills  
  `sha256:5bdfebfee5414248…`
- **persona** `3.8.61` — brain pack only  
  `sha256:414f7e8964517315…`
