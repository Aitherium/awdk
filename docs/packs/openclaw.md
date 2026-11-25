# OpenClaw Research Studio

`openclaw` · version `4.0.0` · 5.1 KB

**[Download openclaw-4.0.0.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.0/openclaw-4.0.0.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v4.0.0/openclaw-4.0.0.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v4.0.0/openclaw-4.0.0.tar.gz
tar xzf openclaw-4.0.0.tar.gz
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

sha256 `2e2a5b9aeedcf03154f2a1927c74126651453fafe88b36ff9f64731148cb58a4`  
Built from `v4.0.0` (adk 4.0.0). [All packs](../packs.md)
