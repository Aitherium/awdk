# Analyst Studio

`analyst` · version `3.3.0` · 5.3 KB

**[Download analyst-3.3.0.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.3.0/analyst-3.3.0.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.3.0/analyst-3.3.0.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.3.0/analyst-3.3.0.tar.gz
tar xzf analyst-3.3.0.tar.gz
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

sha256 `e64b2b8d0e5cf7739d747e0fce2ac748d529976ab9912d1007bec9c9d8b729f2`  
Built from `v3.3.0` (adk 3.3.0). [All packs](../packs.md)
