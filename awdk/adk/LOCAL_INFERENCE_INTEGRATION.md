# Local Inference Endpoint Discovery & Registration

## Overview

The `local_inference.py` module enables agents to automatically discover and register local inference endpoints (llama.cpp, vLLM, Ollama, etc.) with MicroScheduler without requiring manual configuration.

**Key concept:** An agent runs once with `auto_discover_and_register()`, discovers any local OpenAI-compatible endpoint, registers it with MicroScheduler, emits an event to the room, and subsequent LLM calls route through the discovered endpoint.

## Discovery Priority

Discovery searches endpoints in order and returns immediately on first success:

1. **Explicit env var**: `AITHER_LOCAL_LLM_URL` (if set)
2. **MicroScheduler**: `http://127.0.0.1:8150` (if reachable and has healthy backends)
3. **Common ports**: `http://127.0.0.1:8080|8081|8000` (llama-server defaults)
4. **None found**: Returns explicit "not found" result

Each endpoint is verified with a real `/v1/models` or `/health` probe, not just a TCP port check. A port that accepts connections but serves nothing is correctly rejected.

## Module API

### `LocalInferenceDiscovery` (dataclass)

Result of a discovery attempt:

```python
@dataclass
class LocalInferenceDiscovery:
    found: bool                           # Whether an endpoint was discovered
    endpoint_url: Optional[str] = None    # e.g., http://127.0.0.1:8080
    model: Optional[str] = None           # First model from /v1/models response
    source: Optional[str] = None          # env | microscheduler | port | none
    details: Optional[str] = None         # Human-readable explanation
```

### Core Functions

#### `async discover_local_endpoint()` → `LocalInferenceDiscovery`

Discover a local OpenAI-compatible endpoint.

**Returns immediately on first success; probes with 2s timeout each.**

```python
result = await discover_local_endpoint()
if result.found:
    print(f"Found at {result.endpoint_url}, model={result.model}")
else:
    print(f"Not found: {result.details}")
```

#### `async register_with_microscheduler(endpoint_url, backend_name=None, model=None)` → `(bool, Optional[str])`

Register a discovered endpoint with MicroScheduler so LLM calls route through it.

- **Requires**: `AITHER_INTERNAL_SECRET` env var (for authentication with MicroScheduler)
- **Returns**: `(success, error_message)`

```python
success, error = await register_with_microscheduler(
    endpoint_url="http://127.0.0.1:8080",
    model="bonsai-27b",
)
if success:
    print("Registered with MicroScheduler")
else:
    print(f"Registration failed: {error}")
```

#### `emit_discovery_event(discovery, room="main", actor_id=None)` → `dict`

Create an AitherEvent for local endpoint discovery.

**Does NOT post the event; returns dict for the caller to POST.**

```python
event = emit_discovery_event(discovery)
# Then POST to http://127.0.0.1:8362/events with bearer token
```

#### `async post_discovery_event(event, event_daemon_url="http://127.0.0.1:8362", bearer_token=None)` → `(bool, Optional[str])`

POST an event to the event spine daemon.

- **Reads bearer token from**: `AITHER_HARNESS_TOKEN` env var if not provided
- **Returns**: `(success, error_message)`

```python
success, error = await post_discovery_event(event)
```

#### `async auto_discover_and_register(enable_event_emit=True, room="main", actor_id=None)` → `(LocalInferenceDiscovery, Optional[str])`

Complete pipeline: discover, register, and emit event in one call.

```python
discovery, error = await auto_discover_and_register()
if discovery.found:
    print(f"Discovered and registered: {discovery.endpoint_url}")
else:
    print("No local endpoint found")
```

## Integration with adk Agent

### Option 1: Agent Startup (Recommended)

Add to the agent initialization to automatically discover and use local inference:

**File**: `aither-adk/adk/agent.py` (or wherever the agent initializes)

```python
# At agent startup, before creating the LLMRouter
from adk.local_inference import auto_discover_and_register

async def _init_llm():
    # Try to discover and register local endpoint
    discovery, _ = await auto_discover_and_register(
        enable_event_emit=True,
        room="main",
        actor_id=self.agent_id
    )
    
    if discovery.found:
        # Use the discovered endpoint for LLM calls
        logger.info(f"Using local endpoint: {discovery.endpoint_url}")
        return discovery.endpoint_url
    else:
        logger.info("No local endpoint found, using default")
        return None

local_endpoint = await _init_llm()
self.llm_router = LLMRouter(
    base_url=local_endpoint or os.environ.get("AITHER_LLM_URL"),
)
```

### Option 2: On-Demand Discovery

Only discover when explicitly requested (e.g., via CLI flag or user command):

