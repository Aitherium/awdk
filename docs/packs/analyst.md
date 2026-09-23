# Analyst Studio

`analyst` · version `3.8.24` · 5.3 KB

**[Download analyst-3.8.24.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.24/analyst-3.8.24.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.24/analyst-3.8.24.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.24/analyst-3.8.24.tar.gz
tar xzf analyst-3.8.24.tar.gz
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

sha256 `4893e742b7550f1d458b68104f1d66403df17898f0f1cb6189385820588239f4`  
Built from `v3.8.24` (adk 3.8.24). [All packs](../packs.md)
