# Aither System Orchestrator

`aither` · version `3.8.26` · 9.8 KB

**[Download aither-3.8.26.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.26/aither-3.8.26.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.26/aither-3.8.26.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.26/aither-3.8.26.tar.gz
tar xzf aither-3.8.26.tar.gz
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

sha256 `3b1b10ab6b19fd0a90b7bb8f8ce83859cadb884338d55e4e397341a298528c93`  
Built from `v3.8.26` (adk 3.8.26). [All packs](../packs.md)
