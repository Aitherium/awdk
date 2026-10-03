# Aither System Orchestrator

`aither` · version `3.8.50` · 9.8 KB

**[Download aither-3.8.50.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.50/aither-3.8.50.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.50/aither-3.8.50.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.50/aither-3.8.50.tar.gz
tar xzf aither-3.8.50.tar.gz
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

sha256 `1cc1465b36520c0b2e5cf56df875b9a6de0fc1abc8854738643aaa45e7e86409`  
Built from `v3.8.50` (adk 3.8.50). [All packs](../packs.md)
