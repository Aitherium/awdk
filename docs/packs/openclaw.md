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

sha256 `4e4a568dd8756e856b240bb320eb9c57713ac72b96b12a96b5b7152ed9b932ea`  
Built from `v4.0.1` (adk 4.0.1). [All packs](../packs.md)
