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


Built from `v3.8.93` (adk 3.8.93).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.93` | [aither-3.8.93.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.93/aither-3.8.93.tar.gz) | 10.0 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.93` | [analyst-3.8.93.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.93/analyst-3.8.93.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.93` | [bead-space-3.8.93.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.93/bead-space-3.8.93.tar.gz) | 1.6 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.93` | [claude-code-3.8.93.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.93/claude-code-3.8.93.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.93` | [dgg_research-3.8.93.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.93/dgg_research-3.8.93.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.93` | [gobbonet-3.8.93.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.93/gobbonet-3.8.93.tar.gz) | 66.2 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.93` | [hermes-3.8.93.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.93/hermes-3.8.93.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.93` | [iris-3.8.93.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.93/iris-3.8.93.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.93` | [openclaw-3.8.93.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.93/openclaw-3.8.93.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.93` | [persona-3.8.93.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.93/persona-3.8.93.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.93` — skills  
  `sha256:2f2816efb923dfce…`
- **analyst** `3.8.93` — agent config, skills  
  `sha256:c14152ffcc5302a7…`
- **bead-space** `3.8.93` — brain pack only  
  `sha256:775afde7160755c4…`
- **claude-code** `3.8.93` — agent config, skills  
  `sha256:bd9671cfa84fb9ff…`
- **dgg_research** `3.8.93` — agent config, skills  
  `sha256:7615f002c9e5a70a…`
- **gobbonet** `3.8.93` — agent config, Python  
  `sha256:c900986c7d01589a…`
- **hermes** `3.8.93` — agent config, skills  
  `sha256:1240bdb0899d8149…`
- **iris** `3.8.93` — skills  
  `sha256:62bca7be6360fdd2…`
- **openclaw** `3.8.93` — agent config, skills  
  `sha256:132397da77f66623…`
- **persona** `3.8.93` — brain pack only  
  `sha256:6d605634ba8eb5e2…`
