# Claude Code Studio

`claude-code` · version `3.8.99` · 5.0 KB

**[Download claude-code-3.8.99.tar.gz](https://github.com/Aitherium/aither-adk/releases/download/v3.8.99/claude-code-3.8.99.tar.gz)** · [checksum](https://github.com/Aitherium/aither-adk/releases/download/v3.8.99/claude-code-3.8.99.sha256)

```bash
curl -LO https://github.com/Aitherium/aither-adk/releases/download/v3.8.99/claude-code-3.8.99.tar.gz
tar xzf claude-code-3.8.99.tar.gz
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

sha256 `5e9de92a0795d3335a513bba52a5d233c352045a29bbdf486334b233e2a9c2e3`  
Built from `v3.8.99` (adk 3.8.99). [All packs](../packs.md)
