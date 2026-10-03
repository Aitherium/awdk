# Claude Code Studio

`claude-code` · version `3.8.49` · 5.0 KB

**[Download claude-code-3.8.49.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/claude-code-3.8.49.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/claude-code-3.8.49.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.49/claude-code-3.8.49.tar.gz
tar xzf claude-code-3.8.49.tar.gz
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

sha256 `6c38f8e3d829c1a000339894d9f7f5948c6c3ffe95c8635cbe0827080a2d5490`  
Built from `v3.8.49` (adk 3.8.49). [All packs](../packs.md)
