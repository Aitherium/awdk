# M5 Tier-2 Fan-Out Adapter (Forkd)

**Status**: Design Specification (Offline Tests PASSING; Live e2e owner-gated on Linux/KVM)  
**Last Updated**: 2026-07-18  
**Author**: Claude Code (AitherOS M5 Architecture)

---

## Overview

The M5 Tier-2 fan-out adapter enables Claude-Code to fork ~100 warm sandbox children from a parent snapshot in ~100ms, each KVM-isolated and pinned to a chosen M1 Tier-1 account. The forkd daemon (Linux/KVM, :8760) is the substrate; the AitherOS side provides the client, account multiplexing, and fallback strategy.

**Constraints**:
- Forkd requires Linux >= 5.7 + KVM (LIVE runs on OptiPlex/DGX/WSL2, not Windows)
- Reuses M1 Tier-1 account layer (UsageMonitor, RunScope)
- Per-child scope is intersection of parent scope (no privilege escalation)
- Dead forkd daemon degrades gracefully to sequential spawns
- Secrets NEVER embedded in snapshot/child definitions
- No `verify=False` on any HTTP calls (trust internal CA)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Claude-Code CLI / Portal / Agent                                           │
│  ⤓                                                                           │
│  ClaudeRunner (M1 single-account mode) or                                   │
│  ForkdExecutor (M5 multi-account fan-out mode)                              │
│  ⤓                                                                           │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │  ForkdClient (adk/forkd_client.py)                                      ││
│  │  - Health check                                                         ││
│  │  - Create warm parent snapshot                                          ││
│  │  - Fork N children (parallel, limited concurrency)                      ││
│  │  - Exec scoped task per child                                           ││
│  │  - Collect results (never lose any child)                               ││
│  │  - Reclaim children                                                     ││
│  │  - Degrade on forkd unavailable → sequential fallback                   ││
│  └─────────────────────────────────────────────────────────────────────────┘│
│  ⤓                                                                           │
│  HTTP (TLS, trust internal CA)                                              │
│  ⤓                                                                           │
│  Mesh overlay (100.64.x/24 tailnet)                                         │
│  ⤓                                                                           │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │  Forkd Daemon (Linux/KVM, :8760)                                        ││
│  │  - REST server (health, snapshots, fork, exec, reclaim)                 ││
│  │  - Firecracker or equivalent (microVM manager)                          ││
│  │  - Per-child CLAUDE_CONFIG_DIR + account isolation                      ││
│  │  - KVM cgroup limits (CPU, memory per child)                            ││
│  │  - ~100ms fork-from-warm (parent snapshot reuse)                        ││
│  │  - Result collection (stdout, stderr, exit_code)                        ││
│  └─────────────────────────────────────────────────────────────────────────┘│
│  ⤓                                                                           │
│  Individual child VMs (100ms startup, N-way parallelism)                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Component Integration

| Component | File | Purpose |
|-----------|------|---------|
| **ForkdClient** | `adk/forkd_client.py` | REST client, health check, snapshot/fork/exec/reclaim |
| **ForkdExecutor** | `adk/forkd_executor.py` | Strategy pattern wrapper; integrates into ClaudeRunner |
| **ChildSpec** | `adk/forkd_client.py` | Per-child config (task, account, scope, timeout, metadata) |
| **ForkResult** | `adk/forkd_client.py` | Per-child outcome (exit_code, stdout/stderr, account, session_id) |
| **M1 Account Monitor** | `adk/claude_account_usage.py` | UsageMonitor.select_account() — least-loaded PRIMARY account |
| **RunScope** | `adk/claude_runner.py` | account_profile field pins child to account; intersected scope |
| **ClaudeRunner** | `adk/claude_runner.py` | Fallback sequential spawns if forkd unavailable |
| **Tests** | `tests/test_forkd_offline.py` | Offline suite (mock forkd, positive/fail-closed/degrade/secret) |

---

## Forkd REST Contract

### Endpoints

#### `GET /health`
**Purpose**: Liveness check.  
**Request**: None  
**Response**: 
```json
{
  "status": "ok"
}
```
**Status Code**: `200`

---

