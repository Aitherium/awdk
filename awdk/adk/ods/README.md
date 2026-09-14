# ODS Model Resolver

Deterministic, offline model selection for local LLM inference based on hardware constraints and user preferences.

## Overview

The **Open Data Systems (ODS)** model resolver is a vendored, ported component from [github.com/Osmantic/ODS](https://github.com/Osmantic/ODS). It powers model-fitting decisions in the awdk without requiring external binaries, network calls, or third-party services.

### Design Principles

- **Deterministic**: Same hardware + constraints → same model, every time
- **Offline**: No network calls; all decisions from vendored catalog
- **Fail-closed**: Raises `OdsError` on any logic failure; never silent/empty
- **Profile-aware**: Routes `auto` profile to best family for detected hardware (gemma4 on Apple, qwen elsewhere)
- **Extensible**: Edge rules (unified-memory substitution, context floor, Spark override) baked in; easy to add more

## API

### OdsResolver

```python
from adk.ods import OdsResolver, OdsRecommendation, OdsError

resolver = OdsResolver()  # Loads catalog from package data on first call

recommendation = resolver.resolve(
    backend='nvidia',           # nvidia|amd|apple|cpu|unknown
    memory_type='discrete',     # discrete|unified
    vram_mb=24576,
    ram_gb=32,
    profile='qwen',             # qwen|gemma4|auto
    tier='3',                   # hardware tier; None = auto-detect
    host_arch='x86_64',         # x86_64|arm64|unknown
    max_size_mb=None,           # max model size; None = no limit
    installable_only=False,     # if True, only recommend install_recommendation=True models
)

print(recommendation.selected.name)
print(recommendation.reason)
print(recommendation.confidence)
# → OdsRecommendation with policy, source='ods', selected model, reason, confidence, alternatives
```

### resolve_role() — one pick per ADK role

Upstream ODS answers exactly one question: *which single model should this box
install*. Calling `resolve()` once per ADK tier therefore returns the same model
five times. `resolve_role()` narrows upstream's **feasible**
set — it never adds a candidate upstream rejected, and the arch-policy
substitution still applies to the role's own pick.

```python
rec = resolver.resolve_role(
    'coding',                   # fast | balanced | reasoning | coding | long_context | chat
    backend='nvidia', memory_type='discrete', vram_mb=24576, ram_gb=64,
    profile='qwen', tier='T3',
)
rec.policy   # '...+role-coding'
rec.reason   # '... Role 'coding' matched specialty 'Code'.'
```

| Role | Preferred specialties | Within-group objective |
|---|---|---|
| `fast` | Fast | most capable (every Fast record is already ≥110 tok/s) |
| `balanced` | *(none — upstream's own pick)* | — |
| `reasoning` | Reasoning → Quality | most capable |
| `coding` | Code → Tool Use | most capable |
| `long_context` | Long Context | longest context |
| `chat` | Chat → Balanced → General | most capable |

`embedding` is **not** a role: the catalog is a generation-model library with
zero embedding records, so `resolve_role('embedding')` raises `OdsError`.
`recommend_config()` fills that tier from `adk.embeddings.CANONICAL_MODEL`.

When no model of a preferred specialty fits, the pick falls back to upstream's
overall best and `reason` says so — a labelled degradation, not a silent one.

### classify_host() — tier + backend + compose overlays

A port of ODS `scripts/classify-hardware.sh`, which is vendored verbatim as
`_upstream_classify.sh` and executed as the reference in
`tests/test_classify_differential.py`.

```python
from adk.ods.hardware import classify_host

# The identifying string for a Strix Halo box is its CPU name, not its GPU name.
host = classify_host(gpu_name='AMD Radeon Graphics',
                     cpu_name='AMD Ryzen AI MAX+ 395',
                     vendor='amd', memory_type='discrete', vram_mb=512, ram_gb=128)
host.tier              # 'SH_LARGE'
host.memory_type       # 'unified'  <- known_gpus CORRECTED the probe
host.vram_mb           # 98304      <- ...and its memory figure
host.compose_overlays  # ('docker-compose.base.yml', 'docker-compose.amd.yml')
host.bandwidth_gbps    # 256
host.source            # 'known_gpu' | 'heuristic_class' | 'unknown'
```

Two passes, both upstream's: `known_gpus` (matched on `device_id`, then on GPU
**and CPU** name, longest pattern winning so `RX 7900 XT` cannot claim an
`RX 7900 XTX` host), then the `heuristic_classes` vendor+capacity ladder.
An unrecognised host degrades to `cpu`/`T1` rather than raising.

Compose overlays key on the resolved **backend** (`OVERLAY_MAP`), with one macOS
override — not on the platform. `hardware-classes.json` is the declarative
mirror of that map; upstream ships a contract test asserting they agree and it
is ported here, which is that file's real job in this package.

`memory_type` must be a real token when you call this. It is compared exactly
against the ladder, so `""` matches no NVIDIA class and silently drops a 24GB
host to `cpu`/`T1` — sized from RAM instead of VRAM.

This matters beyond bookkeeping: `recommend_config()` used to pass `tier=None`,
which pins the tier to `"1"` and makes upstream's Spark/GB10 arch-policy guard
structurally unreachable.

### Return Shape

```python
@dataclass
class OdsRecommendation:
    policy: str              # e.g. 'unified-memory-coder-next-a3b-v1', '...+role-coding'
    source: str              # always 'ods'
    confidence: float        # 0.0-1.0 (0.95+ high confidence)
    profile: str             # resolved profile (qwen or gemma4)
    host_arch: str           # echoed from input
    memory_capacity_gb: float # usable capacity (35% CPU, 55% unified, 100% discrete)
    memory_label: str        # human-readable (e.g. 'NVIDIA A100 discrete (24GB)')
    selected: ModelRecord    # the recommended model
    reason: str              # human-readable reason
    alternatives: list[ModelRecord]  # top 3 alternatives
```

## Integration with recommend_config()

The resolver is **PRIMARY** in `LLMFitClient.recommend_config()`:

```python
from adk.llmfit import LLMFitClient

client = LLMFitClient()

# Hardware comes from system_info(); ODS is tried first (deterministic, offline).
result = await client.recommend_config(use_llmfit=False)   # default

# Real output on an RTX 5090 / 128GB host, 2026-07-25:
# result = {
#   'hardware': {'gpu': 'NVIDIA GeForce RTX 5090', 'vram_gb': 31.84, 'ram_gb': 125,
#                'backend': 'cuda', 'ods_class': 'nvidia_pro', 'ods_tier': 'T3',
#                'ods_backend': 'nvidia', 'memory_type': 'discrete',
#                'classified_backend': 'nvidia', 'classified_by': 'heuristic_class',
#                'bandwidth_gbps': 1792},
#   'fast':      {'model': 'qwen2.5-1.5b-instruct',        'provider': 'ods', 'specialty': 'Fast'},
#   'balanced':  {'model': 'qwen3.5-27b',                  'provider': 'ods', 'specialty': 'Quality'},
#   'reasoning': {'model': 'deepseek-r1-distill-qwen-32b', 'provider': 'ods', 'specialty': 'Reasoning'},
#   'coding':    {'model': 'qwen2.5-coder-3b-instruct',    'provider': 'ods', 'specialty': 'Code'},
#   'embedding': {'model': 'nomic-embed-text',             'provider': 'aither-embeddings'},
# }
```

### Path Priority (recommend_config)

```
recommend_config(use_llmfit=False) called
  ├─→ Path 1: ODS Resolver (PRIMARY) ──────────────────────
  │   ├─ Catch OdsError on import → log, fall to Path 2
  │   ├─ Call system_info() → get hw dict
  │   ├─ If hw is None → log, fall to Path 2
  │   ├─ classify_host() → real tier/backend/memory_type/overlays
  │   ├─ Call OdsResolver.resolve_role() once per GENERATION tier
  │   │    (fast, balanced, reasoning, coding — NOT embedding)
  │   ├─ Fill 'embedding' from adk.embeddings.CANONICAL_MODEL
  │   ├─ Assemble config with provider='ods' tag
  │   └─ Return config if ANY generation tier resolved
  │      (the static embedding tier is excluded from that test on
  │       purpose — counting it would make the check vacuously true
  │       and this fallback unreachable)
  │
  ├─ Path 2: llmfit (FALLBACK) ─────────────────────────
  │   ├─ Call system_info() if not cached
  │   ├─ Call llmfit.top_models(use_case=tier) for each tier
  │   ├─ Assemble config with provider='llmfit' tag
  │   └─ Return config (or error dict if unavailable)
  │
  └─ Return config dict (never None, always has 'hardware' + tier dicts or 'error' key)
```

Return shape is **unchanged** from llmfit era (backward compatible):
- Each tier (fast, balanced, reasoning, coding, embedding) is `{...}` or `None`
- New `provider` field indicates source: `'ods'`, `'llmfit'`, or
  `'aither-embeddings'` (embedding tier only)
- Scoring range (0.0-1.0) is normalized across both paths for caller compatibility

If ODS is unavailable or `use_llmfit=True` is passed, the call falls back to llmfit.

## Traceability & Versioning

To identify which ODS version is running:

```python
from adk.ods import ODS_VENDORED_COMMIT, ODS_LAST_VENDORED_DATE

print(f"Using ODS catalog {ODS_VENDORED_COMMIT} vendored {ODS_LAST_VENDORED_DATE}")
# Output: Using ODS catalog abc123def456 vendored 2026-07-25
```

Each tier recommendation includes:
- `source: 'ods'` — indicates ODS resolver was used (deterministic)
- `score: float` — confidence 0.0-1.0 (ODS always >= 0.75 unless fallback)
- Optional `source_metadata` — raw policy, reason, alternatives for debugging

**Catalog Metadata:** model-library.json may include top-level metadata:
```json
{
  "version": "1.0",
  "metadata": {
    "upstream_commit": "abc123...",
    "upstream_date": "2026-07-25T...",
    "vendored_from": "https://github.com/Osmantic/ODS"
  },
  "models": [...]
}
```

This allows downstream tools to validate which upstream ODS version generated recommendations.

## Hardware Routing Rules

### Profile Routing (auto)

| Backend      | Memory Type | Profile → |
|--------------|-------------|-----------|
| apple        | unified     | gemma4    |
| nvidia       | discrete    | qwen      |
| amd          | discrete    | qwen      |
| cpu          | —           | qwen      |
| unknown      | —           | qwen      |

### Tier Detection

Transcribed from `gpu-database.json` → `heuristic_classes`, in file order (first
match wins). `classify_host()` walks exactly this ladder after trying
`known_gpus` name patterns.

> An earlier revision of this section listed a tier table that appears nowhere
> in the vendored data (nvidia 2–8GB → "tier 2", apple → `SH_*`, a cpu 32GB+
> tier). It was invented alongside the fabricated catalog. Do not reintroduce a
> hand-written table here — transcribe the file.

| Vendor | Memory | Threshold | Tier |
|---|---|---|---|
| nvidia | discrete | ≥ 92160 MB | `NV_ULTRA` |
| nvidia | discrete | ≥ 40960 MB | `T4` |
| nvidia | discrete | ≥ 20480 MB | `T3` |
| nvidia | discrete | ≥ 12288 MB | `T2` |
| nvidia | discrete | ≤ 4095 MB | `T0` (backend drops to `cpu`) |
| nvidia | discrete | ≥ 4096 MB | `T1` |
| nvidia | unified | RAM ≥ 92160 / 49152 / 20480 / 12288 / 0 MB | `NV_ULTRA` / `T4` / `T3` / `T2` / `T1` |
| amd | unified | RAM ≥ 92160 MB / else | `SH_LARGE` / `SH_COMPACT` |
| amd | discrete | ≥ 20480 / 12288 / 0 MB | `T3` / `T2` / `T1` |
| apple | unified | RAM ≥ 131072 / 65536 / 32768 / 0 MB | `T4` / `T3` / `T2` / `T1` |
| none | none | any | `T1` (backend `cpu`) |

`known_gpus` overrides all of the above for 14 recognised devices, and can
correct a wrong probe — a Strix Halo APU reporting `discrete`/512MB is
reclassified `unified`/98304MB/`SH_LARGE`.

### Edge Rules

#### 1. Unified-Memory Coder Substitution (Apple Silicon)

When:
- `backend='apple'` AND `memory_type='unified'` AND `profile='qwen'` AND tier in ['SH_LARGE']
- Model family = qwen AND specialty = 'Code'

Then:
- **Substitute** to `qwen3.6-35b-a3b-ud-q4` (Unified Density variant)
- **Policy**: `'unified-memory-coder-next-a3b-v1'`
- **Reason**: "Unified-memory optimization for Apple Silicon code workload"

#### 2. Hermes Context Floor (Reasoning Models)

When:
- Model family = 'hermes' AND tier in ['3', '4', 'NV_ULTRA', 'SH_LARGE']

Then:
- **Clamp** model.context_length to min(131072, catalog value)
- **Reason**: "Hermes reasoning models support extended context; tier allows it"

#### 3. Spark aarch64 Override (ARM64 Discrete)

When:
- `host_arch='arm64'` AND `backend='nvidia'` AND tier='NV_ULTRA'

Then:
- **Prefer** models with `specialty='Spark'` or ARM-optimized quantizations
- **Policy**: `'spark-aarch64-nv-ultra-a3b-v1'`
- **Reason**: "ARM64 hardware with high-end GPU; Spark variant optimal"

#### 4. Smallest-Model Fallback (there is no "bootstrap" policy)

> An earlier revision of this section described a `'bootstrap-fallback'` policy
> pinned to `qwen3.5-2b-q4`. No such policy exists in the vendored code, and no
> record in the vendored library carries a `Bootstrap` specialty (upstream's
> score table weights one, but nothing uses it). This is what actually happens:

When nothing fits the capacity envelope, `rank_models()` relaxes the pool in
three steps — drop the fit check, then the size ceiling, then the profile/family
filter — and returns the **single lowest-`vram_required_gb` model** of the first
non-empty pool. The policy string is unchanged (`POLICY`, plus any arch-policy
tag); there is no distinct fallback policy name and no confidence penalty.

`OdsResolver` only raises `OdsError` when the catalog itself yields nothing —
that path is unreachable with a non-empty catalog, which is the point: the
resolver never returns an empty pick.

## Scoring & Ranking

Each candidate model is scored based on:

1. **Memory fit** (base score)
   - Candidate VRAM ≤ available VRAM → fit_ratio = candidate / available
   - fit_ratio < (1 - VRAM_FIT_TOLERANCE_GB / available) → excluded
   - fit_ratio >= 0.98 (very tight fit) → apply -0.35 penalty
   - Otherwise → score += fit_ratio * 0.50

2. **Profile match**
   - Model family matches profile → score += 0.30
   - Model specialty matches profile → score += 0.20

3. **Context length** (reasoning workloads)
   - Model context >= 32000 → score += 0.15
   - Model context >= 128000 → score += 0.25

4. **Quantization**
   - Q4 quantization (good balance) → score += 0.10
   - Q5/Q6 (lossless) → score += 0.15

5. **Throughput** (if profile defines runtime_profiles)
   - Extrapolate tokens/sec from catalog
   - Higher TPS → score += 0.10

Final score = min(1.0, base + adjustments).

## Catalog Schema

### model-library.json

```json
{
  "version": "1.0",
  "models": [
    {
      "id": "qwen3.5-2b-q4",
      "name": "Qwen 3.5 2B Q4",
      "family": "qwen",
      "gguf_file": "qwen3.5-2b-q4.gguf",
      "gguf_url": "https://huggingface.co/...",
      "gguf_sha256": "abc123...",
      "size_mb": 1500,
      "vram_required_gb": 1.2,
      "context_length": 32000,
      "quantization": "q4",
      "specialty": "Bootstrap",
      "llm_model_name": "qwen3.5-2b-instruct",
      "install_recommendation": true,
      "runtime_profiles": {
        "qwen": { "tps": 18, "tokens_per_batch": 256 }
      },
      "app_compatibility": ["General", "Code", "Chat"]
    }
  ]
}
```

### gpu-database.json

```json
{
  "known_gpus": { "nvidia": { "10de:2184": {...} } },
  "heuristic_classes": [ { "vendor": "nvidia", "vram_min_gb": 20, "tier": "3" } ],
  "known_gpu_bandwidth": { "nvidia": { "A100-80GB": 2039.0 } },
  "defaults": {
    "vram_fit_tolerance_gb": 0.25,
    "tight_fit_threshold": 0.98,
    "profile_default": "auto"
  }
}
```

## Re-vendor Procedure

To update the catalog from upstream ODS:

### 1. Fetch upstream

```bash
cd /tmp
git clone https://github.com/Osmantic/ODS.git
cd ODS
git checkout main  # or a specific tag/commit
TARGET_COMMIT=$(git rev-parse --short HEAD)
```

### 2. Copy files

All FIVE vendored files, verbatim — the two scripts are vendored as code, not
just as data, and are what the differential tests run as their reference:

```bash
cp ods/config/model-library.json     /path/to/awdk/adk/ods/
cp ods/config/gpu-database.json      /path/to/awdk/adk/ods/
cp ods/config/hardware-classes.json  /path/to/awdk/adk/ods/
cp ods/scripts/select-model.py       /path/to/awdk/adk/ods/_upstream_select.py
cp ods/scripts/classify-hardware.sh  /path/to/awdk/adk/ods/_upstream_classify.sh
```

Do not hand-edit any of them: `ODS_VENDORED_SHA256` pins each one and
`validate_catalog.py --verify-vendored` will fail. `.gitattributes` pins them to
LF so `core.autocrlf` cannot break the hashes on checkout.

### 3. Update metadata

Edit `/path/to/awdk/adk/ods/__init__.py`:

```python
ODS_VENDORED_COMMIT = "abc123def456"  # from step 1
ODS_VENDORED_URL = "https://github.com/Osmantic/ODS"
```

### 4. Validate schema

```bash
cd /path/to/awdk
python -c "from adk.ods import load_catalog; load_catalog()"
```

### 5. Run tests

```bash
python -m pytest adk/ods/tests/test_resolver.py -v
python -m pytest adk/ods/tests/test_integration.py -v
```

Expected: All POSITIVE assertions pass (known hardware profiles return expected models).

### 6. Commit

```bash
git add adk/ods/*.json adk/ods/__init__.py
git commit -m "chore: vendor ODS catalog $(TARGET_COMMIT)"
```

## Constants & Configuration

```python
# In adk/ods/__init__.py
ODS_VENDORED_COMMIT = None          # unknown: vendored from a zipball, not a clone
ODS_VENDORED_REF = "main"
ODS_VENDORED_RELEASE = "2.5.3"      # ods/manifest.json at time of vendoring
ODS_VENDORED_URL = "https://github.com/Osmantic/ODS"
ODS_VENDORED_SHA256 = {...}         # per-file integrity anchor; see validate_catalog.py

# In adk/ods/_upstream_select.py — the ONLY home for selection tunables.
# adk/ods/data.py used to re-declare these; the copies were dead AND the
# discrete share was wrong (0.95 vs upstream's 1.00), so they were removed.
VRAM_FIT_TOLERANCE_GB = 0.25        # fits(): required <= capacity + tolerance
# usable_memory_gb(), verbatim:
#   unified / apple  -> max(ram_gb * 0.55, 2.0)
#   cpu / no VRAM    -> min(max(ram_gb * 0.35, 3.0), 8.0)
#   discrete GPU     -> vram_mb / 1024.0     (FULL VRAM — 100%, not 95%)
# score_model() weights specialty (Code 4.4 ... Fast 2.0), family, context and
# size, and applies a 0.35/0.15 headroom penalty above a 0.98/0.92 fit ratio.
```

## Error Handling

```python
from adk.ods import OdsError

try:
    recommendation = resolver.resolve(...)
except OdsError as e:
    # Catalog missing, corrupt, or no viable candidates
    # Fallback: print error message to user, suggest manual model selection
    print(f"Model selection failed: {e}")
    # Do NOT return None or empty dict — fail-closed
    raise
```

Common errors:

| Error | Cause | Action |
|-------|-------|--------|
| "Catalog missing or invalid schema" | JSON files not found or corrupt | Re-run installation; check package integrity |
| "No viable candidates after exhausting fallback pool" | Constraints too tight (e.g., max_size_mb=100 MB on GPU) | Loosen constraints; manually select model |
| "Hardware detection failed" | Backend/memory_type mismatch | Double-check hardware specs; pass explicit tier |

## Backward Compatibility

The return shape of `LLMFitClient.recommend_config()` is **unchanged**:

```python
{
  'hardware': { 'backend': 'nvidia', 'vram_gb': 24, ... },
  'fast': { 'model': 'qwen3.5-7b-q4', 'score': 0.92, 'tps': 22 },
  'balanced': { 'model': 'qwen3-37b-q4', 'score': 0.85, 'tps': 16 },
  'reasoning': { 'model': 'qwen3.6-35b-q4', 'score': 0.88, 'tps': 14 },
  'coding': { 'model': 'qwen3-coder-37b-q4', 'score': 0.91, 'tps': 12 },
  'embedding': { 'model': 'bge-small-en-q4', 'score': 0.80, ... },
}
```

Callers do NOT need to change; ODS→llmfit transformation is transparent.

## License

Vendored from [Osmantic/ODS](https://github.com/Osmantic/ODS) under Apache License 2.0.
See `NOTICE` and repository `LICENSE` for details.
