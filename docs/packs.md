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


Built from `v3.8.65` (adk 3.8.65).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.65` | [aither-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/aither-3.8.65.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.65` | [analyst-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/analyst-3.8.65.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.65` | [bead-space-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/bead-space-3.8.65.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.65` | [claude-code-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/claude-code-3.8.65.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.65` | [dgg_research-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/dgg_research-3.8.65.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.65` | [gobbonet-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/gobbonet-3.8.65.tar.gz) | 65.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.65` | [hermes-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/hermes-3.8.65.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.65` | [iris-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/iris-3.8.65.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.65` | [openclaw-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/openclaw-3.8.65.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.65` | [persona-3.8.65.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.65/persona-3.8.65.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.65` — skills  
  `sha256:c5539d1f854ff204…`
- **analyst** `3.8.65` — agent config, skills  
  `sha256:8b22f3940a4760d5…`
- **bead-space** `3.8.65` — brain pack only  
  `sha256:5c7243f72b9950ea…`
- **claude-code** `3.8.65` — agent config, skills  
  `sha256:71e0df68da8d2df8…`
- **dgg_research** `3.8.65` — agent config, skills  
  `sha256:3c4719b3b8777044…`
- **gobbonet** `3.8.65` — agent config, Python  
  `sha256:f15d5f9d3237f6ea…`
- **hermes** `3.8.65` — agent config, skills  
  `sha256:2c9be7f4a32e1e08…`
- **iris** `3.8.65` — skills  
  `sha256:b56da82620a353b0…`
- **openclaw** `3.8.65` — agent config, skills  
  `sha256:486f9568d4ee0624…`
- **persona** `3.8.65` — brain pack only  
  `sha256:4a4e2d3e06cf2a28…`
