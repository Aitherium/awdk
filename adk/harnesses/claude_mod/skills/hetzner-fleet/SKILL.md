---
name: hetzner-fleet
description: >-
  Provision bare metal and VPS directly into your mesh from Hetzner Cloud. Hetzner Cloud is a European VPS provider with a flat, pleasant API: one bearer token, no IAM, no service accounts. This skill wires it in as a provisioning target so a single deploy call spins up a server, attaches your SSH key, and registers it with your fleet.
---

# hetzner-fleet — provision bare metal and VPS directly into your mesh from Hetzner Cloud

Hetzner Cloud is a European VPS provider with a flat, pleasant API: one bearer token, no
IAM, no service accounts. This skill wires it in as a provisioning target so a single
deploy call spins up a server, attaches your SSH key, and registers it with your fleet.

> **Live-measured against the Hetzner API v1 on 2026-07-27.** Every number below was read
> from the API, not from marketing pages. Two findings worth knowing before you start:
>
> 1. **A fresh project has no SSH key, and that blocks everything.** Creating a server with
>    no `ssh_keys` makes Hetzner **email you a root password** instead — a credential you
>    did not choose and that never reaches your automation. Upload a key *first*. Verified:
>    the bootstrap uploaded one key, re-running reported `Already existed: True` and
>    created no duplicate, and the API then reported exactly **1** key.
> 2. **Capacity is genuinely constrained right now** — Hetzner has a standing
>    "limited availability of cloud instances" advisory. Measured live availability by
>    datacenter: `nbg1-dc3` 12 server types, `hel1-dc2` 12, `ash-dc1` 10, `hil-dc1` 10,
>    `sin-dc1` 12 — and **`fsn1-dc14`: ZERO**. A provision that fails for no capacity is
>    not your bug. Retry in another location.
>
> Not verified here: nothing in this document claims a provisioning *duration*, because no
> server was created while writing it. If you see a wall-clock figure quoted for
> "time to running", it did not come from this skill.

## How it's wired

AitherComet (the mesh deployment service) routes cloud provisioning through a provider-
dispatch layer. Hetzner and DigitalOcean sit alongside the GPU-rental providers in that
same layer. Three moving parts are needed:

1. **SSH public key in Hetzner account** (prerequisite; you do this once)
2. **API token in vault** (prerequisite; platform does this)
3. **REST call to POST /deploy** (you do this to provision)

```
┌─────────────────────────────────────────┐
│  You: POST /deploy                      │
│  {                                      │
│    "deployment_id": "run-xyz",          │
│    "target": "cloud-gpu",               │  ← still called "cloud-gpu" even for VPS
│    "gpu_provider": "hetzner",           │  ← dispatch to Hetzner instead of Vast
│    "server_type": "cax11",              │  ← Hetzner's server type codes
│    "location": "fsn1-dc14",             │  ← datacenter location (or "nbg1", "ash")
│    "onstart": "#!/bin/bash\n...",       │  ← cloud-init script (optional)
│    "budget_usd": 50.00                  │  ← fail-closed cost guard
│  }                                      │
└─────────────────────────────────────────┘
              ↓ (AitherComet :8126, TLS)
         Cloud router
              ↓
     ┌───────┴────────┐
     ↓                ↓
 if gpu_provider     (other)
  == "hetzner"
     ↓
Provider dispatch
     ├─→ Vault: get_secret(HETZNER_API_KEY)
     ├─→ Hetzner API: list SSH keys
     ├─→ Hetzner API: create server
     ├─→ Poll for "running" status + SSH ready
     └─→ return {instance_id, ssh_host, ssh_port, gpu_model="N/A", price_per_hour}
```

The provider returns an `InstanceInfo` object that AitherComet uses to:
- Record the deployment state to `AitherOS/Library/Data/comet/deployments.json`
- Create tunnels or direct SSH access depending on your deployment strategy
- Attach the node to the mesh and register it with AitherDirectory

## Prerequisites: SSH key and vault token

### Step 1: Create or load an SSH keypair

If you don't have a keypair yet, generate one:

