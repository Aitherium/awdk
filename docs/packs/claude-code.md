# Claude Code Studio

`claude-code` · version `3.8.25` · 5.0 KB

**[Download claude-code-3.8.25.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.25/claude-code-3.8.25.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.25/claude-code-3.8.25.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.25/claude-code-3.8.25.tar.gz
tar xzf claude-code-3.8.25.tar.gz
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

sha256 `b07daba4f4e7c81d8635c4eb2d3fd43f846c887aa576c3e419b44a36cba1df7b`  
Built from `v3.8.25` (adk 3.8.25). [All packs](../packs.md)
