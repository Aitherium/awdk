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


Built from `v3.8.92` (adk 3.8.92).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.92` | [aither-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/aither-3.8.92.tar.gz) | 10.0 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.92` | [analyst-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/analyst-3.8.92.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.92` | [bead-space-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/bead-space-3.8.92.tar.gz) | 1.6 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.92` | [claude-code-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/claude-code-3.8.92.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.92` | [dgg_research-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/dgg_research-3.8.92.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.92` | [gobbonet-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/gobbonet-3.8.92.tar.gz) | 65.9 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.92` | [hermes-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/hermes-3.8.92.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.92` | [iris-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/iris-3.8.92.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.92` | [openclaw-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/openclaw-3.8.92.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.92` | [persona-3.8.92.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.92/persona-3.8.92.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.92` — skills  
  `sha256:bdaf37688807aebd…`
- **analyst** `3.8.92` — agent config, skills  
  `sha256:f422f50911f34562…`
- **bead-space** `3.8.92` — brain pack only  
  `sha256:dd9985a2d482ef6e…`
- **claude-code** `3.8.92` — agent config, skills  
  `sha256:b2e85a1c397cebc0…`
- **dgg_research** `3.8.92` — agent config, skills  
  `sha256:8ce97b8ceb3b1b19…`
- **gobbonet** `3.8.92` — agent config, Python  
  `sha256:aa74b4b3aa30283c…`
- **hermes** `3.8.92` — agent config, skills  
  `sha256:896a2c485cd1d5e3…`
- **iris** `3.8.92` — skills  
  `sha256:80119ee3d5314418…`
- **openclaw** `3.8.92` — agent config, skills  
  `sha256:2cbaddcfd0a36879…`
- **persona** `3.8.92` — brain pack only  
  `sha256:abdde975e6220dc7…`