```bash
# Generate a 4096-bit RSA key (standard for cloud provisioning)
ssh-keygen -t rsa -b 4096 -f ~/.ssh/hetzner-provisioning -N ""

# Or: use an existing key
cat ~/.ssh/id_rsa.pub   # ← the PUBLIC key (begins with "ssh-rsa ...")
```

Save the public key somewhere you won't lose it — you'll paste it into Hetzner's dashboard
or use the API to upload it. **Do not commit the private key to git.** Store it in your
vault if you need it retrievable:

```python
# Store the private half in the vault. Note secret_type must be one of the
# service's known types -- api_key / token / password / certificate /
# private_key / connection_string / generic. Passing "ssh_key" is rejected,
# and the rejection surfaces only as a generic "failed to store".
from your_platform.secrets import SecretsClient   # your own vault client

await SecretsClient().store(
    "HETZNER_SSH_PRIVATE_KEY",
    private_key_pem,
    secret_type="private_key",
    access_level="internal",
)
```

### Step 2: Upload public key to Hetzner account

The key must exist in Hetzner Cloud account **before you provision a server**. Two paths:

**Via REST API (for automation):**

```bash
# Get your Hetzner API token (from https://console.hetzner.cloud/account/api-tokens)
export HETZNER_API_KEY="<your-token-here>"

# Upload the public key
curl -X POST https://api.hetzner.cloud/v1/ssh_keys \
  -H "Authorization: Bearer $HETZNER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "provisioning-key",
    "public_key": "ssh-rsa AAAA..."
  }'
# Response: {id: 12345, name: "provisioning-key", public_key: "ssh-rsa AAAA...", labels: {...}}
```

