# Grid Distributed Inference Setup

Run distributed AI inference across heterogeneous hardware — GPU, Mac, and CPU cluster nodes — with a single command.

```
                        YOUR NETWORK
    ┌──────────────────────────────────────────────────┐
    │                                                  │
    │  ┌─────────────┐  ┌──────────────┐  ┌─────────┐ │
    │  │  Main PC     │  │  Mac Mini    │  │ Mini PC │ │
    │  │  RTX 2060S   │  │  Apple M2    │  │ x86_64  │ │
    │  │  8GB VRAM    │  │  16GB RAM    │  │ 32GB RAM│ │
    │  │              │  │              │  │         │ │
    │  │  vLLM :8120  │  │ llama.cpp    │  │ llama   │ │
    │  │  Nemotron-8B │  │ :8121        │  │ .cpp    │ │
    │  │  TQ4         │  │ DeepSeek-R1  │  │ :8121   │ │
    │  │              │  │ 8B Q4        │  │ Qwen2.5 │ │
    │  │  effort 1-6  │  │ effort 7-8   │  │ -32B Q4 │ │
    │  │  15-25 tok/s │  │ 8-15 tok/s   │  │ eff 9-10│ │
    │  │              │  │ Metal GPU    │  │ 5-10t/s │ │
    │  └─────────────┘  └──────────────┘  └─────────┘ │
    │       ▲ adk shell                                │
    │       │ adk-serve                                │
    └──────────────────────────────────────────────────┘
```

All remote nodes run **llama.cpp** with `--api-oai` — uniform OpenAI-compatible API everywhere. Mac uses Metal GPU acceleration; mini PCs use CPU with `--mlock`.

## Hardware Requirements

| Node | Role | Minimum | Recommended |
|------|------|---------|-------------|
| **Main PC** | GPU orchestrator (vLLM) | NVIDIA GPU 6GB+ VRAM, Docker | 8GB+ VRAM, 16GB RAM |
| **Mac Mini** | Reasoning (llama.cpp + Metal) | Apple Silicon, 8GB RAM | M2/M3/M4, 16GB+ RAM |
| **Mini PC** | CPU cluster (llama.cpp) | x86_64, 32GB RAM | 64GB+ RAM, fast DDR5 |

Multiple mini PCs can be used — each runs an independent llama.cpp server. The first node in the list is used for routing.

## Quick Start

### 1. Set up Mac (one-time)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Aitherium/awdk/main/scripts/setup-mac-node.sh)
```

This installs llama.cpp via Homebrew, downloads DeepSeek-R1 8B Q4_K_M (~5GB), creates a launchd service with Metal GPU acceleration and `--api-oai`, and binds to `0.0.0.0` for LAN access.

Verify: `curl http://<mac-ip>:8121/v1/models` should return model list.

### 2. Set up cluster nodes (one-time, per node)

```bash
# On each mini PC:
bash <(curl -fsSL https://raw.githubusercontent.com/Aitherium/awdk/main/scripts/setup-cluster-node.sh)
```

This installs llama.cpp, downloads Qwen2.5-32B Q4_K_M (~20GB), and creates a systemd service.

Verify: `curl http://<node-ip>:8121/v1/chat/completions -d '{"model":"qwen","messages":[{"role":"user","content":"hi"}]}'`

### 3. Deploy on main PC

```bash
pip install awdk

# All three tiers (explicit IPs)
adk deploy grid \
  --mac-host 192.168.1.100 \
  --cluster-nodes '["192.168.1.10","192.168.1.11"]'

# Auto-discover the reasoning node on LAN (TCP scan of your /24 subnet for
# llama.cpp on :8121 -- or $LLAMACPP_PORT -- that answers /v1/models)
adk deploy grid --cluster-nodes '["192.168.1.10"]'

# GPU only (add Mac/cluster later)
adk deploy grid
```

If `--mac-host` is omitted, the deploy scans your LAN automatically. Discovery is a
plain subnet scan, not mDNS/Avahi: it probes every address in the local /24 for an
OpenAI-compatible llama.cpp server (`--api-oai`) on the grid port and uses the first
one that answers. Nodes on another subnet or behind a firewall must be given explicitly.

Node health is available two ways: `adk grid status` in a terminal, or
`GET /grid/status` on the ADK server (same auth as every other data route), which
returns `{configured, total, healthy, nodes: [{role, host, port, state, models}]}`.

### 4. Start using it

```bash
adk shell                     # Interactive terminal
adk-serve --port 8080         # HTTP API (OpenAI-compatible)
```

## Effort Routing

The LLM router selects the backend based on task effort level:

| Effort | Tier | Backend | Model | Speed |
|--------|------|---------|-------|-------|
| 1-6 | Local GPU | vLLM (localhost:8120) | Nemotron-8B TQ4 | 15-25 tok/s |
| 7-8 | Reasoning | llama.cpp (Mac:8121) | DeepSeek-R1 8B Q4 | 8-15 tok/s |
| 9-10 | Cluster | llama.cpp (node:8121) | Qwen2.5-32B Q4 | 5-10 tok/s |

