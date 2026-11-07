# Analyst Studio

`analyst` · version `3.8.98` · 5.3 KB

**[Download analyst-3.8.98.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.98/analyst-3.8.98.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.98/analyst-3.8.98.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.98/analyst-3.8.98.tar.gz
tar xzf analyst-3.8.98.tar.gz
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

sha256 `6fa186bed8878155864ce1c0c8014abdc443aa686cbf4fc07a852caa683f12d7`  
Built from `v3.8.98` (adk 3.8.98). [All packs](../packs.md)
