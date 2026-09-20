# Aither System Orchestrator

`aither` · version `3.8.22` · 9.8 KB

**[Download aither-3.8.22.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/aither-3.8.22.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/aither-3.8.22.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.22/aither-3.8.22.tar.gz
tar xzf aither-3.8.22.tar.gz
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

sha256 `ffd348c81aee6730fa6e71279d1bbd51a282a2bea52a4d2395addae56f7cc40a`  
Built from `v3.8.22` (adk 3.8.22). [All packs](../packs.md)
