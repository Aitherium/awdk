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


Built from `v3.8.47` (adk 3.8.47).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.47` | [aither-3.8.47.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.47/aither-3.8.47.tar.gz) | 9.9 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.47` | [analyst-3.8.47.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.47/analyst-3.8.47.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.47` | [bead-space-3.8.47.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.47/bead-space-3.8.47.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.47` | [claude-code-3.8.47.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.47/claude-code-3.8.47.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.47` | [dgg_research-3.8.47.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.47/dgg_research-3.8.47.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.47` | [gobbonet-3.8.47.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.47/gobbonet-3.8.47.tar.gz) | 58.3 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.47` | [hermes-3.8.47.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.47/hermes-3.8.47.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.47` | [iris-3.8.47.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.47/iris-3.8.47.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.47` | [openclaw-3.8.47.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.47/openclaw-3.8.47.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.47` | [persona-3.8.47.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.47/persona-3.8.47.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.47` — skills  
  `sha256:07401a4d824ab41a…`
- **analyst** `3.8.47` — agent config, skills  
  `sha256:2b3e072c99367081…`
- **bead-space** `3.8.47` — brain pack only  
  `sha256:0e473b645dbb9fa7…`
- **claude-code** `3.8.47` — agent config, skills  
  `sha256:7071b7fbf352b354…`
- **dgg_research** `3.8.47` — agent config, skills  
  `sha256:2ec2132f84876a8b…`
- **gobbonet** `3.8.47` — agent config, Python  
  `sha256:f639adad66940686…`
- **hermes** `3.8.47` — agent config, skills  
  `sha256:c37798e19062768e…`
- **iris** `3.8.47` — skills  
  `sha256:e32abe38623f98a9…`
- **openclaw** `3.8.47` — agent config, skills  
  `sha256:842b68bbe54dca14…`
- **persona** `3.8.47` — brain pack only  
  `sha256:05e3d73675986d26…`
