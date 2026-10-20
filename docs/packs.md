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


Built from `v3.8.79` (adk 3.8.79).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.79` | [aither-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/aither-3.8.79.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.79` | [analyst-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/analyst-3.8.79.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.79` | [bead-space-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/bead-space-3.8.79.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.79` | [claude-code-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/claude-code-3.8.79.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.79` | [dgg_research-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/dgg_research-3.8.79.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.79` | [gobbonet-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/gobbonet-3.8.79.tar.gz) | 65.7 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.79` | [hermes-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/hermes-3.8.79.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.79` | [iris-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/iris-3.8.79.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.79` | [openclaw-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/openclaw-3.8.79.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.79` | [persona-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/persona-3.8.79.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.79` — skills  
  `sha256:9cb036990b0d9874…`
- **analyst** `3.8.79` — agent config, skills  
  `sha256:749c95c4103f2703…`
- **bead-space** `3.8.79` — brain pack only  
  `sha256:d5b15742a6a46882…`
- **claude-code** `3.8.79` — agent config, skills  
  `sha256:db5fcd2b8d1c8719…`
- **dgg_research** `3.8.79` — agent config, skills  
  `sha256:0a423f16d11773d4…`
- **gobbonet** `3.8.79` — agent config, Python  
  `sha256:cc7ec2a7e6742a7e…`
- **hermes** `3.8.79` — agent config, skills  
  `sha256:f1d960f8d38c4981…`
- **iris** `3.8.79` — skills  
  `sha256:cafa07f13c509567…`
- **openclaw** `3.8.79` — agent config, skills  
  `sha256:02c782cfd39b1e12…`
- **persona** `3.8.79` — brain pack only  
  `sha256:3c3823ab05fd6e99…`
