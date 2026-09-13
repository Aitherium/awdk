# Aither System Orchestrator

`aither` · version `3.8.18` · 4.4 KB

**[Download aither-3.8.18.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/aither-3.8.18.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/aither-3.8.18.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.18/aither-3.8.18.tar.gz
tar xzf aither-3.8.18.tar.gz
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

sha256 `642184c21656a862824e7bd6eb98627087ccf06c06874b58e6a6fd8875b3837e`  
Built from `v3.8.18` (adk 3.8.18). [All packs](../packs.md)
