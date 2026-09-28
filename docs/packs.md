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


Built from `v3.8.29` (adk 3.8.29).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.29` | [aither-3.8.29.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.29/aither-3.8.29.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.29` | [analyst-3.8.29.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.29/analyst-3.8.29.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.29` | [bead-space-3.8.29.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.29/bead-space-3.8.29.tar.gz) | 1.6 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.29` | [claude-code-3.8.29.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.29/claude-code-3.8.29.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.29` | [dgg_research-3.8.29.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.29/dgg_research-3.8.29.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.29` | [gobbonet-3.8.29.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.29/gobbonet-3.8.29.tar.gz) | 47.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.29` | [hermes-3.8.29.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.29/hermes-3.8.29.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.29` | [iris-3.8.29.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.29/iris-3.8.29.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.29` | [openclaw-3.8.29.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.29/openclaw-3.8.29.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.29` | [persona-3.8.29.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.29/persona-3.8.29.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.29` — skills  
  `sha256:61ffabf0f96b2d8e…`
- **analyst** `3.8.29` — agent config, skills  
  `sha256:170cbe944290f0d1…`
- **bead-space** `3.8.29` — brain pack only  
  `sha256:d32e6cf0cf619f12…`
- **claude-code** `3.8.29` — agent config, skills  
  `sha256:23aa3d9eefc6de0f…`
- **dgg_research** `3.8.29` — agent config, skills  
  `sha256:bead0e78ba5f870c…`
- **gobbonet** `3.8.29` — agent config, Python  
  `sha256:67cc8829f58ff195…`
- **hermes** `3.8.29` — agent config, skills  
  `sha256:fbdedb0047c7bf03…`
- **iris** `3.8.29` — skills  
  `sha256:29132c76cb76c0a2…`
- **openclaw** `3.8.29` — agent config, skills  
  `sha256:512115d009507a03…`
- **persona** `3.8.29` — brain pack only  
  `sha256:c4e42b84f1706ad1…`
