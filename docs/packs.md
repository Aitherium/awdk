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


Built from `v3.8.42` (adk 3.8.42).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.42` | [aither-3.8.42.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.42/aither-3.8.42.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.42` | [analyst-3.8.42.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.42/analyst-3.8.42.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.42` | [bead-space-3.8.42.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.42/bead-space-3.8.42.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.42` | [claude-code-3.8.42.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.42/claude-code-3.8.42.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.42` | [dgg_research-3.8.42.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.42/dgg_research-3.8.42.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.42` | [gobbonet-3.8.42.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.42/gobbonet-3.8.42.tar.gz) | 58.3 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.42` | [hermes-3.8.42.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.42/hermes-3.8.42.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.42` | [iris-3.8.42.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.42/iris-3.8.42.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.42` | [openclaw-3.8.42.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.42/openclaw-3.8.42.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.42` | [persona-3.8.42.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.42/persona-3.8.42.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.42` — skills  
  `sha256:c538feb593c24273…`
- **analyst** `3.8.42` — agent config, skills  
  `sha256:4a4ec62f4f219482…`
- **bead-space** `3.8.42` — brain pack only  
  `sha256:8c3701ebd7ccc217…`
- **claude-code** `3.8.42` — agent config, skills  
  `sha256:d3c3f0283397b7be…`
- **dgg_research** `3.8.42` — agent config, skills  
  `sha256:610cf7f0027d6586…`
- **gobbonet** `3.8.42` — agent config, Python  
  `sha256:2589144fd6800a27…`
- **hermes** `3.8.42` — agent config, skills  
  `sha256:349df6fc1a84605a…`
- **iris** `3.8.42` — skills  
  `sha256:1a301b4115b2276f…`
- **openclaw** `3.8.42` — agent config, skills  
  `sha256:42919a4d02160084…`
- **persona** `3.8.42` — brain pack only  
  `sha256:cb46b38e6527f94a…`
