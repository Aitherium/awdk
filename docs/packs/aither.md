# Aither System Orchestrator

`aither` · version `3.8.89` · 9.8 KB

**[Download aither-3.8.89.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.89/aither-3.8.89.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.89/aither-3.8.89.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.89/aither-3.8.89.tar.gz
tar xzf aither-3.8.89.tar.gz
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

sha256 `a3ed9d444bb945c4ed04558eaae05f149decc37409a22ee601d4fe20856421f2`  
Built from `v3.8.89` (adk 3.8.89). [All packs](../packs.md)
