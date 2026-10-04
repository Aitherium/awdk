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

sha256 `8bcbb1861709c4bb92eb378e8b141b4ce4ddbe264bcec894d3be8c61237a3d31`  
Built from `v3.8.60` (adk 3.8.60). [All packs](../packs.md)
