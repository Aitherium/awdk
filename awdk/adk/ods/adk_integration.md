# ODS Integration Architecture

Detailed specification of how ODS resolver integrates with llmfit and the ADK pipeline.

## Overview

The ODS (Osmantic Model Selector) resolver is a deterministic model selection engine that recommends GGUF models based on hardware capabilities. It is **vendored** into the adk/ods/ directory and serves as the PRIMARY selector for the ADK's `LLMFitClient.recommend_config()` method.

**Key Properties:**
- **Deterministic:** Same inputs always produce the same model recommendation
- **Offline:** No network calls; catalog is packaged and loaded from disk
- **Fail-Closed:** Raises `OdsError` on missing/corrupt catalog or impossible constraints; never returns None or silent empty results
- **Hardware-Aware:** Detects backend (NVIDIA, AMD, Apple, CPU), memory type (discrete/unified), and tier
- **Edge Rules:** Implements Spark aarch64 override, unified-memory substitution, Hermes context promotion

## System Architecture

### Components

```
┌─────────────────────────────────────────────────────────┐
│  adk.llmfit.LLMFitClient.recommend_config()             │
│  (Public API entry point)                               │
└────────────────┬────────────────────────────────────────┘
                 │
       ┌─────────▼────────────┐
       │ sys_info()           │
       │ (detect hardware)    │
       └─────────┬────────────┘
                 │
    ┌────────────▼─────────────────────────────┐
    │ OdsResolver.resolve()                     │
    │ (PRIMARY selector)                        │
    │                                           │
    │  1. Load catalog from package data        │
    │  2. Normalize model records               │
    │  3. Calculate usable memory               │
    │  4. Filter candidates (family, size)      │
    │  5. Score & rank candidates               │
    │  6. Apply edge rules (policies)           │
    │  7. Return OdsRecommendation              │
    └────────────────┬─────────────────────────┘
                 │
       ┌─────────▼────────────┐
       │ (fallback to llmfit) │
       │ if ODS unavailable   │
       └─────────┬────────────┘
                 │
       ┌─────────▼──────────────────────────────┐
       │ Dict[hardware, fast, balanced, ...]     │
       │ (backward-compatible return shape)     │
       └──────────────────────────────────────────┘
```

### Data Files

**Vendored ODS Catalog (adk/ods/model-library.json)**
- ~200 GGUF models with metadata:
  - `id`, `name`, `family` (qwen, gemma4, phi, llama, hermes)
  - `gguf_file`, `gguf_url`, `gguf_sha256`
  - `size_mb`, `vram_required_gb`, `context_length`
  - `specialty` (Bootstrap, Fast, Balanced, Code, Reasoning, Quality, General)
  - `app_compatibility` (General, Code, Chat, Reasoning, Embedding)
  - `runtime_profiles` (performance data per use-case)

**Hardware Database (adk/ods/gpu-database.json)**
- Known GPU mappings (device ID → tier)
- Heuristic classes (vendor + memory_type + VRAM threshold → tier)
- GPU bandwidth LUT (for performance estimation)
- Defaults (VRAM fit tolerance, etc.)

**Legacy Hardware Classes (adk/ods/hardware-classes.json)**
- Deprecated threshold-based tier definitions (kept for compatibility)

## system_info() Flow

The `system_info()` method detects hardware and returns a dict:

```python
{
    "cpu_cores": int,
    "cpu_name": str,
    "total_ram_gb": float,
    "available_ram_gb": float,
    "has_gpu": bool,
    "gpu_name": str,
    "gpu_vram_gb": float,
    "backend": str,          # cuda|rocm|metal|cpu_x86|cpu_arm
    "unified_memory": bool,
    "gpu_count": int,
    "raw": dict              # original response
}
```

**Detection Sequence:**
1. Try llmfit binary or REST API (if available)
   - Queries nvidia-smi, Apple Metal, AMD ROCm APIs
   - Returns full hardware snapshot
2. Fallback to psutil + platform module (if llmfit unavailable)
   - CPU cores, RAM via psutil
   - Backend detection via platform.system() + device enumeration
   - Sets `backend` to cpu_x86, cpu_arm, or unknown

**Backend Mapping:**
- NVIDIA GPU → cuda
- AMD GPU → rocm
- Apple Silicon GPU → metal
- Intel Arc GPU → sycl (future)
- CPU-only → cpu_x86 or cpu_arm

## resolve() Algorithm

