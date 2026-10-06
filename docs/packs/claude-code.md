# Claude Code Studio

`claude-code` · version `3.8.60` · 5.0 KB

**[Download claude-code-3.8.60.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/claude-code-3.8.60.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/claude-code-3.8.60.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.60/claude-code-3.8.60.tar.gz
tar xzf claude-code-3.8.60.tar.gz
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

sha256 `f09a7487fa2e44f6e98d13103c8c0be9d578a1ff786bb3de09b987343d5de2f5`  
Built from `v3.8.60` (adk 3.8.60). [All packs](../packs.md)
