---
name: ods
description: Stand up ODS (Osmantic Deployment System) — one installer that turns a PC, Mac, or Linux box into a private AI server with local LLM inference, a chat UI, voice, agents, workflow automation, RAG and image generation, all in Docker with no cloud dependency. Use when someone wants the whole local AI stack rather than wiring individual services by hand.
---

# ods — turn a box you already own into a private AI server

[ODS](https://github.com/Osmantic/ODS) (Apache-2.0) is the "I want the whole stack, not six
afternoons of wiring" option. One installer brings up local inference, a chat UI, voice,
autonomous agents, workflow automation, vector search and image generation — in Docker, on
your hardware, with nothing phoning home.

**Choose this over hand-wiring when** you want a working environment today and are happy to
run Docker. **Choose [`local-inference`](local-inference.md) instead** when you want one model
and one endpoint with nothing else running — ODS is a stack, and a stack costs RAM.

## What you get

| Service | What it's for | Default |
|---|---|---|
| **llama-server** | local LLM inference (llama.cpp) | `:11434` Linux/Docker, `:8080` macOS/Windows |
| **Open WebUI** | the chat interface | `:3000` |
| **Hermes Agent** | autonomous/browser agent with memory + skills | see [`hermes-agent`](hermes-agent.md) |
| **n8n** | workflow automation, 400+ integrations | |
| **Qdrant** | vector DB for RAG/search | |
| **ComfyUI** | image generation | |
| **OpenCode** | browser-based coding assistant | |
| **APE** | Agent Policy Engine — tool-call auditing | |

Every port is configurable via environment variables — see `.env.example`.

## Install

```bash
curl -fsSL https://install.osmantic.com/ods.sh | bash     # Linux / macOS
```

Windows (PowerShell):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
git clone https://github.com/Osmantic/ODS.git
cd ODS
.\install.ps1
```

Manual, if you'd rather read the script first (recommended for any `curl | bash`):

```bash
git clone https://github.com/Osmantic/ODS.git
cd ODS/ods
./install.sh
```

**Requirements:** Docker Desktop on Windows/macOS; Apple Silicon M1+ for native macOS
support. NVIDIA / AMD / Intel Arc GPUs are optional — **there is a CPU fallback**, so a box
without a GPU still works, just slower.

**Check — the stack is up when the chat UI answers:**

```bash
curl -sSL -o /dev/null -w "webui %{http_code}\n" http://localhost:3000
curl -s http://localhost:11434/v1/models | head -c 300      # Linux/Docker
curl -s http://localhost:8080/v1/models   | head -c 300      # macOS/Windows
```

A `200` on the UI with **no models listed** means the stack came up but no weights were
pulled yet — that's the single most common "it's broken" that isn't. Pull a model from the
catalog before concluding anything is wrong.

## Configuration

| File | What it controls |
|---|---|
| `.env` (from `.env.example`) | ports, service toggles, paths |
| `ods/config/model-library.json` | the model catalog offered at install |
| `extensions/services/<name>/manifest.yaml` + `compose.yaml` | add your own service |

The extension shape is worth knowing: a service is a `manifest.yaml` plus a `compose.yaml`
under `extensions/services/<name>/`, so adding a service to the stack is a directory, not a
patch to the core compose file. ODS also ships `config/extensions-catalog.json` and
`config/golden-paths.json` describing the sanctioned combinations.

## Point your agents at it

ODS's llama-server is **OpenAI-compatible**, so it's a drop-in endpoint for every agent skill
in this pack — you already have the base URL:

| Agent | Setting |
|---|---|
| [`tau`](tau.md) | `base_url` in `~/.tau/catalog.toml`, `api = "openai-completions"` |
| [`openclaw`](openclaw.md) | `baseUrl` in `~/.openclaw/openclaw.json` |
| [`hermes-agent`](hermes-agent.md) | `base_url` in `~/.hermes/cli-config.yaml` (already bundled in ODS) |
| [`deer-flow`](deer-flow.md) | `base_url` under `models:` in `config.yaml` |

Use `http://localhost:11434/v1` on Linux/Docker and `http://localhost:8080/v1` on
macOS/Windows. **Getting the port wrong per-platform is the most common wiring mistake here**
— the same stack genuinely listens on different ports depending on the host.

## Upstream vs fork

`Osmantic/ODS` is upstream and canonical for install instructions;
[`wizzense/ODS`](https://github.com/wizzense/ODS) is a fork. Install from whichever you intend
to track, but **don't mix** — a fork's `install.sh` and upstream's `model-library.json` can
drift, and the failure shows up as a service that won't start rather than as a clear version
error.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| UI up, no models | none pulled yet | pull from the model catalog |
| `curl :11434` refused on macOS | wrong port for the platform | use `:8080` |
| Everything slow / swapping | model too big for RAM | smaller model — [`local-inference`](local-inference.md) sizing table |
| Install fails on Windows | execution policy | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` |
| GPU not used | drivers/runtime missing | CPU fallback is working as designed; fix drivers to accelerate |
| Port already in use | another service on that port | change it in `.env` |

## Next

- **[`local-inference`](local-inference.md)** — model sizing and quantization, applies to ODS's llama-server too
- **[`hermes-agent`](hermes-agent.md)** — the agent ODS bundles
- **[`aither-start`](aither-start.md)** — the guided path this plugs into
- **[`awnode`](aithernode.md)** — expose this box's GPU/files to agents over MCP