**Via dashboard** (one-time, manual):
1. Log in to [console.hetzner.cloud](https://console.hetzner.cloud)
2. Click **SSH Keys** (left sidebar)
3. Click **Add SSH Key**
4. Paste your public key and name it (e.g., "provisioning-key")

### Step 3: Verify API token is in vault

AitherComet reads `HETZNER_API_KEY` from the vault at provisioning time. If you're self-
hosting, you must store it first:

```python
from your_platform.secrets import SecretsClient   # your own vault client

await SecretsClient().store(
    "HETZNER_API_KEY", "<your-token-from-the-Hetzner-console>",
    secret_type="api_key", access_level="internal",
)
```

Then prove the round-trip before relying on it — a write that 401s and a secret that
simply is not set look identical at every later call site:

```python
from your_platform.secrets import get_secret   # your platform's read helper
assert get_secret("HETZNER_API_KEY"), "vault readback failed"
```

Verify it was stored:

```bash
curl -X GET http://127.0.0.1:8111/secrets/HETZNER_API_KEY \
  -H "X-API-Key: $YOUR_VAULT_API_KEY"
# Expected: 200 with secret object
```

## Provisioning a server

> **Note (measured live 2026-08-18):** AitherComet is published on host **8126**
> (container 8125). The port `:8150` this runbook used to name is the LLM router;
> every command here pointed at a service that could not answer it.
>
> **TRY BOTH SCHEMES — the scheme is NOT stable across restarts.** On its first boot
> this service logged `Uvicorn running on https://0.0.0.0:8125` and answered only
> `https://`; after a restart the same container answered only `http://`, and the
> HTTPS probes showed up in its own log as `Invalid HTTP request received`. Pinning
> either spelling is a latent outage: the wrong one returns `000`, which reads as
> "the service is down" while it is `Up (healthy)` and serving. Probe `http://` and
> `https://` (with `-k`) and take whichever answers.
>
> If `/deploy` does not respond at all, check whether the unit is **masked** before
> concluding the service is retired: a masked quadlet leaves no container, running or
> stopped, so `podman ps -a` shows nothing and it looks like it was never deployed.

Use AitherComet's `/deploy` endpoint:

```bash
curl -X POST http://127.0.0.1:8126/deploy \
  -H "Content-Type: application/json" \
  -d '{
    "deployment_id": "model-inference-run-abc123",
    "service_name": "inference-node",
    "target": "cloud-gpu",
    "gpu_provider": "hetzner",
    "server_type": "cax11",
    "location": "fsn1-dc14",
    "budget_usd": 50.00,
    "env_vars": {
      "NODE_NAME": "inference-1",
      "HF_TOKEN": "hf_...",
      "MODEL": "meta-llama/Llama-2-7b"
    },
    "onstart": "#!/bin/bash\necho ''Server starting''\n"
  }'
```

**Expected response (202 Accepted):**

```json
{
  "deployment_id": "model-inference-run-abc123",
  "status": "provisioning",
  "service_name": "inference-node",
  "started_at": "2026-07-27T14:22:45Z",
  "metrics": {
    "instance_id": "45621839",
    "ssh_host": "161.156.70.45",
    "ssh_port": 22,
    "exposed_ports": {},
    "gpu_model": "N/A",
    "price_per_hour": 0.0065
  },
  "target": "cloud-gpu",
  "message": "Provisioning instance on Hetzner..."
}
```

Poll the deployment status:

```bash
curl -X GET http://127.0.0.1:8126/deployments/model-inference-run-abc123
# Shows: status in [provisioning, starting, running, failed, destroyed]
```

### Server types — read from `GET /v1/server_types` on 2026-07-27

Gross EUR, cheapest location. **Verify against the API rather than trusting this table** —
prices change and this is a snapshot, which is exactly why the numbers below carry a date.

| Type | vCPU | RAM | Disk | Arch | €/mo | €/hr | Available in |
|---|---|---|---|---|---|---|---|
| `cx23`  | 2  | 4GB  | 40GB  | x86 | 6.49  | 0.0104 | fsn1, hel1, nbg1 |
| `cax11` | 2  | 4GB  | 40GB  | arm | 6.99  | 0.0112 | fsn1, hel1, nbg1 |
| `cx33`  | 4  | 8GB  | 80GB  | x86 | 8.99  | 0.0160 | fsn1, hel1, nbg1 |
| `cpx21` | 3  | 4GB  | 80GB  | x86 | 10.99 | 0.0176 | **all six** |
| `cax21` | 4  | 8GB  | 80GB  | arm | 12.49 | 0.0200 | fsn1, hel1, nbg1 |
| `cx43`  | 8  | 16GB | 160GB | x86 | 18.49 | 0.0296 | fsn1, hel1, nbg1 |
| `cpx31` | 4  | 8GB  | 160GB | x86 | 20.49 | 0.0328 | **all six** |
| `cax31` | 8  | 16GB | 160GB | arm | 24.99 | 0.0400 | fsn1, hel1, nbg1 |
| `cx53`  | 16 | 32GB | 320GB | x86 | 34.99 | 0.0561 | fsn1, hel1, nbg1 |
| `cpx41` | 8  | 16GB | 240GB | x86 | 37.99 | 0.0609 | **all six** |
| `ccx13` | 2  | 8GB  | 80GB  | x86 | 50.49 | 0.0809 | **all six** (dedicated vCPU) |

**The `cx`/`cax` line is cheaper but German-only.** If you need a US location, your options
start at `cpx21`/`cpx31` — that price gap is the cost of being close to your users, and it
is easy to miss because the cheap types simply do not appear in `ash`/`hil` at all.

`ccx*` are dedicated-vCPU (not shared) and cost roughly 2.5× the `cpx` equivalent; take
them only when you actually need predictable CPU. The most expensive type in the API is
`ccx63` at €1006.99/mo — there is no €4.90/hr "bare metal" tier in this API.

### Locations

Read from `GET /v1/datacenters` on 2026-07-27, with the count of server types each one
actually had available at that moment:

| Datacenter | Where | Types available |
|---|---|---|
| `nbg1-dc3` | Nuremberg, DE | 12 |
| `hel1-dc2` | Helsinki, FI | 12 |
| `sin-dc1`  | Singapore | 12 |
| `ash-dc1`  | Ashburn, VA, US | 10 |
| `hil-dc1`  | Hillsboro, OR, US | 10 |
| `fsn1-dc14` | Falkenstein, DE | **0** |

**Falkenstein was completely exhausted** — not "some types unavailable", zero. It is also
the location most tutorials default to, so the first thing many people try is the one that
cannot work. Hetzner's standing "limited availability of cloud instances" advisory is real.

Treat capacity as a **normal, expected failure**, not an error state: pick a fallback
location list up front and walk it. Query availability rather than guessing:

```
GET /v1/datacenters      -> .datacenters[].server_types.available  (ids)
GET /v1/server_types     -> map id -> name
```

Do **not** hammer the same location in a tight retry loop hoping capacity frees up. If a
datacenter reports zero available types, it will not change in the seconds you are willing
to wait — move to the next location instead.

## Cost guard: fail-closed by design

The `budget_usd` field enforces a hard cap on estimated spend. AitherComet computes
worst-case price from available offers and **blocks provisioning if it exceeds the cap**.
This is intentional.

```
┌─ Worst-case price calculation ────────────────┐
│ Best offer: €0.0400/hr for cax31              │
│ Estimated duration: 4 hours (default)         │ ← DEFAULT_ESTIMATED_HOURS
│ Price/hr in USD: €0.0400 * ~1.10 = ~$0.044    │ ← EUR->USD is approximate
│ Worst-case spend: $0.044 * 4 = ~$0.18         │
│                                               │
│ budget_usd: $50.00                            │
│ $0.20 < $50.00 → ALLOWED ✓                   │
└───────────────────────────────────────────────┘
```

If this guard fails, you get `BudgetBlockedError: GPU rental blocked by quota`. Causes:

1. **Quota exhausted:** you''ve already provisioned instances totaling your budget; destroy
   one or increase your cap in the request.
2. **Unpriced offer:** a server type has no price data — the gate refuses it outright
   ($0/hr is impossible and looks like data corruption).
3. **High price spike:** rare; Hetzner pricing shifts with supply. Increase `budget_usd`
   and retry.

This gate runs **before** instance creation and is the only thing that keeps a code bug
from spending real money on an infinite loop. Never remove it, never bypass it, never
stub it in tests.

## Management: starting, stopping, destroying

Once provisioned, you manage the instance via `/deployments/{id}` endpoints:

```bash
# Get current status
curl -X GET http://127.0.0.1:8126/deployments/model-inference-run-abc123

# Stop (suspend without destroying) — keeps the instance and its data
curl -X POST http://127.0.0.1:8126/deployments/model-inference-run-abc123/stop
# Hetzner respects the stopped state; you''re charged ~10% of running cost while stopped

# Start (resume from stopped state)
curl -X POST http://127.0.0.1:8126/deployments/model-inference-run-abc123/start

# Destroy (irreversible; data is gone)
curl -X POST http://127.0.0.1:8126/deployments/model-inference-run-abc123/teardown
# Hetzner immediately powers down and deallocates the instance.
# No backup, no recovery, no "are you sure?" — be sure before you send this.
```

⚠️ **Destroying a server is permanent.** You cannot retrieve its data after teardown.
If you need persistent state across deployments, use Object Storage (see below).

## SSH access and debugging

Once the server is running, you have direct SSH access:

```bash
ssh -i ~/.ssh/hetzner-provisioning root@161.156.70.45
# (or whatever hostname was returned in the deployment metrics)

# The node auto-registers with AitherDirectory and becomes discoverable:
curl -X GET http://127.0.0.1:8001/directory/nodes \
  | jq ''.[] | select(.hostname == "161.156.70.45")''
```

From inside the node, you can run workloads and they''ll have full mesh connectivity.

## Optional: cold-tier object storage on Hetzner S3

Hetzner Object Storage is S3-compatible and can be wired as a cold-tier backend for
artifact and data storage. This is optional — local storage works fine
for most deployments.

### Set up Hetzner Object Storage bucket

1. Log in to [console.hetzner.cloud](https://console.hetzner.cloud)
2. Click **Object Storage** (left sidebar)
3. Create a new bucket (e.g., "cold-tier")
4. Note the endpoint (e.g., `https://fsn1.your-project.storage.hetzner.cloud`)
5. Generate an API token: **Security** → **Object Storage API Tokens** → Create token
6. Store credentials in vault:

```bash
secret_store "S3_ENDPOINT" "https://fsn1.your-project.storage.hetzner.cloud" url
secret_store "S3_ACCESS_KEY" "<access-key>" api_key
secret_store "S3_SECRET_KEY" "<secret-key>" api_key
secret_store "S3_BUCKET" "cold-tier" config
```

### Enable S3 cold tier

Edit your deployment to enable S3 cold tier:

```yaml
# your compose file (e.g. docker-compose.yml) or your service env
services:
  storage:
    environment:
      COLD_S3_ENABLED: "1"
      S3_ENDPOINT: $''MINIO_ENDPOINT''
      S3_ACCESS_KEY: $''MINIO_ACCESS_KEY''
      S3_SECRET_KEY: $''MINIO_SECRET_KEY''
      S3_BUCKET: $''MINIO_BUCKET''
```

Then restart your storage service. Objects archived to S3 are encrypted at rest on Hetzner''s side.

⚠️ **Object Storage caveat:** Hetzner has a standing advisory (open since 2026-01-15):
"High traffic of Object Storage may lead to timeouts." If you''re backing large model
checkpoints or datasets, run a pre-flight test:

```bash
# Upload a large test object, read it back, check latency
dd if=/dev/zero bs=1M count=100 | \
  aws s3 cp - s3://cold-tier/test-100mb \
    --endpoint-url https://fsn1.your-project.storage.hetzner.cloud
```

If you see > 10s latencies, keep that data warm (local or mesh-replicated) instead.

## Troubleshooting provisioning failures

| Error | Cause | Fix |
|---|---|---|
| `BudgetBlockedError: GPU rental blocked` | Budget cap hit | Increase `budget_usd` in request |
| `Hetzner API error: capacity not available` | No servers free in chosen location | Retry with different `location` |
| `SSH connection refused` | Server is running but SSH not ready | Wait 30s and retry — cloud-init is still booting |
| `Key not found in account` | SSH public key not uploaded | Upload key to Hetzner console or API first |
| `PermissionError: denied by cost guard` | Budget policy on account | Increase budget_usd or check cost limits in configuration |
| `Hetzner API error: 401 Unauthorized` | API token missing or expired | Verify `HETZNER_API_KEY` in vault |

## Before claiming a Hetzner deployment is working

1. **SSH key uploaded:** verify in Hetzner console or with `curl` to `/ssh_keys` endpoint.
2. **API token stored:** confirm `HETZNER_API_KEY` is readable from vault (no timeout, no 401).
3. **Server reachable:** SSH in and run a command that proves the node is awake:
   ```bash
   ssh -i ~/.ssh/hetzner-provisioning root@<ip> "docker ps"
   # Output proves Docker is running
   ```
4. **Registered with fleet:** node appears in `GET /directory/nodes` and passes a full
   health check (`GET /directory/nodes/<id>/health`).
5. **Cost guard engaged:** provision with a deliberately low `budget_usd` (e.g., $0.01) and
   confirm it is refused with `BudgetBlockedError`. This proves the gate is not stubbed.
6. **Destroy works:** teardown returns 202, then polling shows status → destroyed. Re-provision
   with the same instance name and confirm you get a new IP (proof that the old one was
   actually destroyed).
7. **Capacity fallback works:** force a provision into a datacenter you have confirmed is
   exhausted (`server_types.available` empty) and check that your code moves to the next
   location rather than retrying the dead one. Do not assert that a short retry against the
   same location succeeds — that has not been demonstrated, and a datacenter reporting zero
   available types will not recover on a timescale worth blocking on.

## Part of one substrate

Hetzner provisioning slots into AitherComet's provider dispatch alongside the GPU-rental
providers — same `/deploy` endpoint, same state tracking, same cost guard. If you also run
self-hosted nodes or mesh peers, they register in the same AitherDirectory and show up
alongside cloud instances in `GET /targets`.

This is how a fleet that starts with local Raspberry Pi runners scales to bare metal:
the provisioning layer is pluggable, the mesh is uniform, and cost is metered and
gated the same way for all of it.

Hetzner Cloud terms: https://www.hetzner.cloud/legal/terms-and-conditions — check current
pricing and any signup credit there rather than trusting a figure quoted in a document.
