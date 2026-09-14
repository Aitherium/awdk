# Memory Recall Skill

Use persistent GraphRAG memory to retrieve and reinforce learned facts, decisions,
and patterns from prior interactions.

## Recall Patterns

### 1. Query the Knowledge Graph

When you need information:

```python
# Simple keyword search
results = await graph.search("topic", limit=10)

# Authority-ranked recall with reinforcement
results = await graph.recall_with_activation(
    "topic or question",
    limit=10,
    reinforce=True  # Bump importance of recalled facts
)
```

### 2. Store Facts and Triples

Persist learned knowledge:

```python
# Simple triple (subject, relation, object)
await graph.remember("ProjectX", "uses", "PostgreSQL", metadata={date, source})

# Complex record with role/tier/confidence
await graph.store({
    "content": "Full description of the learned fact",
    "role": "fact",           # or "decision", "correction", "insight"
    "tier": "persistent",     # or "ephemeral", "transient"
    "confidence": 0.85,       # How confident are you? (0–1)
    "tags": ["project", "architecture"]
})
```

### 3. Correct or Supersede Old Knowledge

When facts become stale or wrong:

```python
await graph.supersede(
    old_node_id,
    new_record="Updated fact about X",
    reason="discovered at 2024-01-15",
    cascade=True  # Decay confidence of related facts
)
```

## Reinforcement Loop

- **Recall with `reinforce=True`** bumps the `reinforcement_count` and `last_reinforced`
  timestamp of returned facts, making them "stick" longer during decay/sweep.
- Facts recalled frequently become more prominent. Unused facts age out.
- This creates a self-sharpening memory: useful knowledge persists, noise decays.

## Example: Multi-Turn Learning

**Turn 1**: User teaches me a fact.
```python
await graph.remember("Database", "for", "Project X", metadata={user_id, date})
```

**Turn 2**: User asks about Project X. I recall and reinforce.
```python
results = await graph.recall_with_activation("Project X", reinforce=True)
# Now the "Database for Project X" fact is marked reinforced and ages slower
```

**Turn 3**: User corrects me. I supersede.
```python
await graph.supersede(old_id, "Database is actually MongoDB, not PostgreSQL", reason="correction")
```

## Safety & Discipline

- Never store secrets or sensitive data in the graph (no API keys, passwords).
- Confidence < 0.5 signals uncertainty — escalate or verify before relying on it.
- `superseded_by` metadata prevents stale facts from being returned.
- Always verify external information before storing it as a "fact".
