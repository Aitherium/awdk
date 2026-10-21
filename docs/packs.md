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


Built from `v3.8.80` (adk 3.8.80).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.80` | [aither-3.8.80.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.80/aither-3.8.80.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.80` | [analyst-3.8.80.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.80/analyst-3.8.80.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.80` | [bead-space-3.8.80.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.80/bead-space-3.8.80.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.80` | [claude-code-3.8.80.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.80/claude-code-3.8.80.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.80` | [dgg_research-3.8.80.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.80/dgg_research-3.8.80.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.80` | [gobbonet-3.8.80.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.80/gobbonet-3.8.80.tar.gz) | 65.7 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.80` | [hermes-3.8.80.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.80/hermes-3.8.80.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.80` | [iris-3.8.80.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.80/iris-3.8.80.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.80` | [openclaw-3.8.80.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.80/openclaw-3.8.80.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.80` | [persona-3.8.80.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.80/persona-3.8.80.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.80` — skills  
  `sha256:e546b27e33d5cb5c…`
- **analyst** `3.8.80` — agent config, skills  
  `sha256:14d5f966b2f2c30f…`
- **bead-space** `3.8.80` — brain pack only  
  `sha256:be5b60353333cd13…`
- **claude-code** `3.8.80` — agent config, skills  
  `sha256:143b304375d2da1e…`
- **dgg_research** `3.8.80` — agent config, skills  
  `sha256:73a35125fe8f6f0c…`
- **gobbonet** `3.8.80` — agent config, Python  
  `sha256:6fce490625eb4476…`
- **hermes** `3.8.80` — agent config, skills  
  `sha256:0eec69b69f1408b5…`
- **iris** `3.8.80` — skills  
  `sha256:39c16c8c95acb8ee…`
- **openclaw** `3.8.80` — agent config, skills  
  `sha256:e0b4fab1d4fc54ce…`
- **persona** `3.8.80` — brain pack only  
  `sha256:5fe19604e22f59ab…`
