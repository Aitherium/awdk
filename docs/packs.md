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
| **[GobboPack](packs/gobbonet.md)** | `3.8.60` | [gobbonet-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/gobbonet-3.8.60.tar.gz) | 58.3 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.60` | [hermes-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/hermes-3.8.60.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.60` | [iris-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/iris-3.8.60.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.60` | [openclaw-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/openclaw-3.8.60.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.60` | [persona-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/persona-3.8.60.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.60` — skills  
  `sha256:803dc437b94dba4d…`
- **analyst** `3.8.60` — agent config, skills  
  `sha256:5855a89ae68c3a15…`
- **bead-space** `3.8.60` — brain pack only  
  `sha256:f79d63f89af6b21e…`
- **claude-code** `3.8.60` — agent config, skills  
  `sha256:fe743719175d0255…`
- **dgg_research** `3.8.60` — agent config, skills  
  `sha256:20d11427e9aa1de8…`
- **gobbonet** `3.8.60` — agent config, Python  
  `sha256:537a4a538e6082ed…`
- **hermes** `3.8.60` — agent config, skills  
  `sha256:77d372501e904a98…`
- **iris** `3.8.60` — skills  
  `sha256:99e918c1f5767540…`
- **openclaw** `3.8.60` — agent config, skills  
  `sha256:fcb39f742e53c994…`
- **persona** `3.8.60` — brain pack only  
  `sha256:8352f02659b687b7…`