```python
# In response to `adk agent discover-local`
from adk.local_inference import discover_local_endpoint, register_with_microscheduler

discovery = await discover_local_endpoint()
if discovery.found:
    print(f"Found: {discovery.endpoint_url}")
    success, error = await register_with_microscheduler(discovery.endpoint_url)
    if success:
        print("Registered with MicroScheduler")
else:
    print(f"Not found: {discovery.details}")
```

## Environment Variables

### Discovery

- **`AITHER_LOCAL_LLM_URL`** (optional): Explicit override. If set, probes this URL first and returns immediately if healthy.
  ```bash
  export AITHER_LOCAL_LLM_URL="http://192.168.1.100:8080"
  adk agent start
  ```

### Registration

- **`AITHER_INTERNAL_SECRET`** (required for registration): Authentication credential for MicroScheduler registration.
  ```bash
  export AITHER_INTERNAL_SECRET=<your-internal-key>
  ```

### Event Emission

- **`AITHER_HARNESS_TOKEN`** (optional): Bearer token for posting events to the spine daemon. If not set, events POST without auth.
  ```bash
  export AITHER_HARNESS_TOKEN="your-harness-token"
  ```

## Event Type

Discovery emits an `local_inference_discovered` event:

```json
{
  "type": "local_inference_discovered",
  "actor": {
    "kind": "adk_agent",
    "id": "agent-id",
    "name": "agent-name"
  },
  "pillar": "orchestration",
  "tier": "host",
  "room": "main",
  "payload": {
    "found": true,
    "endpoint_url": "http://127.0.0.1:8080",
    "model": "bonsai-27b",
    "source": "port",
    "details": "Discovered on port 8080"
  }
}
```

- **Type**: `local_inference_discovered`
- **Pillar**: `orchestration` (selecting which inference backend is orchestration)
- **Tier**: `host` (discovery runs on the local machine, not the fleet)
- **Payload fields**:
  - `found`: bool — whether an endpoint was discovered
  - `endpoint_url`: str | null — OpenAI-compatible base URL
  - `model`: str | null — first model served by the endpoint
  - `source`: str — where it was found (env | microscheduler | port | none)
  - `details`: str — human-readable explanation

## Error Handling

All functions fail **soft, never silent**:

- **Discovery**: Returns `found=False` with detailed explanation in `details` field. Never raises.
- **Registration**: Returns `(False, error_message)` instead of raising. Check return value.
- **Event emission**: Returns `(False, error_message)`. Log the error rather than crashing.

**Example**:

```python
discovery, _ = await auto_discover_and_register()
if discovery.found:
    # Endpoint is registered and ready
    print(f"Using: {discovery.endpoint_url}")
elif discovery.source == "none":
    # No local endpoint found
    print("No local LLM, falling back to cloud")
else:
    # Endpoint was found but registration failed
    print(f"Found but failed to register: {discovery.details}")
```

## Probing Behavior

The module uses **real HTTP calls**, not TCP port checks:

1. **GET `/v1/models`** (OpenAI-compatible standard)
   - Healthy: HTTP 200 with `{"data": [{"id": "model-name"}, ...]}` response
   - Model name is extracted from the first entry

2. **Fallback to GET `/health`**
   - Healthy: HTTP 200 response body (any content)

**A port that accepts TCP but returns nothing is correctly rejected.** This is critical because many services bind ports but don't respond to HTTP.

**Timeout**: 2 seconds per endpoint (configurable in `_probe_endpoint`). Discovery will not hang the agent startup.

## Testing

See `test_discovery_real.py` for a real end-to-end test that:
- Imports the module
- Runs discovery on the current machine
- Creates and validates an event
- Shows the correct "not found" response when no inference server is running

## Future Enhancements

1. **Ollama integration**: Detect Ollama-specific model format and APIs
2. **Hardware scoring**: Choose model quant based on available VRAM (bridge to pooled-inference-ops)
3. **Mesh auto-join**: Automatically join the AitherAeon room once endpoint is registered
4. **Health monitoring**: Periodic health checks to detect when local endpoint goes down
5. **Failover**: Automatically fall back to cloud inference if local endpoint becomes unhealthy

## See Also

Everything below ships in this package, so a reader can follow it:

- `adk/llamacpp_setup.py` — install llama.cpp and a GGUF that fits this machine
  (`adk gobbonet --setup-model` drives it end to end)
- `adk/packs/gobbonet/backend.py` — the discovery this module registers, from the
  consumer side: which local servers are probed and in what order
- `adk/models/fit.py` — what your hardware can actually run, in plain language
- `adk/models/mirror.py` — resumable, rate-capped, size-verified weight download

The registration target (MicroScheduler) and the event protocol are platform
services, not part of this package: an endpoint registers over HTTP and nothing
here imports them. If no platform is reachable, discovery still works and
registration is skipped — local inference does not depend on it.
