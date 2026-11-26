# Analyst Studio

`analyst` · version `4.0.1` · 5.3 KB

**[Download analyst-4.0.1.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/analyst-4.0.1.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/analyst-4.0.1.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v4.0.1/analyst-4.0.1.tar.gz
tar xzf analyst-4.0.1.tar.gz
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

sha256 `25a7d3139d7e912d5308f583d510ccc9b48360aaaf1d21e8977cd1a6b3976eac`  
Built from `v4.0.1` (adk 4.0.1). [All packs](../packs.md)