#### `POST /snapshots`
**Purpose**: Create or reuse a warm parent snapshot.  
**Request**:
```json
{
  "account_profile": "account-1",
  "allowed_tools": ["bash", "python"],
  "mcp_config": {
    "servers": {
      "github": {"type": "stdio", "command": "..."}
    }
  },
  "ttl_sec": 300
}
```

| Field | Type | Semantics |
|-------|------|-----------|
| `account_profile` | string | M1 account that warmed parent; '' = default |
| `allowed_tools` | list[str] | Tools available to children (non-empty, fail-closed) |
| `mcp_config` | dict | Live MCP server config (reused by all children from this snapshot) |
| `ttl_sec` | int | Snapshot time-to-live before auto-reclaim (default 300s) |

**Response**:
```json
{
  "snapshot_id": "snap-0001",
  "created_at": 1626000000,
  "status": "ready"
}
```

**Status Code**: `201` (created), `200` (reused existing)

**Semantics**: Forkd maintains a small pool of warm snapshots per account/tool-set. If a matching snapshot exists within TTL, returns it; otherwise creates a new one.

---

#### `POST /fork`
**Purpose**: Fork a child from a snapshot.  
**Request**:
```json
{
  "snapshot_id": "snap-0001",
  "child_id": "child-001",
  "account_profile": "account-1"
}
```

| Field | Type | Semantics |
|-------|------|-----------|
| `snapshot_id` | string | From `/snapshots` response |
| `child_id` | string | Unique per fan-out (UUIDv4 short) |
| `account_profile` | string | Account to pin this child to (M1 selection or explicit) |

**Response**:
```json
{
  "child_id": "child-001",
  "session_id": "sess-child-001",
  "snapshot_id": "snap-0001",
  "state": "forked"
}
```

**Status Code**: `201`

**Semantics**: Forkd creates a new KVM microVM from the parent snapshot. Child gets unique `session_id` for subsequent exec calls. Child inherits parent's MCP config but runs isolated in its own KVM namespace.

---

#### `POST /exec/{child_id}`
**Purpose**: Execute a task on a forked child.  
**Request**:
```json
{
  "task": "print('Hello from child')",
  "allowed_tools": ["python"],
  "mcp_config": {
    "servers": { ... }
  },
  "timeout_sec": 300,
  "metadata": {
    "goal_id": "goal-123",
    "user": "alice"
  }
}
```

| Field | Type | Semantics |
|-------|------|-----------|
| `task` | string | Task prompt/code to run (non-empty, fail-closed) |
| `allowed_tools` | list[str] | Intersected scope (subset of snapshot.allowed_tools) |
| `mcp_config` | dict | Live MCP servers (reused from snapshot or updated) |
| `timeout_sec` | int | Max task runtime (per-child); default 300s |
| `metadata` | dict | Traceability labels (goal_id, user, trace_id, etc.) |

**Response**:
```json
{
  "child_id": "child-001",
  "exit_code": 0,
  "stdout": "Hello from child\n",
  "stderr": "",
  "state": "completed"
}
```

**Status Code**: `200`

**Semantics**: Forkd injects task into child's stdin (or equivalent in-VM messaging), waits for completion, collects stdout/stderr/exit_code. No intermediate streaming (collect-on-completion model).

---

#### `DELETE /children/{child_id}`
**Purpose**: Reclaim a child (free VM, cgroup limits).  
**Request**: None  
**Response**:
```json
{
  "status": "reclaimed"
}
```

**Status Code**: `200`

**Semantics**: Forkd stops the child VM, releases resources (memory, CPU cgroup), removes entry from active pool. Safe to call idempotently (returns 200 even if child already reclaimed).

---

### Error Handling

All endpoints return:
- `400 Bad Request`: Malformed payload, missing required fields
- `404 Not Found`: Snapshot/child not found, invalid path
- `500 Internal Server Error`: Forkd daemon error, KVM failure, resource exhaustion

**Client Behavior**: 
- `400/404` on child operations → mark as FAILED in ForkResult, proceed to next child
- `500` on snapshot create → fail-closed if `degrade_on_error=False`, degrade if `True`
- `5xx` on fork/exec → try reclaim, mark FAILED, continue (never abort fan-out)

