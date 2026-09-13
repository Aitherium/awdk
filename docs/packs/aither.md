# Aither System Orchestrator

`aither` · version `3.8.17` · 4.4 KB

**[Download aither-3.8.17.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.17/aither-3.8.17.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.17/aither-3.8.17.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.17/aither-3.8.17.tar.gz
tar xzf aither-3.8.17.tar.gz
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

sha256 `87c34a5fa7589f55f3ae670def426f7f870d628e41b6fe627e7378e30ccf7398`  
Built from `v3.8.17` (adk 3.8.17). [All packs](../packs.md)
