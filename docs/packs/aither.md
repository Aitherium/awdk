# Aither System Orchestrator

`aither` · version `4.0.1` · 10.0 KB

**[Download aither-4.0.1.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/aither-4.0.1.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/aither-4.0.1.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/aither-4.0.1.tar.gz
tar xzf aither-4.0.1.tar.gz
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

sha256 `b2337a961d77ccb1d27b3a70d91ea388858dd04286b58498dc1f72a02caf69f5`  
Built from `v4.0.1` (adk 4.0.1). [All packs](../packs.md)