---

## M5 Flow: Fork-from-Warm Fan-Out

### Step-by-Step (Happy Path)

```python
# 1. Caller prepares children
children = [
    ChildSpec(
        child_id="child-001",
        account_profile="",  # Auto-select
        task="print('Processing batch 1')",
        allowed_tools=["python", "bash"],
        timeout_sec=60,
        metadata={"batch": "1", "goal_id": "goal-123"}
    ),
    # ... up to ~100 children ...
]

# 2. ForkdClient/ForkdExecutor.fanout()
executor = ForkdExecutor(account_monitor, claude_runner)

# 2a. Health check forkd daemon
available = await executor.forkd_client.health_check()  # GET /health
if not available and degrade_on_error:
    # Fall back to sequential spawns (see Degradation section)
    return await executor._degrade_to_sequential(children, snapshot)

# 2b. Create/reuse warm parent snapshot
snapshot = await executor.forkd_client.create_snapshot(
    account_profile="account-default",
    allowed_tools=["python", "bash"],
    mcp_config=mcp_config,
    ttl_sec=300
)  # POST /snapshots

# 3. Fork N children (parallel, semaphore-limited)
for each child in parallel (limit=10):
    # 3a. Select M1 account (or use explicit account_profile)
    account = UsageMonitor.select_account()  # Least-loaded PRIMARY
    
    # 3b. POST /fork
    resp = await client.post(f"/fork", json={
        "snapshot_id": snapshot.snapshot_id,
        "child_id": child.child_id,
        "account_profile": account,
    })
    # Response: { session_id, ... }
    
    # 3c. Intersect scope (fail-closed: no escalation)
    intersected_tools = [
        t for t in child.allowed_tools
        if t in snapshot.allowed_tools
    ]
    if not intersected_tools:
        result.error = "No tools in intersection"
        result.state = FAILED
        continue
    
    # 3d. POST /exec/{child_id}
    resp = await client.post(f"/exec/{child.child_id}", json={
        "task": child.task,
        "allowed_tools": intersected_tools,
        "mcp_config": snapshot.mcp_config,
        "timeout_sec": child.timeout_sec,
        "metadata": child.metadata,
    })
    # Response: { exit_code, stdout, stderr, ... }
    
    # 3e. Collect result
    result = ForkResult(
        child_id=child.child_id,
        state=COMPLETED if exit_code == 0 else FAILED,
        account_profile=account,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        session_id=session_id,
    )

# 4. Reclaim children (after collecting results)
for each child:
    # DELETE /children/{child_id}
    await client.delete(f"/children/{child.child_id}")

# 5. Return results (all collected, no child lost)
return [ ForkResult(...), ... ]
```

### Scope Intersection (Fail-Closed)

**Parent scope** (from snapshot):
```
allowed_tools: ["bash", "python", "mcp-github"]
```

**Child request**:
```
allowed_tools: ["bash", "admin", "dangerous"]
```

**Intersected scope** (what child ACTUALLY gets):
```
allowed_tools: ["bash"]  # Only bash (others not in parent)
```

**Semantics**: No privilege escalation via fork. Child cannot request tools parent doesn't allow.

---

## M1 Account Integration

### Account Selection (UsageMonitor.select_account)

**Location**: `adk/claude_account_usage.py:305-362`

```python
def select_account(self, available_profiles: list[str]) -> str:
    """
    Select least-loaded PRIMARY account; round-robin on TIE.
    
    Args:
        available_profiles: List of account profiles (e.g., ["account-1", "account-2", ...])
    
    Returns:
        Selected profile name (e.g., "account-1")
    
    Raises:
        ValueError if available_profiles empty or no profiles with rolling_total_cost_usd < throttle_threshold
    """
```

**Algorithm**:
1. Filter profiles to those NOT throttled (rolling_total_cost_usd < THROTTLE_THRESHOLD)
2. Find PRIMARY account (usage type == "primary") with minimum rolling_total_cost_usd
3. If multiple accounts tie, round-robin among them
4. Fail-closed: if no available account, raise ValueError

