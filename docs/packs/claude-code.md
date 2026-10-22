# Claude Code Studio

`claude-code` · version `3.8.88` · 5.0 KB

**[Download claude-code-3.8.88.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.88/claude-code-3.8.88.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.88/claude-code-3.8.88.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.88/claude-code-3.8.88.tar.gz
tar xzf claude-code-3.8.88.tar.gz
python claude-code/install.py
```

Installs to `~/.aither/packs/claude-code/`, which adk discovers with no
configuration. The installer verifies the pack is discoverable rather than
assuming it. adk itself:

```bash
pip install aither-adk
```

## About

A coding-focused agent for feature development, debugging, testing,
refactoring, and code review. Works with any programming language and
integrates with version control.

## Skills

- `debugging`
- `feature-development`

## Contents

```
agent.yaml
brain_pack.yaml
skills/debugging.md
skills/feature-development.md
```

---

sha256 `37a93877cfceef1eea3a9ce0b5747cc487c40a707b5191c08643be7d5b2cef45`  
Built from `v3.8.88` (adk 3.8.88). [All packs](../packs.md)
