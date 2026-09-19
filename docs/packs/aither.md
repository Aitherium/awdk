# Aither System Orchestrator

`aither` · version `3.8.20` · 4.4 KB

**[Download aither-3.8.20.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.20/aither-3.8.20.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.20/aither-3.8.20.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.20/aither-3.8.20.tar.gz
tar xzf aither-3.8.20.tar.gz
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
skills/coordination.md
skills/memory-recall.md
```

---

sha256 `4b9b738cc14c4774c867d1acc3e9474084a4b42515408e782c2af8cd767d2780`  
Built from `v3.8.20` (adk 3.8.20). [All packs](../packs.md)