**Per-Child Usage Tracking**:
- Each child pins to selected account via `RunScope.account_profile`
- ClaudeRunner isolates CLAUDE_CONFIG_DIR per account (line 594-600, claude_runner.py)
- UsageMonitor updates rolling cost AFTER task completion

**Fail-Closed Scenario**:
```
All accounts throttled → select_account() raises ValueError
→ ForkResult.error = "Account selection failed; denying fork"
→ Child marked FAILED
→ Fan-out continues (never loses other children)
```

---

## Degradation Path (Forkd Unavailable)

### When Degradation Triggers

Degradation activates when:
1. Forkd health check times out or returns non-200
2. `ForkdClient.degrade_on_error=True` (default)

### Degradation Behavior

Instead of forking, ForkdClient calls `ClaudeRunner.submit()` sequentially for each child:

```python
async def _degrade_to_sequential(
    self,
    children: List[ChildSpec],
    snapshot: ForkdParentSnapshot,
) -> List[ForkResult]:
    """
    Fallback when forkd unavailable: run children sequentially via ClaudeRunner.
    """
    results = []
    
    for child in children:
        # 1. Select account (same as fork path)
        account = UsageMonitor.select_account(...)
        
        # 2. Intersect scope (same fail-closed intersection)
        intersected_tools = [t for t in child.allowed_tools if t in snapshot.allowed_tools]
        
        # 3. Build RunScope
        scope = RunScope(
            allowed_tools=intersected_tools,
            account_profile=account,
            mcp_config=snapshot.mcp_config,
        )
        
        # 4. Submit to runner (sequential, NOT forked)
        rec = self.claude_runner.submit(task=child.task, scope=scope, goal_id=child.goal_id)
        
        # 5. Collect result
        result = ForkResult(
            child_id=child.child_id,
            account_profile=account,
            exit_code=0 if rec.status == "success" else 1,
            stdout=rec.result,
            stderr=rec.error_msg,
            state=COMPLETED if rec.status == "success" else FAILED,
        )
        results.append(result)
    
    return results
```

### Guarantees

- **No fan-out time guarantee** (sequential ≠ ~100ms), but results ARE collected
- **Account selection same** (same M1 layer, same scoring)
- **Scope intersection same** (fail-closed, no escalation)
- **Never loses children** (all are attempted, all results returned)
- **User warned** (`logger.warning()` logs degradation)

### Fallback-to-Fallback (degrade_on_error=False)

If `degrade_on_error=False` and forkd unavailable:
```python
raise RuntimeError("forkd create_snapshot failed: ...")
```
Caller must handle this (e.g., gate on user permission, require forkd online).

---

## Per-Child Account Isolation

### CLAUDE_CONFIG_DIR Isolation

Each child (forked or sequential) gets isolated credentials home:

**Forkd path** (KVM):
```
Child VM mounts CLAUDE_CONFIG_DIR from host:
  /home/aither/runs/{run_id}/claude-home/{account_profile}/
  └── credentials.json
  └── config.json
```
Host creates per-child home before fork, child sees account-specific credentials.

**Sequential fallback path** (no fork):
```
ClaudeRunner._build_workspace() creates:
  /home/aither/runs/{run_id}/claude-home/
  └── credentials.json (for selected account)
```
RunScope.account_profile triggers isolated home lookup (claude_runner.py:594-600).

**Result**: No cross-account credential bleed; each child uses ONLY its pinned account's API key.

---

## Secrets Handling (Never Embedded)

### ChildSpec Guarantees

```python
@dataclass
class ChildSpec:
    child_id: str              # UUID short
    account_profile: str       # Account NAME (not key), resolved at runtime
    task: str                  # Prompt/code; may MENTION secrets (e.g., "use get_secret()") but never CONTAINS them
    allowed_tools: List[str]   # Tool names (no API keys)
    mcp_config: Dict[str, Any] # MCP config; never includes actual credentials
    metadata: Dict[str, Any]   # Labels; never includes secrets
```

### Secret-Safety Checks

1. **Task**:  ✓ Safe to log (may mention "get_secret('key')" but never contains `sk-ant-...`)
2. **Metadata**: ✓ Safe (only labels, no credentials)
3. **account_profile**: ✓ Safe (name, not key; key resolved from CLAUDE_CONFIG_DIR at runtime)
4. **mcp_config**: ✓ Safe (tools config, not auth tokens; tokens loaded from vault at runtime)

