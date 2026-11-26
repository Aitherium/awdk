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
  `sha256:b2337a961d77ccb1…`
- **analyst** `4.0.1` — agent config, skills  
  `sha256:5b817d089104db9c…`
- **bead-space** `4.0.1` — brain pack only  
  `sha256:b4241afe8564c814…`
- **claude-code** `4.0.1` — agent config, skills  
  `sha256:0e668a32961c9dcf…`
- **dgg_research** `4.0.1` — agent config, skills  
  `sha256:ce61c471caa3e133…`
- **gobbonet** `4.0.1` — agent config, Python  
  `sha256:561f64ea296a4484…`
- **hermes** `4.0.1` — agent config, skills  
  `sha256:4c670eec1ed482cf…`
- **iris** `4.0.1` — skills  
  `sha256:02e2a3ee11751012…`
- **openclaw** `4.0.1` — agent config, skills  
  `sha256:e8552c02beaeb274…`
- **persona** `4.0.1` — brain pack only  
  `sha256:66b00fcca3393539…`
