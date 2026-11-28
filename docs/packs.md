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


Built from `v4.0.1` (adk 4.0.1).

| Pack | Version | Download | Size | What it is |
|---|---|---|---|---|
| **[Aither System Orchestrator](packs/aither.md)** | `4.0.1` | [aither-4.0.1.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/aither-4.0.1.tar.gz) | 10.0 KB | Aither — System Overseer & Orchestrator Brain Pack |
| **[Analyst Studio](packs/analyst.md)** | `4.0.1` | [analyst-4.0.1.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/analyst-4.0.1.tar.gz) | 5.3 KB | Analyst — Data & Structured-ML Agent Brain Pack |
| **[BeadSpace](packs/bead-space.md)** | `4.0.1` | [bead-space-4.0.1.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/bead-space-4.0.1.tar.gz) | 1.5 KB | BeadSpace — an aither-adk agent pack for bead-space |
| **[Claude Code Studio](packs/claude-code.md)** | `4.0.1` | [claude-code-4.0.1.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/claude-code-4.0.1.tar.gz) | 5.0 KB | Claude Code — Software Development Agent Brain Pack |
| **[DGG Research](packs/dgg_research.md)** | `4.0.1` | [dgg_research-4.0.1.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/dgg_research-4.0.1.tar.gz) | 7.2 KB | DGG Research — brain pack |
| **[GobboPack](packs/gobbonet.md)** | `4.0.1` | [gobbonet-4.0.1.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/gobbonet-4.0.1.tar.gz) | 66.2 KB | GobboNet Companion — an agent harness for a local-first chat client |
| **[Hermes Architecture Studio](packs/hermes.md)** | `4.0.1` | [hermes-4.0.1.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/hermes-4.0.1.tar.gz) | 4.9 KB | Hermes — Architecture & Reasoning Agent Brain Pack |
| **[Iris Visual Artisan](packs/iris.md)** | `4.0.1` | [iris-4.0.1.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/iris-4.0.1.tar.gz) | 8.2 KB | Iris — Visual Artisan Brain Pack |
| **[OpenClaw Research Studio](packs/openclaw.md)** | `4.0.1` | [openclaw-4.0.1.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/openclaw-4.0.1.tar.gz) | 5.1 KB | OpenClaw — Web Research Agent Brain Pack |
| **[Persona](packs/persona.md)** | `4.0.1` | [persona-4.0.1.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/persona-4.0.1.tar.gz) | 1.5 KB | Persona — an aither-adk agent pack for persona |

## Contents

- **aither** `4.0.1` — skills  
  `sha256:e96a278c0684d06c…`
- **analyst** `4.0.1` — agent config, skills  
  `sha256:17903a4f8d41b682…`
- **bead-space** `4.0.1` — brain pack only  
  `sha256:0755d2bcb59805a0…`
- **claude-code** `4.0.1` — agent config, skills  
  `sha256:b7252bb46745245f…`
- **dgg_research** `4.0.1` — agent config, skills  
  `sha256:1dfa971c3f825776…`
- **gobbonet** `4.0.1` — agent config, Python  
  `sha256:c5c1e0c868bf3e6d…`
- **hermes** `4.0.1` — agent config, skills  
  `sha256:77df540c6e3024bf…`
- **iris** `4.0.1` — skills  
  `sha256:52a03e4b4f9e03d0…`
- **openclaw** `4.0.1` — agent config, skills  
  `sha256:dd3c8b4af2e5cb55…`
- **persona** `4.0.1` — brain pack only  
  `sha256:1b4491b6dd8a0301…`