### Vault Integration

Credentials never flow through forkd payloads:
```python
# WRONG (NEVER DO THIS):
ChildSpec(task="...", metadata={"api_key": "sk-ant-..."})

# CORRECT:
ChildSpec(
    task="from adk.secrets import get_secret; key = get_secret('my-key')",
    metadata={"key_name": "my-key"}  # Reference, not value
)
```

Child VM loads actual key from vault (:8111) at runtime, not from payload.

---

## Integration Points

### 1. ClaudeRunner Integration (Fallback)

**File**: `adk/claude_runner.py`  
**Seam**: ClaudeRunner.submit() for sequential degradation

```python
# In ForkdClient._degrade_to_sequential():
rec = self.claude_runner.submit(
    task=child.task,
    scope=scope,  # RunScope with account_profile
    goal_id=child.goal_id,
)
```

**Guarantee**: Existing ClaudeRunner handles scope isolation; ForkdClient just routes through it.

---

### 2. UsageMonitor Integration (Account Selection)

**File**: `adk/claude_account_usage.py`  
**Seam**: UsageMonitor.select_account()

```python
# In ForkdClient._fork_one():
account = self.account_monitor.select_account(
    available_profiles=self.account_monitor.list_profiles()
)
```

**Guarantee**: select_account() is fail-closed (raises ValueError on throttled/missing), never returns invalid account.

---

### 3. Mesh Onboarding (Forkd Node Registration)

**File**: the platform's mesh registration module (AitherMesh)  
**Endpoint**: `POST /nodes/register`

See section "[Forkd Node Registration](#forkd-node-registration)" below.

---

### 4. Executor Strategy Integration (Optional)

**File**: `adk/forkd_executor.py`  
**Public API**: `ForkdExecutor.fanout(children, parent_snapshot)`

Allows ClaudeRunner to optionally delegate to forkd executor:

```python
# In ClaudeRunner.submit() or a new multi_submit():
if has_forkd_executor and fan_out_requested:
    results = await executor.fanout(children, ...)
else:
    results = [self.submit(task) for task in tasks]  # Sequential
```

**Note**: This is optional; basic ForkdClient usage doesn't require modifying ClaudeRunner internals.

---

## Forkd Node Registration (Mesh Onboarding)

### Playbook-Driven Registration (Multi-OS)

**File**: the mesh-agent deployment playbook  
**Usage**: `Invoke-AitherPlaybook deploy-mesh-agent -ComputerName forkd-node-01 -ControlPlaneUrl http://headscale:8443`

**Phases**:
1. **Ensure Headscale** (control-plane) is LAN-accessible (:8443)
2. **Join tailnet** (Linux/WSL2): register node at headscale, assign overlay IP (100.64.x.x/24)
3. **Deploy adk agent** with mesh reach (`adk up --reach mesh`)
4. **Verify** agents appear in mesh registry

After playbook, forkd node has:
- Overlay IP (e.g., 100.64.0.50)
- Mesh connectivity (can reach all nodes on 100.64/10)
- SSH accessible from admin host

---

### Direct Node Registration (AitherMesh.py)

**Endpoint**: `POST https://mesh-core:8125/nodes/register`  
**Authentication**: X-Aither-PSK or X-Internal-Key

**Payload**:
```json
{
  "name": "forkd-tier2-01",
  "host": "100.64.0.50",
  "port": 8760,
  "capabilities": {
    "cpu_cores": 16,
    "memory_gb": 64.0,
    "gpus": [],
    "gpu_count": 0,
    "total_vram_mb": 0,
    "services": ["forkd", "vllm", "ollama"]
  },
  "labels": {
    "role": "tier-2-compute",
    "provider": "kvm",
    "tier": "tier-2",
    "aither_owner": "<tenant-id>"
  },
  "hardware": { ... },
  "metadata": {
    "vllm_url": "http://100.64.0.50:8124/v1",
    "resident_models": ["qwen-3.6-b", "gemma-2b"],
    "tags": ["compute", "inference"]
  },
  "challenge": "<HMAC-SHA256(node_id:timestamp:hostname)>",
  "signature": "<signed with PSK>"
}
```