### 1. Profile Resolution

Resolves `profile` argument (qwen, gemma4, auto) to concrete profile:

```
if profile == "auto":
    if tier == "CLOUD":
        profile = "qwen"  # CLOUD always qwen
    elif backend in (apple, nvidia, sycl):
        profile = "gemma4"  # Apple/NVIDIA prefer gemma4
    else:
        profile = "qwen"  # default to qwen
else:
    profile = profile  # use explicit
```

### 2. Catalog Loading & Normalization

Load model-library.json via importlib.resources (package data) or explicit path.
Normalize each record to `ModelRecord` dataclass, validating schema:
- All required fields present
- Types correct (vram_required_gb numeric, context_length int)
- Bounds checks (0 < vram_required_gb ≤ 1000 GB, context ≤ 200K tokens)
- Installability: if `install_recommendation=true`, `gguf_url` must be present

### 3. Memory Capacity Calculation

Compute usable memory based on backend and memory_type:

```python
if backend == "cpu" or (backend == "unknown" and vram_mb == 0):
    # CPU-only: 35% of RAM, clamped 3–8GB
    usable = max(3.0, min(8.0, ram_gb * 0.35))
elif memory_type == "unified":
    # Unified (Apple): 55% of RAM, min 2GB
    usable = max(2.0, ram_gb * 0.55)
else:
    # Discrete GPU: VRAM directly (MB → GB)
    usable = vram_mb / 1024.0
```

### 4. Candidate Filtering

Filter models by:
- **Profile/family:** gemma4 profile → only family=gemma4 or id='qwen3.5-2b-q4' (bootstrap)
                      qwen profile → exclude family=gemma4
- **Max size:** if `max_size_mb` specified, exclude models > limit
- **Installability:** if `installable_only=true`, only include models with `install_recommendation=true`
- **Memory fit:** model's `vram_required_gb` ≤ `usable_memory + VRAM_FIT_TOLERANCE_GB` (default 0.25 GB)

### 5. Fallback Pool (No Candidates)

If primary filtering yields no candidates, escalate constraints:

**Level 1:** Relax `installable_only` (keep profile + max_size + fit)
**Level 2:** Relax `max_size_mb` (keep profile + fit)
**Level 3:** Any model that fits memory (any profile)
**Level 4:** Smallest model regardless (crisis mode)

Return smallest model by `vram_required_gb` from first level with matches.
Tag policy as `fallback-pool-stage-N` for traceability.

### 6. Scoring & Ranking

Score each candidate model:

```
score = 75.0  # Base calibrated for 0.5–1.0 confidence mapping

# Memory fit penalty (tight fits disfavored)
fit_ratio = model.vram_required_gb / usable_memory_gb
if fit_ratio > 0.98:
    score -= 8.0  # Very tight
elif fit_ratio > 0.92:
    score -= 3.0  # Marginal

# Specialty bonus (scaled 2x for importance)
score += SPECIALTY_WEIGHTS[model.specialty] * 2.0
# Code=4.4, Quality=4.1, General=3.8, Balanced=3.5, Reasoning=3.3, Fast=2.0, Bootstrap=1.0

# Family bonus
if profile == "qwen" and model.family == "qwen":
    score += 1.0
elif profile == "gemma4" and model.family == "gemma4":
    score += 1.4

# Context bonus (high tiers prefer high context)
if tier in ("3", "4", "NV_ULTRA", "SH_LARGE", "SH_COMPACT"):
    if model.context_length >= 32000:
        score += 2.5
    if model.context_length >= 128000:
        score += 2.5

# Quantization bonus
score += QUANTIZATION_WEIGHTS[model.quantization] * 2.0
# q6=2.0, q5=1.5, q4=0.5, q8=-0.5, fp8=-1.0
```

Sort candidates by score descending, then by required_memory, then context_length.

### 7. Edge Rules (Architecture Policies)

Apply overrides after scoring:

**Spark aarch64 (NV_ULTRA + arm64):**
- Prefer Spark-compatible variant: `qwen3.6-35b-a3b` models
- Policy tag: `spark-aarch64-nv-ultra-a3b-v1`

**Unified-memory coder substitution:**
- If `memory_type=unified` and model is coder, substitute to `qwen3.6-35b-a3b-ud-q4` variant
- Policy tag: `unified-memory-coder-next-a3b-v1`

**Hermes context floor (future):**
- Phi-family models on tiers 3–4 get context promotion: declared → 128K or clamped 131K

