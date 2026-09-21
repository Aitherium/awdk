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


Built from `v3.8.22` (adk 3.8.22).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.22` | [aither-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/aither-3.8.22.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.22` | [analyst-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/analyst-3.8.22.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.22` | [bead-space-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/bead-space-3.8.22.tar.gz) | 1.6 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.22` | [claude-code-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/claude-code-3.8.22.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.22` | [dgg_research-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/dgg_research-3.8.22.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.22` | [gobbonet-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/gobbonet-3.8.22.tar.gz) | 47.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.22` | [hermes-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/hermes-3.8.22.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.22` | [iris-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/iris-3.8.22.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.22` | [openclaw-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/openclaw-3.8.22.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.22` | [persona-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/persona-3.8.22.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.22` — skills  
  `sha256:702ffb129d59dfb3…`
- **analyst** `3.8.22` — agent config, skills  
  `sha256:b0701dc1ebc9ed27…`
- **bead-space** `3.8.22` — brain pack only  
  `sha256:e0c77e547b28958f…`
- **claude-code** `3.8.22` — agent config, skills  
  `sha256:2552c23ed62cb91e…`
- **dgg_research** `3.8.22` — agent config, skills  
  `sha256:bc258a727336551a…`
- **gobbonet** `3.8.22` — agent config, Python  
  `sha256:c79c2fd400f9050d…`
- **hermes** `3.8.22` — agent config, skills  
  `sha256:7ca3279590d16a32…`
- **iris** `3.8.22` — skills  
  `sha256:0423597b4ae6721c…`
- **openclaw** `3.8.22` — agent config, skills  
  `sha256:205be874c2274be5…`
- **persona** `3.8.22` — brain pack only  
  `sha256:8ef9def97fc37009…`
