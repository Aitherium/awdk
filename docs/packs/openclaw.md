# OpenClaw Research Studio

`openclaw` · version `3.8.81` · 5.2 KB

**[Download openclaw-3.8.81.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.81/openclaw-3.8.81.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.81/openclaw-3.8.81.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.81/openclaw-3.8.81.tar.gz
tar xzf openclaw-3.8.81.tar.gz
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

sha256 `d332ab71cdb5e6050a545c0ddbc2c49f886214c599009451a391356486f91cfc`  
Built from `v3.8.81` (adk 3.8.81). [All packs](../packs.md)
