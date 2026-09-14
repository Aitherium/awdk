<div align="center">

<img src="assets/aitheros-logo.png" alt="AitherOS" width="200" />

# Aither ADK

**Build AI agent fleets. 3 lines, any backend, local or cloud.**

Runtime backend switching, hybrid reasoning, TQ4 quantization, 48 identities, knowledge graph memory, fleet orchestration.

[![PyPI](https://img.shields.io/pypi/v/awdk?style=flat-square&color=blue)](https://pypi.org/project/awdk/)
[![Status](https://img.shields.io/badge/status-beta-blueviolet?style=flat-square)](https://aitherium.com)
[![Built By](https://img.shields.io/badge/built%20by-one%20person-cyan?style=flat-square)](#)
[![License](https://img.shields.io/badge/license-Apache--2.0-green?style=flat-square)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-2600%2B-green?style=flat-square)](#)

```bash
pip install awdk
```

[Get Started](#get-started) |
[Architecture](#architecture) |
[Agents](#agents) |
[ADK Docs](awdk/README.md) |
[Roadmap](ROADMAP.md) |
[aitherium.com](https://aitherium.com)

</div>

---

## Get Started

### 3 Lines to Your First Agent

```python
import asyncio
from adk import AitherAgent

async def main():
    agent = AitherAgent("aither")  # Auto-detects Ollama/vLLM on localhost
    response = await agent.chat("Hello! What can you help me with?")
    print(response.content)

asyncio.run(main())
```

Works with **Ollama, OpenAI, Anthropic, DeepSeek, vLLM, LM Studio, Groq, Together**, or any OpenAI-compatible API.

### Quickstart

```bash
pip install awdk
adk quickstart          # GPU detection + setup + auth in one command
```

Or go step by step:

```bash
adk setup nemotron              # Deploy Nemotron-8B (one command)
adk setup --tier nano           # 6GB GPU? TQ4 4-bit quantization
adk setup --reasoning-api anthropic  # Hybrid: local + cloud reasoning
adk doctor                      # Check everything works
adk start                       # Chat with your codebase
```

### Runtime Backend Switching

```python
agent = AitherAgent("research-bot")
response = await agent.chat("Analyze this codebase")  # Uses auto-detected backend

# Switch backends at runtime — no restart needed
agent.switch_backend("anthropic", api_key="sk-ant-...")
agent.switch_backend("deepseek")
agent.switch_backend("vllm", base_url="http://dgx-spark:8000/v1")

# Hybrid: local Nemotron for chat, cloud for reasoning
agent.set_reasoning_backend("anthropic")  # Only effort 7+ goes to cloud
```

### Run as a Server

```bash
aither-serve --identity aither --port 8080
```

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.2","messages":[{"role":"user","content":"hello"}]}'
```

### Custom Tools

```python
from adk import AitherAgent, tool
from adk.llm import LLMRouter

@tool
def search_web(query: str) -> str:
    """Search the web for information."""
    return f"Results for: {query}"

agent = AitherAgent(
    "research-bot",
    identity="lyra",
    llm=LLMRouter(provider="openai", api_key="sk-..."),
)

response = await agent.run("Research AI agent frameworks in 2026")
```

### Fleet Mode

```bash
# Single agent
aither-serve --identity aither

# Fleet of specialists
aither-serve --agents aither,lyra,demiurge,hydra,athena

# OpenAI-compatible API
curl http://localhost:8080/v1/chat/completions \
  -d '{"model":"aither","messages":[{"role":"user","content":"hello"}]}'
```

---

## Architecture

The ADK is extracted from **AitherOS**, a full-stack agentic operating system with 208 microservices across 12 architectural layers.

```
LAYER 10  UI          AitherVeil (Next.js dashboard)
LAYER 9   TRAINING    Model lifecycle, daydream corpus, session harvesting
LAYER 8.5 MESH        Distributed node network, GPU sharing
LAYER 8   SECURITY    RBAC, secrets, flux monitoring, recovery
LAYER 7   AUTOMATION  Scheduler, demand, autonomic routines
LAYER 6   GPU         VRAM coordination, parallel inference, ComfyUI
LAYER 5   AGENTS      Agent council, forge dispatch, genesis orchestration
LAYER 3   COGNITION   Reasoning, judgment, flow control, will policies
LAYER 2   PERCEPTION  Voice, vision, portal, reflex
LAYER 1   CORE        Node, Pulse, Watch, MicroScheduler
LAYER 0   INFRA       Chronicle, Secrets, Nexus, Strata
```

**By the numbers:**
- 208 microservices (24 compound containers absorbing 92 sub-services)
- 65 Docker containers
- 2600+ passing tests across 120+ test files
- 43 specialist agents with persistent identity (17 ship with ADK)
- 170+ PowerShell automation scripts

---

## Agents

17 agent identities ship with `awdk` as package data:

| Agent | Role | Specialty |
|-------|------|-----------|
| **Aither** | Orchestrator | System coordination, delegation, awareness synthesis |
| **Atlas** | Project Manager | Roadmaps, research delegation, executive reporting |
| **Demiurge** | Code Craftsman | Code generation, refactoring, architecture |
| **Lyra** | Researcher | Knowledge synthesis, deep-dive analysis |
| **Athena** | Security Oracle | Vulnerability analysis, threat assessment |
| **Hydra** | Code Guardian | Multi-perspective code review, quality assurance |
| **Prometheus** | Worldbuilder | Simulation, game integration, procedural generation |
| **Apollo** | Performance | Optimization, benchmarking, profiling |
| **Iris** | Creative Muse | Image/video generation via ComfyUI |
| **Viviane** | Memory Guardian | Knowledge retrieval, context preservation |
| **Vera** | Content Creator | Writing, editing, social media |
| **Hera** | Community | Social engagement, publishing |
| **Morgana** | Secrets Keeper | Encryption, secure configuration |
| **Saga** | Documentation | Technical writing, knowledge base |
| **Themis** | Compliance | Ethics, fairness, policy enforcement |
| **Chaos** | Chaos Engineer | Resilience testing, failure injection |
| **Muse** | Artist | Creative and artistic generation |

The full AitherOS system runs 43 agents including domain specialists, seven deadly sins personas, and Arthurian-themed memory guardians.

---

## Features

- **Multi-backend LLM** — Ollama, OpenAI, Anthropic, DeepSeek, vLLM, Groq, Together, LM Studio, Aitherium cloud
- **Runtime backend switching** — `agent.switch_backend("deepseek")` at any time, no restart
- **Hybrid reasoning** — Local model for chat, cloud API for hard tasks (effort 7+)
- **TQ4 quantization** — TurboQuant 4-bit fits Nemotron-8B on 6GB GPUs
- **Effort-based routing** — Effort 1-3 small models, 4-6 orchestrator, 7-10 reasoning
- **`@tool` decorator** — Function calling with any model
- **Graph memory** — CodeGraph (AST indexing) + MemoryGraph (persistent recall) with schema migrations
- **Vector memory** — Embedding-based semantic search (4-backend fallback chain)
- **Fleet orchestration** — Multi-agent coordination with delegation
- **Swarm coding** — 11 agents in 4-phase pipeline (architect, swarm, review, judge)
- **Group chat** — Multi-agent sessions with 7 presets
- **MCP bridge** — 100+ tools via `mcp.aitherium.com`
- **Slash-command bridge** — ADK server auto-exposes all 35 CLI commands to AitherShell
- **Training pipeline** — `adk train launch/status/logs` for model fine-tuning
- **OpenAI-compatible server** — Drop-in replacement
- **SQLite persistence** — Conversations, KV store, knowledge graphs with versioned schema
- **DGX Spark detection** — Auto-discovers remote vLLM endpoints on LAN
- **Cross-platform pairing** — Link users across Telegram, Discord, Slack, WhatsApp
- **Voice** — STT/TTS/emotion via AitherVoice
- **Privacy-first** — Opt-in telemetry, data stays local
- **Apache-2.0** — Fully permissive license

---

## Hardware Profiles

| Tier | GPU VRAM | Quantization | Models |
|------|----------|-------------|--------|
| **Cloud Only** | None | N/A | `AITHER_API_KEY` for cloud inference |
| **Nano** | 6-8 GB | TQ4 (4-bit) | Nemotron-8B TQ4 via vLLM |
| **Lite** | 10-16 GB | BNB (8-bit) | Nemotron-8B via vLLM |
| **Standard-TQ4** | 12-16 GB | TQ4 (4-bit) | Nemotron-8B + DeepSeek-R1-14B, both TQ4 |
| **Standard** | 20-24 GB | BNB (8-bit) | Nemotron-8B + DeepSeek-R1-14B |
| **Full** | 24 GB+ | BNB (8-bit) | Standard + Nomic embeddings |
| **Hybrid** | Any + API key | Mixed | Local Nemotron + cloud API for reasoning |

**No GPU? No problem.** Set `AITHER_API_KEY` and your agents use [Aitherium cloud](https://aitherium.com) for inference. Have a GPU? They auto-detect vLLM/Ollama. Both? They route intelligently by effort level.

**`adk setup` auto-detects your hardware** and recommends the right tier. TQ4 quantization runs Nemotron on a 6GB RTX 3060.

---

## Install

```bash
# PyPI (recommended)
pip install awdk

# With optional dependencies
pip install awdk[graphs]      # numpy for 10x cosine similarity
pip install awdk[embedding]   # sentence-transformers for local GPU embeddings
pip install awdk[all]         # everything

# AitherShell interactive terminal (downloads automatically)
adk shell
```

### CLI Commands

```
adk quickstart          One-command setup wizard
adk start               Chat with your codebase (zero config)
adk setup nemotron      Deploy Nemotron-8B on your GPU
adk backend list        Show detected LLM backends
adk backend set deepseek   Switch to DeepSeek API
adk train status        Check training readiness
adk train launch        Launch a fine-tuning run
adk tools               List available tools (local + cloud)
adk doctor              System health check
adk shell               AitherShell interactive terminal
adk backup              Export all agent data
```

---

## Stay Updated

1. **Star** this repository
2. **Watch** -> "Releases only" for minimal noise
3. **Sign up** at [aitherium.com](https://aitherium.com)

Contact: hello@aitherium.com

---

## License

AitherADK is **Apache-2.0** (fully permissive).

The full AitherOS platform uses a dual license:
- **AGPL-3.0** for open source use
- **Commercial license** for enterprise deployments

---

*Built solo. Shipping real.*
