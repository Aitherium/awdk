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
  `sha256:f803d2afba0b70b9…`
- **analyst** `3.8.60` — agent config, skills  
  `sha256:bcc1c66a76671a33…`
- **bead-space** `3.8.60` — brain pack only  
  `sha256:e10cdcdf0d596e88…`
- **claude-code** `3.8.60` — agent config, skills  
  `sha256:a5c3494b3ad4a369…`
- **dgg_research** `3.8.60` — agent config, skills  
  `sha256:7fc820dc6bfc5e27…`
- **gobbonet** `3.8.60` — agent config, Python  
  `sha256:6e47277f6e342e15…`
- **hermes** `3.8.60` — agent config, skills  
  `sha256:86fc74846e0145cd…`
- **iris** `3.8.60` — skills  
  `sha256:863efb7e32e33214…`
- **openclaw** `3.8.60` — agent config, skills  
  `sha256:c0194e20bda3e309…`
- **persona** `3.8.60` — brain pack only  
  `sha256:94a3111f990150b2…`
