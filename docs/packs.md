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


Built from `v3.8.57` (adk 3.8.57).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.57` | [aither-3.8.57.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.57/aither-3.8.57.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.57` | [analyst-3.8.57.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.57/analyst-3.8.57.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.57` | [bead-space-3.8.57.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.57/bead-space-3.8.57.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.57` | [claude-code-3.8.57.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.57/claude-code-3.8.57.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.57` | [dgg_research-3.8.57.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.57/dgg_research-3.8.57.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.57` | [gobbonet-3.8.57.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.57/gobbonet-3.8.57.tar.gz) | 58.3 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.57` | [hermes-3.8.57.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.57/hermes-3.8.57.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.57` | [iris-3.8.57.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.57/iris-3.8.57.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.57` | [openclaw-3.8.57.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.57/openclaw-3.8.57.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.57` | [persona-3.8.57.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.57/persona-3.8.57.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.57` — skills  
  `sha256:f6e226b3c9621359…`
- **analyst** `3.8.57` — agent config, skills  
  `sha256:fce7ad6bedce4e5f…`
- **bead-space** `3.8.57` — brain pack only  
  `sha256:da7f0849bcc40f63…`
- **claude-code** `3.8.57` — agent config, skills  
  `sha256:4f80919905808c33…`
- **dgg_research** `3.8.57` — agent config, skills  
  `sha256:41bd21277d8c93dd…`
- **gobbonet** `3.8.57` — agent config, Python  
  `sha256:547b7c3e3f8fda0b…`
- **hermes** `3.8.57` — agent config, skills  
  `sha256:42de23c5eb211ff4…`
- **iris** `3.8.57` — skills  
  `sha256:622f26ab7af3e209…`
- **openclaw** `3.8.57` — agent config, skills  
  `sha256:c5fff7ca6b1734c9…`
- **persona** `3.8.57` — brain pack only  
  `sha256:8b1593896f9faf76…`
