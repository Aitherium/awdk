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


Built from `v3.8.81` (adk 3.8.81).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.81` | [aither-3.8.81.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.81/aither-3.8.81.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.81` | [analyst-3.8.81.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.81/analyst-3.8.81.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.81` | [bead-space-3.8.81.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.81/bead-space-3.8.81.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.81` | [claude-code-3.8.81.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.81/claude-code-3.8.81.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.81` | [dgg_research-3.8.81.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.81/dgg_research-3.8.81.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.81` | [gobbonet-3.8.81.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.81/gobbonet-3.8.81.tar.gz) | 65.8 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.81` | [hermes-3.8.81.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.81/hermes-3.8.81.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.81` | [iris-3.8.81.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.81/iris-3.8.81.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.81` | [openclaw-3.8.81.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.81/openclaw-3.8.81.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.81` | [persona-3.8.81.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.81/persona-3.8.81.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.81` — skills  
  `sha256:c70c661be5cc6777…`
- **analyst** `3.8.81` — agent config, skills  
  `sha256:7651b1a5aac2e46f…`
- **bead-space** `3.8.81` — brain pack only  
  `sha256:e44d145c369d1745…`
- **claude-code** `3.8.81` — agent config, skills  
  `sha256:a2bf6beacf200209…`
- **dgg_research** `3.8.81` — agent config, skills  
  `sha256:d416d6af74e7995f…`
- **gobbonet** `3.8.81` — agent config, Python  
  `sha256:7448d9b9ee928f36…`
- **hermes** `3.8.81` — agent config, skills  
  `sha256:6b69bce3123449e3…`
- **iris** `3.8.81` — skills  
  `sha256:a40f2459292b2927…`
- **openclaw** `3.8.81` — agent config, skills  
  `sha256:37e4d2b684b7651f…`
- **persona** `3.8.81` — brain pack only  
  `sha256:a274c93d7c2f4dc6…`
