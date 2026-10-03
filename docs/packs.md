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


Built from `v3.8.48` (adk 3.8.48).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.48` | [aither-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/aither-3.8.48.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.48` | [analyst-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/analyst-3.8.48.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.48` | [bead-space-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/bead-space-3.8.48.tar.gz) | 1.6 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.48` | [claude-code-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/claude-code-3.8.48.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.48` | [dgg_research-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/dgg_research-3.8.48.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.48` | [gobbonet-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/gobbonet-3.8.48.tar.gz) | 58.3 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.48` | [hermes-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/hermes-3.8.48.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.48` | [iris-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/iris-3.8.48.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.48` | [openclaw-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/openclaw-3.8.48.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.48` | [persona-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/persona-3.8.48.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.48` — skills  
  `sha256:5697efa196b59b5f…`
- **analyst** `3.8.48` — agent config, skills  
  `sha256:28bb586e4d826a9b…`
- **bead-space** `3.8.48` — brain pack only  
  `sha256:fc367c6dae74de5f…`
- **claude-code** `3.8.48` — agent config, skills  
  `sha256:2e88c196f17cd43d…`
- **dgg_research** `3.8.48` — agent config, skills  
  `sha256:d0071153e8e072cc…`
- **gobbonet** `3.8.48` — agent config, Python  
  `sha256:5499eb387b5dcf73…`
- **hermes** `3.8.48` — agent config, skills  
  `sha256:9a4f84225c83a160…`
- **iris** `3.8.48` — skills  
  `sha256:237a25217329cd9b…`
- **openclaw** `3.8.48` — agent config, skills  
  `sha256:e8506f931a1d656e…`
- **persona** `3.8.48` — brain pack only  
  `sha256:5276b611bc334e43…`
