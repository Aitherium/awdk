# Aither System Orchestrator

`aither` · version `4.0.0` · 10.0 KB

**[Download aither-4.0.0.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.0/aither-4.0.0.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v4.0.0/aither-4.0.0.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v4.0.0/aither-4.0.0.tar.gz
tar xzf aither-4.0.0.tar.gz
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

sha256 `9b6b0684771234b0f701a10101dc1f2802f141723af304510a53ceaa4b60bba2`  
Built from `v4.0.0` (adk 4.0.0). [All packs](../packs.md)
