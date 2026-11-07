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


Built from `v3.8.100` (adk 3.8.100).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.100` | [aither-3.8.100.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.100/aither-3.8.100.tar.gz) | 10.0 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.100` | [analyst-3.8.100.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.100/analyst-3.8.100.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.100` | [bead-space-3.8.100.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.100/bead-space-3.8.100.tar.gz) | 1.6 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.100` | [claude-code-3.8.100.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.100/claude-code-3.8.100.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.100` | [dgg_research-3.8.100.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.100/dgg_research-3.8.100.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.100` | [gobbonet-3.8.100.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.100/gobbonet-3.8.100.tar.gz) | 66.2 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.100` | [hermes-3.8.100.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.100/hermes-3.8.100.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.100` | [iris-3.8.100.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.100/iris-3.8.100.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.100` | [openclaw-3.8.100.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.100/openclaw-3.8.100.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.100` | [persona-3.8.100.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.100/persona-3.8.100.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.100` — skills  
  `sha256:69ca8c8db337b58d…`
- **analyst** `3.8.100` — agent config, skills  
  `sha256:f2c70eede14fb885…`
- **bead-space** `3.8.100` — brain pack only  
  `sha256:456c4f38350336e7…`
- **claude-code** `3.8.100` — agent config, skills  
  `sha256:231484765758318c…`
- **dgg_research** `3.8.100` — agent config, skills  
  `sha256:77c1d9dba55cf947…`
- **gobbonet** `3.8.100` — agent config, Python  
  `sha256:8ff582c7f13d8ec2…`
- **hermes** `3.8.100` — agent config, skills  
  `sha256:4bfea14d9f060c19…`
- **iris** `3.8.100` — skills  
  `sha256:743e54b8d9a649e7…`
- **openclaw** `3.8.100` — agent config, skills  
  `sha256:51124a4cbcf01cfc…`
- **persona** `3.8.100` — brain pack only  
  `sha256:7f26b7a6579f40b2…`
