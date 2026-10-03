# Analyst Studio

`analyst` · version `3.8.49` · 5.3 KB

**[Download analyst-3.8.49.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/analyst-3.8.49.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/analyst-3.8.49.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/analyst-3.8.49.tar.gz
tar xzf analyst-3.8.49.tar.gz
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

sha256 `f059aabf69b96a01e07920e26e38193aaf8064000592e87b7775af5b527d67c6`  
Built from `v3.8.49` (adk 3.8.49). [All packs](../packs.md)
