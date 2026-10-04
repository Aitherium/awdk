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
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.57` | [aither-3.8.57.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.57/aither-3.8.57.tar.gz) | 9.9 KB | Aither — System Overseer & Orchestrator Brain Pack |
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
  `sha256:8aa2e6a7dfd96f4f…`
- **analyst** `3.8.57` — agent config, skills  
  `sha256:3880ea4c371bc15a…`
- **bead-space** `3.8.57` — brain pack only  
  `sha256:78a306f28b6b6666…`
- **claude-code** `3.8.57` — agent config, skills  
  `sha256:2a5d83901c97e530…`
- **dgg_research** `3.8.57` — agent config, skills  
  `sha256:39b17fa491f7f886…`
- **gobbonet** `3.8.57` — agent config, Python  
  `sha256:602015c6e6a6791a…`
- **hermes** `3.8.57` — agent config, skills  
  `sha256:fa5117245bfa9979…`
- **iris** `3.8.57` — skills  
  `sha256:c15756b55c1caaa8…`
- **openclaw** `3.8.57` — agent config, skills  
  `sha256:31d679a2a7373dc5…`
- **persona** `3.8.57` — brain pack only  
  `sha256:92e6c7ef8c707e66…`
