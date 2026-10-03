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


Built from `v3.8.46` (adk 3.8.46).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `3.8.46` | [aither-3.8.46.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.46/aither-3.8.46.tar.gz) | 9.8 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `3.8.46` | [analyst-3.8.46.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.46/analyst-3.8.46.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `3.8.46` | [bead-space-3.8.46.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.46/bead-space-3.8.46.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `3.8.46` | [claude-code-3.8.46.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.46/claude-code-3.8.46.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `3.8.46` | [dgg_research-3.8.46.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.46/dgg_research-3.8.46.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `3.8.46` | [gobbonet-3.8.46.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.46/gobbonet-3.8.46.tar.gz) | 58.3 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `3.8.46` | [hermes-3.8.46.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.46/hermes-3.8.46.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `3.8.46` | [iris-3.8.46.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.46/iris-3.8.46.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `3.8.46` | [openclaw-3.8.46.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.46/openclaw-3.8.46.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `3.8.46` | [persona-3.8.46.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.46/persona-3.8.46.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `3.8.46` — skills  
  `sha256:9c9eee9e425ff448…`
- **analyst** `3.8.46` — agent config, skills  
  `sha256:a35b75c77dd2a092…`
- **bead-space** `3.8.46` — brain pack only  
  `sha256:f27881beeb0837b4…`
- **claude-code** `3.8.46` — agent config, skills  
  `sha256:92c513968148650d…`
- **dgg_research** `3.8.46` — agent config, skills  
  `sha256:d6e8b91d8c149d52…`
- **gobbonet** `3.8.46` — agent config, Python  
  `sha256:611ebdd2ddb28a1f…`
- **hermes** `3.8.46` — agent config, skills  
  `sha256:084b0c8abf2425de…`
- **iris** `3.8.46` — skills  
  `sha256:8256335e74156a69…`
- **openclaw** `3.8.46` — agent config, skills  
  `sha256:4c020e7d1646969f…`
- **persona** `3.8.46` — brain pack only  
  `sha256:5a510b84e09e4f90…`
