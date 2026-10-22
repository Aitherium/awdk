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


Built from `v3.8.83` (adk 3.8.83).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.83` | [aither-3.8.83.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.83/aither-3.8.83.tar.gz) | 9.9 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.83` | [analyst-3.8.83.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.83/analyst-3.8.83.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.83` | [bead-space-3.8.83.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.83/bead-space-3.8.83.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.83` | [claude-code-3.8.83.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.83/claude-code-3.8.83.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.83` | [dgg_research-3.8.83.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.83/dgg_research-3.8.83.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.83` | [gobbonet-3.8.83.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.83/gobbonet-3.8.83.tar.gz) | 65.8 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.83` | [hermes-3.8.83.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.83/hermes-3.8.83.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.83` | [iris-3.8.83.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.83/iris-3.8.83.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.83` | [openclaw-3.8.83.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.83/openclaw-3.8.83.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.83` | [persona-3.8.83.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.83/persona-3.8.83.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.83` — skills  
  `sha256:71aacc54ad5e4825…`
- **analyst** `3.8.83` — agent config, skills  
  `sha256:33a9246293440374…`
- **bead-space** `3.8.83` — brain pack only  
  `sha256:d820b3cf837e02d5…`
- **claude-code** `3.8.83` — agent config, skills  
  `sha256:0bbcf41aa038298c…`
- **dgg_research** `3.8.83` — agent config, skills  
  `sha256:35bb507b2cd10922…`
- **gobbonet** `3.8.83` — agent config, Python  
  `sha256:a6424385b431aa05…`
- **hermes** `3.8.83` — agent config, skills  
  `sha256:d90a0746afb1aeb5…`
- **iris** `3.8.83` — skills  
  `sha256:258f000156bc2bb0…`
- **openclaw** `3.8.83` — agent config, skills  
  `sha256:217c38e2de33024c…`
- **persona** `3.8.83` — brain pack only  
  `sha256:680d1f7bd1e78b68…`
