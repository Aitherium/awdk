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


Built from `v3.8.98` (adk 3.8.98).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.98` | [aither-3.8.98.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.98/aither-3.8.98.tar.gz) | 10.0 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.98` | [analyst-3.8.98.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.98/analyst-3.8.98.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.98` | [bead-space-3.8.98.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.98/bead-space-3.8.98.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.98` | [claude-code-3.8.98.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.98/claude-code-3.8.98.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.98` | [dgg_research-3.8.98.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.98/dgg_research-3.8.98.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.98` | [gobbonet-3.8.98.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.98/gobbonet-3.8.98.tar.gz) | 66.2 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.98` | [hermes-3.8.98.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.98/hermes-3.8.98.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.98` | [iris-3.8.98.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.98/iris-3.8.98.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.98` | [openclaw-3.8.98.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.98/openclaw-3.8.98.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.98` | [persona-3.8.98.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.98/persona-3.8.98.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.98` — skills  
  `sha256:ac9206f0896eade3…`
- **analyst** `3.8.98` — agent config, skills  
  `sha256:6fa186bed8878155…`
- **bead-space** `3.8.98` — brain pack only  
  `sha256:91450ee201d45cf6…`
- **claude-code** `3.8.98` — agent config, skills  
  `sha256:30755b97b69b5d31…`
- **dgg_research** `3.8.98` — agent config, skills  
  `sha256:8cef6a8b51aed9eb…`
- **gobbonet** `3.8.98` — agent config, Python  
  `sha256:07387851e919913d…`
- **hermes** `3.8.98` — agent config, skills  
  `sha256:0f15145349ba56bf…`
- **iris** `3.8.98` — skills  
  `sha256:447673882b6695a9…`
- **openclaw** `3.8.98` — agent config, skills  
  `sha256:fa33f625b4fdff03…`
- **persona** `3.8.98` — brain pack only  
  `sha256:94b1610fb58ac908…`
