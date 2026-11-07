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


Built from `v3.8.99` (adk 3.8.99).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.99` | [aither-3.8.99.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.99/aither-3.8.99.tar.gz) | 10.0 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.99` | [analyst-3.8.99.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.99/analyst-3.8.99.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.99` | [bead-space-3.8.99.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.99/bead-space-3.8.99.tar.gz) | 1.6 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.99` | [claude-code-3.8.99.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.99/claude-code-3.8.99.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.99` | [dgg_research-3.8.99.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.99/dgg_research-3.8.99.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.99` | [gobbonet-3.8.99.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.99/gobbonet-3.8.99.tar.gz) | 66.2 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.99` | [hermes-3.8.99.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.99/hermes-3.8.99.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.99` | [iris-3.8.99.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.99/iris-3.8.99.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.99` | [openclaw-3.8.99.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.99/openclaw-3.8.99.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.99` | [persona-3.8.99.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.99/persona-3.8.99.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.99` — skills  
  `sha256:08937175b69c5cb1…`
- **analyst** `3.8.99` — agent config, skills  
  `sha256:024cd8fa69bbafc7…`
- **bead-space** `3.8.99` — brain pack only  
  `sha256:be512b370ac42b48…`
- **claude-code** `3.8.99` — agent config, skills  
  `sha256:653a0f5b6efc38ac…`
- **dgg_research** `3.8.99` — agent config, skills  
  `sha256:44c50e0112464858…`
- **gobbonet** `3.8.99` — agent config, Python  
  `sha256:c8b979e26ee34556…`
- **hermes** `3.8.99` — agent config, skills  
  `sha256:b9721685a5fb091c…`
- **iris** `3.8.99` — skills  
  `sha256:fcc255444db18ea1…`
- **openclaw** `3.8.99` — agent config, skills  
  `sha256:a880c641f4f4a5ff…`
- **persona** `3.8.99` — brain pack only  
  `sha256:08ec6835de69402e…`
