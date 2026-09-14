# Landmark Map Integration Guide

## Overview

`landmark_map.py` implements Prospector Phase 1's semantic region indexing. It:

1. **Extends project_map** with landmark metadata (tools, skills, intent hints)
2. **Persists snapshots** with HMAC integrity (CodeGraph pattern)
3. **Ranks landmarks** by keyword + intent multiplier
4. **Fails open** — empty snapshot returns [], never crashes
5. **Reuses proven patterns** — no core-service edits needed

## Architecture

### Dataclasses

```python
@dataclass
class LandmarkNode:
    id: str                          # "landmark_0", "landmark_auth", etc.
    name: str                        # "Authentication", "Routing", etc.
    rank: int                        # 0=highest importance
    purpose: str                     # "auth", "routing", "api", etc.
    files: List[str]                # absolute paths in this landmark
    tools: List[str]                # tool names defined here (Phase 2+)
    skills: List[str]]              # skills available here (Phase 2+)
    intent_hints: Dict[str, float]  # {"CODE": 1.3, "DEBUG": 1.1, ...}

@dataclass
class LandmarkSnapshot:
    root: str                        # project root
    n_landmarks: int                 # from project_map
    landmarks: List[LandmarkNode]   # indexed semantic regions
    edges: List[LandmarkEdge]       # cross-landmark dependencies
    built_at: str                    # ISO timestamp
    hint_quality: Dict              # surprise_events, precision_metrics, etc.
```

### Persistence (HMAC Pattern)

Files are stored in `$AITHER_DATA_DIR/Library/Data/`:

```
{root-sha8}_landmark_snapshot.pkl      # pickle object
{root-sha8}_landmark_snapshot.pkl.hmac # HMAC-SHA256 sidecar
```

**HMAC key** comes from `$AITHER_INTERNAL_SECRET` env var (or default).

**Verification**: missing/corrupt HMAC → refuse load, return `None` (fail-open).

## Public APIs

### Load & Query

```python
from adk.landmark_map import load_landmark_snapshot, hints_for

# Load snapshot (returns None if missing or corrupt)
snapshot = load_landmark_snapshot(root="/path/to/project")

# Query with intent scaling
if snapshot:
    dirs = snapshot.hints_for(
        "where is tool ranking decided?",
        intent_type="CODE",  # optional: "CODE", "DEBUG", "CONVERSATION", etc.
        k=3
    )
else:
    # Graceful fallback to project_map
    from lib.cognitive.project_map import hints_for as pm_hints
    dirs = pm_hints("where is tool ranking?", root)
```

### Build from project_map

```python
from lib.cognitive.project_map import load_map
from adk.landmark_map import LandmarkSnapshot, save_landmark_snapshot

# Load project_map (from arc-agi-3 builder)
pm = load_map(root="/path/to/project")

# Enrich into snapshot (Phase 1: synthetic landmarks from purpose grouping)
snapshot = LandmarkSnapshot.from_project_map(pm)

# Persist with HMAC
save_landmark_snapshot(snapshot, root_path=pm["root"])
```

### Export for Embedding

```python
# Export deterministic JSONL for vLLM embedding (concept_card style)
result = snapshot.export_jsonl("/tmp/landmarks.jsonl")
# {"exported": 42, "output_path": "...", "generated_at": "2026-07-15T..."}
```

### Status

```python
from adk.landmark_map import landmark_status

status = landmark_status(root="/project")
# {
#   "available": true,
#   "root": "/project",
#   "n_landmarks": 42,
#   "built_at": "2026-07-15T...",
#   "landmarks": ["Auth", "Routing", ...],
#   "edges": 156
# }
```

## Ranking Algorithm

For each landmark, compute:

```
keyword_score = 4 * distinct_terms + name_hits*3 + purpose_hits*2 + file_hits
intent_boost = landmark.intent_hints[intent_type] * INTENT_RELEVANCE_SCALE[intent_type]
final_score = keyword_score * intent_boost

sorted by final_score (descending)
```

Where:
- `distinct_terms` = number of unique query terms matched (not repetition count)
- `intent_type` defaults to `"DEFAULT"` (scale = 1.0)
- Returns top-k **files** (not landmarks), sorted by score

## Fail-Open Behavior

**Never crashes.** Returns empty results on:

- Missing snapshot
- Corrupt HMAC
- Empty query
- No matching landmarks
- Unparseable pickle

All error paths log and return `[]` or `None` gracefully.

## Integration Points (Zero Core Edits)

### Read-Only Reuse

