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


Built from `v3.8.73` (adk 3.8.73).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.73` | [aither-3.8.73.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.73/aither-3.8.73.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.73` | [analyst-3.8.73.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.73/analyst-3.8.73.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.73` | [bead-space-3.8.73.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.73/bead-space-3.8.73.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.73` | [claude-code-3.8.73.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.73/claude-code-3.8.73.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.73` | [dgg_research-3.8.73.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.73/dgg_research-3.8.73.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.73` | [gobbonet-3.8.73.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.73/gobbonet-3.8.73.tar.gz) | 65.7 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.73` | [hermes-3.8.73.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.73/hermes-3.8.73.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.73` | [iris-3.8.73.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.73/iris-3.8.73.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.73` | [openclaw-3.8.73.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.73/openclaw-3.8.73.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.73` | [persona-3.8.73.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.73/persona-3.8.73.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.73` — skills  
  `sha256:9c96ddad0facd29c…`
- **analyst** `3.8.73` — agent config, skills  
  `sha256:a40020e0690a2869…`
- **bead-space** `3.8.73` — brain pack only  
  `sha256:b3a6b11178bcc0a1…`
- **claude-code** `3.8.73` — agent config, skills  
  `sha256:5eafe60439d322a1…`
- **dgg_research** `3.8.73` — agent config, skills  
  `sha256:aa70da0b9acc0f32…`
- **gobbonet** `3.8.73` — agent config, Python  
  `sha256:01c8cad41ac1a88a…`
- **hermes** `3.8.73` — agent config, skills  
  `sha256:3c6e36a6168afd90…`
- **iris** `3.8.73` — skills  
  `sha256:363c4096d46e67cc…`
- **openclaw** `3.8.73` — agent config, skills  
  `sha256:d33d8c56d0f4997c…`
- **persona** `3.8.73` — brain pack only  
  `sha256:96ae43f2a39068f3…`