### 8. Return OdsRecommendation

```python
OdsRecommendation(
    policy=str,             # Computed from model + hardware (e.g., "spark-aarch64-nv-ultra-a3b-v1")
    source="ods",           # Always "ods" (for backward compat with llmfit)
    confidence=float,       # 0.0–1.0 normalized from score (70–90+ → 0.5–1.0)
    profile=str,            # Resolved (qwen or gemma4)
    host_arch=str,          # Echoed from input
    memory_capacity_gb=float,  # Usable memory (GB)
    memory_label=str,       # Human-readable (e.g., "NVIDIA A100 discrete (24GB)")
    selected=ModelRecord,   # Recommended model
    reason=str,             # Human-readable explanation
    alternatives=list[ModelRecord],  # Top 3 alternatives (excluding primary)
)
```

## Confidence & Traceability

**Confidence Score:**
Normalized from base score (70–90+) to 0.5–1.0 range:
```
confidence = min(0.99, max(0.50, (score - 50) / 40.0))
```

- **0.95+:** High confidence (specialty match, profile match, good fit)
- **0.80–0.95:** Good confidence (family match, acceptable fit)
- **0.50–0.80:** Fallback/bootstrap (limited options)

**Source Tagging:**
Each recommendation includes `source="ods"` for traceability.
Callers can check: `if result.source == "ods": log "Using deterministic ODS resolver"`

**Policy Naming:**
Policy field encodes hardware override type:
- `spark-aarch64-nv-ultra-a3b-v1` → Spark aarch64 override
- `unified-memory-coder-next-a3b-v1` → Unified-memory substitution
- `default-{family}-{tier}-v1` → Standard policy

## Error Handling (Fail-Closed)

Raises `OdsError` on:
1. **Catalog missing/corrupt:** JSONDecodeError, FileNotFoundError
2. **Schema invalid:** Missing required fields, type mismatches, bounds violations
3. **No candidates:** All fallback levels exhausted without finding any model

**Never returns:**
- None
- Empty list/dict
- Silent empty result

**Wrapper in llmfit.recommend_config():**
OdsError is caught and wrapped in error dict:
```python
{
    "error": "ODS catalog unavailable: ...",
    "reason": "descriptive message"
}
```

## Integration with llmfit.recommend_config()

### Path Priority (unless use_llmfit=True)

```
1. ODS Resolver (PRIMARY)
   ├─ system_info() → hardware dict
   ├─ For each tier (fast, balanced, reasoning, coding, embedding):
   │  └─ OdsResolver.resolve(tier-specific params)
   ├─ Tag all results with provider="ods"
   └─ Return config dict

2. llmfit REST/CLI (FALLBACK)
   ├─ Call system_info() if not cached
   ├─ Call top_models(use_case=tier, limit=1) for each tier
   └─ Return config dict (llmfit-formatted)

3. Error Dict
   └─ Both unavailable → return {"error": "No selector available"}
```

### Return Shape (Backward Compatible)

```python
{
    "hardware": {
        "gpu": str,
        "vram_gb": float,
        "ram_gb": int,
        "cpu_cores": int,
        "backend": str,  # cuda|rocm|metal|cpu_x86|cpu_arm
    },
    "fast": {
        "model": str,              # model id
        "provider": "ods" or "llmfit",
        "score": float,            # 0.0–1.0 confidence (ODS) or 0–100 (llmfit, scaled)
        "estimated_tps": float,
        "fit_level": "good" or "marginal" or "too_tight",  # ODS: always "good"
        "best_quant": str,         # q4, q5, q6, etc.
        "params_b": float
    } or None,
    ... (repeat for balanced, reasoning, coding, embedding)
}
```

**ODS vs llmfit:**
- ODS: `fit_level` always "good" (pre-filtered), confidence 0.5–1.0
- llmfit: `fit_level` from binary scorer, confidence scaled to 0.0–1.0

Callers check:
```python
if config.get("fast", {}).get("provider") == "ods":
    # Using ODS (deterministic, offline)
else:
    # Using llmfit (online, potentially stale)
```

## Re-Vendor Procedure

### Step 1: Fetch Upstream

```bash
cd /tmp
git clone https://github.com/Osmantic/ODS.git
cd ODS
git checkout main  # or specific tag
TARGET_COMMIT=$(git rev-parse --short HEAD)
TARGET_URL=$(git remote get-url origin)
```