**Result**: Node appears in AitherMeshBalancer registry (fleet dashboard, model router, Strata peer discovery).

---

### Endpoint Registry Binding (Portal Reach)

**File**: the platform's `ManagedAgentEndpointStore` module, `register()` binding  
**Function**: `ManagedAgentEndpointStore.register()`

After forkd node joins mesh, register its endpoint for portal discovery:

```python
await ManagedAgentEndpointStore.for_tenant(tenant_id).register(
    tenant=tenant_id,
    name="forkd-tier2-01",
    invoke_url="http://100.64.0.50:8080",  # Or HTTPS via tunnel
    reach="mesh",                           # Reaches via tailnet overlay
    ssh_host="100.64.0.50",
    ssh_user="aither",
    ssh_port=22,
    node_os="linux",
    shell="bash",
    capabilities="vllm,forkd,compute",
    provider_hint="kvm"
)
```

**Result**: Portal can invoke forkd endpoint, can SSH to node for debugging.

---

## File Structure

```
awdk/
├── adk/
│   ├── forkd_client.py          # ForkdClient, ChildSpec, ForkResult, ChildState
│   ├── forkd_executor.py        # ForkdExecutor (strategy pattern)
│   ├── claude_runner.py          # Existing; RunScope integration
│   ├── claude_account_usage.py   # Existing; UsageMonitor integration
│   └── claude_accounts.py        # Existing; account storage
│
├── tests/
│   └── test_forkd_offline.py     # Offline suite (mock forkd)
│       ├── test_forkd_positive_basic_fanout()
│       ├── test_forkd_positive_account_selection()
│       ├── test_forkd_failclosed_empty_tools_rejected()
│       ├── test_forkd_failclosed_scope_intersection()
│       ├── test_forkd_failclosed_empty_task_rejected()
│       ├── test_forkd_degrade_daemon_unavailable()
│       ├── test_forkd_degrade_partial_failure()
│       ├── test_forkd_secret_safety_no_embedding()
│       └── test_forkd_secret_safety_claude_config_isolation()
│
└── docs/
    └── M5_FORKD_DESIGN.md        # This file
```

---

## Public API

### ForkdClient

**Module**: `adk.forkd_client`

```python
class ForkdClient:
    async def __init__(
        self,
        forkd_base_url: str,              # "http://100.64.0.50:8760"
        account_monitor,                   # UsageMonitor
        claude_runner,                     # ClaudeRunner
        timeout: float = 30.0,
        degrade_on_error: bool = True,
    )
    
    async def health_check() -> bool
    async def create_snapshot(
        account_profile: str = "",
        allowed_tools: Optional[List[str]] = None,
        mcp_config: Optional[Dict[str, Any]] = None,
        ttl_sec: int = 300,
    ) -> ForkdParentSnapshot
    
    async def fanout(
        children: List[ChildSpec],
        snapshot: Optional[ForkdParentSnapshot] = None,
        parallel_limit: int = 10,
    ) -> List[ForkResult]
    
    async def close() -> None
```

### ForkdExecutor

**Module**: `adk.forkd_executor`

```python
class ForkdExecutor:
    def __init__(
        self,
        account_monitor,
        claude_runner,
        forkd_url: Optional[str] = None,
        degradation_warning_threshold: int = 50,
    )
    
    async def fanout(
        self,
        children: List[ChildSpec],
        parent_snapshot: Optional[ForkdParentSnapshot] = None,
        parallel_limit: int = 10,
    ) -> List[ForkResult]
    
    async def close() -> None

def get_forkd_executor(
    account_monitor,
    claude_runner,
    forkd_url: Optional[str] = None,
) -> ForkdExecutor
```

### ChildSpec / ForkResult

**Module**: `adk.forkd_client`

