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


Built from `v3.8.50` (adk 3.8.50).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.50` | [aither-3.8.50.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.50/aither-3.8.50.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.50` | [analyst-3.8.50.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.50/analyst-3.8.50.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.50` | [bead-space-3.8.50.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.50/bead-space-3.8.50.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.50` | [claude-code-3.8.50.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.50/claude-code-3.8.50.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.50` | [dgg_research-3.8.50.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.50/dgg_research-3.8.50.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.50` | [gobbonet-3.8.50.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.50/gobbonet-3.8.50.tar.gz) | 58.3 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.50` | [hermes-3.8.50.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.50/hermes-3.8.50.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.50` | [iris-3.8.50.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.50/iris-3.8.50.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.50` | [openclaw-3.8.50.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.50/openclaw-3.8.50.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.50` | [persona-3.8.50.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.50/persona-3.8.50.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.50` — skills  
  `sha256:1cc1465b36520c0b…`
- **analyst** `3.8.50` — agent config, skills  
  `sha256:f4a85350f2d92663…`
- **bead-space** `3.8.50` — brain pack only  
  `sha256:c67acf8197824070…`
- **claude-code** `3.8.50` — agent config, skills  
  `sha256:13c73a9bf31057ff…`
- **dgg_research** `3.8.50` — agent config, skills  
  `sha256:8371d270b292ee3d…`
- **gobbonet** `3.8.50` — agent config, Python  
  `sha256:e8b8f50303181898…`
- **hermes** `3.8.50` — agent config, skills  
  `sha256:7200c274158818b9…`
- **iris** `3.8.50` — skills  
  `sha256:02c3f19575046935…`
- **openclaw** `3.8.50` — agent config, skills  
  `sha256:09e001d8ab852884…`
- **persona** `3.8.50` — brain pack only  
  `sha256:a0cf73ecf38f6dea…`
