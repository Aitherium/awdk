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


Built from `v3.8.79` (adk 3.8.79).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.79` | [aither-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/aither-3.8.79.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.79` | [analyst-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/analyst-3.8.79.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.79` | [bead-space-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/bead-space-3.8.79.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.79` | [claude-code-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/claude-code-3.8.79.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.79` | [dgg_research-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/dgg_research-3.8.79.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.79` | [gobbonet-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/gobbonet-3.8.79.tar.gz) | 65.7 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.79` | [hermes-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/hermes-3.8.79.tar.gz) | 4.8 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.79` | [iris-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/iris-3.8.79.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.79` | [openclaw-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/openclaw-3.8.79.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.79` | [persona-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/persona-3.8.79.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.79` — skills  
  `sha256:ac65d94b9b3c9f86…`
- **analyst** `3.8.79` — agent config, skills  
  `sha256:454ddc49022ee143…`
- **bead-space** `3.8.79` — brain pack only  
  `sha256:2d8ee1707e6f070f…`
- **claude-code** `3.8.79` — agent config, skills  
  `sha256:5df5a9a7f813b0a8…`
- **dgg_research** `3.8.79` — agent config, skills  
  `sha256:0ac9cde98df98578…`
- **gobbonet** `3.8.79` — agent config, Python  
  `sha256:4b58686ffe6fde19…`
- **hermes** `3.8.79` — agent config, skills  
  `sha256:4a8f54655459f8fd…`
- **iris** `3.8.79` — skills  
  `sha256:4258163161edbb47…`
- **openclaw** `3.8.79` — agent config, skills  
  `sha256:033bb56e513fbb37…`
- **persona** `3.8.79` — brain pack only  
  `sha256:b42f4096541f8127…`
