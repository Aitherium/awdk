# Aither System Orchestrator

`aither` · version `3.8.61` · 9.8 KB

**[Download aither-3.8.61.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/aither-3.8.61.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/aither-3.8.61.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.61/aither-3.8.61.tar.gz
tar xzf aither-3.8.61.tar.gz
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

sha256 `35444ff7353b0cb7ee8838098b5756837d54e7f7cd5ce8dd8f477f17bb0b11f2`  
Built from `v3.8.61` (adk 3.8.61). [All packs](../packs.md)
