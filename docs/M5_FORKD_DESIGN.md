# M5 Tier-2 Fan-Out Adapter (Forkd)

**Status**: Design Specification. `adk/forkd_client.py` is IMPLEMENTED and its offline suite
passes (26 tests). `adk/forkd_executor.py` and `tests/test_forkd_live.py` are **design only —
they do not exist yet**; every reference to them below is a plan, not a module you can import.
Live e2e is owner-gated on a Linux/KVM box.  
**Last Updated**: 2026-10-27 (REST contract corrected against the daemon's router)  
**Author**: Claude Code (AitherOS M5 Architecture)

---

## Overview

The M5 Tier-2 fan-out adapter enables Claude-Code to fork ~100 warm sandbox children from a parent snapshot in ~100ms, each KVM-isolated and pinned to a chosen M1 Tier-1 account. The forkd daemon (Linux/KVM, :8889) is the substrate; the AitherOS side provides the client, account multiplexing, and fallback strategy.

**Constraints**:
- Forkd requires Linux >= 5.7 + KVM (LIVE runs on OptiPlex/DGX/WSL2, not Windows)
- Reuses M1 Tier-1 account layer (UsageMonitor, RunScope)
- Per-child scope is a CEILING on the parent's scope: a wider request is denied (no privilege escalation)
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
│  │  - Resolve the operator's warm parent tag                               ││
│  │  - Fork N children (parallel, limited concurrency)                      ││
│  │  - Exec the task as argv in each child                                  ││
│  │  - Collect results (never lose any child)                               ││
│  │  - Reclaim every sandbox it spawned                                     ││
│  │  - Degrade on forkd unavailable → sequential fallback                   ││
│  └─────────────────────────────────────────────────────────────────────────┘│
│  ⤓                                                                           │
│  HTTP (TLS, trust internal CA)                                              │
│  ⤓                                                                           │
│  Mesh overlay (100.64.x/24 tailnet)                                         │
│  ⤓                                                                           │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │  Forkd Daemon (Linux/KVM, :8889)                                        ││
│  │  - REST server (/healthz, /v1/snapshots, /v1/sandboxes …)               ││
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
| **ForkdClient** | `adk/forkd_client.py` | REST client, health check, snapshot resolve/spawn/exec/reclaim |
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

Source of truth: the daemon's own router, `crates/forkd-controller/src/http.rs`,
cross-read against the published `forkd` 0.5.3 Python SDK (`forkd/controller.py`).
**Corrected 2026-10-27**: an earlier revision of this document specified `/fork`,
`/exec/{child_id}`, `/children/{child_id}` and port 8760. The daemon has never served
any of them, so a client written from it could not have worked against a real daemon.

Base URL `$FORKD_URL` (default `http://127.0.0.1:8889`); bearer `$FORKD_TOKEN` sent
as `Authorization: Bearer` when set.

#### `GET /healthz`
Liveness; auth-bypassed. `200` = up — this is `ForkdClient.health_check()`.

#### `GET /v1/snapshots`
Every snapshot the daemon knows: `[{tag, dir, created_at_unix, status, bootable, …}]`.
`resolve_snapshot` binds this client's policy envelope to one of those tags and refuses
a tag it cannot see, or one listed `bootable: false` / `status: failed`.

**There is no REST call that turns a tool scope into a warm parent.** `POST
/v1/snapshots` exists, but it boots a VM from a `kernel` + `rootfs` path on the daemon
host and snapshots that; it accepts no `allowed_tools`. Warm parents come from that
call or the `forkd snapshot` CLI. The tool scope is OUR policy layer, carried in
`ForkdParentSnapshot.allowed_tools`, and `$FORKD_SNAPSHOT_TAG` (or `snapshot_tag=`)
names the warm parent to use.

#### `POST /v1/sandboxes`
Fork n children from a tag.
```json
{ "snapshot_tag": "warm", "n": 1, "per_child_netns": true }
```
Optional fields: `memory_limit_mib`, `prewarm`, `live_fork`, `hugepages`.

Response `201`: a LIST of SandboxInfo —
`{id, snapshot_tag, netns, guest_addr, created_at_unix, pid, memory_limit_mib}`.
`ForkdClient` forks one child per call (`n: 1`) and keeps `id` as the child's handle.

#### `POST /v1/sandboxes/{id}/exec`
```json
{ "args": ["sh", "-lc", "<task>"], "timeout_secs": 300 }
```
Response `200`: `{stdout, stderr, exit_code}`. A non-zero `exit_code` is a FAILED
child; it is not a transport error.

The exec surface takes **argv**, not a named tool set, so a child's scope cannot be
narrowed at exec time. The client therefore enforces the parent scope as a **ceiling**:
a child requesting a tool the warm parent's scope lacks is DENIED, never silently
narrowed — nothing downstream could enforce a narrowing. (The degrade path spawns a
real agent, so there the scope is still intersected.)

#### `DELETE /v1/sandboxes/{id}`
Terminate one sandbox. Called in a `finally` for every sandbox the client spawned;
an id that never spawned is never deleted.

Routes the daemon serves and this client does not use: `GET /v1/sandboxes`,
`GET /v1/sandboxes/{id}`, `POST /v1/sandboxes/{id}/{branch,eval,ping}`,
`POST /v1/snapshots`, `DELETE /v1/snapshots/{tag}`, `GET /version`, `GET /metrics`.

---

### Error Handling

- `400` malformed payload / unsafe tag, `404` unknown snapshot or sandbox,
  `500` daemon or KVM failure, `503` netns pool exhausted.
- `400/404` on a child operation → that child is FAILED, the fan-out continues.
- Snapshot missing, not bootable, or the list call failing → degrade (or FAIL when
  `degrade_on_error=False`); a fan-out never raises for a daemon-side failure.
- `5xx` on spawn/exec → FAILED child; its sandbox is still reclaimed.

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
available = await executor.forkd_client.health_check()  # GET /healthz
if not available and degrade_on_error:
    # Fall back to sequential spawns (see Degradation section)
    return await executor._degrade_to_sequential(children, snapshot)

# 2b. Resolve the warm parent the operator already created
#     (`forkd snapshot` CLI, or POST /v1/snapshots with kernel+rootfs).
snapshot = await executor.forkd_client.resolve_snapshot(
    os.environ["FORKD_SNAPSHOT_TAG"],   # e.g. "warm"
    allowed_tools=["python", "bash"],   # OUR policy envelope, not the daemon's
)  # GET /v1/snapshots (refuses a tag it cannot see or one not bootable)

# 3. Fork N children (parallel, semaphore-limited)
for each child in parallel (limit=10):
    # 3a. Select M1 account (or use explicit account_profile)
    account = UsageMonitor.select_account()  # Least-loaded PRIMARY

    # 3b. Scope CEILING (fail-closed: no escalation)
    outside = set(child.allowed_tools) - set(snapshot.allowed_tools)
    if outside:
        result.state = DENIED          # exec takes argv; nothing could narrow it
        continue

    # 3c. POST /v1/sandboxes  (one child -> n: 1)
    [sbx] = await client.spawn_sandboxes(snapshot.snapshot_id, n=1,
                                         per_child_netns=True)
    # Response: [ { id, snapshot_tag, netns, guest_addr, ... } ]

    # 3d. POST /v1/sandboxes/{id}/exec
    ex = await client.exec_command(sbx["id"], ["sh", "-lc", child.task],
                                   timeout_secs=child.timeout_sec)
    # Response: { stdout, stderr, exit_code }

    # 3e. Collect result (session_id is the daemon's sandbox id)
    result = ForkResult(
        child_id=child.child_id,
        state=COMPLETED if ex["exit_code"] == 0 else FAILED,
        account_profile=account,
        exit_code=ex["exit_code"],
        result_text=ex["stdout"],
        error=ex["stderr"],
        session_id=sbx["id"],
    )

# 4. Reclaim every sandbox that spawned (client does this in a finally)
for each spawned sandbox:
    # DELETE /v1/sandboxes/{id}
    await client.kill_sandbox(sbx_id)

# 5. Return results (all collected, no child lost)
return [ ForkResult(...), ... ]
```

### Scope Is a Ceiling on the Fork Path (Fail-Closed)

**Parent scope** (our policy envelope on the warm snapshot):
```
allowed_tools: ["bash", "python", "mcp-github"]
```

**Child request**:
```
allowed_tools: ["bash", "admin", "dangerous"]
```

**Outcome**: the child is `DENIED` — spawned nowhere, executed nowhere.

**Semantics**: No privilege escalation via fork. The daemon's exec surface takes argv,
not a named tool set, so `admin` cannot be stripped and cannot be enforced away at exec
time; granting the fork with the request intact would be escalation, and quietly
narrowing it would be a promise nothing downstream keeps. Deny is the only honest
answer. The degrade path spawns a real agent that DOES accept a tool list, so there the
scope is still intersected (`test_degrade_intersects_tools_no_escalation`).

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

If `degrade_on_error=False` and forkd is unavailable, the reverse of the usual rule
applies: every child comes back `state=FAILED, error="forkd daemon unavailable"` and the
caller decides what to do (gate on user permission, require forkd online). `fanout()`
never raises for a daemon-side failure — a raise mid-`gather` would lose the children
that already succeeded.

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
  "port": 8889,
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
│   └── test_forkd_offline.py     # Offline suite (mock forkd, 26 tests)
│       ├── test_only_published_routes()          # freezes the daemon's router
│       ├── test_defaults_match_the_daemon()      # :8889, $FORKD_TOKEN
│       ├── test_fanout_all_children_complete_and_pin_accounts()
│       ├── test_child_asking_outside_parent_scope_is_denied_not_narrowed()
│       ├── test_requested_throttled_account_denied()
│       ├── test_degrade_*()                       # daemon down, no tag, list failure
│       ├── test_partial_spawn_failure_isolated_and_no_reclaim()
│       ├── test_nonzero_exit_is_a_failure_with_stderr()
│       └── test_secret_*()                        # never on the wire
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
    def __init__(
        self,
        base_url: Optional[str] = None,     # $FORKD_URL, default http://127.0.0.1:8889
        *,
        http_fn=None,                       # injected transport (offline tests, internal CA)
        account_select=None,                # injected M1 selector
        degrade_spawn=None,                 # injected sequential fallback
        snapshot_tag: Optional[str] = None, # $FORKD_SNAPSHOT_TAG: the warm parent
        token: Optional[str] = None,        # $FORKD_TOKEN -> Authorization: Bearer
        timeout: float = 30.0,
        degrade_on_error: bool = True,
    )

    async def health_check() -> bool                       # GET /healthz
    async def list_snapshots() -> List[Dict]               # GET /v1/snapshots
    async def resolve_snapshot(tag, *, allowed_tools, …)   # tag must exist + be bootable
    async def spawn_sandboxes(tag, n=1, per_child_netns=True) -> List[Dict]
    async def exec_command(sandbox_id, args, timeout_secs=30) -> Dict
    async def kill_sandbox(sandbox_id) -> None

    async def fanout(
        children: List[ChildSpec],
        *,
        snapshot: Optional[ForkdParentSnapshot] = None,
        parallel_limit: int = 10,
    ) -> List[ForkResult]
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
- [ ] Forkd daemon starts and responds to /healthz
- [ ] the warm parent tag the client is given resolves (GET /v1/snapshots) and reports `bootable: true`
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

