# Analyst Studio

`analyst` · version `3.8.79` · 5.3 KB

**[Download analyst-3.8.79.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/analyst-3.8.79.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/analyst-3.8.79.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.79/analyst-3.8.79.tar.gz
tar xzf analyst-3.8.79.tar.gz
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

sha256 `7a19641234b9fe4ac85fb4a0ec9828e38fb52e41f9e9624fc250c0fe9dbd43c2`  
Built from `v3.8.79` (adk 3.8.79). [All packs](../packs.md)
