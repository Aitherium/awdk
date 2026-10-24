# Analyst Studio

`analyst` · version `3.8.91` · 5.3 KB

**[Download analyst-3.8.91.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/analyst-3.8.91.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/analyst-3.8.91.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.91/analyst-3.8.91.tar.gz
tar xzf analyst-3.8.91.tar.gz
python analyst/install.py
```

Installs to `~/.aither/packs/analyst/`, which adk discovers with no
configuration. The installer verifies the pack is discoverable rather than
assuming it. adk itself:

```bash
pip install aither-adk
```

## About

A data-analysis agent that classifies, regresses, and forecasts over
structured data using zero-shot foundation models (TabFM + TimesFM), and
reasons about the results. Adapts to new labeled data in-context (support
set) instead of gradient training.

## Skills

- `anomaly-detection`
- `structured-inference`

## Contents

```
agent.yaml
brain_pack.yaml
skills/anomaly-detection.md
skills/structured-inference.md
```

---

sha256 `ca81940ba53edc022b5cb7fdc5321d59759eb711ae2c7731d9cbdd99da10b3dd`  
Built from `v3.8.91` (adk 3.8.91). [All packs](../packs.md)
