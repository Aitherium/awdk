# Self-Hosting Runbook: Memory Tiering & Mesh Integration

This runbook describes the complete flow for deploying an autonomous agent on your infrastructure with durable memory, secure mesh networking, and cloud integration.

## Quick Start

```bash
# 1. Install and create a local agent
pip install awdk
adk up --identity my-agent

# 2. Provision local Qdrant for working-set memory (optional, but recommended)
adk stack qdrant

# 3. Join the mesh for remote access and cloud sync
adk mesh join

# 4. Verify everything is working
adk status
```

## Architecture: Two-Tier Memory

The agent's memory is split into two tiers:

### Local Tier: Qdrant (Working Set)

**Purpose**: Fast, reliable, rehydratable local memory.

- **Storage**: Qdrant vector database (in-process or Docker)
- **Sync**: Two-way synchronization (push & pull)
- **Rehydration**: Complete enumeration on fresh instance (scroll API)
- **Latency**: <10ms local queries
- **Scope**: Single agent (per-tenant isolated)

**Use case**: Agent graph, conversation context, entity embeddings.

### Cloud Tier: Nexus (Durable RAG)

**Purpose**: Durable, shared, cloud-backed storage for training & RAG.

- **Storage**: LanceDB on the fleet's Nexus (:8122)
- **Sync**: Push-only (append, not enumerate)
- **Rehydration**: Degraded (semantic search, not full enumeration)
- **Latency**: 50-200ms (network-dependent)
- **Scope**: Multi-tenant (platform-wide via mesh)

**Use case**: Document repository, knowledge base, cross-agent RAG.

**Entitlement**: Requires `cloud_sync` or `grid_sync` SKU.

---

## Step 1: Install awdk

```bash
# Via pip (recommended)
pip install awdk

# Or build from source
git clone https://github.com/aitherium/awdk
cd awdk
pip install -e .
```

Verify:
```bash
adk --version
```

---

## Step 2: Create & Run a Local Agent

```bash
# Interactive setup (prompts for name, identity, etc.)
adk up

# Or non-interactive
adk up --identity my-agent --name my-node --yes

# Check status
adk status

# Inspect config
cat ~/.aither/config.yaml
```

The agent is now running on `localhost:8080` (or your configured port).

### What Happens

1. Agent process (Room + LLM runtime) starts as a supervised background service.
2. Autostart hook registered so it survives reboot.
3. Cloudflared quick-tunnel opened (optional, for remote access).
4. Config saved to `~/.aither/config.yaml`.

---

## Step 3: Provision Local Qdrant (Recommended)

If you want agent memory to survive restarts and cross-agent rehydration, run:

```bash
adk stack qdrant
```

### What Happens

1. Generates a random API key (32 bytes, URL-safe).
2. Stores the key in `~/.aither/config.yaml`.
3. Starts a Docker container `aitheros-workspace-qdrant` on port `6333`.
4. Sets env vars:
   - `AITHER_FLEET_QDRANT_URL=http://localhost:6333`
   - `AITHER_FLEET_QDRANT_API_KEY=<key>`

### Configuration

Edit `~/.aither/config.yaml` manually if needed:

```yaml
qdrant_url: http://localhost:6333
qdrant_api_key: <your-key>
```

Or set env vars directly:

```bash
export AITHER_FLEET_QDRANT_URL=http://localhost:6333
export AITHER_FLEET_QDRANT_API_KEY=<your-key>
```

### Verify

```bash
# Check container is running
docker ps | grep qdrant

# Test Qdrant is reachable
curl -s http://localhost:6333/health
```

---

## Step 4: Join the Mesh (Optional, for Cloud Sync)

To enable cloud integration and cross-node communication:

```bash
adk mesh join
```

### Prerequisites

- Network reachability to `conductor.aitherium.com:8193` (or your Conductor)
- Valid `AITHER_API_KEY` (from `adk login`)
- Optional: `AITHER_CA_BUNDLE` (internal TLS cert for in-fleet usage)

### What Happens

1. Generates a WireGuard keypair.
2. Posts to Conductor to request overlay IP.
3. Fetches AitherNet topology (server pubkey + endpoint).
4. Brings up `aithernet0` interface on `10.77.x.x/16` subnet.
5. Verifies handshake.
6. Stores config in `/etc/wireguard/aithernet0.conf` (Linux) or `%PROGRAMDATA%\AitherMesh` (Windows).

### Verify

