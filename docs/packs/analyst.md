# Analyst Studio

`analyst` · version `4.0.0` · 5.3 KB

**[Download analyst-4.0.0.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v4.0.0/analyst-4.0.0.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v4.0.0/analyst-4.0.0.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v4.0.0/analyst-4.0.0.tar.gz
tar xzf analyst-4.0.0.tar.gz
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

sha256 `66e026d07c19da862f4f496c293e02526059030430f6fd5b039da9ac740c9094`  
Built from `v4.0.0` (adk 4.0.0). [All packs](../packs.md)
