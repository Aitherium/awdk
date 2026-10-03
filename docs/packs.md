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


Built from `v3.8.48` (adk 3.8.48).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.48` | [aither-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/aither-3.8.48.tar.gz) | 9.9 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.48` | [analyst-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/analyst-3.8.48.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.48` | [bead-space-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/bead-space-3.8.48.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.48` | [claude-code-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/claude-code-3.8.48.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.48` | [dgg_research-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/dgg_research-3.8.48.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.48` | [gobbonet-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/gobbonet-3.8.48.tar.gz) | 58.3 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.48` | [hermes-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/hermes-3.8.48.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.48` | [iris-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/iris-3.8.48.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.48` | [openclaw-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/openclaw-3.8.48.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.48` | [persona-3.8.48.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.48/persona-3.8.48.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.48` — skills  
  `sha256:80697e4dcdf6f675…`
- **analyst** `3.8.48` — agent config, skills  
  `sha256:ce7335f7aa7fff89…`
- **bead-space** `3.8.48` — brain pack only  
  `sha256:b470eac7d9264aee…`
- **claude-code** `3.8.48` — agent config, skills  
  `sha256:eddc3f3bb5ab84e0…`
- **dgg_research** `3.8.48` — agent config, skills  
  `sha256:ae69074c1658f38f…`
- **gobbonet** `3.8.48` — agent config, Python  
  `sha256:32b8878fb7b924bb…`
- **hermes** `3.8.48` — agent config, skills  
  `sha256:f431bfc4ed3ca13b…`
- **iris** `3.8.48` — skills  
  `sha256:0cccb99d8a449204…`
- **openclaw** `3.8.48` — agent config, skills  
  `sha256:3ad8b1f1f8f099e8…`
- **persona** `3.8.48` — brain pack only  
  `sha256:1960e1b3c1bb55dd…`
