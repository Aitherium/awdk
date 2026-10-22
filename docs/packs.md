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


Built from `v3.8.88` (adk 3.8.88).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.88` | [aither-3.8.88.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.88/aither-3.8.88.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.88` | [analyst-3.8.88.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.88/analyst-3.8.88.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.88` | [bead-space-3.8.88.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.88/bead-space-3.8.88.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.88` | [claude-code-3.8.88.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.88/claude-code-3.8.88.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.88` | [dgg_research-3.8.88.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.88/dgg_research-3.8.88.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.88` | [gobbonet-3.8.88.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.88/gobbonet-3.8.88.tar.gz) | 65.8 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.88` | [hermes-3.8.88.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.88/hermes-3.8.88.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.88` | [iris-3.8.88.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.88/iris-3.8.88.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.88` | [openclaw-3.8.88.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.88/openclaw-3.8.88.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.88` | [persona-3.8.88.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.88/persona-3.8.88.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.88` — skills  
  `sha256:183466bc1b18232f…`
- **analyst** `3.8.88` — agent config, skills  
  `sha256:95c90c5926db4e9f…`
- **bead-space** `3.8.88` — brain pack only  
  `sha256:716ced833bcfe881…`
- **claude-code** `3.8.88` — agent config, skills  
  `sha256:37a93877cfceef1e…`
- **dgg_research** `3.8.88` — agent config, skills  
  `sha256:d0aa8f0cfc50a218…`
- **gobbonet** `3.8.88` — agent config, Python  
  `sha256:1a83f93570fa54af…`
- **hermes** `3.8.88` — agent config, skills  
  `sha256:90775a5f6be837c7…`
- **iris** `3.8.88` — skills  
  `sha256:2182ab0645034541…`
- **openclaw** `3.8.88` — agent config, skills  
  `sha256:a9a6bdb532218ca2…`
- **persona** `3.8.88` — brain pack only  
  `sha256:893a1286db89b592…`
