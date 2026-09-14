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


Built from `v3.8.18` (adk 3.8.18).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.18` | [aither-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/aither-3.8.18.tar.gz) | 4.4 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.18` | [analyst-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/analyst-3.8.18.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.18` | [bead-space-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/bead-space-3.8.18.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.18` | [claude-code-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/claude-code-3.8.18.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.18` | [dgg_research-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/dgg_research-3.8.18.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.18` | [gobbonet-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/gobbonet-3.8.18.tar.gz) | 45.7 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.18` | [hermes-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/hermes-3.8.18.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.18` | [iris-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/iris-3.8.18.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.18` | [openclaw-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/openclaw-3.8.18.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.18` | [persona-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/persona-3.8.18.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.18` — skills  
  `sha256:2915cca0ce4e17d5…`
- **analyst** `3.8.18` — agent config, skills  
  `sha256:55121dad0ad671d1…`
- **bead-space** `3.8.18` — brain pack only  
  `sha256:1dbcf1805c4e1b4f…`
- **claude-code** `3.8.18` — agent config, skills  
  `sha256:266689c1635d70a2…`
- **dgg_research** `3.8.18` — agent config, skills  
  `sha256:718d1c3afa9afec7…`
- **gobbonet** `3.8.18` — agent config, Python  
  `sha256:8e5520df02c3118d…`
- **hermes** `3.8.18` — agent config, skills  
  `sha256:c1751f89fdbcb3a3…`
- **iris** `3.8.18` — skills  
  `sha256:e413e52820f6ddfb…`
- **openclaw** `3.8.18` — agent config, skills  
  `sha256:b0d88699e18c333f…`
- **persona** `3.8.18` — brain pack only  
  `sha256:2f8c4907aee622f8…`
