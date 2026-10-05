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


Built from `v3.8.60` (adk 3.8.60).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.60` | [aither-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/aither-3.8.60.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.60` | [analyst-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/analyst-3.8.60.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.60` | [bead-space-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/bead-space-3.8.60.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.60` | [claude-code-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/claude-code-3.8.60.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.60` | [dgg_research-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/dgg_research-3.8.60.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.60` | [gobbonet-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/gobbonet-3.8.60.tar.gz) | 58.5 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.60` | [hermes-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/hermes-3.8.60.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.60` | [iris-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/iris-3.8.60.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.60` | [openclaw-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/openclaw-3.8.60.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.60` | [persona-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/persona-3.8.60.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.60` — skills  
  `sha256:93651d8d1c547f4c…`
- **analyst** `3.8.60` — agent config, skills  
  `sha256:e19abb049b623d85…`
- **bead-space** `3.8.60` — brain pack only  
  `sha256:5923397a1cee33f9…`
- **claude-code** `3.8.60` — agent config, skills  
  `sha256:ad89b31d01c1959b…`
- **dgg_research** `3.8.60` — agent config, skills  
  `sha256:b88d058978f84f44…`
- **gobbonet** `3.8.60` — agent config, Python  
  `sha256:eec50d3fd42b132a…`
- **hermes** `3.8.60` — agent config, skills  
  `sha256:b98f0ec30f4191f8…`
- **iris** `3.8.60` — skills  
  `sha256:9a7dcd8e344ff678…`
- **openclaw** `3.8.60` — agent config, skills  
  `sha256:217cb1a22fd12016…`
- **persona** `3.8.60` — brain pack only  
  `sha256:a7b2b2f56321a66e…`
