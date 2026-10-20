# Claude Code Studio

`claude-code` · version `3.8.80` · 5.0 KB

**[Download claude-code-3.8.80.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.80/claude-code-3.8.80.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.80/claude-code-3.8.80.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.80/claude-code-3.8.80.tar.gz
tar xzf claude-code-3.8.80.tar.gz
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

sha256 `a913e3ab0dd3edb01ac95bcdbdfb2e86c956395efb10a1eef08f6a8c94b5583d`  
Built from `v3.8.80` (adk 3.8.80). [All packs](../packs.md)
