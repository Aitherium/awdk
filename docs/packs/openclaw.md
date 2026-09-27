# OpenClaw Research Studio

`openclaw` · version `3.8.28` · 5.2 KB

**[Download openclaw-3.8.28.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.28/openclaw-3.8.28.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.28/openclaw-3.8.28.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.28/openclaw-3.8.28.tar.gz
tar xzf openclaw-3.8.28.tar.gz
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

sha256 `2abaf31c4f637cf2d6e4c008c2f9b841e8d2a66764f5616c2174f18358f5edd6`  
Built from `v3.8.28` (adk 3.8.28). [All packs](../packs.md)
