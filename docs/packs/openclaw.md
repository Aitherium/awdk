# OpenClaw Research Studio

`openclaw` · version `4.0.1` · 5.1 KB

**[Download openclaw-4.0.1.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/openclaw-4.0.1.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/openclaw-4.0.1.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/openclaw-4.0.1.tar.gz
tar xzf openclaw-4.0.1.tar.gz
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

sha256 `8e0d6218b2165bb1a5eca4a3d9c3601127820d50201911cdd734cff39f2e4a1d`  
Built from `v4.0.1` (adk 4.0.1). [All packs](../packs.md)
