# Aither System Orchestrator

`aither` · version `3.8.49` · 9.8 KB

**[Download aither-3.8.49.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/aither-3.8.49.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/aither-3.8.49.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/aither-3.8.49.tar.gz
tar xzf aither-3.8.49.tar.gz
python aither/install.py
```

Installs to `~/.aither/packs/aither/`, which adk discovers with no
configuration. The installer verifies the pack is discoverable rather than
assuming it. adk itself:

```bash
pip install aither-adk
```

## About

The default brain pack for the Aither orchestrator agent. Provides core
capabilities for system coordination, synthesis, delegation, and memory-based
decision-making. Bundles GraphRAG memory for persistent knowledge retention.

## Skills

- `coordination`
- `memory-recall`

## Contents

```
brain_pack.yaml
patterns/README.md
patterns/critique_plan/system.md
patterns/explain_code/system.md
patterns/extract_wisdom/system.md
patterns/report_150w/system.md
patterns/summarize/system.md
patterns/write_commit_message/system.md
skills/coordination.md
skills/memory-recall.md
```

---

sha256 `49add2f71e0e5d51af71f370ea05613726d470c682b1244e453e1ec9298d45d5`  
Built from `v3.8.49` (adk 3.8.49). [All packs](../packs.md)
