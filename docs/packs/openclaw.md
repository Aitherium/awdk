# OpenClaw Research Studio

`openclaw` · version `3.8.42` · 5.1 KB

**[Download openclaw-3.8.42.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.42/openclaw-3.8.42.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.42/openclaw-3.8.42.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.42/openclaw-3.8.42.tar.gz
tar xzf openclaw-3.8.42.tar.gz
python openclaw/install.py
```

Installs to `~/.aither/packs/openclaw/`, which adk discovers with no
configuration. The installer verifies the pack is discoverable rather than
assuming it. adk itself:

```bash
pip install aither-adk
```

## About

A web research agent that searches the open web, reads primary sources,
cross-checks claims against its knowledge graph, and writes cited reports.
Sign-in-free; runs on the operator's own LLM key.

## Skills

- `source-verification`
- `web-research`

## Contents

```
agent.yaml
brain_pack.yaml
skills/source-verification.md
skills/web-research.md
```

---

sha256 `399e3b9166eb532e2075f24f1b29d934e56d01e5a0d4d231c17b0c6b835ca396`  
Built from `v3.8.42` (adk 3.8.42). [All packs](../packs.md)