Each tier falls back to the next lower one if unavailable:
- Cluster fails → reasoning → local GPU
- Reasoning fails → local GPU
- Local GPU is always the last resort

## Model Sizing

| Model | Params | Quantization | Memory Required | Where |
|-------|--------|-------------|-----------------|-------|
| Nemotron-Orchestrator-8B | 8B | TQ4 (TurboQuant) | ~6.4GB VRAM | GPU (vLLM) |
| DeepSeek-R1 8B | 8B | Q4_K_M | ~5GB RAM | Mac (llama.cpp + Metal) |
| Qwen2.5-32B-Instruct | 32B | Q4_K_M | ~20GB RAM | CPU cluster (llama.cpp) |

## Configuration

`adk deploy grid` saves config to `~/.aither/config.json`:

```json
{
  "profile": "grid_distributed",
  "backend": "vllm",
  "base_url": "http://localhost:8120/v1",
  "model": "aither-orchestrator",
  "reasoning_backend": "openai",
  "reasoning_url": "http://192.168.1.100:8121/v1",
  "reasoning_model": "deepseek-r1-8b",
  "cluster_backend": "openai",
  "cluster_url": "http://192.168.1.10:8121/v1",
  "cluster_model": "qwen2.5-32b"
}
```

Environment variables override saved config:

| Variable | Purpose |
|----------|---------|
| `AITHER_GRID_MAC_HOST` | Mac Mini IP address |
| `AITHER_GRID_CLUSTER_NODES` | JSON array of cluster node IPs |
| `AITHER_CLUSTER_BACKEND` | Cluster provider type (default: `openai`) |
| `AITHER_CLUSTER_BASE_URL` | Cluster endpoint URL |
| `AITHER_CLUSTER_MODEL` | Cluster model name |

## Managing Nodes

After initial deploy, use `adk grid` to add/remove nodes without re-deploying:

```bash
# Show current topology and health
adk grid status

# Add nodes
adk grid add reasoning 192.168.1.100          # Mac reasoning node
adk grid add cluster 192.168.1.10             # CPU cluster node
adk grid add cluster 192.168.1.11             # Another cluster node
adk grid add cluster 192.168.1.12 --port 9000 # Custom port

# Remove a node
adk grid remove 192.168.1.12

# Test connectivity
adk grid test                                 # All nodes
adk grid test 192.168.1.100                   # Specific node
```

## Cloud Sync (api.aitherium.com)

Grid config can be synced to your Aitherium workspace so you can pull it on another machine:

```bash
# Authenticate (one-time)
adk login

# Push config to workspace
adk grid sync

# On another machine — pull config
adk login
adk grid pull
```

When you're signed in, this persists `grid/config.json` to your Aitherium account, so your node IPs, ports, models, and routing config travel with you across machines. Local-only? It stays in `~/.aither/`.

## Docker Compose

The `docker-compose.grid.yml` runs vLLM on the main PC:

```bash
# Just vLLM (run adk shell locally)
docker compose -f docker-compose.grid.yml up -d

# With MCP server container
docker compose -f docker-compose.grid.yml --profile node up -d

# With embedding model
docker compose -f docker-compose.grid.yml --profile embeddings up -d
```

## Verification

```bash
# 1. Check vLLM health
curl http://localhost:8120/health

# 2. Check Mac reasoning (llama.cpp)
curl http://<mac-ip>:8121/v1/models

# 3. Check cluster node
curl http://<node-ip>:8121/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen","messages":[{"role":"user","content":"hi"}]}'

# 4. Check saved config
cat ~/.aither/config.json

# 5. Test routing
adk shell
# Simple question → GPU (fast)
# "Think step by step about X" → Mac reasoning
# "Deeply analyze this codebase" → Cluster (big model)
```

## Troubleshooting

**vLLM won't start / OOM**
- Check VRAM: `nvidia-smi`. Need 6GB+ free.
- Reduce context: set `ADK_VLLM_MAX_MODEL_LEN=4096` in env.
- Check logs: `docker logs -f adk-vllm-orchestrator`

**Mac llama.cpp not reachable from LAN**
- Check launchd service: `launchctl list | grep aitherium`
- Check logs: `tail -f ~/aither/logs/llamacpp.err`
- Verify macOS firewall allows port 8121.
- Test: `curl http://<mac-ip>:8121/v1/models`
- Re-run `bash scripts/setup-mac-node.sh` to regenerate the launchd plist.

**llama.cpp cluster node not responding**
- Check service: `systemctl status aither-llamacpp`
- Check logs: `journalctl -u aither-llamacpp -f`
- Verify `--api-oai` flag is in the systemd unit (required for `/v1/chat/completions`).
- **Upgraded from older setup?** Re-run `bash scripts/setup-cluster-node.sh` to regenerate the systemd unit with `--api-oai`.
- Test health: `curl http://localhost:8121/health`

**Routing not using expected tier**
- Check config: `cat ~/.aither/config.json`
- Ensure effort level is being passed. In `adk shell`, complex questions auto-score higher effort.
- Verify with `adk shell --debug` to see effort routing decisions.
