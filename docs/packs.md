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


Built from `v3.8.22` (adk 3.8.22).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.22` | [aither-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/aither-3.8.22.tar.gz) | 9.9 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.22` | [analyst-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/analyst-3.8.22.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.22` | [bead-space-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/bead-space-3.8.22.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.22` | [claude-code-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/claude-code-3.8.22.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.22` | [dgg_research-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/dgg_research-3.8.22.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.22` | [gobbonet-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/gobbonet-3.8.22.tar.gz) | 47.4 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.22` | [hermes-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/hermes-3.8.22.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.22` | [iris-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/iris-3.8.22.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.22` | [openclaw-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/openclaw-3.8.22.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.22` | [persona-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/persona-3.8.22.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.22` — skills  
  `sha256:c8d4a863e8222ddc…`
- **analyst** `3.8.22` — agent config, skills  
  `sha256:ae6e4bb115c3bd3d…`
- **bead-space** `3.8.22` — brain pack only  
  `sha256:fd029e82740de94b…`
- **claude-code** `3.8.22` — agent config, skills  
  `sha256:9f4ea51733da775a…`
- **dgg_research** `3.8.22` — agent config, skills  
  `sha256:9d682bbfb1a211e9…`
- **gobbonet** `3.8.22` — agent config, Python  
  `sha256:3022f0222af79f7f…`
- **hermes** `3.8.22` — agent config, skills  
  `sha256:d1f4e85f95f5238c…`
- **iris** `3.8.22` — skills  
  `sha256:ac52345b7b5d9894…`
- **openclaw** `3.8.22` — agent config, skills  
  `sha256:14352e47499c6bbc…`
- **persona** `3.8.22` — brain pack only  
  `sha256:3ab7846e06a721b3…`