### Step 2: Copy Data Files

```bash
cp config/model-library.json /path/to/awdk/adk/ods/
cp config/gpu-database.json /path/to/awdk/adk/ods/
cp config/hardware-classes.json /path/to/awdk/adk/ods/
```

### Step 3: Update Metadata

Edit `adk/ods/__init__.py`:
```python
ODS_VENDORED_COMMIT = "<TARGET_COMMIT>"
ODS_LAST_VENDORED_DATE = "<ISO8601 today>"
ODS_VENDORED_URL = "https://github.com/Osmantic/ODS"
```

### Step 4: Validate Schema

```bash
python adk/ods/tools/validate_catalog.py adk/ods/model-library.json
python adk/ods/tools/validate_catalog.py adk/ods/gpu-database.json
# Exit 0 on success
```

### Step 5: Run Integration Tests

```bash
pytest adk/ods/tests/test_resolver.py::TestOdsResolverPositive -v
# All POSITIVE assertions must pass (specific model IDs)
```

### Step 6: Commit

```bash
git add adk/ods/model-library.json adk/ods/gpu-database.json adk/ods/__init__.py
git commit -m "chore(ods): vendor catalog from upstream $TARGET_COMMIT

Re-vendor ODS model library and GPU database from upstream commit $TARGET_COMMIT.
All POSITIVE test assertions pass, schema validation successful.

ODS upstream: $TARGET_URL
Vendored date: $(date -Iseconds)
"
```

## Testing Strategy

### Unit Tests (test_resolver.py)

**POSITIVE assertions** (verify specific model IDs):
- `test_resolver_nvidia_24gb_tier3` → expects qwen family, ~10–37B range
- `test_resolver_apple_16gb_auto` → expects gemma4 family, profile resolves to gemma4
- `test_resolver_cpu_8gb_tier0` → expects qwen3.5-2b-q4 (bootstrap)
- `test_resolver_spark_aarch64_nv_ultra` → expects Spark variant, policy contains "spark"
- `test_resolver_unified_memory_coder` → expects unified-memory-coder policy

**NEGATIVE assertions** (error conditions):
- `test_resolver_missing_catalog` → OdsError on FileNotFoundError
- `test_resolver_corrupt_json` → OdsError on JSONDecodeError
- `test_resolver_catalog_empty_models` → OdsError on empty models list
- `test_resolver_invalid_model_record` → OdsError on schema mismatch

### Integration Tests (test_integration.py)

- `test_recommend_config_ods_primary` → returns config with fast/balanced/reasoning/coding/embedding, all provider="ods"
- `test_recommend_config_ods_unavailable_fallback_llmfit` → if ODS fails, tries llmfit
- `test_recommend_config_all_unavailable` → returns error dict
- `test_confidence_tagging` → checks source and confidence fields
- `test_alternatives_populated` → top 3 alternatives returned

### Validation Tests (validate_catalog.py)

- `test_validate_catalog_valid` → returns (True, "")
- `test_validate_catalog_missing_file` → returns (False, "not found")
- `test_validate_catalog_invalid_json` → returns (False, "Invalid JSON")
- `test_validate_catalog_missing_models_key` → returns (False, "missing 'models'")
- `test_validate_catalog_malformed_model` → returns (False, "missing required fields")
- `test_validate_gpu_database_schema` → similar for GPU database

## Performance Characteristics

**Resolver Execution (per resolve() call):**
- Catalog load (first call): ~50–200ms (JSON parse + validation)
- Subsequent calls: ~1–5ms (cached catalog)
- Scoring & ranking: ~5–20ms
- Total: ~50ms first call, ~5–20ms cached

**Memory Footprint:**
- Catalog in memory: ~5–10MB (6 model entries in template, ~200 in production)
- GPU database: ~500KB
- OdsResolver instance: ~1MB

**Caching:**
- Catalog cached via `@lru_cache(maxsize=1)` in data.py
- Single OdsResolver instance per process (held in llmfit.LLMFitClient)

## Future Work

1. **Hermes context floor:** Implement Phi-family context promotion on high tiers
2. **Dynamic profiles:** Tier-specific profile overrides (e.g., tier 4 → larger models)
3. **User entitlements:** Filter models by user tier/quota (metered vs. unlimited)
4. **Rollout strategy:** A/B test ODS vs llmfit on real hardware; gather metrics
5. **Catalog versioning:** Support multiple catalog versions, gradual rollout
