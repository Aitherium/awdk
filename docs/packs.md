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


Built from `v3.8.26` (adk 3.8.26).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.26` | [aither-3.8.26.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.26/aither-3.8.26.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.26` | [analyst-3.8.26.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.26/analyst-3.8.26.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.26` | [bead-space-3.8.26.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.26/bead-space-3.8.26.tar.gz) | 1.6 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.26` | [claude-code-3.8.26.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.26/claude-code-3.8.26.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.26` | [dgg_research-3.8.26.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.26/dgg_research-3.8.26.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.26` | [gobbonet-3.8.26.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.26/gobbonet-3.8.26.tar.gz) | 47.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.26` | [hermes-3.8.26.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.26/hermes-3.8.26.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.26` | [iris-3.8.26.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.26/iris-3.8.26.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.26` | [openclaw-3.8.26.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.26/openclaw-3.8.26.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.26` | [persona-3.8.26.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.26/persona-3.8.26.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.26` — skills  
  `sha256:79c8b338e10a7775…`
- **analyst** `3.8.26` — agent config, skills  
  `sha256:659c5f0c761377be…`
- **bead-space** `3.8.26` — brain pack only  
  `sha256:d936aaa11db3df56…`
- **claude-code** `3.8.26` — agent config, skills  
  `sha256:90ef3c5d67898dc3…`
- **dgg_research** `3.8.26` — agent config, skills  
  `sha256:93600bd986842b23…`
- **gobbonet** `3.8.26` — agent config, Python  
  `sha256:ea1af72467e36bf9…`
- **hermes** `3.8.26` — agent config, skills  
  `sha256:ee04d3bdcda29a2f…`
- **iris** `3.8.26` — skills  
  `sha256:ad799178c364ca85…`
- **openclaw** `3.8.26` — agent config, skills  
  `sha256:88d4ecbe38836769…`
- **persona** `3.8.26` — brain pack only  
  `sha256:08b4dd4cd7f1f893…`