| Component | Lines | Usage |
|-----------|-------|-------|
| `project_map.load_map()` | 52-85 | Load base map for enrichment |
| `project_map._keyword_rank()` | 108-131 | Stopword set (reused) |
| `CodeGraph.concept_card()` | 368-393 | Deterministic JSON format pattern |
| `CodeGraph._verify_pickle_hmac()` | 246-265 | Exact HMAC verification logic |
| `CodeGraph._compute_file_hmac()` | 237-243 | Exact HMAC computation logic |
| `CodeGraph._write_pickle_hmac()` | 268-275 | Exact sidecar writing logic |
| `ContextPipeline._INTENT_RELEVANCE_SCALE` | 4257-4277 | Intent → score mapping |
| `ContextPipeline._coarse_code_intent()` | 263-277 | Fallback classifier (not used yet) |

### Subscribe-Only (Phase 2+)

- `SurpriseDetector.observe()` — feed surprise signals for training
- `LearnedWorldModel.TransitionDataset.record()` — persist training data for fine-tuning
- `CodeGraph.hybrid_query()` — semantic rank (when enriching from arc-agi-3)

## Phase Roadmap

**Phase 1 (NOW):**
- [x] LandmarkNode + LandmarkSnapshot dataclasses
- [x] Persistence with HMAC (atomic tmp+replace)
- [x] Keyword ranking from purpose grouping
- [x] Export to deterministic JSONL
- [x] Fail-open behavior (no crashes)

**Phase 2 (next):**
- [ ] Enrich from arc-agi-3 builder (tools[], skills[], edges[])
- [ ] Semantic rank (CodeGraph.hybrid_query integration)
- [ ] MCP tool `map_localize()` in Genesis
- [ ] Integrate `hints_for()` into ContextPipeline context seams

**Phase 3 (research):**
- [ ] Subscribe to SurpriseDetector for training signals
- [ ] Regret tracking (when hint was used but unhelpful)
- [ ] Fine-tune intent_hints via LearnedWorldModel
- [ ] Concept-card embedding (vLLM prompt cache reuse)

## Example: Full Workflow

```python
#!/usr/bin/env python3
"""Complete Prospector Phase 1 workflow."""

from lib.cognitive.project_map import load_map
from adk.landmark_map import (
    LandmarkSnapshot,
    save_landmark_snapshot,
    load_landmark_snapshot,
    hints_for,
)

# Step 1: Load project_map from arc-agi-3
root = "/path/to/project"
pm = load_map(root)
if not pm:
    print("No project_map found — run arc-agi-3 builder first")
    exit(1)

# Step 2: Enrich into snapshot (Phase 1: synthetic from purpose)
snapshot = LandmarkSnapshot.from_project_map(pm)
print(f"Created {len(snapshot.landmarks)} landmarks")

# Step 3: Persist with HMAC
if save_landmark_snapshot(snapshot, root_path=root):
    print(f"Saved snapshot to Library/Data/")

# Step 4: Query with intent scaling
hints = hints_for(
    "where is rate limiting enforced?",
    root=root,
    intent_type="CODE",
    k=3
)
print(f"Top-3 directories to search:\n{chr(10).join(hints)}")

# Step 5: Export for embedding (Phase 2+)
snapshot = load_landmark_snapshot(root)
if snapshot:
    result = snapshot.export_jsonl("/tmp/landmarks.jsonl")
    print(f"Exported {result['exported']} landmarks to JSONL")
```

## Testing

All 9 core tests pass:

```bash
pytest adk/test_landmark_map.py -v

✓ test_terms_of
✓ test_landmark_node_to_dict
✓ test_landmark_snapshot_from_project_map
✓ test_snapshot_hints_for_code_intent
✓ test_snapshot_hints_for_conversation_intent
✓ test_snapshot_fail_open_empty
✓ test_save_and_load_snapshot
✓ test_export_jsonl
✓ test_hints_for_public_api
```

## Lint Status

```bash
ruff check adk/landmark_map.py
ruff check adk/test_landmark_map.py

# Result: All checks passed!
```

## Key Design Decisions

1. **Fail-open**: Returns `[]` or `None`, never crashes. Users can always fall back to `project_map.hints_for()`.

2. **HMAC integrity**: Exact CodeGraph pattern. Any deviation breaks cache validation.

3. **Deterministic output**: JSON with `sort_keys=True, ensure_ascii=True`, sorted edge lists. Same state → byte-identical JSONL.

4. **Lazy imports**: Core deps (CodeGraph, ContextPipeline) imported in functions, not at module level. Allows fallback if unavailable.

5. **Intent scaling**: Reads `_INTENT_RELEVANCE_SCALE` read-only; never edits ContextPipeline.

6. **Standalone**: Zero core-service edits. Lives in awdk, importable as `from adk.landmark_map import ...`.

## Future: Moving to Core

When Phase 2/3 integrate into Genesis. This describes a change inside the
AitherOS server-side core, not anything you run from this package — the import
below is server-side and will not resolve in an `awdk` install.

```python
# Before (Phase 1 — awdk, what this package ships)
from adk.landmark_map import hints_for

# After (Phase 2+ — AitherOS core library, server-side; same code)
from lib.cognitive.landmark_map import hints_for
```

Zero API changes needed — just move the file.