```python
@dataclass
class ChildSpec:
    child_id: str = field(default_factory=...)
    account_profile: str = ""              # Account selector ('' = auto)
    task: str = ""                         # Task prompt/code (non-empty, fail-closed)
    allowed_tools: List[str] = field(...)  # Requested tools (intersected with parent)
    mcp_config: Dict[str, Any] = field(...)
    timeout_sec: int = 300
    metadata: Dict[str, Any] = field(...)  # Labels (no secrets)
    goal_id: str = ""
    
    def validate(self) -> None
        # Raises ValueError if allowed_tools empty or task empty/whitespace

@dataclass
class ForkResult:
    child_id: str
    state: ChildState                      # FORKED, EXECUTING, COMPLETED, FAILED, RECLAIMED
    account_profile: str                   # Which account this child used
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    metadata: Dict[str, Any] = field(...)
    error: Optional[str] = None            # Forkd communication or execution error
    elapsed_sec: float = 0.0
    session_id: Optional[str] = None       # Unique per child (not parent's)
```

---

## Testing

### Offline Test Suite

**File**: `tests/test_forkd_offline.py`  
**Status**: ✓ Passing (no KVM/Linux deps)

Run:
```bash
cd awdk
python -m pytest tests/test_forkd_offline.py -v
```

**Coverage**:
- ✓ Positive: N children forked, scoped, results collected
- ✓ Account selection: auto-select and explicit account_profile
- ✓ Fail-closed: empty tools/task rejected; scope intersection enforced
- ✓ Degradation: daemon unavailable → sequential fallback, results still collected
- ✓ Partial failure: one child fork fails, others proceed
- ✓ Secret safety: no sk-ant-/xoxb-/ghp_ in payloads; CLAUDE_CONFIG_DIR isolation

### Live E2E (Owner-Gated)

**Prerequisite**: Linux/KVM node with forkd daemon running  
**Gate**: Requires `--live-e2e` flag + Linux platform check

```bash
# Windows/WSL2: offline only
pytest tests/test_forkd_offline.py -v

# OptiPlex/DGX/WSL2 (Linux): include live tests
pytest tests/test_forkd_live.py -v --live-e2e
```

**Live test checklist**:
- [ ] Forkd daemon starts and responds to /health
- [ ] create_snapshot() creates warm parent (measures startup latency)
- [ ] fanout(N=100) completes in <100ms (per spec)
- [ ] Each child has isolated CLAUDE_CONFIG_DIR
- [ ] Results collected; no child lost
- [ ] Reclaim frees resources (cgroup limits reset)
- [ ] Account selection varies per fan-out (load balancing verified)

---

## Known Limitations & Debt

| ID | Issue | Impact | Workaround |
|----|----|--------|-----------|
| D-??? | Forkd daemon lifecycle not managed by compose | Requires manual SSH to start forkd | Run forkd via playbook; heartbeat monitoring |
| D-??? | MCP config reuse across children may stale | MCP server state mutation | Re-negotiate MCP for each child (slower) |
| D-??? | No per-child MCP server (all children share parent's servers) | Concurrent MCP requests may contend | MCP servers are stateless; contention acceptable for <100 concurrent |
| D-??? | Forkd snapshot TTL not configurable per child | Snapshot reclaimed while child running | Increase default TTL or extend on exec completion |

---

## Future Work

1. **MCP server pooling**: Forkd spawns dedicated MCP servers per child (instead of reusing parent's)
2. **Streaming results**: Collect child output as it arrives (not block on completion)
3. **Persistence**: Store fan-out results to KV for audit/replay
4. **Cost tracking**: Per-child cost attribution to account (integrated with UsageMonitor)
5. **Elastic scale-down**: Forkd auto-reclaims idle children under memory pressure
6. **Live dashboard**: Portal widget showing active children, throughput, failures
7. **Circuit breaker**: Automatic degrade-to-sequential if forkd error rate > threshold

---

## References

- **Forkd Daemon**: (wizzense/forkd, Firecracker fork-from-warm orchestrator)
- **M1 Tier-1 Account Layer**: `adk/claude_account_usage.py`, `adk/claude_accounts.py`
- **RunScope Isolation**: `adk/claude_runner.py:172-277`, `adk/claude_runner.py:594-600`
- **Mesh Onboarding**: the mesh-agent deployment playbook
- **Node Registration**: the platform's mesh registration module (AitherMesh)