```bash
adk mesh status
# Output: interface aithernet0, overlay IP, last handshake time
```

---

## Step 5: Enable Cloud Sync (Optional, Requires License)

Once the mesh is joined, the agent can push memory to the cloud Nexus for durable storage and shared RAG.

### Prerequisites

- Agent is on the mesh (Step 4)
- Tenant holds an active `cloud_sync` or `grid_sync` entitlement
- Nexus endpoint is reachable (via mesh or gateway)

### Configuration

Env vars are set automatically when the mesh joins. Verify with:

```bash
adk status
# Output: includes "Cloud Sync: enabled (Nexus)" if licensed
```

### How It Works

- **Push**: Agent's `graph_memory` auto-syncs new nodes to Nexus every 60s (configurable).
- **Pull**: On fresh instance, agent enumerates Nexus for prior context (degraded — semantic search only).
- **Failure**: If Nexus is unreachable, push fails silently; agent continues with local Qdrant.

---

## Memory Flow Examples

### Example 1: Standalone Agent (No Cloud)

```
┌─────────────────────┐
│   Agent Process     │
│  (Room + vLLM)      │
└──────────┬──────────┘
           │
           ├─► Local SQLite (graph_memory.db)
           │   - Source of truth
           │   - Survived to rehydration
           │
           └─► Qdrant (if enabled)
               - Working-set copy
               - Enumerable via scroll
               - Rehydrates on restart
```

**Use case**: Single-node sovereign deployment, air-gapped, no cloud.

### Example 2: Agent + Cloud Sync

```
┌─────────────────────┐
│   Agent Process     │
│  (Room + vLLM)      │
└──────────┬──────────┘
           │
           ├─► Local SQLite (graph_memory.db)
           │   - Source of truth
           │
           ├─► Qdrant (local working set)
           │   - Two-way sync
           │
           └─► Mesh ──► Conductor ──► Fleet Nexus
               (WireGuard tunnel)
               - Cloud-backed RAG
               - Platform-wide visibility
               - Durable backup
```

**Use case**: Hybrid agent with local responsiveness + cloud durability.

### Example 3: Multi-Agent Fleet

```
Agent A                Agent B
  │                      │
  ├─► Qdrant A           ├─► Qdrant B
  │   (working set)      │   (working set)
  │                      │
  └────────┬─────────────┘
           │
           ▼
        Mesh Tunnel
           │
           ▼
      Fleet Nexus
      (shared RAG)
```

**Use case**: Multi-node cluster where each agent maintains local memory but shares a common knowledge base.

---

## Troubleshooting

### Agent Fails to Start

```bash
# Check logs
adk logs

# Check if port is in use
netstat -tulpn | grep 8080

# Restart
adk down && adk up
```

### Qdrant Not Running

```bash
# Check Docker
docker ps | grep qdrant

# Start manually
docker start aitheros-workspace-qdrant

# Or provision again
adk stack qdrant
```

### Mesh Join Fails

```bash
# Verify conductor is reachable
curl -k https://conductor.aitherium.com:8193/health

# Check DNS resolution
nslookup conductor.aitherium.com

# Set insecure bootstrap (dev only!)
export AITHER_MESH_INSECURE_BOOTSTRAP=1
adk mesh join

# View detailed logs
AITHER_LOG_LEVEL=DEBUG adk mesh join
```

### Cloud Sync Not Working

```bash
# Verify you have the right entitlement
adk status | grep "cloud_sync"

# Check if Nexus is reachable
curl -s http://localhost:8122/health  # Local fleet
# OR
curl -s https://gateway.aitherium.com/v1/memory/health  # Cloud gateway

# Check graph_memory logs
# (AITHER_GRAPH_DEBUG was documented here and is read by nothing -- setting it
#  did exactly nothing. The real switch is the log level.)
AITHER_ADK_LOG_LEVEL=DEBUG adk status
```

---

## Configuration Reference

### Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `AITHER_FLEET_QDRANT_URL` | (unset) | Local Qdrant endpoint |
| `AITHER_FLEET_QDRANT_API_KEY` | (unset) | Qdrant API key |
| `AITHER_FLEET_MEMORY_URL` | `http://localhost:8122` | Nexus cloud backend |
| `AITHER_CLOUD_MODE` | (unset) | `cloud_first` or `cloud_only` for cloud-primary |
| `AITHER_FLEET_SYNC` | `auto` | `true`/`false` to enable/disable auto-sync |
| `AITHER_GRAPH_AUTOSYNC` | `true` | Auto-push new nodes to dataplane |
| `AITHER_CONDUCTOR_URL` | `https://aitheros-conductor:8193` | Mesh Conductor endpoint |
| `AITHER_MESH_PSK` | (unset) | Pre-shared key for Conductor auth |
| `AITHER_CA_BUNDLE` | (unset) | Path to internal CA cert (for in-fleet TLS) |
| `AITHER_MESH_INSECURE_BOOTSTRAP` | (unset) | `1` to allow insecure bootstrap (dev only) |

### Config File

Location: `~/.aither/config.yaml`

Example:
```yaml
api_key: sk-ant-...
qdrant_url: http://localhost:6333
qdrant_api_key: ...
cloud_mode: cloud_first
identity_url: https://api.aitherium.com
```

---

## Advanced: Tiering Tuning

### Prefer Local Qdrant (Fastest)

```bash
export AITHER_FLEET_QDRANT_URL=http://localhost:6333
export AITHER_FLEET_MEMORY_URL=http://localhost:8122
# graph_memory will use Qdrant (if set) over Nexus for enumeration
```

### Cloud-First (For Testing Sync)

```bash
export AITHER_CLOUD_MODE=cloud_first
export AITHER_FLEET_MEMORY_URL=https://gateway.aitherium.com/v1/memory
# Agent will prefer cloud Nexus; falls back to local
```

### Disable Auto-Sync (For Testing)

```bash
export AITHER_FLEET_SYNC=false
# Agent won't push to Qdrant/Nexus; only local SQLite
```

---

## Maintenance

### Backup Local Memory

```bash
# SQLite database
cp ~/.aither/graph/default.db ~/backups/

# Qdrant data
docker cp aitheros-workspace-qdrant:/qdrant/storage ~/backups/qdrant/
```

### Wipe & Reset

```bash
# Stop agent
adk down

# Remove local memory
rm -rf ~/.aither/graph/

# Remove Qdrant data
docker rm aitheros-workspace-qdrant

# Re-provision
adk up
adk stack qdrant
```

### Monitor Memory Usage

```bash
# Local SQLite
du -sh ~/.aither/graph/default.db

# Qdrant container
docker stats aitheros-workspace-qdrant

# Agent memory
ps aux | grep room | grep -v grep
```

---

## Security Notes

1. **API Keys**: Store `AITHER_FLEET_QDRANT_API_KEY` securely. Never commit to version control.
2. **TLS**: In-fleet usage (mesh joined) verifies the internal CA. Out-of-fleet requires valid public TLS.
3. **Secrets**: All agent secrets (API keys, tokens) live in AitherSecrets vault, not in code or env.
4. **Entitlements**: Cloud sync requires active `cloud_sync` or `grid_sync` license. Attempting sync without license returns 402 Payment Required.

---

## FAQ

**Q: Can I use Nexus without Qdrant?**

A: Yes, but with degradation. Nexus's `/search` endpoint is semantic-only, so rehydration on a fresh instance may miss nodes that don't match the query semantics. Qdrant's scroll API enumerates fully, making it authoritative for local tier.

**Q: Is local Qdrant required?**

A: No, it's optional. Agent works with just local SQLite + cloud Nexus, but you lose the fast working-set rehydration. Recommended for production.

**Q: Can I run multiple agents sharing one Qdrant?**

A: Yes. Qdrant isolates tenants by `tenant_id` in payloads. Each agent sees only its own nodes.

**Q: What if Qdrant goes down mid-operation?**

A: Agent continues with local SQLite. Sync is best-effort; write path never blocks on dataplane failure.

**Q: How often does auto-sync run?**

A: Every 60 seconds by default. Configurable via `GraphMemory(..., auto_sync=...)`.

---

## Next Steps

1. **Deploy**: Move this setup into a persistent VM/container for production.
2. **Monitor**: Integrate with observability (Prometheus, ELK, etc.).
3. **Scale**: Add more agents; they all share cloud Nexus RAG.
4. **Customize**: Tune SKU entitlements, memory decay, embedding models, etc.

For more, see:
- [AGENT_DEV_GUIDE.md](./AGENT_DEV_GUIDE.md) — agent development
- Mesh integration & billing/entitlement internals live in the AitherOS platform repo
  (private). Customers don't need them for self-hosting; if you believe you do, ask
  support@aitherium.com for the relevant excerpt.
